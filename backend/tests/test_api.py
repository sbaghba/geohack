"""Contract tests: every endpoint returns the agreed shape.   cd backend && pytest -q"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.contract import Report, SuggestResponse  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
SITE = {"lat": 35.655, "lon": -78.462}


import pytest  # noqa: E402

import app.services.analyze as _analyze  # noqa: E402
import app.services.layers as _layers  # noqa: E402


@pytest.fixture(autouse=True)
def _force_mock(monkeypatch):
    """These tests check the contract shapes on sample data, even after a real grid is built."""
    monkeypatch.setattr(_analyze, "real_mode", lambda: False)
    monkeypatch.setattr(_layers, "real_mode", lambda: False)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_analyze_shape_and_inputs_matter():
    small = Report.model_validate(client.post("/api/analyze", json={**SITE, "mw": 50}).json())
    big = Report.model_validate(client.post("/api/analyze", json={**SITE, "mw": 300, "cooling": "liquid"}).json())
    assert big.energy.annual_mwh > small.energy.annual_mwh
    assert big.water.wue_l_per_kwh < small.water.wue_l_per_kwh
    assert big.site.lat == SITE["lat"]


def test_errors_use_contract_shape():
    r = client.post("/api/analyze", json={"lat": 48.85, "lon": 2.35})
    assert r.status_code == 422 and r.json()["error"] == "out_of_coverage"
    r = client.post("/api/analyze", json={**SITE, "cooling": "ice"})
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    r = client.get("/api/layers/nope")
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


def test_suggest():
    s = SuggestResponse.model_validate(client.post("/api/suggest", json={**SITE, "n": 2}).json())
    assert len(s.candidates) == 2


def test_layers():
    names = [l["name"] for l in client.get("/api/layers").json()["layers"]]
    assert "pressure" in names
    fc = client.get("/api/layers/burden").json()
    assert fc["type"] == "FeatureCollection" and fc["features"][0]["properties"]["hex_id"]


def _events(body: dict) -> list[tuple[str, dict]]:
    out = []
    with client.stream("POST", "/api/chat", json=body) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        text = "".join(r.iter_text())
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_chat_move_emits_report():
    ev = _events({"messages": [{"role": "user", "content": "move it east"}], "config": {**SITE, "mw": 100}})
    names = [e for e, _ in ev]
    assert names[0] == "tool_call" and names[1] == "report" and names[-1] == "done"
    Report.model_validate(ev[1][1])


def test_chat_better_emits_suggestions():
    ev = _events({"messages": [{"role": "user", "content": "where would be better?"}]})
    assert "suggestions" in [e for e, _ in ev]
