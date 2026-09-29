"""异常归类：不同失败原因可区分，未捕获异常不泄露内部细节。"""
import asyncio
import logging

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.exceptions import (
    CATEGORY_CLIENT,
    CATEGORY_DEPENDENCY,
    CATEGORY_TIMEOUT,
    CATEGORY_UNEXPECTED,
    CATEGORY_VALIDATION,
    classify_exception,
)
from main import create_app

logger = logging.getLogger("tests.exceptions")


def test_classify_exception_categories():
    cases = [
        (HTTPException(status_code=404), CATEGORY_CLIENT),
        (HTTPException(status_code=500), CATEGORY_UNEXPECTED),
        (TimeoutError("t"), CATEGORY_TIMEOUT),
        (asyncio.TimeoutError("t"), CATEGORY_TIMEOUT),
        (ConnectionRefusedError("c"), CATEGORY_DEPENDENCY),
        (RuntimeError("x"), CATEGORY_UNEXPECTED),
        (ValueError("x"), CATEGORY_UNEXPECTED),
    ]
    for exc, expected in cases:
        actual = classify_exception(exc)
        logger.info(
            "输入 exc=%r | 判定依据: 类型映射 => 期望 %s, 实际 %s",
            exc, expected, actual,
        )
        assert actual == expected


def test_unhandled_exception_classified_and_sanitized(client):
    r = client.get("/boom", headers={"X-Request-ID": "req-boom-1"})
    body = r.json()
    logger.info(
        "输入: GET /boom (内部 raise RuntimeError('demo failure')) | "
        "判定依据: 500 + error=unexpected_error + 不泄露内部信息 + 请求标识一致 => %d %s",
        r.status_code, body,
    )
    assert r.status_code == 500
    assert body["error"] == CATEGORY_UNEXPECTED
    assert body["detail"] == "internal error"
    assert "demo failure" not in r.text and "Traceback" not in r.text
    assert body["request_id"] == "req-boom-1"
    assert r.headers["x-request-id"] == "req-boom-1"


def test_http_exception_classified_client_error(client):
    r = client.get("/no-such-route")
    body = r.json()
    logger.info(
        "输入: GET /no-such-route | 判定依据: 404 归类 client_error => %d %s",
        r.status_code, body,
    )
    assert r.status_code == 404
    assert body["error"] == CATEGORY_CLIENT


def test_validation_error_classified(client):
    r = client.get("/items/not-an-int")
    body = r.json()
    logger.info(
        "输入: GET /items/not-an-int (item_id 应为 int) | 判定依据: 422 归类 validation_error => %d",
        r.status_code,
    )
    assert r.status_code == 422
    assert body["error"] == CATEGORY_VALIDATION


def test_timeout_and_dependency_categories_distinct():
    """超时与依赖连接失败在响应中可区分。"""
    app = create_app(Settings())

    @app.get("/fail/timeout")
    def fail_timeout():
        raise TimeoutError("simulated timeout")

    @app.get("/fail/dependency")
    def fail_dependency():
        raise ConnectionRefusedError("simulated connection refused")

    client = TestClient(app, raise_server_exceptions=False)
    r_timeout = client.get("/fail/timeout")
    r_dep = client.get("/fail/dependency")
    logger.info(
        "输入: GET /fail/timeout 与 /fail/dependency | 判定依据: error 字段分别为 %s / %s => %s / %s",
        CATEGORY_TIMEOUT, CATEGORY_DEPENDENCY,
        r_timeout.json(), r_dep.json(),
    )
    assert r_timeout.status_code == 500
    assert r_timeout.json()["error"] == CATEGORY_TIMEOUT
    assert r_dep.status_code == 500
    assert r_dep.json()["error"] == CATEGORY_DEPENDENCY
    assert r_timeout.json()["error"] != r_dep.json()["error"]
