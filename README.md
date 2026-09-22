# API Gateway Guard（API 网关治理层）

在现有 FastAPI Web 服务之上实现的一套**本地可复现**的 API 网关治理层，提供三大能力：

1. **多版本接口共存与不兼容拒绝**
2. **按调用方维度的并发安全限流**
3. **基于幂等标识的副作用去重与可安全重试**

所有治理结论都做到**一致且可解释**：失败原因使用稳定的错误码区分，限流给出可判定重试时机的信息，幂等返回可区分的状态。

---

## 目录结构

```
app/
  errors.py        统一错误码 ErrorCode 与异常 GatewayError
  versioning.py    多版本注册表：版本协商 + 字段级兼容性校验
  ratelimit.py     并发安全的固定窗口限流器（RateLimiter）
  idempotency.py   幂等存储与执行包装（IdempotencyStore）
  gateway.py       治理编排：限流 -> 版本协商 -> 幂等执行
  api.py           FastAPI 装配：HTTP 接口与错误码 -> HTTP 状态码映射
main.py            启动入口（uvicorn main:app）
tests/             单元 + HTTP 端到端测试（含并发场景与结构化日志）
```

---

## 一、版本兼容规则

- 每个请求**必须**通过请求头 `X-Api-Version` 显式声明版本，缺失即拒绝
  （`VERSION_MISSING`，HTTP 400）。系统**不会**猜测版本、也不会把旧请求
  静默路由到最新版本。
- 资源不支持所声明的版本时拒绝（`VERSION_UNSUPPORTED`，HTTP 404），错误体
  中返回 `supported_versions`，调用方可据此纠正。
- 版本存在但**请求字段与该版本语义不兼容**时拒绝
  （`VERSION_INCOMPATIBLE`，HTTP 422），错误体中返回
  `incompatible_fields`。示例资源 `orders`：

  | 版本 | 接受字段 | 说明 |
  |------|----------|------|
  | v1   | `name` | 不含促销语义 |
  | v2   | `name`、`coupon_id` | 新增促销券；`coupon_id` 对 v1 属于不兼容字段 |

  因此向 v1 提交 `coupon_id` 会被**明确拒绝而不是静默忽略**，避免旧版本语义
  被新版本字段污染。

### 版本错误码（可区分）

| 错误码 | HTTP | 含义 |
|--------|------|------|
| `VERSION_MISSING` | 400 | 未携带版本标识 |
| `VERSION_UNSUPPORTED` | 404 | 该资源不存在此版本 |
| `VERSION_INCOMPATIBLE` | 422 | 版本存在，但字段/语义不兼容 |

---

## 二、限流语义

- **维度**：按调用方（请求头 `X-Caller`，缺省为 `anonymous`）独立计数，
  各调用方互不影响。
- **算法**：固定时间窗口计数，默认每个 `60s` 窗口最多 `5` 次
  （`create_app(rate_limit=..., window_seconds=...)` 可调）。
- **并发准确性**："判定 + 计数"在同一把锁内原子完成。高并发下恰好放行
  `limit` 次，**不漏放（不会超发）也不重复计数**。测试用 100 并发 / 配额 10
  验证，结果严格为 10 放行 + 90 拒绝。
- **窗口边界**：窗口起点由 `int(now // window) * window` 的整数窗口下标决定，
  边界处一次性整体切换到新窗口。临界前一刻仍按旧窗口拒绝，进入新窗口立即
  恢复满配额，杜绝抖动导致的漏放 / 重复计数。
- **超限响应**：HTTP 429，错误码 `RATE_LIMITED`，错误体包含
  `caller / limit / window_epoch / retry_after / reason=QUOTA_EXCEEDED`，
  同时返回标准 `Retry-After` 响应头（距下个窗口的秒数），调用方可据此
  决定重试时机。

---

## 三、幂等契约

通过请求头 `Idempotency-Key` 携带幂等标识（**不带则不启用幂等**，每次真实
执行，状态为 `DIRECT`）。请求体指纹使用键排序后的 SHA-256 生成。

| 结论状态 | 触发条件 | 是否执行副作用 | HTTP |
|----------|----------|----------------|------|
| `DIRECT` | 未带幂等键 | 执行 | 200 |
| `COMPLETED` | 首次携带该键，执行成功 | 执行（首次） | 201 |
| `REPLAYED` | 相同键 + 相同请求体重放 | **不执行**，复用缓存结果 | 200 |
| `RETRIED` | 前序失败后，相同键 + 相同体重试成功 | 重新执行 | 201 |
| `PROCESSING` | 相同键的前序请求仍在处理中 | 不并发执行第二个 | 409（可重试） |
| `CONFLICT` | 相同键但请求体指纹不同 | 不执行，明确拒绝 | 409 |

