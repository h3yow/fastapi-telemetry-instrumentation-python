"""FastAPI 可观测性示例服务包。"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    """转发到 app.factory.create_app（延迟导入避免循环依赖）。"""
    from app.factory import create_app as _create_app

    return _create_app(*args, **kwargs)
