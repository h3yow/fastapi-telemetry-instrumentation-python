"""下游依赖的 FastAPI 依赖注入封装。

业务端点可用 ``Depends(get_downstream)`` 拿到同一个下游客户端单例，
测试时可通过 ``app.dependency_overrides`` 替换为 fake。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.health import DownstreamClient, downstream


def get_downstream() -> DownstreamClient:
    """返回进程级下游客户端单例。"""
    return downstream


#: 端点参数可直接标注：``client: DownstreamDep``。
DownstreamDep = Annotated[DownstreamClient, Depends(get_downstream)]
