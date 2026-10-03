# SiteSense API Contract v1.0.0

The agreement between frontend (Leaflet) and backend (FastAPI + Gemini) for WolfHacks 2026.
`schemas.py` is the source of truth; this page explains it in plain words. If they ever disagree, `schemas.py` wins and this page gets fixed.

## Files in this folder

| File | Owner | What it is |
| --- | --- | --- |
| `CONTRACT.md` | both | This agreement |
| `schemas.py` | backend | Pydantic models for every request, response and chat event |
| `make_mock.py` | backend | Builds `mock/*.json` from `schemas.py` so mocks never drift |
| `mock/` | backend | `analyze.json`, `suggest.json`, `layers.json`, `layer_sample.geojson` |
| `stub_server.py` | backend | Every endpoint live with sample numbers; deploy in hour 1 |
| `api.js` | frontend | The only file that knows URLs and field names; mock/live switch |

Run the stub: `pip install -r requirements.txt && uvicorn stub_server:app --reload --port 8000`, then open `http://localhost:8000/docs`.
The stub already reacts to inputs: changing `mw` or `cooling` changes energy, water and carbon.

## Conventions

1. **snake_case**, and **units live in the field name**: `_km`, `_mwh`, `_m3_yr`, `_usd`, `_usd_yr`, `_pct`, `_kv`.
2. **Scores are 0-100. Percentages are 0-100**, never 0-1.
3. **Missing data = `null`** and its dotted path is listed in `report.missing` (e.g. `"hazards.subsidence_mm_yr"`). The UI shows "Not available" — never 0.
4. **Coordinates are `lat`, `lon`** (WGS84). GeoJSON geometry is `[lon, lat]` as the spec requires.
5. **Backend sends raw numbers; frontend formats** them (`Intl.NumberFormat`, compact notation, units).
6. `report.mock === true` means sample data → UI shows a "Sample data" badge.
7. Every error response is `{ "error": "<code>", "detail": "<human text>" }`.

## Endpoints

| Method + path | Body | Returns | Target speed |
| --- | --- | --- | --- |
| `POST /api/analyze` | `AnalyzeRequest` | `Report` | < 2 s |
| `POST /api/suggest` | `SuggestRequest` | `SuggestResponse` | < 4 s |
| `GET /api/layers` | — | `{ layers: LayerInfo[] }` (name, label, unit, min, max, higher_is) | < 0.5 s |
| `GET /api/layers/{name}` | — | GeoJSON FeatureCollection of hex polygons, `properties: { hex_id, value }` | < 2 s |
| `POST /api/chat` | `ChatRequest` | `text/event-stream` (see Chat) | first token < 3 s |
| `GET /api/health` | — | `{ ok, llm_ok, model, grid_rows, contract_version }` | instant |

Layer names: `suitability`, `burden`, `pressure`, `subsidence`, `water_stress`.

### AnalyzeRequest

```json
{ "lat": 35.655, "lon": -78.462, "mw": 100, "cooling": "evaporative", "power": "grid", "workload": "ai" }
```

| Field | Values | Default |
| --- | --- | --- |
| `mw` | 1 to 2000 (IT load) | 100 |
| `cooling` | `evaporative`, `air`, `liquid` | `evaporative` |
| `power` | `grid`, `grid_solar`, `gas` | `grid` |
| `workload` | `ai`, `mixed` | `ai` |

`SuggestRequest` = the same plus `radius_km` (5-200, default 50) and `n` (1-5, default 3).

### Report (what each card reads)

