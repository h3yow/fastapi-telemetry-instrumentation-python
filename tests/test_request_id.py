"""请求标识：合法性规则、生成规则、响应头传播与并发隔离。"""
import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.request_id import (
    RequestIDMiddleware,
    generate_request_id,
    get_request_id,
    is_valid_request_id,
    resolve_request_id,
)

logger = logging.getLogger("tests.request_id")

GENERATED_RE = re.compile(r"^req-[0-9a-f]{32}$")


def test_valid_request_id_rules():
    cases = [
        ("abc-123_X.Y", 128, True),
        ("", 128, False),            # 空串非法
        ("a" * 128, 128, True),      # 边界：恰好等于上限
        ("a" * 129, 128, False),     # 超长非法
        ("has space", 128, False),   # 非法字符
        ("中文id", 128, False),       # 非 ASCII 非法
    ]
    for value, max_len, expected in cases:
        actual = is_valid_request_id(value, max_len)
        logger.info(
            "输入 value=%r max_length=%d | 判定依据: 非空+不超长+字符集[A-Za-z0-9._-] => 期望 %s, 实际 %s",
            value, max_len, expected, actual,
        )
        assert actual is expected


def test_generate_request_id_format():
    rid = generate_request_id()
    logger.info("生成结果=%r | 判定依据: 固定格式 req-<uuid4hex>", rid)
    assert GENERATED_RE.match(rid)
    assert generate_request_id() != generate_request_id()  # 每次生成不同


def test_resolve_request_id_fallback():
    cases = [
        (b"up-123", "up-123"),                       # 合法上游标识沿用
        (None, "generated"),                          # 缺失 -> 生成
        (b"bad id", "generated"),                     # 非法字符 -> 生成
        (b"x" * 200, "generated"),                    # 超长 -> 生成
        (b"\xff\xfe\x00", "generated"),               # 非 ASCII 字节 -> 生成
    ]
    for raw, expected_kind in cases:
        rid = resolve_request_id(raw, max_length=128)
        logger.info("输入 raw=%r | 判定依据: 缺失/非法/超长/非ASCII 回退生成 => %r", raw, rid)
        if expected_kind == "generated":
            assert GENERATED_RE.match(rid)
        else:
            assert rid == expected_kind


def test_response_header_echoes_valid_upstream_id(client):
    r = client.get("/", headers={"X-Request-ID": "upstream-abc-1"})
    logger.info(
        "输入 header X-Request-ID=upstream-abc-1 | 判定依据: 合法标识应原样出现在响应头 => %s",
        r.headers.get("x-request-id"),
    )
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "upstream-abc-1"


def test_response_header_generated_when_missing_or_invalid(client):
    for headers, why in [
        ({}, "缺失"),
        ({"X-Request-ID": "bad id with spaces"}, "非法字符"),
        ({"X-Request-ID": "x" * 500}, "超长"),
    ]:
        r = client.get("/", headers=headers)
        rid = r.headers["x-request-id"]
        logger.info("输入 %s (%s) | 判定依据: 回退为生成的标识 => %s", headers, why, rid)
        assert r.status_code == 200  # 异常头不让请求失败
        assert GENERATED_RE.match(rid)


def _build_echo_app() -> FastAPI:
    """带回声接口的测试应用：返回当前上下文中的请求标识。"""
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware, header_name="X-Request-ID", max_length=128)

    @app.get("/echo")
    async def echo():
        await asyncio.sleep(0.01)  # 让并发请求充分交错
        return {"request_id": get_request_id()}

    return app


def test_concurrent_request_id_isolation():
    """并发请求各自的上游标识不能串线。"""
    app = _build_echo_app()
    client = TestClient(app)
    ids = [f"upstream-{i:04d}" for i in range(50)]
    logger.info("输入: 50 个并发请求, 各带唯一 X-Request-ID | 判定依据: 响应体与响应头必须等于各自发送值")

    def send(rid):
        r = client.get("/echo", headers={"X-Request-ID": rid})
        return rid, r.json()["request_id"], r.headers["x-request-id"]

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(send, ids))

    for sent, body_id, header_id in results:
        assert body_id == sent, f"上下文串线: 发送 {sent}, 上下文读到 {body_id}"
        assert header_id == sent
    logger.info("判定通过: %d 个并发请求上下文全部一一对应", len(results))


def test_concurrent_generated_ids_unique():
    """并发且缺失上游标识时，生成的标识互不重复。"""
    app = _build_echo_app()
    client = TestClient(app)
    logger.info("输入: 50 个并发请求均不带 X-Request-ID | 判定依据: 生成标识全部唯一且格式固定")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: client.get("/echo").json()["request_id"], range(50)))

    assert len(set(results)) == 50
    assert all(GENERATED_RE.match(r) for r in results)
    logger.info("判定通过: 50 个生成标识唯一, 示例=%s", results[0])
