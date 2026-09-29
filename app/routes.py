"""业务路由。

对外契约：``GET /`` 仍返回 ``{"Hello": "World"}``，行为不变。
其余端点仅用于演示/验证可观测性能力：

* ``GET /whoami``        回显当前请求标识（异步）
* ``GET /whoami/sync``   同上，但走线程池，验证线程上下文复制
* ``GET /items/{item_id}``   正常 / 触发业务异常 / 触发依赖异常 / 触发未预期异常
"""

from __future__ import annotations

from fastapi import FastAPI

from app.errors import BusinessError, DependencyError
from app.health import MODE_DOWN, DownstreamClient
from app.logging_setup import get_logger
from app.request_context import get_request_id

logger = get_logger(__name__)


async def read_root() -> dict[str, str]:
    return {"Hello": "World"}


async def whoami() -> dict[str, str]:
    return {"request_id": get_request_id()}


def whoami_sync() -> dict[str, str]:
    # Starlette 对 sync 端点使用线程池（anyio 会复制 contextvars 上下文）。
    return {"request_id": get_request_id()}


async def get_item(item_id: str) -> dict[str, str]:
    if item_id == "missing":
        raise BusinessError(f"item {item_id!r} not found", status_code=404)
    if item_id == "boom":
        # 模拟调用下游时对端不可用：使用局部 client，不污染就绪探针所用的
        # 全局单例。依赖异常与业务异常必须可区分。
        probe = DownstreamClient()
        probe.set_mode(MODE_DOWN)
        check = await probe.check()
        if not check.healthy:
            raise DependencyError(
                "downstream dependency is unavailable",
                internal_detail=check.detail or check.reason,
            )
    if item_id == "crash":
        # 模拟一个未预期错误，验证 unexpected_error 归类与兜底。
        raise RuntimeError("unexpected failure in item lookup")
    return {"item_id": item_id, "request_id": get_request_id()}


def register_routes(app: FastAPI) -> None:
    app.add_api_route("/", read_root, methods=["GET"], include_in_schema=False)
    app.add_api_route("/whoami", whoami, methods=["GET"])
    app.add_api_route("/whoami/sync", whoami_sync, methods=["GET"])
    app.add_api_route("/items/{item_id}", get_item, methods=["GET"])
