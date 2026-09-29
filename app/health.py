"""存活与就绪检查：存活只看本进程，就绪看依赖。"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("app.health")


@dataclass(frozen=True)
class DependencyResult:
    name: str
    ok: bool
    reason: str  # "ok" / "unavailable" / "timeout" / "error"


def check_dependency(name: str, timeout_seconds: float, dependency_ok: bool) -> DependencyResult:
    """检查单个依赖。

    本地实现用 dependency_ok 开关模拟依赖状态；
    任何内部异常都被捕获并归类为 "error"，不外抛、不泄露内部细节。
    """
    try:
        if timeout_seconds <= 0:
            return DependencyResult(name=name, ok=False, reason="timeout")
        if dependency_ok:
            return DependencyResult(name=name, ok=True, reason="ok")
        return DependencyResult(name=name, ok=False, reason="unavailable")
    except Exception:
        logger.exception("dependency check failed", extra={"dependency": name})
        return DependencyResult(name=name, ok=False, reason="error")


def liveness() -> dict:
    """存活检查：本进程能响应即存活，不依赖外部状态。"""
    return {"status": "alive"}


def readiness(timeout_seconds: float, dependency_ok: bool) -> tuple[int, dict]:
    """就绪检查：所有依赖可用才就绪，否则 503 并给出每个依赖的可区分原因。"""
    results = [check_dependency("primary-store", timeout_seconds, dependency_ok)]
    ready = all(r.ok for r in results)
    body = {
        "status": "ready" if ready else "not_ready",
        "dependencies": [
            {"name": r.name, "ok": r.ok, "reason": r.reason} for r in results
        ],
    }
    return (200 if ready else 503), body
