"""结构化（JSON 行）日志配置，自动注入当前请求标识。

* 每条日志输出一行 JSON 到 stderr，字段固定为
  ``ts / level / logger / msg / request_id``，附加字段平铺在同一层。
* :class:`RequestIdFilter` 在渲染时从 :data:`app.request_context.request_id_var`
  读取当前请求标识——取值发生在日志真正输出的那一刻、所在上下文之内，
  因此并发请求即使共用同一 logger/handler 也不会串号。
* :func:`configure_logging` 幂等，重复调用不会叠加 handler。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from typing import Any

from app.request_context import request_id_var

#: LogRecord 自带属性白名单；平铺时只输出除此之外的 extra 字段。
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__.keys()) | {
    "request_id", "msg"
}


class RequestIdFilter(logging.Filter):
    """给每条日志记录补充当前上下文的 ``request_id``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染为单行 JSON；异常信息进 ``exc`` 字段。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.UTC
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


_CONFIGURED = False


def configure_logging() -> None:
    """安装 JSON handler 到根 logger（幂等）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    # 不沿用 uvicorn 等既有 handler，保证所有业务日志统一为 JSON 行。
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取 logger；未初始化时先做一次幂等配置，保证单测直接调用也可用。"""
    configure_logging()
    return logging.getLogger(name)
