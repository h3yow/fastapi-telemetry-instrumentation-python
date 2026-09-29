"""应用工厂：组装配置、日志、中间件、异常处理器、路由与探针端点。"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app import config, health, logging_setup, metrics, middleware
from app.middleware import OUTCOME_STATE_KEY
from app.routes import register_routes


def create_app(settings: config.Settings | None = None) -> FastAPI:
    """构建并返回 ASGI 应用。"""
    settings = settings or config.load_settings()
    logging_setup.configure_logging()

    app = FastAPI(title=settings.service_name, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.settings = settings

    # 中间件与异常处理器。
    middleware.install_exception_handlers(app)
    middleware.install_middleware(app)

    # 业务路由（保持 GET / 行为不变）。
    register_routes(app)

    @app.get(settings.metrics_path, include_in_schema=False)
    async def prometheus_metrics() -> Response:
        body = metrics.registry.render_prometheus(settings.service_name)
        return Response(content=body, media_type="text/plain; version=0.0.4")

    @app.get(settings.health_liveness_path, include_in_schema=False)
    async def live() -> dict:
        return await health.liveness()

    @app.get(settings.health_readiness_path, include_in_schema=False)
    async def ready(request: Request) -> Response:
        body, status_code = await health.readiness(
            timeout=settings.dependency_probe_timeout)
        if status_code >= 500:
            # 依赖降级导致的不就绪是“预期内的失败结论”，与服务自身 5xx 区分，
            # 保证健康状态、日志 outcome 与指标三者说法一致。
            setattr(request.state, OUTCOME_STATE_KEY, "dependency_error")
        return JSONResponse(content=body, status_code=status_code)

    return app
