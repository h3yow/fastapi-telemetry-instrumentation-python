"""请求标识的解析、生成与按请求隔离的上下文存储。

规则（与 README 一致）：

1. 优先沿用上游 ``X-Request-ID``（名称可配置），值必须非空、长度不超过
   配置上限，且只包含 ``A-Z a-z 0-9 - _ . ~`` —— 显式拒绝空白与控制字符，
   从根上杜绝日志/响应头注入。
2. 缺失或非法时生成形如 ``req-<uuid4 十六进制>`` 的新标识（固定 36 字符）。
3. 标识存于 :class:`contextvars.ContextVar`：每个 ASGI 请求运行在独立的
   复制上下文中，并发请求之间互不串线；同步线程池端点也会复制上下文。
"""

from __future__ import annotations

#: 允许出现在请求标识中的字符：RFC 3986 unreserved 字符集。
import re
import uuid
from contextvars import ContextVar, Token

_VALID_RE = re.compile(r"^[A-Za-z0-9._~\-]+$")

#: 当前请求标识；未进入请求上下文时为 "-"。
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def get_request_id() -> str:
    """返回当前上下文里的请求标识（无上下文时为 ``-``）。"""
    return request_id_var.get()


def set_request_id(value: str) -> Token[str]:
    """绑定当前上下文的请求标识，返回可用于回滚的 token。"""
    return request_id_var.set(value)


def is_valid_request_id(value: str, max_length: int) -> bool:
    """判断上游标识是否可沿用：非空、未超长、字符集合法。"""
    if not value or len(value) > max_length:
        return False
    return bool(_VALID_RE.fullmatch(value))


def generate_request_id() -> str:
    """生成新的请求标识：``req-`` + UUID4 十六进制（共 36 字符）。"""
    return f"req-{uuid.uuid4().hex}"


def resolve_request_id(inbound: str | None, max_length: int) -> tuple[str, bool]:
    """解析入站标识。

    返回 ``(request_id, reused)``；``reused`` 为 ``True`` 表示沿用了上游
    合法标识，``False`` 表示入站缺失/非法而由本进程生成。
    """
    if inbound is not None:
        candidate = inbound.strip()
        if candidate and is_valid_request_id(candidate, max_length):
            return candidate, True
    return generate_request_id(), False
