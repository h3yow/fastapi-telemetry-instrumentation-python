"""测试公共配置：测试日志输出触发输入与判定依据，便于失败时定位问题类别。"""
import logging
import sys

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from main import create_app


@pytest.fixture(scope="session", autouse=True)
def test_logging():
    """测试日志输出到 stdout：每条用例打印触发输入与判定依据。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[TEST] %(name)s | %(message)s"))
    logger = logging.getLogger("tests")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


@pytest.fixture()
def settings():
    return Settings()


@pytest.fixture()
def client(settings):
    """每个用例独立应用实例，指标互不影响。"""
    return TestClient(create_app(settings), raise_server_exceptions=False)
