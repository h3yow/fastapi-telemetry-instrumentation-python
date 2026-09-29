"""请求标识注入、访问日志/指标采集与异常归类。

结论一致性的关键：异常处理器把每个失败请求归类成固定 ``outcome``
（business_error / dependency_error / unexpected_error / http_4xx / http_5xx），
写入 ``request.state``；中间件在出口统一读取该结论，使结构化访问日志、
响应状态码与 Prometheus 指标对同一请求给出相同说法。
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import metrics
from app.errors import BusinessError, DependencyError
from app.logging_setup import get_logger
from app.request_context import resolve_request_id, set_request_id

logger = get_logger("app.access")

#: request.state 上保存归类结论所用的键。
OUTCOME_STATE_KEY = "outcome"
REQUEST_ID_STATE_KEY = "request_id"

_MAX_HEADER_FIELD_BYTES = 512


def _safe_header(request: Request, name: str) -> str | None:
    """读取请求头；异常或超长一律按“非法”处理，绝不让请求因此失败。"""
    try:
        value = request.headers.get(name)
    except Exception:  # noqa: BLE001 - 任何头解析问题都不能拖垮请求
        return None
    if value is None:
        return None
    if len(value.encode("utf-8", "replace")) > _MAX_HEADER_FIELD_BYTES:
        return None
    return value


def _id_header(request: Request) -> tuple[str, str]:
    settings = request.app.state.settings
    return settings.request_id_header, getattr(
        request.state, REQUEST_ID_STATE_KEY, "-")


def _error_response(request: Request, status_code: int, outcome: str,
                    message: str) -> JSONResponse:
    header_name, request_id = _id_header(request)
    return JSONResponse(
        status_code=status_code,
        content={"error": {"type": outcome, "message": message}},
        headers={header_name: request_id},
    )


def _classify_status(code: int) -> str:
    """未被异常处理器特殊归类时，按状态码段给出口径。"""
    if 200 <= code < 400:
        return "success"
    if 400 <= code < 500:
        return "http_4xx"
    return "http_5xx"


def install_exception_handlers(app: FastAPI) -> None:
    """注册异常处理器：统一 JSON 错误体 + 固定归类，500 不泄露细节。"""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc_handler(request: Request, exc: StarletteHTTPException):
        outcome = "http_4xx" if exc.status_code < 500 else "http_5xx"
        setattr(request.state, OUTCOME_STATE_KEY, outcome)
        # Starlette 的 detail 是受控消息（如 "Not Found"），可安全回显。
        return _error_response(request, exc.status_code, outcome, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_exc_handler(request: Request, exc: RequestValidationError):
        setattr(request.state, OUTCOME_STATE_KEY, "http_4xx")
        return _error_response(request, 422, "http_4xx", "request validation failed")

    @app.exception_handler(BusinessError)
    async def _business_handler(request: Request, exc: BusinessError):
        setattr(request.state, OUTCOME_STATE_KEY, "business_error")
        logger.info(
            "business rule rejected request",
            extra={"outcome": "business_error", "error_type": type(exc).__name__,
                   "business_message": exc.message, "path": request.url.path},
        )
        return _error_response(
            request, exc.status_code, "business_error", exc.message)

    @app.exception_handler(DependencyError)
    async def _dependency_handler(request: Request, exc: DependencyError):
        setattr(request.state, OUTCOME_STATE_KEY, "dependency_error")
        # internal_detail 只进日志，不进对外响应。
        logger.exception(
            "downstream dependency failed",
            extra={"outcome": "dependency_error", "error_type": type(exc).__name__,
                   "path": request.url.path, "detail": exc.internal_detail},
        )
        return _error_response(
            request, 503, "dependency_error",
            "upstream dependency is unavailable")

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        setattr(request.state, OUTCOME_STATE_KEY, "unexpected_error")
        logger.exception(
            "unhandled exception",
            extra={"outcome": "unexpected_error", "error_type": type(exc).__name__,
                   "path": request.url.path},
        )
        # 统一固定文案：内部错误细节不直接暴露。
        return _error_response(
            request, 500, "unexpected_error", "internal server error")


def _emit_metrics(request: Request, status_code: int, outcome: str,
                  duration: float, id_reused: bool) -> str:
    """出口统一动作：计算 route 标签、记指标、写一条访问日志。"""
    route_pattern = None
    route = request.scope.get("route")
    if route is not None:
        route_pattern = getattr(route, "path", None)
    route_label = metrics.normalize_route(request.url.path, route_pattern)
    metrics.registry.observe(
        method=request.method, route=route_label,
        status_code=status_code, outcome=outcome, duration=duration)
    logger.info(
        "request completed",
        extra={"method": request.method, "path": request.url.path,
               "route": route_label, "status_code": status_code,
               "outcome": outcome, "duration_s": round(duration, 6),
               "id_reused": id_reused})
    return route_label


def install_middleware(app: FastAPI) -> None:
    """注册请求标识 + 访问日志/指标采集中间件。"""

    @app.middleware("http")
    async def observability_middleware(request: Request, call_next):
        settings = request.app.state.settings
        header_name = settings.request_id_header
        inbound = _safe_header(request, header_name)
        request_id, reused = resolve_request_id(
            inbound, settings.request_id_max_length)

        # 绑定到当前请求的 contextvar 副本；并发任务与线程池各自隔离。
        # token 无需回滚：每个请求的上下文副本随任务结束而丢弃。
        set_request_id(request_id)
        setattr(request.state, REQUEST_ID_STATE_KEY, request_id)

        if inbound is not None and not reused:
            # 非法/超长不拒绝请求、不丢日志，也不回显原始脏值。
            logger.warning(
                "inbound request id rejected; generated a new one",
                extra={"header": header_name, "inbound_length": len(inbound)},
            )

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Starlette 的 ServerErrorMiddleware 位于本中间件之外：异常若继续
            # 上抛，它生成的 500 不经过这里，会丢指标/访问日志/请求头。
            # 因此对真正未处理的异常就地归类为 unexpected_error。
            duration = time.perf_counter() - start
            setattr(request.state, OUTCOME_STATE_KEY, "unexpected_error")
            logger.exception(
                "unhandled exception (middleware fallback)",
                extra={"outcome": "unexpected_error", "method": request.method,
                       "path": request.url.path},
            )
            _emit_metrics(request, 500, "unexpected_error", duration, reused)
            return _error_response(
                request, 500, "unexpected_error", "internal server error")

        duration = time.perf_counter() - start
        outcome = getattr(request.state, OUTCOME_STATE_KEY, None)
        if outcome is None:
            outcome = _classify_status(response.status_code)
        response.headers[header_name] = request_id
        _emit_metrics(request, response.status_code, outcome, duration, reused)
        return response
