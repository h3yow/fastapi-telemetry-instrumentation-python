"""请求标识：沿用/生成/非法兜底/超长容错/并发隔离。

每个用例都会 print 触发输入与判定依据；失败时从输出即可判断是
“上游沿用失败 / 生成失败 / 并发串号”中的哪一类。
"""

from __future__ import annotations

import concurrent.futures

from fastapi.testclient import TestClient


def test_inbound_valid_id_is_reused(client: TestClient) -> None:
    inbound = "trace-2026-09-30.AB_cd~01"
    print(f"[input] X-Request-ID={inbound!r} -> 期望: 原样沿用")
    r = client.get("/whoami", headers={"X-Request-ID": inbound})
    print(f"[judge] 状态={r.status_code} 响应头={r.headers['x-request-id']!r} "
          f"回显={r.json()['request_id']!r}")
    assert r.status_code == 200
    assert r.headers["x-request-id"] == inbound, "合法上游标识必须原样回显"
    assert r.json()["request_id"] == inbound, "处理逻辑必须读到同一个标识"


def test_missing_id_is_generated(client: TestClient) -> None:
    print("[input] 不带 X-Request-ID -> 期望: 生成 req-<32位hex>")
    r1 = client.get("/whoami")
    r2 = client.get("/whoami")
    rid1, rid2 = r1.headers["x-request-id"], r2.headers["x-request-id"]
    print(f"[judge] 两次生成: {rid1!r}, {rid2!r}")
    assert rid1.startswith("req-") and len(rid1) == 36
    assert rid1 != rid2, "缺失时每次必须生成不同标识"


def test_invalid_id_is_replaced(client: TestClient) -> None:
    # 均为 ASCII 可编码但不满足请求标识白名单（含空格/控制字符/标点）。
    # 非 ASCII 头值在客户端/网关层就无法构造，不属于应用层处理范围。
    for bad in ("has space", "tab\there", "crlf\r\ninjected",
                "(paren)", "semi;colon", "quote\"x"):
        print(f"[input] X-Request-ID={bad!r} -> 期望: 拒绝并重新生成")
        r = client.get("/whoami", headers={"X-Request-ID": bad})
        rid = r.headers["x-request-id"]
        print(f"[judge] 状态={r.status_code} 替换后={rid!r}")
        assert r.status_code == 200, "非法头不能让请求失败"
        assert rid.startswith("req-"), f"非法值 {bad!r} 必须被替换，而非沿用"
        assert "\r" not in rid and "\n" not in rid, "响应头禁止 CRLF 注入"


def test_overlong_header_does_not_break_request(client: TestClient) -> None:
    # 请求上限默认 128；再给一个超 HTTP 头部字段常见长度(512)的值。
    for length in (129, 200, 4096):
        giant = "a" * length
        print(f"[input] X-Request-ID 长度={length} -> 期望: 不失败、不丢日志、重新生成")
        r = client.get("/whoami", headers={"X-Request-ID": giant})
        rid = r.headers["x-request-id"]
        print(f"[judge] 状态={r.status_code} 替换后长度={len(rid)} (={rid[:8]}...)")
        assert r.status_code == 200
        assert rid.startswith("req-") and len(rid) == 36


def test_concurrent_requests_have_isolated_ids(client: TestClient) -> None:
    # 一半请求携带彼此不同的上游标识，一半不带标识（走生成）。
    n = 80
    inputs: list[str | None] = [
        f"upstream-{i}" if i % 2 == 0 else None for i in range(n)
    ]
    print(f"[input] {n} 个并发请求（半数携带唯一上游ID，半数缺失）")

    def one(idx: int, inbound: str | None) -> tuple[str, str]:
        headers = {"X-Request-ID": inbound} if inbound is not None else {}
        r = client.get("/whoami", headers=headers)
        return r.headers["x-request-id"], r.json()["request_id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda args: one(*args), enumerate(inputs)))

    mismatches = [
        (i, inputs[i], results[i])
        for i in range(n)
        if (inputs[i] is not None and results[i][0] != inputs[i])
        or results[i][0] != results[i][1]
        or not results[i][0].startswith(("req-", "upstream-"))
    ]
    generated = [rid for rid, _ in results if rid.startswith("req-")]
    print(f"[judge] 不一致条数={len(mismatches)}；生成型标识去重后="
          f"{len(set(generated))}/{len(generated)}")
    assert not mismatches, f"并发串号/错号: {mismatches[:3]}"
    assert len(set(generated)) == len(generated), "不同请求生成了相同标识"
    # 每个上游标识必须恰好被一个请求沿用。
    for i, inbound in enumerate(inputs):
        if inbound is not None:
            assert results[i] == (inbound, inbound)