| Section | Card | Key fields |
| --- | --- | --- |
| `site` | header | `lat, lon, hex_id, label, county, state, tier` (1 = full NC detail, 2 = coarse US) |
| `scores` | Overview | `suitability, burden, quadrant` (`good`/`tradeoff`/`poor`/`avoid`), `pressure, pressure_drivers[]` |
| `energy` | Energy | `pue, annual_mwh, peak_grid_mw, homes_equiv, county_share_pct, grid_region, annual_cost_usd, nearest_substation_km/kv, nearest_line_km/kv` |
| `carbon` | Energy | `grid_lb_per_mwh, tons_co2_yr, cars_equiv` |
| `water` | Water | `wue_l_per_kwh, onsite_m3_yr, offsite_m3_yr, households_equiv, aqueduct_stress (0-5), aqueduct_label, drought_category` |
| `economy` | Jobs & Taxes | `capex_usd, construction_jobs, permanent_jobs, county_unemployed, county_unemployment_pct, property_tax_usd_yr, county_levy_share_pct` |
| `hazards` | Hazards | `fema_zone, in_floodplain, nri{overall, hurricane, heat_wave, riverine_flooding, coastal_flooding, tornado}, nri_rating, subsidence_mm_yr` |
| `community` | Community | `pop_1km/3km/5km, homes_1km, svi_pct, median_income_usd, schools_1km[], hospitals_1km[]` (each `{name, kind, lat, lon, distance_km}`) |
| `land` | Community | `acres, converted_acres{forest, cropland, pasture, wetland, developed, other}, flags[]` |
| `mitigations[]` | toggles | `{key, label, deltas[{field, before, after, pct_change}]}` |
| `sources[]` | every card's "source" link | `{key, name, url, vintage}` — card shows sources whose `key` it uses |
| `missing[]` | badges | dotted paths that are null |

Flags: `in_floodplain`, `in_wetland`, `protected_area`, `school_within_500m`, `outside_nc`.

Full example: `mock/analyze.json`.

## Chat

**Request:** `{ "messages": [{ "role": "user", "content": "..." }, ...], "report": <Report on screen>, "config": <AnalyzeRequest> }`.
Send the full history every time; the backend keeps no session state.

**Response:** Server-Sent Events. Each event is `event: <name>` + `data: <json>` + blank line.

| Event | Data | Frontend does |
| --- | --- | --- |
| `token` | `{ text }` | Append to the assistant bubble |
| `tool_call` | `{ id, name, args }` | Show "Moving site..." / "Finding better sites..." |
| `report` | full `Report` | Backend already re-ran analyze: move pin to `report.site`, set sliders from `report.request`, re-render cards |
| `suggestions` | `SuggestResponse` | Drop ghost pins for `candidates` |
| `done` | `{ finish_reason }` | Stop spinner, enable input |
| `error` | `{ message }` | Show retry button |

Tools Gemini can call: `geocode`, `move_site`, `set_config`, `find_better_sites`, `compare`. The backend executes them; the frontend only reacts to `report` and `suggestions`.
Because this is a POST, use `fetch` streaming (already done in `api.chat`), not `EventSource`.

## Errors

| HTTP | `error` | When |
| --- | --- | --- |
| 422 | `invalid_request` | Bad field value, unknown layer name |
| 422 | `out_of_coverage` | Pin outside the contiguous US |
| 429 | `rate_limited` | Gemini quota hit (chat only) |
| 503 | `upstream_unavailable` | A live lookup (FEMA, Overpass, Gemini) is down |
| 500 | `internal` | Bug |

A pin inside the US but outside NC is **not** an error: it returns 200 with `site.tier = 2`, the `outside_nc` flag, and nulls listed in `missing`.

## Deployment

- Frontend: GitHub Pages or Vercel. Backend: Render/Railway or laptop + Cloudflare Tunnel.
- Backend CORS allows the frontend domain plus `http://localhost:5173` and `http://127.0.0.1:5500`.
- Frontend sets `api.configure({ mode: 'live', baseUrl: '<backend URL>' })`; flip to `mode: 'mock'` if the backend is down during judging.

## Change rules

1. **Adding** a field or event: anytime. Regenerate mocks (`python make_mock.py`) and post in team chat.
2. **Renaming, removing or changing a field's type/units:** needs the other person's OK in chat first; bump `CONTRACT_VERSION`.
3. **After 3:00 PM Saturday:** no renames or removals. Add instead.
4. Frontend reads fields only inside `api.js` adapters or card render functions — never scattered string paths.

## Sign-off

- [ ] Frontend: ____________ (time ____)
- [ ] Backend: ____________ (time ____)
