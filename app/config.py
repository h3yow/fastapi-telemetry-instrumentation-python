"""服务配置项（从环境变量读取，均有默认值）。

配置项一览（见 README「配置项」一节）：

* ``SERVICE_NAME``            服务名，用于日志/指标中的 ``service`` 标签
* ``REQUEST_ID_HEADER``       请求标识所在的请求/响应头名称
* ``REQUEST_ID_MAX_LENGTH``   沿用上游请求标识时允许的最大长度（字节）
* ``METRICS_PATH``            指标暴露端点路径
* ``HEALTH_LIVENESS_PATH``    存活探针路径
* ``HEALTH_READINESS_PATH``   就绪探针路径
* ``DEPENDENCY_PROBE_TIMEOUT`` 就绪探针等待下游依赖的最长时间（秒）
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """不可变运行配置。"""

    service_name: str
    request_id_header: str
    request_id_max_length: int
    metrics_path: str
    health_liveness_path: str
    health_readiness_path: str
    dependency_probe_timeout: float


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def load_settings() -> Settings:
    """从环境变量读取配置；缺省或非法值回落到内置默认。"""
    return Settings(
        service_name=os.environ.get("SERVICE_NAME", "fastapi-telemetry-demo"),
        request_id_header=os.environ.get("REQUEST_ID_HEADER", "X-Request-ID"),
        request_id_max_length=_env_int("REQUEST_ID_MAX_LENGTH", 128),
        metrics_path=os.environ.get("METRICS_PATH", "/metrics"),
        health_liveness_path=os.environ.get("HEALTH_LIVENESS_PATH", "/health/live"),
        health_readiness_path=os.environ.get("HEALTH_READINESS_PATH", "/health/ready"),
        dependency_probe_timeout=_env_float("DEPENDENCY_PROBE_TIMEOUT", 0.5),
    )