关键保证：

- **副作用只发生一次**：同一键的重复请求直接返回首次结果（`order_id` 相同），
  绝不重复触发下游。
- **处理中可区分**：首个请求未完成时到达的重复请求得到 `PROCESSING`
  （错误码 `IDEMPOTENCY_PROCESSING`，带 `Retry-After`），不会并发执行。
- **标识冲突可区分**：同键不同体得到 `CONFLICT`
  （错误码 `IDEMPOTENCY_CONFLICT`），不会覆盖既有结果。
- **失败后可安全重试**：业务处理中途抛异常时记录置为 `FAILED` 并记录错误；
  随后携带相同键 + 相同请求体的请求会重新执行，成功返回 `RETRIED`，之后再
  重放则是 `REPLAYED`。失败后若更换请求体，仍判 `CONFLICT`。

> 说明：当前为**进程内**存储（线程安全），适用于本地复现与单进程部署；
> 多实例部署时需将 `IdempotencyStore` 与 `RateLimiter` 的状态后端替换为
> Redis 等共享存储，治理语义保持不变。

---

## 四、HTTP 接口

### `GET /health`
健康检查，返回 `{"status": "ok"}`。

### `POST /api/orders`

请求头：

| 头 | 必填 | 说明 |
|----|------|------|
| `X-Caller` | 否 | 调用方标识（限流维度），缺省 `anonymous` |
| `X-Api-Version` | 是 | `v1` 或 `v2` |
| `Idempotency-Key` | 否 | 幂等标识 |
| `X-Request-Id` | 否 | 请求标识；不传则自动生成（贯穿日志） |

成功响应示例：

```json
{
  "request_id": "....",
  "idempotency_status": "COMPLETED",
  "data": { "order_id": "....", "name": "book", "api_version": "v2" }
}
```

错误响应示例（429）：

```json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "调用方 'acme' 在窗口 0 超出配额 5",
    "caller": "acme",
    "limit": 5,
    "window_epoch": 0,
    "retry_after": 60,
    "reason": "QUOTA_EXCEEDED"
  }
}
```

---

## 五、本地验证方法

环境要求：Python 3.14、[uv](https://docs.astral.sh/uv/)。

### 1. 安装依赖并运行测试

```bash
uv sync
uv run pytest -v          # 全量 22 个用例
uv run pytest -s          # 同时在控制台查看结构化治理日志
```

日志会打印**请求标识、限流判定（allowed/reason/used/limit/retry_after）、
幂等状态（COMPLETED/REPLAYED/PROCESSING/CONFLICT/RETRIED/FAILED）**，
便于解释每一次判定依据。重点场景：

```bash
# 并发超限（严格 10 放行 / 90 拒绝）
uv run pytest tests/test_ratelimit.py::test_concurrent_over_limit_is_exact -s
# 重复幂等（副作用只一次）+ 冲突 + 处理中 + 失败重试
uv run pytest tests/test_idempotency.py -s
# 版本不兼容
uv run pytest tests/test_versioning.py -s
# HTTP 端到端（并发超限 / 重复幂等 / 版本不兼容）
uv run pytest tests/test_api.py -s
```

### 2. 启动本地服务

```bash
uv run uvicorn main:app --reload
# 打开交互文档：http://127.0.0.1:8000/docs
```

### 3. 手工 curl 验证

```bash
# v1 正常
curl -s -X POST localhost:8000/api/orders \
  -H 'X-Caller: acme' -H 'X-Api-Version: v1' \
  -H 'Content-Type: application/json' -d '{"name":"book"}'

# v1 携带 v2 字段 -> 422 VERSION_INCOMPATIBLE
curl -s -X POST localhost:8000/api/orders \
  -H 'X-Caller: acme' -H 'X-Api-Version: v1' \
  -H 'Content-Type: application/json' -d '{"name":"book","coupon_id":"CP1"}'

# 幂等：连续两次相同 Idempotency-Key，第二次 200 REPLAYED 且 order_id 相同
curl -s -X POST localhost:8000/api/orders \
  -H 'X-Caller: acme' -H 'X-Api-Version: v2' -H 'Idempotency-Key: k-1' \
  -H 'Content-Type: application/json' -d '{"name":"book","coupon_id":"CP1"}'

# 连续超过 5 次 -> 429 RATE_LIMITED（带 Retry-After）
for i in $(seq 1 6); do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/api/orders \
    -H 'X-Caller: burst' -H 'X-Api-Version: v1' \
    -H 'Content-Type: application/json' -d '{"name":"x"}'
done
```
