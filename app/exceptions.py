"""未捕获异常的归类与统一处理。"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from fastapi.responses import JSONResponse

from app.request_id import get_request_id

logger = logging.getLogger("app.exceptions")

# 失败原因类别（可区分、可枚举）
CATEGORY_VALIDATION = "validation_error"    # 请求参数校验失败
CATEGORY_CLIENT = "client_error"            # 4xx 业务错误
CATEGORY_TIMEOUT = "timeout"                # 超时
CATEGORY_DEPENDENCY = "dependency_error"    # 下游依赖/连接失败
CATEGORY_UNEXPECTED = "unexpected_error"    # 其余未捕获异常


def classify_exception(exc: BaseException) -> str:
    """把异常归类为可区分的失败原因标签。"""
    if isinstance(exc, RequestValidationError):
        return CATEGORY_VALIDATION
    if isinstance(exc, HTTPException):
        return CATEGORY_CLIENT if exc.status_code < 500 else CATEGORY_UNEXPECTED
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return CATEGORY_TIMEOUT
    if isinstance(exc, ConnectionError):
        return CATEGORY_DEPENDENCY
    return CATEGORY_UNEXPECTED


class ExceptionClassificationMiddleware:
    """兜底异常处理中间件：归类记录日志，返回统一 500 响应。

    必须作为用户中间件注册（位于 RequestIDMiddleware 之内），
    这样处理异常时请求标识上下文仍然可用，响应也能带上请求标识头。
    不注册 FastAPI 的 Exception 处理器，因为它会被放到最外层的
    ServerErrorMiddleware，丢失请求上下文。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            category = classify_exception(exc)
            logger.exception(
                "unhandled exception",
                extra={"category": category, "path": scope.get("path", "")},
            )
            body = json.dumps(
                {
                    "error": category,
                    "detail": "internal error",
                    "request_id": get_request_id(),
                }
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})


def register_exception_handlers(app) -> None:
    """注册 4xx/校验异常处理器：归类记录日志，对外返回统一结构。"""

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        category = classify_exception(exc)
        logger.info(
            "request failed", extra={"category": category, "path": request.url.path}
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": category,
                "detail": exc.errors(),
                "request_id": get_request_id(),
            },
        )

    @app.exception_handler(HTTPException)
    async def on_http_exception(request: Request, exc: HTTPException):
        category = classify_exception(exc)
        logger.info(
            "request failed",
            extra={"category": category, "status": exc.status_code, "path": request.url.path},
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": category, "detail": exc.detail, "request_id": get_request_id()},
        )
