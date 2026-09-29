"""运行时配置项（骨架）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    """服务配置，全部可通过环境变量覆盖。"""

    service_name: str = "fastapi-telemetry"
    request_id_header: str = "X-Request-ID"
    request_id_max_length: int = 128
    metrics_max_path_labels: int = 200
    metrics_bucket_ms: tuple[float, ...] = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)
    dependency_timeout_seconds: float = 0.5
    dependency_ok: bool = True  # 本地模拟依赖开关


def get_settings() -> Settings:
    """从环境变量构建配置（前缀 APP_）。"""
    env = os.environ
    return Settings(
        service_name=env.get("APP_SERVICE_NAME", Settings.service_name),
        request_id_header=env.get("APP_REQUEST_ID_HEADER", Settings.request_id_header),
        request_id_max_length=int(
            env.get("APP_REQUEST_ID_MAX_LENGTH", Settings.request_id_max_length)
        ),
        metrics_max_path_labels=int(
            env.get("APP_METRICS_MAX_PATH_LABELS", Settings.metrics_max_path_labels)
        ),
        metrics_bucket_ms=tuple(
            float(x)
            for x in env.get(
                "APP_METRICS_BUCKET_MS",
                ",".join(str(b) for b in Settings.metrics_bucket_ms),
            ).split(",")
        ),
        dependency_timeout_seconds=float(
            env.get("APP_DEPENDENCY_TIMEOUT_SECONDS", Settings.dependency_timeout_seconds)
        ),
        dependency_ok=env.get("APP_DEPENDENCY_OK", "true").lower() in ("1", "true", "yes"),
    )
