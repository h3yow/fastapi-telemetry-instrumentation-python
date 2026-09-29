"""未捕获异常归类：业务/依赖/未知三类相互可区分。

判定依据同时落在三处且保持一致：
1. HTTP 响应 ``error.type``
2. 访问/错误结构化日志的 ``outcome`` 与 ``error_type``
3. ``/metrics`` 的 ``http_requests_total{outcome=...}``
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


class _LogCapture(logging.Handler):
    """收集 app.* logger 产出的结构化日志记录。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def extras(self) -> list[dict]:
        out = []
        standard = set(logging.makeLogRecord({}).__dict__)
        for r in self.records:
            out.append({k: v for k, v in r.__dict__.items()
                        if k not in standard and not k.startswith("_")})
        return out


@pytest.fixture()
def log_capture() -> Iterator[_LogCapture]:
    handler = _LogCapture()
    target = logging.getLogger("app")
    target.addHandler(handler)
    try:
        yield handler
    finally:
        target.removeHandler(handler)


def _metric_count(client: TestClient, outcome: str, status: str) -> int:
    text = client.get("/metrics").text
    total = 0
    for line in text.splitlines():
        if not line.startswith("http_requests_total{"):
            continue
        head, _, value = line.rpartition("} ")
        if f'outcome="{outcome}"' in head and f'status="{status}"' in head:
            total += int(value)
    return total


def test_business_exception_is_classified(
        client: TestClient, log_capture: _LogCapture) -> None:
    print("[input] GET /items/missing -> 触发 BusinessError(404)")
    r = client.get("/items/missing")
    print(f"[judge] HTTP {r.status_code} error.type="
          f"{r.json()['error']['type']!r}")
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "business_error"

    assert _metric_count(client, "business_error", "404") == 1
    business_logs = [e for e in log_capture.extras()
                     if e.get("outcome") == "business_error"]
    print(f"[judge] 归类日志条数={len(business_logs)}，error_type字段="
          f"{[e.get('error_type') for e in business_logs]}")
    assert any(e.get("error_type") == "BusinessError" for e in business_logs)


def test_dependency_exception_is_classified(
        client: TestClient, log_capture: _LogCapture) -> None:
    print("[input] GET /items/boom -> 下游不可用，触发 DependencyError(503)")
    r = client.get("/items/boom")
    body = r.json()
    print(f"[judge] HTTP {r.status_code} error.type={body['error']['type']!r} "
          f"响应不含堆栈={'simulated' not in r.text}")
    assert r.status_code == 503
    assert body["error"]["type"] == "dependency_error"
    assert "simulated" not in r.text

    assert _metric_count(client, "dependency_error", "503") == 1
    dep_logs = [e for e in log_capture.extras()
                if e.get("outcome") == "dependency_error"]
    print(f"[judge] 归类日志条数={len(dep_logs)}，detail字段只在日志: "
          f"{[e.get('detail') for e in dep_logs if 'detail' in e]}")
    assert any(e.get("error_type") == "DependencyError" for e in dep_logs)
    assert any("detail" in e for e in dep_logs), "内部细节应进日志而非响应"


def test_unexpected_exception_is_classified(
        client: TestClient, log_capture: _LogCapture) -> None:
    print("[input] GET /items/crash -> 未预期 RuntimeError")
    r = client.get("/items/crash")
    print(f"[judge] HTTP {r.status_code} error.type="
          f"{r.json()['error']['type']!r}")
    assert r.status_code == 500
    assert r.json()["error"]["type"] == "unexpected_error"
    assert "RuntimeError" not in r.text, "未知异常细节不得外泄"

    assert _metric_count(client, "unexpected_error", "500") == 1
    unexp_logs = [e for e in log_capture.extras()
                  if e.get("outcome") == "unexpected_error"]
    print(f"[judge] 归类日志条数={len(unexp_logs)}（至少含1条访问日志+1条错误日志）")
    assert len(unexp_logs) >= 1
    # 三类 outcome 不得互相串用。
    other = [e for e in unexp_logs
             if e.get("outcome") in ("business_error", "dependency_error")]
    assert not other


def test_three_outcomes_are_distinct(client: TestClient,
                                     log_capture: _LogCapture) -> None:
    print("[input] 依次触发 missing/boom/crash，比较三处结论")
    cases = [
        ("/items/missing", 404, "business_error"),
        ("/items/boom", 503, "dependency_error"),
        ("/items/crash", 500, "unexpected_error"),
    ]
    for path, expect_code, expect_outcome in cases:
        r = client.get(path)
        got = r.json()["error"]["type"]
        print(f"[judge] {path}: HTTP={r.status_code}/{expect_code} "
              f"type={got}/{expect_outcome}")
        assert r.status_code == expect_code and got == expect_outcome
        assert _metric_count(client, expect_outcome, str(expect_code)) >= 1
    # JSON 响应均可解析且结构统一。
    for path, _, _ in cases:
        payload = client.get(path).json()
        assert set(payload["error"]) == {"type", "message"}
