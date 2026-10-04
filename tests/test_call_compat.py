"""既有调用写法兼容性回归：同一请求内容，等价构造写法结论必须一致。

覆盖业务面：
A. 构造写法兼容：升级前的位置传参写法 (request_id, caller, version,
   idempotency_key, payload) 必须照旧走完版本校验、配额、幂等与业务执行，
   与按参数名传递的写法结论一致。
B. 失败边界：缺失/格式非法/不认识/已停用/请求体不匹配，五种结论互可区分，
   且与构造写法无关；被拒绝的请求一次都不能进入业务处理。
C. 配额：按 (caller, version) 作用域隔离，构造写法不影响命中的配额范围。
D. 幂等：作用域 (key, caller, version)，跨版本/跨调用方隔离，
   构造写法不影响回放与作用域归属。
E. 并发：同一内容用两种写法并发提交，结论一致且业务只执行一次。
F. 入口等价：同一内容从 HTTP 入口与直接服务调用进入，结论一致。
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from gateway import (
    ErrorCode,
    GatewayError,
    IdempotencyStore,
    QuotaPolicy,
    ScopedSlidingWindowRateLimiter,
    VersionRegistry,
    VersionRule,
    VersionStatus,
)
from gateway.service import GatewayRequest, GatewayService

logger = logging.getLogger("tests.call_compat")

#: 升级前既有的位置参数顺序，属于对外契约。
ARGS = ("request_id", "caller", "version", "idempotency_key", "payload")


def build_positional(rid, caller, version, key, payload) -> GatewayRequest:
    """升级前既有的写法：全部按位置传参。"""
    return GatewayRequest(rid, caller, version, key, payload)


def build_keyword(rid, caller, version, key, payload) -> GatewayRequest:
    """按参数名逐个传递的写法。"""
    return GatewayRequest(
        request_id=rid, caller=caller, version=version,
        idempotency_key=key, payload=payload,
    )


def build_mixed(rid, caller, version, key, payload) -> GatewayRequest:
    """混合写法：前两个位置参数，其余按参数名。"""
    return GatewayRequest(
        rid, caller, version=version, idempotency_key=key, payload=payload
    )


BUILDERS = [build_positional, build_keyword, build_mixed]


@pytest.fixture
def registry() -> VersionRegistry:
    reg = VersionRegistry()
    reg.register(VersionRule("v0", status=VersionStatus.RETIRED, replacement="v2"))
    reg.register(
        VersionRule(
            "v1",
            required_fields=frozenset({"name"}),
            status=VersionStatus.DEPRECATED,
            sunset="2027-03-01",
            replacement="v2",
        )
    )
    reg.register(
        VersionRule(
            "v2",
            required_fields=frozenset({"name", "email"}),
            removed_fields=frozenset({"legacy_id"}),
        )
    )
    return reg


def make_service(registry: VersionRegistry, policy: QuotaPolicy | None = None) -> GatewayService:
    return GatewayService(
        versions=registry,
        limiter=ScopedSlidingWindowRateLimiter(
            policy=policy or QuotaPolicy(default_limit=100), window_seconds=60.0
        ),
        idempotency=IdempotencyStore(),
    )


# ---- A. 构造写法兼容 --------------------------------------------------------


def test_legacy_positional_request_flows_through_whole_pipeline(registry):
    """升级前的位置传参写法：版本校验、配额、幂等、业务执行一个不落。"""
    svc = make_service(registry)
    calls = []
    resp = svc.handle(
        build_positional("r1", "alice", "v2", "k1", {"name": "a", "email": "e@x"}),
        handler=lambda p: calls.append(dict(p)) or {"echo": p},
    )
    assert resp.status == "executed"
    assert resp.result == {"echo": {"name": "a", "email": "e@x"}}
    # 业务处理确实跑到了，且只跑一次
    assert calls == [{"name": "a", "email": "e@x"}]
    logger.info("A 构造兼容 | 位置写法走完全流程 | status=%s calls=%d", resp.status, len(calls))


@pytest.mark.parametrize("builder", BUILDERS, ids=lambda b: b.__name__)
def test_equivalent_builders_give_identical_conclusion(registry, builder):
    """同一内容，任一等价写法在干净网关上结论相同（含废弃公告）。"""
    svc = make_service(registry)
    resp = svc.handle(
        builder("r1", "alice", "v1", "k1", {"name": "a"}),
        handler=lambda p: {"echo": p},
    )
    assert resp.status == "executed"
    assert resp.result == {"echo": {"name": "a"}}
    assert resp.deprecation is not None  # v1 废弃期公告不因写法丢失
    assert resp.deprecation.sunset == "2027-03-01"
    assert resp.deprecation.replacement == "v2"
    logger.info("A 构造兼容 | %s | status=%s deprecation=%s",
                builder.__name__, resp.status, resp.deprecation)


def test_idempotency_scope_shared_across_builders(registry):
    """同一 (key, caller, version)：位置写法提交、关键字写法重试，必须回放同一结果。"""
    svc = make_service(registry)
    first = svc.handle(
        build_positional("r1", "alice", "v2", "k-share", {"name": "a", "email": "e@x"}),
        handler=lambda p: {"echo": p},
    )
    second = svc.handle(
        build_keyword("r2", "alice", "v2", "k-share", {"name": "a", "email": "e@x"}),
        handler=lambda p: {"echo": p},
    )
    third = svc.handle(
        build_mixed("r3", "alice", "v2", "k-share", {"name": "a", "email": "e@x"}),
        handler=lambda p: {"echo": p},
    )
    assert first.status == "executed"
    assert second.status == third.status == "replayed"
    assert second.result == third.result == first.result
    logger.info("A 构造兼容 | 跨写法幂等回放 | first=%s second=%s third=%s",
                first.status, second.status, third.status)


# ---- B. 失败边界：五种结论互可区分，与写法无关 ------------------------------

#: (用例名, 版本值, 期望错误码)；payload 固定缺 email，使 v2 落在"请求体不匹配"。
REJECTION_CASES = [
    ("missing", None, ErrorCode.MISSING_VERSION),
    ("invalid_format", "vX", ErrorCode.INVALID_VERSION),
    ("invalid_type", 1.5, ErrorCode.INVALID_VERSION),
    ("unsupported", "v9", ErrorCode.UNSUPPORTED_VERSION),
    ("retired", "v0", ErrorCode.VERSION_RETIRED),
    ("incompatible", "v2", ErrorCode.INCOMPATIBLE_VERSION),
]


@pytest.mark.parametrize("builder", BUILDERS, ids=lambda b: b.__name__)
@pytest.mark.parametrize("case,version,code", REJECTION_CASES, ids=[c[0] for c in REJECTION_CASES])
def test_rejection_conclusion_is_builder_independent(registry, builder, case, version, code):
    svc = make_service(registry)
    calls = []
    with pytest.raises(GatewayError) as excinfo:
        svc.handle(
            builder("r1", "alice", version, "k1", {"name": "a"}),
            handler=lambda p: calls.append(p),
        )
    assert excinfo.value.code is code
    # 被拒绝的请求一次都不能进入业务处理
    assert calls == []
    logger.info("B 失败边界 | %s | %s -> %s（业务未执行）",
                builder.__name__, case, excinfo.value.code.value)


def test_rejection_codes_are_pairwise_distinct(registry):
    """五类失败的结论两两可区分，且已停用版本不被兼容放行。"""
    svc = make_service(registry)
    codes = set()
    for case, version, code in REJECTION_CASES:
        with pytest.raises(GatewayError) as excinfo:
            svc.handle(
                build_positional("r", "alice", version, f"k-{case}", {"name": "a"}),
                handler=lambda p: p,
            )
        assert excinfo.value.code is code
        codes.add(excinfo.value.code)
    # 缺失/非法/不认识/已停用/不匹配：五类边界对应五个互可区分的结论
    assert codes == {
        ErrorCode.MISSING_VERSION,
        ErrorCode.INVALID_VERSION,
        ErrorCode.UNSUPPORTED_VERSION,
        ErrorCode.VERSION_RETIRED,
        ErrorCode.INCOMPATIBLE_VERSION,
    }
    logger.info("B 失败边界 | 五类结论互可区分 | %s", sorted(c.value for c in codes))


# ---- C. 配额：按 (caller, version) 隔离，写法不影响命中范围 ----------------


def test_quota_scope_isolated_across_versions_and_callers(registry):
    """(alice, v1) 打满后：同 caller 的 v2、其他 caller 的 v1 都不受牵连。"""
    policy = QuotaPolicy(default_limit=100, version_limits={"v1": 2})
    svc = make_service(registry, policy)
    payload = {"name": "a"}

    # 混合写法打满 (alice, v1) 的 2 次额度
    r1 = svc.handle(build_positional("q1", "alice", "v1", "qk1", payload), lambda p: 1)
    r2 = svc.handle(build_keyword("q2", "alice", "v1", "qk2", payload), lambda p: 2)
    assert r1.status == r2.status == "executed"

    # 第 3 次（又一种写法）被拒绝，且拒绝详情指向 (alice, v1) 作用域
    with pytest.raises(GatewayError) as excinfo:
        svc.handle(build_mixed("q3", "alice", "v1", "qk3", payload), lambda p: 3)
    err = excinfo.value
    assert err.code is ErrorCode.RATE_LIMITED
    assert err.details["scope"] == {"caller": "alice", "version": "v1"}
    assert err.details["limit"] == 2
    assert err.details["matched_rule"] == "version"
    assert err.retry_after is not None and err.retry_after >= 0

    # 跨版本隔离：同 caller 在 v2 上仍有额度
    r4 = svc.handle(
        build_positional("q4", "alice", "v2", "qk4", {"name": "a", "email": "e@x"}),
        lambda p: 4,
    )
    assert r4.status == "executed"
    # 跨调用方隔离：其他 caller 在 v1 上仍有额度
    r5 = svc.handle(build_keyword("q5", "bob", "v1", "qk5", payload), lambda p: 5)
    assert r5.status == "executed"
    logger.info(
        "C 配额隔离 | (alice,v1) 限 2 打满后拒绝 scope=%s | (alice,v2)=%s (bob,v1)=%s",
        err.details["scope"], r4.status, r5.status,
    )


def test_quota_scope_shared_across_builders(registry):
    """同一 (caller, version)：不同写法消耗的是同一份配额。"""
    policy = QuotaPolicy(default_limit=100, version_limits={"v1": 2})
    svc = make_service(registry, policy)
    payload = {"name": "a"}
    svc.handle(build_positional("s1", "carol", "v1", "sk1", payload), lambda p: 1)
    svc.handle(build_keyword("s2", "carol", "v1", "sk2", payload), lambda p: 2)
    with pytest.raises(GatewayError) as excinfo:
        svc.handle(build_mixed("s3", "carol", "v1", "sk3", payload), lambda p: 3)
    assert excinfo.value.code is ErrorCode.RATE_LIMITED
    logger.info("C 配额隔离 | 三种写法共用 (carol,v1) 配额 | 第 3 次=%s",
                excinfo.value.code.value)


# ---- D. 幂等：作用域 (key, caller, version)，跨版本/跨调用方隔离 -----------


def test_same_key_isolated_across_versions_and_callers(registry):
    """同一幂等键：跨版本、跨调用方都是全新操作，互不回放、互不冲突。"""
    svc = make_service(registry)
    marker = object()

    v1 = svc.handle(
        build_positional("d1", "alice", "v1", "dk", {"name": "a"}),
        lambda p: ("v1-result", marker),
    )
    assert v1.status == "executed"

    # 同 (key, caller, version) 换写法重试 -> 回放同一结果
    v1_again = svc.handle(
        build_keyword("d2", "alice", "v1", "dk", {"name": "a"}),
        lambda p: ("other", marker),
    )
    assert v1_again.status == "replayed"
    assert v1_again.result == v1.result

    # 同键跨版本 -> 全新执行，不回放 v1 的结果
    v2 = svc.handle(
        build_mixed("d3", "alice", "v2", "dk", {"name": "a", "email": "e@x"}),
        lambda p: ("v2-result", marker),
    )
    assert v2.status == "executed"
    assert v2.result != v1.result

    # 同键同版本跨调用方 -> 全新执行，不回放、不报冲突
    bob = svc.handle(
        build_positional("d4", "bob", "v1", "dk", {"name": "a"}),
        lambda p: ("bob-result", marker),
    )
    assert bob.status == "executed"
    assert bob.result != v1.result

    # 各作用域的回放仍然各自独立
    assert svc.handle(
        build_keyword("d5", "bob", "v1", "dk", {"name": "a"}), lambda p: None
    ).result == bob.result
    logger.info(
        "D 幂等隔离 | 同键四作用域 | v1=%s v1重试=%s v2=%s bob=%s",
        v1.status, v1_again.status, v2.status, bob.status,
    )


def test_same_key_different_payload_conflicts_regardless_of_builder(registry):
    """同作用域同键不同内容 -> IDEMPOTENCY_CONFLICT，与写法无关。"""
    svc = make_service(registry)
    svc.handle(
        build_positional("d1", "alice", "v1", "dk2", {"name": "a"}), lambda p: 1
    )
    for builder in BUILDERS:
        with pytest.raises(GatewayError) as excinfo:
            svc.handle(
                builder("d2", "alice", "v1", "dk2", {"name": "b"}), lambda p: 2
            )
        assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    logger.info("D 幂等隔离 | 同键不同内容 | 三种写法均 -> IDEMPOTENCY_CONFLICT")


# ---- E. 并发：同一内容两种写法并发提交，结论一致 ----------------------------


def test_concurrent_mixed_builders_execute_exactly_once(registry):
    """同一 (key, caller, version, payload) 用两种写法并发提交：
    业务恰好执行一次，其余要么回放同一结果，要么得到 IN_PROGRESS 拒绝。"""
    svc = make_service(registry)
    payload = {"name": "a", "email": "e@x"}
    n = 12
    barrier = threading.Barrier(n)
    exec_count = 0
    exec_lock = threading.Lock()
    results: list[tuple[str, object]] = []
    res_lock = threading.Lock()

    def handler(p):
        nonlocal exec_count
        with exec_lock:
            exec_count += 1
        time.sleep(0.02)  # 拉大并发重叠窗口
        return {"echo": p}

    def run(i):
        builder = BUILDERS[i % len(BUILDERS)]
        barrier.wait(timeout=5)
        try:
            resp = svc.handle(builder(f"r{i}", "alice", "v2", "ck", payload), handler)
            outcome: tuple[str, object] = (resp.status, resp.result)
        except GatewayError as e:
            outcome = ("rejected", e.code)
        with res_lock:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    executed = [r for r in results if r[0] == "executed"]
    replayed = [r for r in results if r[0] == "replayed"]
    rejected = [r for r in results if r[0] == "rejected"]
    assert len(results) == n
    assert len(executed) == 1, f"业务必须恰好执行一次，实际 {len(executed)}"
    assert exec_count == 1
    # 拒绝只能是"处理中"，且所有成功结论的结果一致
    assert all(code is ErrorCode.IDEMPOTENCY_IN_PROGRESS for _, code in rejected)
    expected = {"echo": payload}
    assert all(res == expected for _, res in executed + replayed)
    logger.info(
        "E 并发一致 | %d 线程混合写法 | executed=%d replayed=%d in_progress=%d 业务执行=%d",
        n, len(executed), len(replayed), len(rejected), exec_count,
    )


def test_concurrent_same_content_same_quota_conclusion(registry):
    """并发下同一 (caller, version) 的配额结论确定：放行数恰好等于额度。"""
    policy = QuotaPolicy(default_limit=100, version_limits={"v2": 5})
    svc = make_service(registry, policy)
    n = 10
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def run(i):
        builder = BUILDERS[i % len(BUILDERS)]
        barrier.wait(timeout=5)
        try:
            svc.handle(
                builder(f"rq{i}", "dave", "v2", f"cq{i}", {"name": "a", "email": "e@x"}),
                lambda p: p,
            )
            outcome = "allowed"
        except GatewayError as e:
            assert e.code is ErrorCode.RATE_LIMITED
            outcome = "limited"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert outcomes.count("allowed") == 5
    assert outcomes.count("limited") == 5
    logger.info("E 并发一致 | (dave,v2) 限 5 并发 %d | allowed=%d limited=%d",
                n, outcomes.count("allowed"), outcomes.count("limited"))


# ---- F. 入口等价：HTTP 入口与直接服务调用结论一致 ---------------------------


def test_http_and_direct_entries_give_same_conclusion():
    """同一内容：HTTP 入口与直接 service.handle 的结论一致（含回放与拒绝码）。"""
    from fastapi.testclient import TestClient

    from main import app, service

    service._idempotency.__init__()  # 隔离全局服务状态
    client = TestClient(app)
    body = {
        "caller": "entry-check",
        "version": "v2",
        "idempotency_key": "entry-k1",
        "payload": {"name": "a", "email": "e@x"},
    }

    http_resp = client.post("/orders", json=body)
    assert http_resp.status_code == 201
    assert http_resp.json()["status"] == "executed"

    # 同一内容从直接调用入口进来：命中同一幂等作用域，回放同一结果
    direct = service.handle(
        build_positional("r-entry", "entry-check", "v2", "entry-k1",
                         {"name": "a", "email": "e@x"}),
        handler=lambda p: {"should": "not run"},
    )
    assert direct.status == "replayed"
    assert direct.result == http_resp.json()["result"]

    # 拒绝结论同样一致：HTTP 的 410/VERSION_RETIRED 与直接调用同码
    http_retired = client.post("/orders", json={**body, "version": "v0",
                                                "idempotency_key": "entry-k2"})
    assert http_retired.status_code == 410
    assert http_retired.json()["code"] == "VERSION_RETIRED"
    with pytest.raises(GatewayError) as excinfo:
        service.handle(
            build_keyword("r-entry2", "entry-check", "v0", "entry-k2",
                          {"name": "a", "email": "e@x"}),
            handler=lambda p: p,
        )
    assert excinfo.value.code is ErrorCode.VERSION_RETIRED
    logger.info("F 入口等价 | HTTP=executed 直接调用=%s | 停用版本两入口均 VERSION_RETIRED",
                direct.status)
