# API Gateway Guard (Python)

在 FastAPI 服务之上实现的本地可复现 API 网关治理层，覆盖：

- **多版本接口共存与版本生命周期**：正常提供 / 已废弃但仍可用 / 已停止提供三态；
  未声明、格式非法、不认识、已停用、形状不符五种版本结论互可区分。
- **按"调用方 × 接口版本"组合配额**：跨版本、跨调用方额度互相独立，
  支持版本统一额度与调用方专属覆盖，并发与窗口切换下配额精确。
- **跨版本幂等隔离**：同一幂等标识只在相同 `(caller, version)` 作用域内复用，
  换版本/换调用方不回放、不冲突，失败可同作用域安全重试。

## 结构

```
gateway/
  errors.py         # 统一错误码 GatewayError / ErrorCode
  versioning.py     # VersionRegistry：生命周期 + 版本注册与兼容校验
  ratelimit.py      # SlidingWindowRateLimiter：旧版按 caller 限流（保留兼容）
  quota.py          # QuotaLimiter：按 (caller, version) 的滑动窗口配额
  idempotency.py    # IdempotencyStore + scope()：作用域幂等状态机
  service.py        # GatewayService：按固定顺序组合各环节
main.py             # FastAPI 入口，错误码 -> HTTP 状态映射
tests/              # 单元、并发与端到端测试
```

处理顺序固定为：**版本校验/生命周期 → 配额判定 → 作用域幂等申领 → 执行业务
→ 完成/失败登记**。任一环节拒绝都不会进入后续环节（版本校验失败不消耗配额、
不占用幂等键）。

## 版本生命周期

每个注册版本处于以下三种状态之一：

| 状态 | 行为 |
| --- | --- |
| `ACTIVE`（正常提供） | 正常处理请求，响应无额外提示。 |
| `DEPRECATED`（已废弃但仍可用） | 正常处理请求；响应 `warnings[0]` 附带废弃提示：`status=DEPRECATED`、`sunset_on`（计划停止支持日期）、`successor`（建议迁移的替代版本）与人类可读 `message`。 |
| `RETIRED`（已停止提供） | 明确拒绝，错误码 `VERSION_RETIRED`（HTTP 410），拒绝信息指向替代版本。 |

版本相关的五种结论稳定且互可区分：

| 场景 | 错误码 | HTTP |
| --- | --- | --- |
| 请求未声明版本（缺失/空串） | `VERSION_MISSING` | 400 |
| 版本值格式不合法（合法格式为 `v<数字>`，如 `v2`） | `VERSION_MALFORMED` | 400 |
| 版本未注册（版本不认识） | `UNSUPPORTED_VERSION` | 400 |
| 版本已停止提供 | `VERSION_RETIRED` | 410 |
| 请求体与该版本形状对不上（缺必填 / 带已移除字段） | `INCOMPATIBLE_VERSION` | 422 |

形状校验只针对请求自己声明的版本：旧版本请求**不会**被静默路由到新版本。
停用判定先于形状校验——即使停用版本的请求体形状恰好匹配，结论仍是 `VERSION_RETIRED`。

废弃期响应示例：

```json
{
  "status": "executed",
  "result": {...},
  "warnings": [
    {
      "version": "v1",
      "status": "DEPRECATED",
      "sunset_on": "2027-03-01",
      "successor": "v2",
      "message": "version 'v1' is deprecated and will be retired after 2027-03-01; migrate to 'v2'"
    }
  ]
}
```

## 配额作用范围（caller × version）

配额最小作用域是 **(调用方, 接口版本)** 二元组：

- 同一调用方在 `v1` 与 `v2` 上的额度**相互独立**，一个版本打满不影响另一个版本。
- 不同调用方之间额度**互不影响**。
- `set_version_limit(version, limit, window_seconds)`：设置某版本的**统一额度**
  （`VERSION` 范围，对该版本所有调用方生效）。
- `override_caller_limit(version, caller, limit)`：为特定调用方在该版本设置
  **专属覆盖额度**（`CALLER` 范围），优先于版本统一额度；窗口时长沿用版本配置。

算法为滑动窗口日志，窗口为半开区间 `(now - window, now]`，边界时间戳视为已过期；
判定与计数在同一把锁内原子完成，并发放行数恰好等于额度，窗口切换不多放、不少算。

超限时拒绝为 `RATE_LIMITED`（HTTP 429），除 `retry_after`（窗口内最早一条记录
滑出所需等待秒数，并写入 `Retry-After` 头）外还携带：

- `scope`：命中的配额范围，`VERSION`（版本统一额度）或 `CALLER`（调用方专属覆盖），
  调用方可据此区分"该版本整体吃紧"还是"自己在该版本的专属额度用尽"；
- `remaining`：当前剩余可用次数（拒绝时为 `0`）。

## 跨版本幂等契约

幂等作用域为 **(caller, version)**，通过 `IdempotencyStore.scope(caller, version)`
获得作用域视图后使用与键级 API 一致的 `claim / complete / fail / state_of`：

- 同一幂等标识**仅在相同调用方 + 相同版本**下重复提交时才复用：
  已完成回放缓存结果（`status=replayed`），处理中返回 `IDEMPOTENCY_IN_PROGRESS`。
- **换版本**或**换调用方**再用同一标识，是互不相关的两笔操作：
  不会回放旧结果、不会误判处理中；即使请求体指纹不同也不会误判成 `IDEMPOTENCY_CONFLICT`。
- 冲突判定（同键不同指纹）同样只在相同作用域内成立。
- 业务处理失败后，键在同作用域内释放，允许安全重试；失败状态不外溢到其他作用域。
- 所有状态迁移在同一把锁内完成：同作用域 N 个并发请求恰好 1 个获得执行权，
  其他作用域的并发请求不被阻塞。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 全部单测 + 并发测试
uv run uvicorn main:app --reload   # 本地启动服务
```

测试覆盖：版本废弃/停用与五种版本结论、跨版本跨调用方配额隔离与覆盖范围、
同一标识跨版本/跨调用方幂等隔离，以及这些场景在并发下的结论一致性；
同时保留了既有的版本兼容、限流与幂等行为测试（无回归）。

带日志观察（请求标识、配额判定与幂等状态）：

```bash
uv run pytest -q -o log_cli=true --log-cli-level=INFO
```

示例请求：

```bash
# v1 已废弃：201 且 warnings 带迁移提示
curl -X POST localhost:8000/orders -H 'content-type: application/json' -d '{
  "caller": "alice", "version": "v1",
  "idempotency_key": "order-123",
  "payload": {"name": "demo"}
}'

# v0 已停用：410 VERSION_RETIRED
curl -X POST localhost:8000/orders -H 'content-type: application/json' -d '{
  "caller": "alice", "version": "v0",
  "idempotency_key": "order-124", "payload": {}
}'
```
