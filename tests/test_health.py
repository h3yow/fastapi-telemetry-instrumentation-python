"""健康检查：存活恒真；就绪随依赖状态切换并给出可区分原因。

每类故障给出确定且互不相同的 reason，测试失败时能直接分辨是
“存活被依赖拖垮 / 就绪未反映故障 / 原因不可区分 / 细节泄露 / 进程被拖垮”。
不连接任何外部服务：依赖是进程内可切换模式的 fake。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.health import (
    MODE_DOWN,
    MODE_ERROR,
    MODE_TIMEOUT,
    MODE_UP,
)


def test_liveness_is_always_ok(client: TestClient) -> None:
    print("[input] 依赖依次置为 up/down/timeout/error，访问 /health/live")
    statuses = {}
    for mode in (MODE_UP, MODE_DOWN, MODE_TIMEOUT, MODE_ERROR):
        from app.health import downstream
        downstream.set_mode(mode)
        r = client.get("/health/live")
        statuses[mode] = r.status_code
    print(f"[judge] 各模式下存活状态码={statuses}（期望全部200）")
    assert all(code == 200 for code in statuses.values()), \
        "存活只反映本进程，依赖故障绝不能把它拖成失败"


def test_readiness_ok_when_dependency_up(client: TestClient) -> None:
    print("[input] 依赖=up，访问 /health/ready")
    r = client.get("/health/ready")
    print(f"[judge] 状态码={r.status_code} body={r.json()}")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["checks"]["downstream"] == "up"


def test_readiness_fails_when_dependency_down(client: TestClient) -> None:
    print("[input] 依赖=down，访问 /health/ready")
    from app.health import downstream
    downstream.set_mode(MODE_DOWN)
    r = client.get("/health/ready")
    print(f"[judge] 状态码={r.status_code} reason={r.json()['checks']['downstream']}")
    assert r.status_code == 503, "依赖不可用必须体现为不就绪"
    assert r.json()["status"] == "unavailable"
    assert r.json()["checks"]["downstream"] == "dependency_unavailable"


def test_readiness_fails_when_dependency_times_out(client: TestClient) -> None:
    print("[input] 依赖=timeout（永久挂起），探针超时 0.1s")
    from app.health import downstream
    downstream.set_mode(MODE_TIMEOUT)
    r = client.get("/health/ready")
    print(f"[judge] 状态码={r.status_code} reason={r.json()['checks']['downstream']}")
    assert r.status_code == 503
    assert r.json()["checks"]["downstream"] == "dependency_timeout", \
        "超时必须与依赖不可用区分开"


def test_readiness_fails_when_dependency_raises(client: TestClient) -> None:
    print("[input] 依赖=error（探针内部抛异常）")
    from app.health import downstream
    downstream.set_mode(MODE_ERROR)
    r = client.get("/health/ready")
    print(f"[judge] 状态码={r.status_code} reason={r.json()['checks']['downstream']}")
    assert r.status_code == 503
    assert r.json()["checks"]["downstream"] == "dependency_error"


def test_readiness_reasons_are_distinguishable(client: TestClient) -> None:
    from app.health import downstream
    reasons = {}
    for mode in (MODE_DOWN, MODE_TIMEOUT, MODE_ERROR):
        downstream.set_mode(mode)
        reasons[mode] = client.get("/health/ready").json()["checks"]["downstream"]
    downstream.set_mode(MODE_UP)
    print(f"[judge] 三类失败原因={reasons}")
    assert len(set(reasons.values())) == 3, "失败原因必须两两可区分"


def test_readiness_does_not_expose_internal_details(client: TestClient) -> None:
    print("[input] 依赖=error，检查 503 响应是否泄露内部异常信息")
    from app.health import downstream
    downstream.set_mode(MODE_ERROR)
    r = client.get("/health/ready")
    body = r.text
    print(f"[judge] 响应体={body}")
    assert "Traceback" not in body and "RuntimeError" not in body
    assert "simulated" not in body, "内部细节只允许进日志，不能进对外响应"


def test_probe_never_crashes_process(client: TestClient) -> None:
    print("[input] 依赖故障期间继续打业务流量与存活探针 20 轮")
    from app.health import downstream
    downstream.set_mode(MODE_ERROR)
    for i in range(20):
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
    downstream.set_mode(MODE_UP)
    assert client.get("/health/ready").status_code == 200, "故障消除后必须能恢复就绪"
    print("[judge] 全程进程存活，就绪随依赖恢复，未被异常拖垮")
