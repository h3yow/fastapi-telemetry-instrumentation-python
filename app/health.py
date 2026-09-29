"""存活/就绪探针与一个可注入故障的下游依赖客户端。

语义（见 README）：

* **liveness（存活）**：只判断本进程事件循环是否仍能响应，恒为 200。
  依赖故障不影响存活，避免依赖抖动触发整个进程被重启。
* **readiness（就绪）**：检查下游依赖。依赖异常、超时等不同失败原因
  通过独立的 ``reason`` 区分；探针内部吞掉一切异常并加超时保护，
  依赖出错既不会拖垮进程，也不会把堆栈/内部细节写进 HTTP 响应。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.logging_setup import get_logger

logger = get_logger(__name__)

#: 依赖的可切换模式：正常 / 不可用 / 超时 / 内部错误。
MODE_UP = "up"
MODE_DOWN = "down"
MODE_TIMEOUT = "timeout"
MODE_ERROR = "error"
_VALID_MODES = {MODE_UP, MODE_DOWN, MODE_TIMEOUT, MODE_ERROR}


@dataclass(frozen=True)
class ProbeResult:
    """一次依赖探针的结论。``detail`` 只进日志，不进对外响应。"""

    name: str
    healthy: bool
    reason: str
    detail: str = ""


class DownstreamClient:
    """模拟下游依赖；测试通过 set_mode 注入各类故障。"""

    def __init__(self) -> None:
        self._mode: str = MODE_UP

    def set_mode(self, mode: str) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"unknown downstream mode: {mode!r}")
        self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode

    async def check(self) -> ProbeResult:
        """执行一次依赖检查；每种模式给出确定、可区分的结论。"""
        mode = self._mode
        if mode == MODE_UP:
            return ProbeResult("downstream", True, "up")
        if mode == MODE_DOWN:
            return ProbeResult(
                "downstream", False, "dependency_unavailable",
                detail="downstream reports unavailable (mode=down)",
            )
        if mode == MODE_TIMEOUT:
            # 真实超时由 readiness 的 asyncio.wait_for 兜底；
            # 这里挂起，模拟依赖长时间不响应。
            await asyncio.Event().wait()
        # MODE_ERROR：依赖内部直接抛错，探针必须兜住。
        raise RuntimeError("simulated downstream internal failure (mode=error)")


#: 进程级单例；测试通过 set_mode 改状态。
downstream = DownstreamClient()


async def liveness() -> dict:
    """存活探针：能执行到这里即说明事件循环存活，恒返回 ok。"""
    return {"status": "ok", "checks": {"self": "alive"}}


async def readiness(
    client: DownstreamClient | None = None,
    timeout: float = 0.5,
) -> tuple[dict, int]:
    """就绪探针。

    返回 ``(body, http_status)``：任一检查失败即为 503。
    所有异常/超时都被归因为固定 reason，详情只写结构化日志。
    """
    client = client if client is not None else downstream
    try:
        result = await asyncio.wait_for(client.check(), timeout=timeout)
    except TimeoutError:
        logger.warning(
            "readiness dependency check timed out",
            extra={"dependency": "downstream", "reason": "dependency_timeout",
                   "timeout_s": timeout},
        )
        return (
            {"status": "unavailable",
             "checks": {"downstream": "dependency_timeout"}},
            503,
        )
    except Exception:
        logger.exception(
            "readiness dependency check raised",
            extra={"dependency": "downstream", "reason": "dependency_error"},
        )
        return (
            {"status": "unavailable",
             "checks": {"downstream": "dependency_error"}},
            503,
        )

    if result.healthy:
        logger.info("readiness ok", extra={"dependency": result.name})
        return {"status": "ok", "checks": {result.name: "up"}}, 200

    logger.warning(
        "readiness dependency unhealthy",
        extra={"dependency": result.name, "reason": result.reason,
               "detail": result.detail},
    )
    return (
        {"status": "unavailable", "checks": {result.name: result.reason}},
        503,
    )
