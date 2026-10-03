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
| `nisar --list` / `--max 1` | NISAR L2 GCOV provisional, newest dual-pol scene over the demo point | ~6-7 GB each |
| `nisar --product gunw --max 4` | NISAR L2 interferograms (subsidence) | check `--list` |
| `nisar --product sme2 --max 1` | NISAR L3 soil moisture | check `--list` |
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
    llm_tools.py       Gemini tool schemas + system prompt
    reference.py       sourced constants: eGRID, EIA prices, NC county tax, model assumptions
    impact.py          energy/carbon/water/jobs/tax formulas + mitigations
    store.py           loads grid + points, spatial lookups
    live.py            FEMA flood-zone point query
    real.py            real Report + suggest from the grid
    services/
      analyze.py       real when data/grid exists, else sample
      layers.py        data/layers/*.geojson, else sample
      chat.py          Gemini streaming loop with server-side tools
  scripts/
    gemini_smoke_test.py
    download_data.py
    build_grid.py      NC grid ETL (A100 server)
  tests/test_api.py
```

## M2: real data

**1. Build the grid on the A100 server** (~10-30 min first run; downloads cached in `data/raw`):

```bash
cd ~/envs/geohack && git pull && cd backend
pip install -r requirements.txt -r requirements-etl.txt
python -u scripts/build_grid.py 2>&1 | tee build.log
```

Steps: `boundaries` (Census + H3 hexes) → `census` (ACS 2023 pop, households, income, county unemployment) → `nri` (FEMA NRI hazards + social vulnerability) → `aqueduct` (WRI water stress; ~1 GB zip) → `landcover` (WorldCover shares) → `osm` (substations, power lines, schools, hospitals) → `score` → `layers`.
A failed step is reported and skipped; re-run just that one with `--only aqueduct`.

**2. Commit the outputs** (small) and pull them on the laptop / Render:

```bash
git add data/grid data/points data/layers && git commit -m "NC grid" && git push
```

**3. Switch to real data:** set `USE_MOCK=false` in `backend/.env` (Render: already false) and restart uvicorn.
`/api/health` shows `grid_rows` > 0; reports come back with `"mock": false`.

What's real vs. not yet:

| Field | Source |
| --- | --- |
| energy, carbon, water, jobs, tax | model (`app/reference.py`) with eGRID 2023, EIA 2024 price, NCDOR 2025-26 county tax |
| substation/line distance, schools/hospitals within 1 km | OSM points (exact, at request time) |
| water stress | WRI Aqueduct 4.0 |
| hazards, social vulnerability | FEMA NRI tracts; flood zone = live FEMA NFHL query |
| population, households, income, unemployment | ACS 2023 5-year |
| land converted | ESA WorldCover shares × campus acres |
| scores | percentiles across all NC hexes (see `step_score`) |
| subsidence, siting pressure, drought | M3 (null + listed in `missing`) |

Outside NC (tier 2): eGRID state rate, EIA state price, FEMA flood zone; the rest null with `outside_nc` flag.

**Chat:** with `GEMINI_API_KEY` set, `/api/chat` runs the real Gemini loop (tools execute server-side and stream `report` / `suggestions` events). Without a key it falls back to the scripted reply.
