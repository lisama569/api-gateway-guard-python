# API Gateway Guard (Python)

在现有 FastAPI 服务之上实现的本地可复现 API 网关治理层，覆盖三项能力：

- **多版本接口共存**：版本间不兼容的请求被明确拒绝，错误可区分。
- **按调用方限流**：并发下配额准确，超限返回可判断重试时机的信息。
- **幂等处理**：重复请求不重复触发副作用，处理中 / 已完成 / 冲突可区分，失败可安全重试。

## 结构

```
gateway/
  errors.py       # 统一错误码 GatewayError / ErrorCode
  versioning.py   # VersionRegistry：版本注册与兼容校验
  ratelimit.py    # SlidingWindowRateLimiter：按 caller 的滑动窗口限流
  idempotency.py  # IdempotencyStore：幂等键状态机
  service.py      # GatewayService：按固定顺序组合三者
main.py           # FastAPI 入口，错误码 -> HTTP 状态映射
tests/            # 单元与并发测试
```

处理顺序固定为：**版本校验 → 限流 → 幂等申领 → 执行业务 → 完成/失败登记**。
任一环节拒绝都不会进入后续环节（版本不兼容不消耗配额、不占用幂等键）。

## 版本兼容规则

- 请求必须显式声明版本；未注册的版本拒绝为 `UNSUPPORTED_VERSION`（HTTP 400）。
- 请求体必须包含该版本的全部必填字段，缺失拒绝为 `INCOMPATIBLE_VERSION`（HTTP 422）。
- 请求体不得携带该版本已移除的字段，携带拒绝为 `INCOMPATIBLE_VERSION`（HTTP 422）。
- 校验只针对请求自己声明的版本：旧版本请求**不会**被静默路由到新版本，
  也不会因形状恰好匹配旧版本而被错误接受。

内置示例：`v1` 要求 `name`；`v2` 要求 `name + email` 并移除 `legacy_id`。

## 限流语义

- 维度：按 `caller` 独立计数；算法：滑动窗口日志。
- 窗口为半开区间 `(now - window, now]`：边界上的时间戳视为已过期，
  窗口边界抖动不会造成漏放或重复计数。
- 判定与计数在同一把锁内原子完成，并发请求下放行数恰好等于配额。
- 超限拒绝为 `RATE_LIMITED`（HTTP 429），响应携带 `retry_after`
  （同时写入 `Retry-After` 头），含义为"窗口内最早一次请求滑出窗口所需等待秒数"，
  调用方可据此判断重试时机。

## 幂等契约

- 请求携带 `idempotency_key`；服务端对请求体计算稳定指纹（SHA-256）。
- 首次申领：进入 `IN_PROGRESS` 并执行副作用。
- 处理中重复申领：拒绝为 `IDEMPOTENCY_IN_PROGRESS`（HTTP 409），不重复执行。
- 已完成重复申领：直接复用缓存结果返回（`status=replayed`），不重复执行。
- 同一键携带不同请求体：拒绝为 `IDEMPOTENCY_CONFLICT`（HTTP 409），
  与"处理中"明确区分。
- 业务处理抛异常：键被释放为 `FAILED`，相同请求体可安全重试。
- 所有状态迁移在同一把锁内完成：N 个并发相同请求中恰好 1 个获得执行权。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 运行全部测试（并发超限 / 重复幂等 / 版本不兼容）
uv run uvicorn main:app --reload   # 本地启动服务
```

测试日志（`gateway.service` logger）会打印请求标识、限流判定与幂等状态，例如：

```
INFO request_id=req-7 caller=alice rate_limit allowed=False remaining=0 retry_after=59.98
INFO request_id=req-3 idempotency_key=same-key state=IN_PROGRESS should_execute=False
```

可用 `-o log_cli=true` 在 pytest 中直接观察：

```bash
uv run pytest -q -o log_cli=true --log-cli-level=INFO
```

示例请求：

```bash
curl -X POST localhost:8000/orders -H 'content-type: application/json' -d '{
  "caller": "alice", "version": "v1",
  "idempotency_key": "order-123",
  "payload": {"name": "demo"}
}'
```
