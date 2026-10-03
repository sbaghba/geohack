# SiteSense backend (FastAPI)

Built on `../contract/` — `contract/schemas.py` is imported directly (see `app/contract.py`), so there is one copy of the API shapes.

## M1 checklist

- [x] Mock JSON matching the contract (`contract/mock/`)
- [x] FastAPI app: `/api/health`, `/api/analyze`, `/api/suggest`, `/api/layers`, `/api/layers/{name}`, `/api/chat` (SSE) — all serving contract mocks (`USE_MOCK=true`)
- [x] Contract tests (`pytest -q`, 7 tests)
- [ ] **You:** get a Gemini key, run `scripts/gemini_smoke_test.py` → PASS
- [ ] **You:** deploy (Render or laptop + tunnel), send the URL to frontend
- [ ] **You:** on the A100 server, start `scripts/download_data.py all` in the background

## Run locally

```powershell
# Windows PowerShell, from the repo root (geohack/)
cd backend
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # then paste GEMINI_API_KEY into .env
pytest -q
uvicorn app.main:app --reload --port 8000
# open http://localhost:8000/docs
```

```bash
# macOS / Linux
cd backend && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && cp .env.example .env
pytest -q && uvicorn app.main:app --reload --port 8000
```

## Gemini check (10 min)

1. Get a key at https://aistudio.google.com → "Get API key". Your teammate should make one too (backup for rate limits).
2. Put it in `backend/.env` as `GEMINI_API_KEY=...`.
3. `python scripts/gemini_smoke_test.py` — expects Gemini to call `move_site`, accept the result, answer, and stream. Prints `PASS`.
   If the model name is wrong it prints the models your key can use; set `GEMINI_MODEL` in `.env`.

## Deploy (pick one)

**A. Render (stays up while laptops sleep)**
1. Push the repo to GitHub (root must contain `Dockerfile`, `render.yaml`, `backend/`, `contract/`).
2. Render → New → Blueprint → select the repo. Paste `GEMINI_API_KEY` when asked.
3. URL looks like `https://sitesense-api.onrender.com`. Free plan sleeps when idle: hit `/api/health` before demos.

**B. Laptop + Cloudflare quick tunnel (fastest, no account)**
```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000
# second terminal (install: winget install Cloudflare.cloudflared)
cloudflared tunnel --url http://localhost:8000
```
It prints `https://<random>.trycloudflare.com` — send that to frontend. The URL changes every restart.

CORS already allows `localhost:5173`, `127.0.0.1:5500`, `*.github.io`, `*.vercel.app`, `*.trycloudflare.com`. Add others with `ALLOWED_ORIGINS`.

Frontend switches over with: `api.configure({ mode: 'live', baseUrl: '<your URL>' })`.

## Downloads on the A100 server

```bash
pip install asf_search
export EARTHDATA_TOKEN=...      # https://urs.earthdata.nasa.gov → Generate Token (free account)
python scripts/download_data.py probe            # what exists over the demo area (no login needed)
nohup python scripts/download_data.py all > download.log 2>&1 &
tail -f download.log
```

| Command | Gets | Size guide |
| --- | --- | --- |
| `probe` | Counts for NISAR GCOV/GUNW/soil moisture + OPERA DISP-S1 over the Triangle | — |
| `nisar --list` / `--max 1` | NISAR L2 GCOV provisional, newest scene | GCOV HDF5 files are large; check `--list` first |
| `opera --list` / `--frame N --max 12` | OPERA DISP-S1 granules, evenly spaced since 2022 | ~12 files for one frame |
| `landcover` | ESA WorldCover 2021 10 m tiles covering NC (public S3) | 8 tiles |

If `probe` shows **NISAR GUNW** granules, NISAR itself can drive the subsidence layer; otherwise use OPERA.
Land cover uses ESA WorldCover (global, no login) instead of NLCD — same role in the model, works for tier-2 sites too.

## Layout

```
backend/
  app/
    main.py            routes only
    config.py          env settings
    contract.py        imports ../contract/schemas.py + mock builders
    errors.py          {"error","detail"} handlers
    llm_tools.py       Gemini tool schemas + system prompt (used in M2)
    services/
      analyze.py       analyze + suggest   (M2: real grid + impact model)
      layers.py        layer list + GeoJSON (M3: real layers)
      chat.py          SSE stream           (M2: Gemini loop)
  scripts/
    gemini_smoke_test.py
    download_data.py
  tests/test_api.py
```

## Next (M2)

Grid build (H3 res 7 over NC) + P0 joins, `impact.py` from `contract/make_mock.py::core_metrics`, live FEMA/Overpass lookups, and the real Gemini loop in `services/chat.py`.
