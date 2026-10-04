# API Gateway Guard (Python)

在现有 FastAPI 服务之上实现的本地可复现 API 网关治理层，覆盖三条链路：

- **多版本接口共存与版本生命周期**：版本可处于正常提供 / 已废弃但仍可用 / 已停止提供，
  缺失、格式非法、不认识、已停用、请求体不兼容各有可区分结论。
- **按调用方 × 接口版本组合生效的配额**：同一调用方不同版本额度独立，不同调用方互不影响；
  支持版本统一额度与特定调用方覆盖，并发与窗口切换下配额严格准确。
- **跨版本隔离的幂等处理**：幂等作用域为 `(idempotency_key, caller, version)`，
  换版本或换调用方不回放旧结果、不误判冲突；处理中 / 已完成 / 冲突可区分，失败可安全重试。

## 结构

```
gateway/
  errors.py       # 统一错误码 GatewayError / ErrorCode（含结构化 details）
  versioning.py   # VersionRegistry：生命周期解析 + 兼容校验
  ratelimit.py    # SlidingWindowRateLimiter（按 caller，旧）
                  # ScopedSlidingWindowRateLimiter + QuotaPolicy（按 caller×version）
  idempotency.py  # IdempotencyStore：按 (key, caller, version) 隔离的状态机
  service.py      # GatewayService：按固定顺序组合三者
main.py           # FastAPI 入口，错误码 -> HTTP 状态映射
tests/            # 单元、端到端与并发测试
```

处理顺序固定为：**版本生命周期解析 → 请求体兼容校验 → 作用域配额 → 作用域幂等申领
→ 执行业务 → 完成/失败登记**。任一环节拒绝都不会进入后续环节
（版本被停用/不兼容不消耗配额、不占用幂等键）。

## 调用约定与兼容性

`GatewayRequest` 的规范构造方式是**按参数名传参**：

```python
GatewayRequest(
    request_id="r-1", caller="alice", version="v2",
    idempotency_key="order-123", payload={"name": "a", "email": "a@x"},
)
```

**既有调用方无需改代码**：治理层升级前的位置参数写法
`GatewayRequest(rid, caller, version, idempotency_key, payload)` 仍然可用，
构造时会按槽位类型归一化为同一请求对象。兼容性承诺：

- 同一份请求内容，无论用旧位置写法、新位置写法还是关键字写法构造，
  归一化后字段完全一致，后续版本校验、配额判定、幂等申领、业务执行的
  结论（放行/回放/拒绝及拒绝原因）完全相同，并发下也一致；
- 两种写法落在**同一个** `(caller, version)` 配额作用域与
  `(key, caller, version)` 幂等作用域内：老写法消耗的额度新写法同样受限，
  老写法执行过的键新写法提交会回放；
- 拒绝结论只取决于请求内容本身（缺失/非法/不认识/已停用/不兼容），
  与构造写法无关；
- 位置参数歧义（如槽位类型对不上两种顺序）会在构造期抛 `TypeError`，
  不会静默错配；同一槽位位置与关键字混传给两遍也会抛 `TypeError`。

HTTP 入口（`POST /orders`）的 JSON 体字段与 `GatewayRequest` 一一对应，
`version` 可缺省（缺省即 `MISSING_VERSION` 结论），从 HTTP 入口与直接
调用 `GatewayService` 的结论一致。

## 版本生命周期规则

每个版本注册时声明一种状态：

| 状态 | 请求处理 | 成功响应 |
| --- | --- | --- |
| `ACTIVE` 正常提供 | 正常处理 | 无附加公告 |
| `DEPRECATED` 已废弃但仍可用 | 正常处理，兼容校验照常 | 附带废弃公告 |
| `RETIRED` 已停止提供 | 直接拒绝，不消耗配额/幂等键 | — |

废弃公告随**每个**成功响应（包括幂等回放响应）返回，内含：

- `sunset`：计划停止支持日期（ISO `YYYY-MM-DD`）；
- `replacement`：建议迁移到的替代版本。

HTTP 上同时给出标准头：`Deprecation: true`、`Sunset`（UTC HTTP-date）、
`Link: </orders?version=v2>; rel="successor-version"`；响应体内为 `deprecation` 对象。

版本相关失败互可区分（调用方应据此决定是"补参数"、"升级版本"还是"迁移"）：

| 错误码 | HTTP | 触发条件 |
| --- | --- | --- |
| `MISSING_VERSION` | 400 | 未声明版本（缺字段或空串/纯空白） |
| `INVALID_VERSION` | 400 | 版本值格式不合法（当前要求 `v<非负整数>`，如 `v1`、`v2`） |
| `UNSUPPORTED_VERSION` | 400 | 格式合法但注册表里不认识 |
| `VERSION_RETIRED` | 410 | 版本注册过、曾提供，现已停止（响应带替代版本指引） |
| `INCOMPATIBLE_VERSION` | 422 | 版本可用，但请求体缺必填字段或带已移除字段 |

