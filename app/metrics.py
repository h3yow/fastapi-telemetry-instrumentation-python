"""运行时指标：请求计数与耗时直方图，Prometheus 文本格式暴露。

指标口径：

* ``http_requests_total{service,method,route,status,outcome}``
  计数器，每个完成的请求只累加一次。
* ``http_request_duration_seconds_*`` 直方图，桶边界见 :data:`DEFAULT_BUCKETS`，
  含 ``_sum`` / ``_count`` / ``_bucket{le}``。

标签归并（防止标签基数无限增长）：

* ``route``：命中已注册路由时使用路由模板（如 ``/items/{item_id}``），
  路径参数天然归并；未命中任何路由的请求统一记为 ``__unmatched__``。
* ``status``：状态码本身有限（100~599），但仍归类为
  ``200``/``204`` 这类具体值与异常归类标签共用 ``outcome``，
  无法识别的状态码统一 ``status="other"``。
* 当不同 (method, route) 组合数超过 :data:`CARDINALITY_LIMIT` 时，
  新组合整体并入 ``route="__other__"``，指标标签集合从此有上界。

并发：所有读写在同一把锁内完成，``observe`` 中计数器与直方图桶
各自严格 +1，既不会丢也不会重复累计。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

CARDINALITY_LIMIT = 200
FALLBACK_ROUTE = "__other__"
UNMATCHED_ROUTE = "__unmatched__"

DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
)


@dataclass
class _Histogram:
    buckets: tuple[float, ...] = DEFAULT_BUCKETS
    bucket_counts: list[int] = field(default_factory=lambda: [0] * (len(DEFAULT_BUCKETS) + 1))
    total_count: int = 0
    total_sum: float = 0.0

    def observe(self, value: float) -> None:
        self.total_count += 1
        self.total_sum += value
        placed = False
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self.bucket_counts[i] += 1
                placed = True
                break
        if not placed:
            self.bucket_counts[-1] += 1


class MetricsRegistry:
    """线程安全的内存指标注册表。"""

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self._lock = threading.Lock()
        self._buckets = buckets
        # (method, route, status, outcome) -> count
        self._counts: dict[tuple[str, str, str, str], int] = {}
        # (method, route) -> _Histogram
        self._histograms: dict[tuple[str, str], _Histogram] = {}
        self._series: set[tuple[str, str]] = set()

    def observe(self, *, method: str, route: str, status_code: int,
                outcome: str, duration: float) -> None:
        """记录一个完成请求：计数器 +1、直方图记一次耗时（同一把锁）。"""
        status = normalize_status(status_code)
        with self._lock:
            series_key = (method, route)
            if series_key not in self._series:
                if len(self._series) >= CARDINALITY_LIMIT:
                    # 标签数触顶：新组合整体并入兜底桶，阻止基数继续增长。
                    route = FALLBACK_ROUTE
                    series_key = (method, route)
                self._series.add(series_key)
            count_key = (method, route, status, outcome)
            self._counts[count_key] = self._counts.get(count_key, 0) + 1
            hist = self._histograms.get(series_key)
            if hist is None:
                hist = _Histogram(buckets=self._buckets)
                hist.bucket_counts = [0] * (len(self._buckets) + 1)
                self._histograms[series_key] = hist
            hist.observe(max(duration, 0.0))

    def render_prometheus(self, service: str) -> str:
        """渲染 Prometheus 文本 exposition 格式（0.0.4 风格）。"""
        lines: list[str] = []
        with self._lock:
            lines.append("# HELP http_requests_total Total completed HTTP requests.")
            lines.append("# TYPE http_requests_total counter")
            for (method, route, status, outcome), value in sorted(self._counts.items()):
                labels = (
                    f'service="{service}",method="{method}",route="{route}",'
                    f'status="{status}",outcome="{outcome}"'
                )
                lines.append(f'http_requests_total{{{labels}}} {value}')

            lines.append("# HELP http_request_duration_seconds Request latency in seconds.")
            lines.append("# TYPE http_request_duration_seconds histogram")
            for (method, route), hist in sorted(self._histograms.items()):
                base = f'service="{service}",method="{method}",route="{route}"'
                cumulative = 0
                for i, bound in enumerate(hist.buckets):
                    cumulative += hist.bucket_counts[i]
                    lines.append(
                        f'http_request_duration_seconds_bucket{{{base},le="{bound}"}} '
                        f"{cumulative}"
                    )
                cumulative += hist.bucket_counts[-1]
                lines.append(
                    f'http_request_duration_seconds_bucket{{{base},le="+Inf"}} {cumulative}'
                )
                lines.append(
                    f"http_request_duration_seconds_sum{{{base}}} {hist.total_sum:.6f}"
                )
                lines.append(
                    f"http_request_duration_seconds_count{{{base}}} {hist.total_count}"
                )
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """清空指标（主要给测试用）。"""
        with self._lock:
            self._counts.clear()
            self._histograms.clear()
            self._series.clear()


#: 进程级单例；ASGI 单进程内共享，测试通过 reset() 复位。
registry = MetricsRegistry()


def normalize_route(path: str, route_pattern: str | None) -> str:
    """决定 route 标签：命中路由用模板，否则归为未匹配路径。"""
    if route_pattern is None:
        return UNMATCHED_ROUTE
    return route_pattern or path or UNMATCHED_ROUTE


def normalize_status(status_code: int) -> str:
    """状态码标签：已知 HTTP 状态码原样输出字符串，其余归 ``other``。"""
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return str(status_code)
    return "other"
