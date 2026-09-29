"""应用入口：组装中间件、路由与健康检查。"""
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import Settings, get_settings
from app.exceptions import ExceptionClassificationMiddleware, register_exception_handlers
from app.health import liveness, readiness
from app.logging_config import configure_logging
from app.metrics import MetricsMiddleware, MetricsRegistry
from app.request_id import RequestIDMiddleware


def create_app(settings: Settings) -> FastAPI:
    """按配置构建应用（测试可用自定义配置构建独立实例）。"""
    app = FastAPI(title=settings.service_name)

    registry = MetricsRegistry(
        buckets_ms=settings.metrics_bucket_ms,
        max_path_labels=settings.metrics_max_path_labels,
    )
    # 先注册的内层、后注册的外层：RequestID 在最外层，保证所有日志都有请求标识
    app.add_middleware(ExceptionClassificationMiddleware)
    app.add_middleware(MetricsMiddleware, registry=registry)
    app.add_middleware(
        RequestIDMiddleware,
        header_name=settings.request_id_header,
        max_length=settings.request_id_max_length,
    )
    register_exception_handlers(app)

    @app.get("/")
    def read_root():
        return {"Hello": "World"}

    @app.get("/items/{item_id}")
    def read_item(item_id: int):
        """带路径参数的示例路由，用于验证指标路径归并。"""
        return {"item_id": item_id}

    @app.get("/boom")
    def boom():
        """故意抛出未捕获异常的示例路由，用于验证异常归类。"""
        raise RuntimeError("demo failure")

    @app.get("/healthz")
    def healthz():
        return liveness()

    @app.get("/readyz")
    def readyz():
        status, body = readiness(
            timeout_seconds=settings.dependency_timeout_seconds,
            dependency_ok=settings.dependency_ok,
        )
        return JSONResponse(status_code=status, content=body)

    @app.get("/metrics")
    def metrics():
        return PlainTextResponse(registry.render())

    return app


settings = get_settings()
configure_logging(settings.service_name)
app = create_app(settings)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
