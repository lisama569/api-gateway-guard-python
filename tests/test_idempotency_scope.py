"""跨版本/跨调用方幂等隔离：标识只在相同 (caller, version) 作用域内复用。"""

import threading

import pytest

from gateway import (
    ErrorCode,
    GatewayError,
    IdempotencyState,
    IdempotencyStore,
)


def test_replay_only_within_same_caller_and_version():
    store = IdempotencyStore()
    store.scope("alice", "v1").claim("k", "fp")
    store.scope("alice", "v1").complete("k", "fp", {"v": 1})

    same = store.scope("alice", "v1").claim("k", "fp")
    assert same.state is IdempotencyState.COMPLETED
    assert same.cached_result == {"v": 1}

    # 换版本：不回放，是一笔全新操作
    other_version = store.scope("alice", "v2").claim("k", "fp")
    assert other_version.should_execute
    assert other_version.state is IdempotencyState.IN_PROGRESS

    # 换调用方：不回放，是一笔全新操作
    other_caller = store.scope("bob", "v1").claim("k", "fp")
    assert other_caller.should_execute
    assert other_caller.state is IdempotencyState.IN_PROGRESS


def test_in_progress_not_shared_across_scopes():
    store = IdempotencyStore()
    store.scope("alice", "v1").claim("k", "fp")
    # alice/v1 处理中，其他作用域不应被误判为处理中
    assert store.scope("alice", "v2").claim("k", "fp").should_execute
    assert store.scope("bob", "v1").claim("k", "fp").should_execute
    assert not store.scope("alice", "v1").claim("k", "fp").should_execute


def test_conflict_only_within_same_scope():
    store = IdempotencyStore()
    store.scope("alice", "v1").claim("k", "fp-a")
    # 同作用域不同指纹：冲突
    with pytest.raises(GatewayError) as exc:
        store.scope("alice", "v1").claim("k", "fp-b")
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    # 跨版本 / 跨调用方即便指纹也不同，也不构成冲突
    assert store.scope("alice", "v2").claim("k", "fp-b").should_execute
    assert store.scope("bob", "v1").claim("k", "fp-c").should_execute


def test_failure_retry_scoped_to_same_scope():
    store = IdempotencyStore()
    view = store.scope("alice", "v1")
    view.claim("k", "fp")
    view.fail("k", "fp")
    # 同作用域可安全重试
    assert view.claim("k", "fp").should_execute
    # 失败状态不外溢到其他作用域：那边仍是全新操作
    assert store.scope("alice", "v2").claim("k", "fp").should_execute


def test_same_key_two_versions_complete_independently():
    """同一标识在两个版本各自完成一笔，结果互不串用。"""
    store = IdempotencyStore()
    v1 = store.scope("alice", "v1")
    v2 = store.scope("alice", "v2")
    v1.claim("k", "fp1")
    v1.complete("k", "fp1", {"order": "from-v1"})
    v2.claim("k", "fp2")
    v2.complete("k", "fp2", {"order": "from-v2"})

    assert v1.claim("k", "fp1").cached_result == {"order": "from-v1"}
    assert v2.claim("k", "fp2").cached_result == {"order": "from-v2"}
    # 同标识在另一调用方也完全独立
    bob = store.scope("bob", "v1")
    assert bob.claim("k", "fp1").should_execute


def test_concurrent_same_scope_single_execution_other_scopes_free():
    """同作用域 50 线程并发：仅 1 个执行；50 个互不相同的其他作用域并发时全部放行。"""
    store = IdempotencyStore()
    same_scope_claims = []
    other_scope_executed = []
    barrier = threading.Barrier(100)

    def claim_same():
        barrier.wait()
        same_scope_claims.append(store.scope("alice", "v1").claim("k", "fp"))

    def claim_other(i):
        barrier.wait()
        # 每个线程处于独立作用域（不同版本/调用方），互不阻塞也不影响 alice/v1
        other_scope_executed.append(
            store.scope(f"caller-{i}", "v2").claim("k", "fp").should_execute
        )

    threads = [threading.Thread(target=claim_same) for _ in range(50)]
    threads += [threading.Thread(target=claim_other, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len([c for c in same_scope_claims if c.should_execute]) == 1
    assert len([c for c in same_scope_claims if not c.should_execute]) == 49
    assert len(other_scope_executed) == 50
    assert all(other_scope_executed)