判定优先级：先看生命周期（停用即拒绝），再做请求体形状校验——
停用版本不会因请求体形状而被误判成 `INCOMPATIBLE_VERSION`。
废弃版本**不豁免**兼容校验：形状不对仍是 `INCOMPATIBLE_VERSION`。

内置示例：`v0` 已停用（迁往 v2）；`v1` 处于废弃期（2027-03-01 停止，迁往 v2），
要求 `name`；`v2` 正常提供，要求 `name + email` 并移除 `legacy_id`。

## 配额作用范围

配额作用域是 **(caller, version) 二元组**：

- 同一调用方在 `v1`、`v2` 上的额度各自独立计数，一个版本超限不影响另一版本；
- 不同调用方即使在同一版本上也互不影响；
- 算法为滑动窗口日志，窗口为半开区间 `(now - window, now]`，
  边界时间戳视为已过期，窗口切换不会多放或少算；
- 判定与计数在同一把锁内原子完成，并发放行数恰好等于配额。

额度解析优先级：**特定调用方覆盖 > 版本统一额度 > 全局兜底额度**，
由 `QuotaPolicy(default_limit, version_limits, caller_version_limits)` 配置。

超限拒绝为 `RATE_LIMITED`（HTTP 429，带 `Retry-After`），结构化 `details` 携带：

```json
{
  "scope": {"caller": "alice", "version": "v1"},
  "limit": 5,
  "remaining": 0,
  "matched_rule": "caller_version"
}
```

`matched_rule` 标明命中的规则层级（`caller_version` / `version` / `default`），
配合 `scope` 可区分"某调用方在某版本上被单独压额"与"整个版本统一超限"；
`retry_after` 为窗口内最早一次调用滑出所需等待秒数。

> 旧的 `SlidingWindowRateLimiter`（仅按 caller）仍保留，`GatewayService`
> 可直接装配；新链路默认使用 `ScopedSlidingWindowRateLimiter`。

## 跨版本幂等契约

幂等记录按 **(idempotency_key, caller, version) 三元组**隔离：

- 同一键只有在**同一调用方、同一版本**下重复提交时才复用已完成结果
  （`status=replayed`）或识别处理中（`IDEMPOTENCY_IN_PROGRESS`，HTTP 409）；
- 换版本再用该键：视为全新操作，各自执行、各自缓存，绝不回放旧结果；
- 换调用方再用该键：同样隔离，两个调用方的同键请求各有独立结果；
- 同键不同请求体的 `IDEMPOTENCY_CONFLICT`（HTTP 409）只在**同一作用域内**成立；
  跨版本/跨调用方即使请求体不同也不是冲突；
- 业务处理抛异常：键在其作用域内释放为 `FAILED`，同作用域同请求体可安全重试，
  不影响其他作用域；
- 服务端对请求体计算稳定指纹（SHA-256），所有状态迁移在同一把锁内完成：
  每个作用域的 N 个并发请求中恰好 1 个获得执行权。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 全部测试：生命周期 / 跨版本配额 / 跨作用域幂等 / 并发一致性
uv run pytest -v        # 逐条列出每个业务文件下的用例与结果（PASSED/FAILED）
uv run uvicorn main:app --reload   # 本地启动服务
```

测试文件与覆盖面对应关系：

- `tests/test_compat.py`：调用写法兼容性回归——旧位置写法与关键字写法
  构造归一、成功/回放/五类失败分支结论一致、配额与幂等作用域跨写法共享、
  并发混发结论不分裂；
- `tests/test_lifecycle.py`：三态与五类版本结论区分、废弃公告、停用不消耗配额；
- `tests/test_quota.py`：跨版本/跨调用方配额隔离、覆盖优先级、窗口边界、并发严格性、拒绝详情；
- `tests/test_idempotency_scope.py`：同键跨版本/跨调用方隔离、冲突作用域化、并发结论一致性；
- `tests/test_http.py`：HTTP 入口的错误码→状态映射、废弃响应头、作用域化拒绝体。

也可用 `-o log_cli=true` 在 pytest 中直接观察日志：

```bash
uv run pytest -q -o log_cli=true --log-cli-level=INFO
```

示例请求：

```bash
# 废弃版本：201，但响应带 Deprecation/Sunset/Link 头与 deprecation 对象
curl -i -X POST localhost:8000/orders -H 'content-type: application/json' -d '{
  "caller": "alice", "version": "v1",
  "idempotency_key": "order-123",
  "payload": {"name": "demo"}
}'

# 停用版本：410 VERSION_RETIRED，details 指向替代版本
curl -i -X POST localhost:8000/orders -H 'content-type: application/json' -d '{
  "caller": "alice", "version": "v0",
  "idempotency_key": "order-124",
  "payload": {"name": "demo"}
}'

# 迁移到正常版本 v2
curl -X POST localhost:8000/orders -H 'content-type: application/json' -d '{
  "caller": "alice", "version": "v2",
  "idempotency_key": "order-125",
  "payload": {"name": "demo", "email": "demo@example.com"}
}'
```
