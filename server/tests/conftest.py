from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from aqserver.config import Settings  # noqa: E402
from aqserver.main import create_app  # noqa: E402

AGENT_TOKEN = "test-agent-token-123456"
USER, PASSWORD = "tester", "pw-for-tests"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    # Set TEST_DATABASE_URL=postgresql+psycopg://... to run the suite against PostgreSQL.
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    if not url.startswith("sqlite"):
        from aqserver import models  # noqa: F401
        from aqserver.db import Base, make_engine

        engine = make_engine(url)
        Base.metadata.drop_all(engine)
        engine.dispose()
    return Settings(
        data_dir=tmp_path,
        database_url=url,
        agent_token=AGENT_TOKEN,
        dashboard_users={USER: PASSWORD},
    )


@pytest.fixture()
def app(settings):
    return create_app(settings)


@pytest.fixture()
def client(app):
    basic = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    with TestClient(app, headers={"Authorization": f"Basic {basic}"}) as c:
        yield c


@pytest.fixture()
def agent_client(app):
    with TestClient(app, headers={"Authorization": f"Bearer {AGENT_TOKEN}"}) as c:
        yield c
