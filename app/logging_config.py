"""结构化日志配置：JSON 输出，自动注入请求标识。"""
from __future__ import annotations

import json
import logging
import sys

from app.request_id import get_request_id


class RequestContextFilter(logging.Filter):
    """把当前请求标识注入日志记录（无请求上下文时为 "-"）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


class JSONFormatter(logging.Formatter):
    """单行 JSON 日志格式，包含服务名与请求标识。"""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service_name,
            "request_id": getattr(record, "request_id", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(service_name: str) -> None:
    """配置根日志器输出 JSON 结构化日志到 stdout。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(service_name))
    handler.addFilter(RequestContextFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
