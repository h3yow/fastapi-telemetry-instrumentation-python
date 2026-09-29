"""指标：分组计数、路径参数模板化、高基数归并、Prometheus 文本、并发精确一次。

通过直接解析 ``/metrics`` 文本断言，失败时输出触发输入与实际指标行，
可直接分辨是“分组错 / 归并失效 / 基数失控 / 计数丢失”中的哪一类。
"""

from __future__ import annotations

import concurrent.futures
import re
from typing import Any

from fastapi.testclient import TestClient

_LINE_RE = re.compile(r'^http_requests_total\{(.*)\} (\d+)$', re.MULTILINE)


def _parse_labels(raw: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)="([^"]*)"', raw))


def _counter_series(text: str) -> list[tuple[dict[str, str], int]]:
    # 标签值自身可能含 }（路由模板 /items/{item_id}），因此对最后一个 "} <n>"
    # 切分，而不能用 [^}]* 提前截断。
    return [
        (_parse_labels(labels), int(value))
        for labels, value in _LINE_RE.findall(text)
    ]


def _total_for(text: str, **want: Any) -> int:
    return sum(
        value for parsed, value in _counter_series(text)
        if all(parsed.get(k) == str(v) for k, v in want.items())
    )


def test_counter_groups_by_route_method_status(client: TestClient) -> None:
    print("[input] GET / 两次；GET /whoami 一次")
    client.get("/"); client.get("/"); client.get("/whoami")
    text = client.get("/metrics").text
    root = _total_for(text, route="/", method="GET",
                      status="200", outcome="success")
    who = _total_for(text, route="/whoami", method="GET", status="200")
    print(f"[judge] / 计数={root}（期望2），/whoami 计数={who}（期望1）")
    assert root == 2 and who == 1
    # 指标端点本身也被统计：第一次抓取在下一次快照中可见，且带配置的服务名。
    client.get("/metrics")
    text2 = client.get("/metrics").text
    assert _total_for(text2, route="/metrics", service="test-service") >= 1


def test_path_params_are_templated(client: TestClient) -> None:
    print("[input] GET /items/1 /items/2 /items/999")
    for item in ("1", "2", "999"):
        r = client.get(f"/items/{item}")
        assert r.status_code == 200
    text = client.get("/metrics").text
    templated = _total_for(text, route="/items/{item_id}")
    literal = sum(1 for labels in [p for p, _ in _counter_series(text)]
                  if labels.get("route", "").startswith("/items/"))
    print(f"[judge] 模板路由计数={templated}（期望3）；含字面ID的序列数="
          f"{literal - (1 if templated else 0)}")
    assert templated == 3, "路径参数必须归并到路由模板"
    routes = {labels.get("route") for labels in [p for p, _ in _counter_series(text)]}
    assert not {"/items/1", "/items/2", "/items/999"} & routes, "不得出现字面参数标签"




def test_high_cardinality_unknown_paths_are_merged(client: TestClient) -> None:
    # 未注册路径各自不同；必须全部并入 __unmatched__。
    print("[input] GET /random/<1..500>（均未注册）")
    for i in range(500):
        client.get(f"/random/{i}")
    text = client.get("/metrics").text
    unmatched = _total_for(text, route="__unmatched__")
    print(f"[judge] __unmatched__ 计数={unmatched}（期望>=500）")
    assert unmatched == 500, "高基数未知路径必须归并为单一标签"
    random_routes = {l["route"] for l in [p for p, _ in _counter_series(text)]
                     if l["route"].startswith("/random/")}
    assert random_routes == set(), f"出现未归并路径: {sorted(random_routes)[:3]}"


def test_label_cardinality_is_bounded(client: TestClient) -> None:
    # 直接压注册表：超过 CARDINALITY_LIMIT 的新 (method,route) 并入 __other__。
    from app.metrics import CARDINALITY_LIMIT, registry
    print(f"[input] 向注册表写入 {CARDINALITY_LIMIT + 100} 个不同 route")
    for i in range(CARDINALITY_LIMIT + 100):
        registry.observe(method="GET", route=f"/synthetic/{i}",
                         status_code=200, outcome="success", duration=0.001)
    text = registry.render_prometheus("test-service")
    routes = {l["route"] for l in [p for p, _ in _counter_series(text)]}
    print(f"[judge] 不同 route 标签数={len(routes)}（有上界 "
          f"{CARDINALITY_LIMIT}+常量）；__other__ 存在={'__other__' in routes}")
    assert "__other__" in routes
    assert len(routes) <= CARDINALITY_LIMIT + 3


def test_prometheus_text_format(client: TestClient) -> None:
    client.get("/")
    text = client.get("/metrics").text
    print("[judge] 指标文本前几行：")
    print("\n".join(text.splitlines()[:6]))
    assert "# TYPE http_requests_total counter" in text
    assert "# TYPE http_request_duration_seconds histogram" in text
    assert 'http_request_duration_seconds_bucket{' in text
    assert 'le="+Inf"' in text
    assert re.search(r'http_request_duration_seconds_count\{[^}]*\} \d+', text)
    # Content-Type 是 Prometheus 可直接抓取的类型。
    r = client.get("/metrics")
    assert r.headers["content-type"].startswith("text/plain")


def test_concurrent_observations_are_counted_exactly_once(client: TestClient) -> None:
    n_per_route = 300
    print(f"[input] 2 个路由 x {n_per_route} 次并发请求，多线程打点")

    def hit(route: str) -> None:
        client.get(route)

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        list(pool.map(hit, ["/"] * n_per_route))
        list(pool.map(hit, ["/whoami"] * n_per_route))

    text = client.get("/metrics").text
    root = _total_for(text, route="/", status="200")
    who = _total_for(text, route="/whoami", status="200")
    print(f"[judge] / 计数={root}（期望{n_per_route}），"
          f"/whoami 计数={who}（期望{n_per_route}）")
    assert root == n_per_route, "并发计数丢失或重复（路由 /）"
    assert who == n_per_route, "并发计数丢失或重复（路由 /whoami）"
