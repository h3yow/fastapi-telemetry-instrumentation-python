"""pytest 公共夹具。

* :func:`client`      每个测试一个全新应用 + 复位后的指标注册表。
* :func:`downstream`  暴露可注入故障的下游依赖单例，测试结束复位为 up。

所有测试只走内存里的 ASGI 应用（TestClient），不监听端口、不访问外部服务、
不需要任何真实凭据。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import create_app
from app.config import Settings
from app.health import MODE_UP, downstream
from app.metrics import registry


@pytest.fixture()
def settings() -> Settings:
    # 探针超时给小一点，让超时类测试跑得快。
    return Settings(
        service_name="test-service",
        request_id_header="X-Request-ID",
        request_id_max_length=128,
        metrics_path="/metrics",
        health_liveness_path="/health/live",
        health_readiness_path="/health/ready",
        dependency_probe_timeout=0.1,
    )


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    registry.reset()
    downstream.set_mode(MODE_UP)
    app = create_app(settings)
    # 未捕获异常在测试里也应被我们自己归类为 500，而不是抛出。
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    downstream.set_mode(MODE_UP)
