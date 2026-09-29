"""健康检查：存活与就绪分离，依赖降级时可区分原因且不拖垮进程。"""
import logging

from fastapi.testclient import TestClient

from app.config import Settings
from app.health import check_dependency, readiness
from main import create_app

logger = logging.getLogger("tests.health")


def test_liveness_always_ok():
    """依赖不可用时存活检查仍然通过（本进程可判活）。"""
    app = create_app(Settings(dependency_ok=False))
    client = TestClient(app)
    r = client.get("/healthz")
    logger.info(
        "输入: dependency_ok=False 时 GET /healthz | 判定依据: 存活只看本进程 => %d %s",
        r.status_code, r.json(),
    )
    assert r.status_code == 200
    assert r.json() == {"status": "alive"}


def test_readiness_ready_ready_when_dependency_ok(client):
    r = client.get("/readyz")
    logger.info("输入: 依赖正常 GET /readyz | 判定依据: 200 + reason=ok => %s", r.json())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["dependencies"][0]["reason"] == "ok"


def test_readiness_degraded_when_dependency_down():
    app = create_app(Settings(dependency_ok=False))
    client = TestClient(app)
    r = client.get("/readyz")
    body = r.json()
    logger.info(
        "输入: dependency_ok=False GET /readyz | 判定依据: 503 + not_ready + reason=unavailable => %d %s",
        r.status_code, body,
    )
    assert r.status_code == 503
    assert body["status"] == "not_ready"
    dep = body["dependencies"][0]
    assert dep["ok"] is False
    assert dep["reason"] == "unavailable"
    # 不泄露内部细节：只暴露固定的可枚举字段
    assert set(dep.keys()) == {"name", "ok", "reason"}


def test_readiness_timeout_reason():
    status, body = readiness(timeout_seconds=0.0, dependency_ok=True)
    logger.info(
        "输入: timeout_seconds=0 | 判定依据: 超时应归类为 reason=timeout => %d %s",
        status, body,
    )
    assert status == 503
    assert body["dependencies"][0]["reason"] == "timeout"


def test_dependency_check_never_raises():
    """依赖检查内部异常被归类为 error，不外抛、不拖垮进程。"""

    class Exploding:
        def __bool__(self):
            raise RuntimeError("boom internal detail")

    result = check_dependency("primary-store", 0.5, Exploding())
    logger.info(
        "输入: 依赖对象求值时抛 RuntimeError | 判定依据: 捕获并归类 reason=error => %s",
        result,
    )
    assert result.ok is False
    assert result.reason == "error"
