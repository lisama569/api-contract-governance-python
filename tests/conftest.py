import logging

import pytest
from fastapi.testclient import TestClient

from app.api import create_app


@pytest.fixture
def client():
    # raise_server_exceptions=False：让 500 走统一错误契约而不是在测试进程抛出
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _caplog_level(caplog):
    caplog.set_level(logging.DEBUG, logger="contract")
