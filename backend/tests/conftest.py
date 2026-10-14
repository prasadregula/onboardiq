import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import reset_engine_for_tests
from app.providers.screening import reset_screening_provider
from app.providers.storage import reset_storage
from app.rag.store import reset_store

GOLDEN = Path(__file__).parent.parent / "eval" / "golden"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    s = get_settings()
    s.orchestrator_mode = "scripted"
    s.run_async = False
    s.auth_mode = "apikey"
    s.storage_provider = "local"
    s.blob_dir = tmp_path / "blobs"
    s.rag_index_path = tmp_path / "policy_index.json"
    reset_store()
    reset_storage()
    reset_screening_provider()
    reset_engine_for_tests(f"sqlite:///{tmp_path / 'test.db'}")
    yield


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        c.headers.update({"X-API-Key": get_settings().api_key, "X-Analyst": "analyst1"})
        yield c


def golden(name: str) -> dict:
    path = next(GOLDEN.glob(f"{name}*.json"))
    return json.loads(path.read_text())["application"]
