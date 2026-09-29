"""指标：路径归并、标签基数上限、并发计数精确性与文本格式。"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from app.config import Settings
from app.metrics import OVERFLOW_PATH, UNMATCHED_PATH, MetricsRegistry
from main import create_app
from fastapi.testclient import TestClient

logger = logging.getLogger("tests.metrics")


def _counter_value(text: str, path: str, status: str) -> int:
    pattern = rf'^http_requests_total\{{method="GET",path="{re.escape(path)}",status="{status}"\}} (\d+)$'
    m = re.search(pattern, text, re.MULTILINE)
    return int(m.group(1)) if m else 0


def test_path_params_normalized(client):
    for i in range(5):
        client.get(f"/items/{i}")
    text = client.get("/metrics").text
    logger.info(
        "输入: GET /items/0..4 | 判定依据: 路径参数归并为模板 /items/{{item_id}}, 不出现具体 id"
    )
    assert _counter_value(text, "/items/{item_id}", "200") == 5
    assert "/items/1" not in text and "/items/2" not in text
    logger.info("判定通过: /items/{item_id} 计数=5, 无高基数标签")


def test_unmatched_paths_merged(client):
    for i in range(7):
        client.get(f"/no-such-route-{i}")
    text = client.get("/metrics").text
    logger.info(
        "输入: 7 个不同的未知路径 | 判定依据: 全部归并到 %s, 标签数不随路径数增长",
        UNMATCHED_PATH,
    )
    assert _counter_value(text, UNMATCHED_PATH, "404") == 7
    assert "no-such-route" not in text
    logger.info("判定通过: %s 计数=7", UNMATCHED_PATH)


def test_path_label_overflow_cap():
    registry = MetricsRegistry(buckets_ms=(5, 10), max_path_labels=2)
    paths = ["/a", "/b", "/c", "/d"]
    normalized = [registry.normalize_path(p, p) for p in paths]
    logger.info(
        "输入: max_path_labels=2, 4 个不同路由模板 | 判定依据: 前 2 个保留, 其余归并 %s => %s",
        OVERFLOW_PATH, normalized,
    )
    assert normalized == ["/a", "/b", OVERFLOW_PATH, OVERFLOW_PATH]
    # 已归并的模板再次出现时保持一致
    assert registry.normalize_path("/c", "/c") == OVERFLOW_PATH
    logger.info("判定通过: 标签规模上界 = max_path_labels + 2")


def test_concurrent_counting_exact(settings):
    """并发请求计数不丢不重。"""
    app = create_app(settings)
    client = TestClient(app)
    total = 100
    logger.info("输入: %d 个并发 GET / | 判定依据: 计数器必须精确等于 %d", total, total)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda _: client.get("/"), range(total)))

    text = client.get("/metrics").text
    count = _counter_value(text, "/", "200")
    logger.info("实际计数=%d", count)
    assert count == total


def test_metrics_text_format(client):
    client.get("/")
    client.get("/items/3")
    text = client.get("/metrics").text
    logger.info("输入: 2 个请求后抓取 /metrics | 判定依据: Prometheus 文本格式要素齐全")
    assert "# HELP http_requests_total" in text
    assert "# TYPE http_requests_total counter" in text
    assert "# TYPE http_request_duration_ms histogram" in text
    # 直方图 +Inf 桶计数必须等于 count
    m_count = re.search(r"^http_request_duration_ms_count\{[^}]*\} (\d+)$", text, re.MULTILINE)
    m_inf = re.search(r'le="\+Inf"\} (\d+)$', text, re.MULTILINE)
    assert m_count and m_inf and m_count.group(1) == m_inf.group(1)
    logger.info("判定通过: count=%s 与 +Inf 桶一致", m_count.group(1))
