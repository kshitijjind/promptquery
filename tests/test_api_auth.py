"""API auth: enabled by ATLAS_API_TOKEN, off when unset."""
from fastapi.testclient import TestClient
from promptquery.reporting.config import Settings
from promptquery.reporting.engine import ReportingEngine
from promptquery.reporting.server import create_app
from tests.test_atlas_integration import SETTINGS
from tests.test_reporting_engine import ScriptedLLM

def _app(token, tmp):
    s = Settings(**{**SETTINGS.__dict__, "catalog_cache": tmp/"c.json", "api_token": token})
    llm = ScriptedLLM([])
    return TestClient(create_app(s, ReportingEngine(s, llm=llm, fast_llm=llm)))

def test_auth_enforced(tmp_path):
    c = _app("s3cret", tmp_path)
    assert c.get("/health").status_code == 200                      # health stays open
    assert c.get("/health").json()["auth_required"] is True
    assert c.get("/tenants").status_code == 401                     # no token
    assert c.get("/tenants", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/tenants", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/tenants?token=s3cret").status_code == 200        # browser form
    assert c.get("/").status_code == 401
    assert c.post("/chat", json={"message": "hi"}).status_code == 401

def test_no_token_means_open(tmp_path):
    c = _app(None, tmp_path)
    assert c.get("/health").json()["auth_required"] is False
    assert c.get("/tenants").status_code == 200
