"""请求指标：按路由/方法/状态码统计请求量与耗时分布，Prometheus 文本格式导出。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# 归并用的保留标签
UNMATCHED_PATH = "__unmatched__"  # 未匹配任何路由的路径（如 404）
OVERFLOW_PATH = "__overflow__"    # 路由模板数量超过上限时的归并桶


@dataclass(frozen=True)
class Labels:
    method: str
    path: str
    status: str


class _Histogram:
    """固定边界的直方图（累计桶计数）。"""

    __slots__ = ("buckets", "counts", "count", "total")

    def __init__(self, buckets: tuple[float, ...]) -> None:
        self.buckets = buckets
        self.counts = [0] * len(buckets)
        self.count = 0
        self.total = 0.0

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[i] += 1


class MetricsRegistry:
    """线程安全的计数器与直方图注册表。

    标签基数控制：路径标签只取路由模板（如 /items/{item_id}），
    未匹配路由的路径归并为 __unmatched__，路由模板总数超过上限后归并为 __overflow__，
    因此 path 标签规模以 max_path_labels + 2 为上界。
    """

    def __init__(self, buckets_ms: tuple[float, ...], max_path_labels: int) -> None:
        self.buckets_ms = tuple(sorted(buckets_ms))
        self.max_path_labels = max_path_labels
        self._lock = threading.Lock()
        self._counters: dict[Labels, int] = {}
        self._histograms: dict[Labels, _Histogram] = {}
        self._known_paths: set[str] = set()

    def normalize_path(self, raw_path: str, route_path: str | None) -> str:
        """归并路径标签：路由模板优先，未知路径与超限模板分别归并。"""
        if route_path is None:
            return UNMATCHED_PATH
        with self._lock:
            if route_path in self._known_paths:
                return route_path
            if len(self._known_paths) >= self.max_path_labels:
                return OVERFLOW_PATH
            self._known_paths.add(route_path)
            return route_path

    def record(self, method: str, path: str, status: int, duration_ms: float) -> None:
        """记录一次请求（计数 +1，耗时落入直方图）。"""
        labels = Labels(method=method.upper(), path=path, status=str(status))
        with self._lock:
            self._counters[labels] = self._counters.get(labels, 0) + 1
            hist = self._histograms.get(labels)
            if hist is None:
                hist = _Histogram(self.buckets_ms)
                self._histograms[labels] = hist
            hist.observe(duration_ms)

    def render(self) -> str:
        """导出 Prometheus 文本 exposition 格式。"""
        lines = [
            "# HELP http_requests_total Total HTTP requests.",
            "# TYPE http_requests_total counter",
        ]
        with self._lock:
            key = lambda kv: (kv[0].method, kv[0].path, kv[0].status)
            counters = sorted(self._counters.items(), key=key)
            histograms = sorted(self._histograms.items(), key=key)
        for labels, value in counters:
            lines.append(f"http_requests_total{_fmt_labels(labels)} {value}")
        lines += [
            "# HELP http_request_duration_ms HTTP request duration in milliseconds.",
            "# TYPE http_request_duration_ms histogram",
        ]
        for labels, hist in histograms:
            base = _fmt_labels(labels)
            for bound, count in zip(hist.buckets, hist.counts):
                lines.append(
                    f'http_request_duration_ms_bucket{base[:-1]},le="{_fmt_float(bound)}"}} {count}'
                )
            lines.append(f'http_request_duration_ms_bucket{base[:-1]},le="+Inf"}} {hist.count}')
            lines.append(f"http_request_duration_ms_sum{base} {hist.total:.6f}")
            lines.append(f"http_request_duration_ms_count{base} {hist.count}")
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_labels(labels: Labels) -> str:
    return (
        f'{{method="{_escape(labels.method)}",'
        f'path="{_escape(labels.path)}",'
        f'status="{_escape(labels.status)}"}}'
    )


def _fmt_float(value: float) -> str:
    return f"{value:g}"


class MetricsMiddleware:
    """纯 ASGI 中间件：统计每个请求的量与耗时。

    路由模板在下游路由匹配后从 scope["route"] 读取；
    下游抛出的异常记为 500 后继续上抛，保证计数不丢。
    """

    def __init__(self, app, registry: MetricsRegistry) -> None:
        self.app = app
        self.registry = registry

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        status = 500
        start = time.perf_counter()

        async def send_capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_capture)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            route = scope.get("route")
            route_path = getattr(route, "path", None)
            path = self.registry.normalize_path(scope.get("path", ""), route_path)
            self.registry.record(method, path, status, duration_ms)
