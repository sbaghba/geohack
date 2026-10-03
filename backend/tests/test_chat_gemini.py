"""Gemini chat loop with a fake async client: tool call -> backend runs it -> report event -> streamed answer."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from google.genai import types  # noqa: E402

from app.contract import Report, SuggestResponse  # noqa: E402
from app.main import app  # noqa: E402
from app.services import chat  # noqa: E402

client = TestClient(app)


import pytest  # noqa: E402

import app.services.analyze as _analyze  # noqa: E402


@pytest.fixture(autouse=True)
def _force_mock(monkeypatch):
    monkeypatch.setattr(_analyze, "real_mode", lambda: False)


def _resp(*parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))])


class FakeModels:
    def __init__(self, script):
        self.script, self.calls = script, []

    async def generate_content_stream(self, model, contents, config):
        self.calls.append(contents)
        chunks = self.script[len(self.calls) - 1]

        async def gen():
            for c in chunks:
                yield c
        return gen()


class FakeClient:
    def __init__(self, script):
        self.aio = type("A", (), {})()
        self.aio.models = FakeModels(script)


def _events(body, monkeypatch, script):
    fake = FakeClient(script)
    monkeypatch.setattr(chat, "_llm_enabled", lambda: True)
    monkeypatch.setattr(chat, "_gemini", lambda: fake)
    with client.stream("POST", "/api/chat", json=body) as r:
        text = "".join(r.iter_text())
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out, fake


def test_move_site_round_trip(monkeypatch):
    script = [
        [_resp(types.Part(function_call=types.FunctionCall(name="move_site", args={"lat": 35.51, "lon": -78.34})))],
        [_resp(types.Part.from_text(text="Moved. ")), _resp(types.Part.from_text(text="Water use is 1.5M m3/yr."))],
    ]
    ev, fake = _events({"messages": [{"role": "user", "content": "move it to Smithfield"}],
                        "config": {"lat": 35.655, "lon": -78.462, "mw": 100}}, monkeypatch, script)
    names = [e for e, _ in ev]
    assert names == ["tool_call", "report", "token", "token", "done"], names
    rep = Report.model_validate(ev[1][1])
    assert abs(rep.site.lat - 35.51) < 1e-6
    # second Gemini call got the model turn + our function response
    second = fake.aio.models.calls[1]
    assert second[-1].parts[0].function_response.name == "move_site"
    assert second[-1].parts[0].function_response.response["ok"] is True


def test_find_better_sites_emits_suggestions(monkeypatch):
    script = [
        [_resp(types.Part(function_call=types.FunctionCall(name="find_better_sites", args={"radius_km": 40})))],
        [_resp(types.Part.from_text(text="Here are three options."))],
    ]
    ev, _ = _events({"messages": [{"role": "user", "content": "where is better?"}]}, monkeypatch, script)
    sug = [d for e, d in ev if e == "suggestions"]
    assert sug and SuggestResponse.model_validate(sug[0]).radius_km == 40


def test_gemini_error_becomes_error_event(monkeypatch):
    class Boom(FakeModels):
        async def generate_content_stream(self, model, contents, config):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    fake = FakeClient([])
    fake.aio.models = Boom([])
    monkeypatch.setattr(chat, "_llm_enabled", lambda: True)
    monkeypatch.setattr(chat, "_gemini", lambda: fake)
    with client.stream("POST", "/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}) as r:
        text = "".join(r.iter_text())
    assert "event: error" in text and "rate limit" in text and "event: done" in text
