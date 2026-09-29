"""请求标识：解析、生成与上下文传播。"""
from __future__ import annotations

import contextvars
import re
import uuid

_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# 合法标识：1..max_length 个字符，仅字母数字与 - _ . ，兼容 UUID/hex 等常见格式
_VALID_RE = re.compile(r"[A-Za-z0-9._-]+")


def get_request_id() -> str:
    """返回当前请求上下文中的请求标识（无请求上下文时为 "-"）。"""
    return _REQUEST_ID.get()


def is_valid_request_id(value: str, max_length: int) -> bool:
    """判断上游传入的标识是否合法：非空、不超长、字符集受控。"""
    if not value or len(value) > max_length:
        return False
    return _VALID_RE.fullmatch(value) is not None


def generate_request_id() -> str:
    """按固定规则生成新的请求标识：req- 前缀 + uuid4 hex。"""
    return f"req-{uuid.uuid4().hex}"


def resolve_request_id(raw: bytes | None, max_length: int) -> str:
    """从原始头字节解析请求标识；缺失、非法或解码失败时生成新标识。"""
    if raw is None:
        return generate_request_id()
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError:
        return generate_request_id()
    if is_valid_request_id(value, max_length):
        return value
    return generate_request_id()


class RequestIDMiddleware:
    """纯 ASGI 中间件：为每个请求建立独立的请求标识上下文并写入响应头。

    使用 contextvars 保证并发请求之间上下文不串线；
    上游头缺失、非法、超长或含非 ASCII 字节时回退为生成的新标识，请求不失败。
    """

    def __init__(self, app, header_name: str, max_length: int) -> None:
        self.app = app
        self.header_bytes = header_name.lower().encode("latin-1")
        self.max_length = max_length

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = None
        for name, value in scope.get("headers", []):
            if name == self.header_bytes:
                raw = value
                break
        request_id = resolve_request_id(raw, self.max_length)
        token = _REQUEST_ID.set(request_id)
        try:
            async def send_with_request_id(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append((self.header_bytes, request_id.encode("ascii")))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, send_with_request_id)
        finally:
            _REQUEST_ID.reset(token)
