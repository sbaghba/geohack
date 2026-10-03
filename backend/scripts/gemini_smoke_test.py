"""
M1 check: one full Gemini function-calling round trip with our real tool schemas.

    cd backend
    set GEMINI_API_KEY=...        (Windows)   |   export GEMINI_API_KEY=...   (mac/linux)
    python scripts/gemini_smoke_test.py
    python scripts/gemini_smoke_test.py --model gemini-3.5-flash-lite

Pass = Gemini calls move_site with sensible lat/lon, accepts our tool result, and answers using it.
Get a key at https://aistudio.google.com (Get API key). One key per teammate = a backup when rate-limited.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from app.config import settings  # noqa: E402
from app.contract import AnalyzeRequest, build_report  # noqa: E402
from app.llm_tools import SYSTEM_PROMPT, gemini_tools  # noqa: E402

QUESTION = "Move the data center to Smithfield, North Carolina (about 35.51, -78.34) and tell me how much water it would use."


def run(client, model: str) -> bool:
    report = build_report(AnalyzeRequest(lat=35.655, lon=-78.462, mw=100))
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT + "\n\nREPORT:\n" + report.model_dump_json(),
        tools=gemini_tools(),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.2,
    )
    contents: list[types.Content] = [types.Content(role="user", parts=[types.Part.from_text(text=QUESTION)])]

    t0 = time.time()
    resp = client.models.generate_content(model=model, contents=contents, config=config)
    calls = resp.function_calls or []
    print(f"[1] {time.time() - t0:.1f}s  function calls: {[(c.name, dict(c.args or {})) for c in calls]}")
    if not calls:
        print("FAIL: no function call. Model said:", resp.text)
        return False

    call = calls[0]
    if call.name != "move_site":
        print(f"WARN: expected move_site, got {call.name} (still testing the round trip)")
    args = dict(call.args or {})
    new = build_report(AnalyzeRequest(lat=float(args.get("lat", 35.51)), lon=float(args.get("lon", -78.34)), mw=100))
    tool_result = {"ok": True, "site": new.site.model_dump(), "water": new.water.model_dump(), "mock": True}

    # Append the model turn as-is (keeps thought signatures), then our function response.
    contents.append(resp.candidates[0].content)
    contents.append(types.Content(role="user", parts=[types.Part.from_function_response(name=call.name, response=tool_result)]))

    t1 = time.time()
    final = client.models.generate_content(model=model, contents=contents, config=config)
    print(f"[2] {time.time() - t1:.1f}s  answer:\n{final.text}\n")

    # Streaming check (what /api/chat will use)
    t2 = time.time()
    first = None
    n = 0
    for chunk in client.models.generate_content_stream(model=model, contents=contents, config=config):
        if chunk.text:
            first = first or time.time() - t2
            n += 1
    print(f"[3] streaming: {n} chunks, first token after {first or 0:.1f}s")
    ok = bool(final.text) and n > 0
    print("PASS" if ok else "FAIL")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=settings.gemini_model)
    a = ap.parse_args()
    if not settings.gemini_api_key:
        sys.exit("Set GEMINI_API_KEY first (or put it in backend/.env)")
    client = genai.Client(api_key=settings.gemini_api_key)
    print("model:", a.model)
    try:
        ok = run(client, a.model)
    except Exception as e:  # model name typo, quota, etc.
        print(f"ERROR {type(e).__name__}: {e}")
        print("Available models with generateContent:")
        for m in client.models.list():
            if "generateContent" in (getattr(m, "supported_actions", None) or []):
                print("  ", m.name)
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
