"""测试公共配置：打开治理层日志，便于在单测输出中查看
请求标识 / 限流判定 / 幂等状态等可解释信息。

使用 ``uv run pytest -s`` 可直接在控制台看到结构化日志；
默认运行时日志也会被 pytest 的 caplog 捕获。
"""

from __future__ import annotations

import logging
import sys

import pytest

_app_logger = logging.getLogger("app")
_app_logger.setLevel(logging.DEBUG)
if not any(getattr(h, "_gateway_test_handler", False) for h in _app_logger.handlers):
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    _h._gateway_test_handler = True  # type: ignore[attr-defined]
    _app_logger.addHandler(_h)
_app_logger.propagate = True


@pytest.fixture(autouse=True)
def _enable_gateway_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="app")


def log_request(marker: str, **fields: object) -> None:
    """测试辅助：以结构化 key=value 形式打印判定依据。"""
    body = " ".join(f"{k}={v!r}" for k, v in fields.items())
    logging.getLogger("app.test").info("[%s] %s", marker, body)
