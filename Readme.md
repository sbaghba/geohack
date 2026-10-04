# Does Your Data Center Fit? (SiteSense)

**Drop a data center anywhere in North Carolina and see what it would do to the place: the power, water, carbon, jobs, taxes, neighbors, hazards and protected land, all from public data. Then ask an AI analyst about it.**

Live site: **https://doesyourdatacenter.fit** · API: https://sitesense-api.onrender.com/docs
WolfHacks 2026 · Center for Geospatial Analytics track · Team: Sina Baghbanijam (backend, data, model) and Zan Jamieson (frontend)

> The free API server sleeps when idle. The first request can take 30-60 s while it wakes up (the page tells you).

---

## What it does

Data centers are being proposed all over North Carolina, and most residents only hear "jobs and tax revenue" or "it will drink our water." SiteSense lets anyone check for themselves.

1. **Click the map** to place a data center. Choose its size (MW), cooling (evaporative / air / closed-loop liquid), power source and workload.
2. **Read the report** across six tabs:

| Tab | What you see |
|---|---|
| Overview | **Suitability** (is it a good site for a developer?) vs **Burden** (what does it cost the neighbors?), a plain-language verdict, the **Siting Pressure** score with its top 3 reasons, and one-click mitigations |
| Energy | MWh per year, "same as N homes", share of county households, nearest 115 kV+ substation and line, grid carbon, power bill |
| Water | On-site cooling water, off-site water at power plants, "same as N households", WRI water stress, **this week's drought** |
| Jobs | Construction and permanent jobs, county unemployment, estimated property tax using the county's actual 2025-26 rate |
| Community | People within 1/3/5 km, income, social vulnerability, schools nearby, **existing data centers within 25 km**, land that would be converted |
| Risk & Ground | FEMA flood zone, FEMA hazard risk, **ground motion from satellite radar (OPERA / NISAR)**, **protected land within 1 km (PAD-US)**, warnings |

3. **Find Better Spots** searches nearby for sites with higher suitability and lower burden, and skips parks and preserves.
4. **Ask the AI analyst** (Gemini): "Is this good for my town?", "What happens to our water?", "Move it 20 km east", "Make it 50 MW with liquid cooling". It answers using only the numbers in the report, names the dataset behind each one, and can move the site or change the design itself.
5. **Map layers**: suitability, burden, siting pressure, water stress, ground motion and NISAR radar stability hexes, plus a NISAR L-band radar image (vegetation and structure).

Every number comes from a named, linked source, which the sidebar lists. A field with no data is marked unavailable; nothing is filled in by guessing.

## How it works

```
 Browser (Leaflet, vanilla JS)  ──►  FastAPI on Render (Docker)
   map, tabs, layers, chat            /api/analyze  /api/suggest  /api/chat (SSE)
                                      /api/layers   /api/overlays /api/model
                                         │
          prebuilt NC grid (H3 res 7, 29,352 hexes) + point layers
          live lookups: FEMA flood zone, US Drought Monitor, USGS PAD-US
          Gemini (function calling: geocode, move_site, set_config, find_better_sites, compare)
```

- **Grid.** North Carolina is split into 29,352 H3 hexagons (about 5 km² each). An ETL pipeline (`backend/scripts/build_grid.py`) joins Census, FEMA, WRI, ESA, OSM and radar data onto each hex.
- **Suitability** (0-100) averages percentiles of: distance to a 115 kV+ substation and line, water stress, natural-hazard risk, how easy the land is to build on, open water and ground stability.
- **Burden** (0-100) averages percentiles of: people within 3 km, social vulnerability, water stress, forest and wetland, and schools nearby.
- **Hard constraints.** A site inside PAD-US protected land is capped at suitability 10 with an "Avoid" verdict. Hexes that are mostly water or wetland are never suggested.
- **Impact model** (`backend/app/impact.py`, constants in `reference.py`): PUE and water use by cooling type, EPA eGRID carbon intensity for the site's grid subregion, EIA price, NCDOR county tax rate, and jobs and capex per MW. Every assumption is in one file, with its source.
- **Siting Pressure model** (`backend/scripts/pressure_model.py`):
  - **What it measures:** how much a place resembles where data centers already get built.
  - **Training:** XGBoost on a GPU over 210,427 US H3 res-6 hexes, with 539 hexes containing an OpenStreetMap-mapped data center (a positive-unlabeled setup).
  - **Features:** distance to the grid, population within 10 and 50 km, FEMA hazards and social vulnerability, water stress, state power price, grid carbon. Location and distance to existing data centers are deliberately left out.
  - **Validation:** whole states held out (GroupKFold by state). AUC 0.92, and the top 10% of hexes capture 86% of data-center hexes.
  - **Output:** a percentile within NC and nationally, with the top 3 reasons from TreeSHAP.
- **Satellite radar:**
  - **OPERA DISP-S1** (Sentinel-1): local ground velocity for 2021-2025, relative to the surrounding ~12 km. Covers most of NC.
  - **NASA-ISRO NISAR** L2: interferometric coherence (ground stability) for about two-thirds of NC, plus an L-band HV backscatter image clipped to NC.
- **Chat.** Gemini streams over server-sent events. Its tool calls run on the server and update the map, so "move it to Smithfield" really moves the site and reruns the report.
- **API contract.** `contract/schemas.py` (Pydantic) is the single source of truth (v1.4.0). The frontend was built against mock JSON generated from the same schemas.

## Data sources

| Data | Used for |
|---|---|
| [US Census ACS 5-year 2023](https://www.census.gov/data/developers.html), [cartographic boundaries 2023](https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html) | Population, households, income, county unemployment; grid and county boundaries |
| [FEMA National Risk Index](https://hazards.fema.gov/nri/) | Hurricane, heat, flood and tornado risk; social vulnerability |
| [FEMA National Flood Hazard Layer](https://www.fema.gov/flood-maps/national-flood-hazard-layer) (live) | Flood zone at the exact point |
| [U.S. Drought Monitor](https://droughtmonitor.unl.edu/) (live, via FEMA GIS) | This week's drought category |
| [USGS PAD-US 4](https://www.usgs.gov/programs/gap-analysis-project/science/pad-us-data-overview) (live) | Parks, preserves, game lands, easements (GAP 1-3) |
| [WRI Aqueduct 4.0](https://www.wri.org/aqueduct) | Baseline water stress |
| [ESA WorldCover 2021](https://esa-worldcover.org) | Land cover and land that would be converted |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) | Substations, transmission lines, schools, hospitals, existing data centers |
| [EPA eGRID2023](https://www.epa.gov/egrid) | Grid carbon intensity (subregion and state) |
| [EIA State Electricity Profiles 2024](https://www.eia.gov/electricity/state/) | Electricity price |
| [NC Dept. of Revenue 2025-26](https://www.ncdor.gov/taxes-forms/property-tax/property-tax-rates/county-property-tax-rates-and-reappraisal-schedules/fiscal-year-2025-2026) | County property tax rates |
| [OPERA DISP-S1](https://www.earthdata.nasa.gov/data/catalog/asf-opera-l3-disp-s1-v1-1) (ASF DAAC) | Ground motion (subsidence) |
| [NASA-ISRO NISAR L2 GUNW / GCOV](https://nisar-docs.asf.alaska.edu/) (ASF DAAC) | Radar coherence, L-band HV image |

## Limitations (honest list)

- Impact numbers are **planning-level estimates** from published averages (PUE, water use, jobs per MW), not a site engineering study.
- OpenStreetMap does not map every data center or substation.
- Ground-motion coverage is partial: OPERA covers most of NC and NISAR about two-thirds of NC.
- Full detail is for North Carolina. Elsewhere in the lower 48 you get a coarse report (state grid carbon and price, FEMA flood zone, drought, protected land).
- The Siting Pressure score is relative ("resembles"), not a probability that something will be built.

## Run it yourself

```bash
# backend (Python 3.11+)
cd backend
pip install -r requirements.txt
cp .env.example .env            # add GEMINI_API_KEY for the AI analyst (works without it, using a scripted fallback)
pytest -q                       # 18 tests
uvicorn app.main:app --reload --port 8000     # http://localhost:8000/docs

# frontend: any static server; set const API in frontend/index.html to your backend URL
cd frontend && python -m http.server 5500     # http://127.0.0.1:5500
```

To rebuild the data from scratch (on a machine with a GPU for the pressure model), see `backend/README.md`, which covers `download_data.py`, `build_grid.py`, `process_insar.py` and `pressure_model.py`. Deployment is a Render Docker blueprint (`render.yaml`).

## Repository layout

## Repository layout

```
geohack/
├── frontend/                  the website (static, no build step)
│   ├── index.html             map, report tabs, layers, AI chat (Leaflet + vanilla JS)
│   ├── style.css
│   └── icon.png
│
├── backend/                   FastAPI service (deployed on Render)
│   ├── app/
│   │   ├── main.py            routes: /api/analyze, /suggest, /chat (SSE), /layers, /overlays, /model, /health
│   │   ├── real.py            builds a report from the grid + live lookups; Find Better Spots
│   │   ├── impact.py          energy, water, carbon, jobs and tax formulas; mitigations
│   │   ├── reference.py       every assumption and constant, with its source
│   │   ├── live.py            live lookups: FEMA flood zone, US Drought Monitor, USGS PAD-US
│   │   ├── store.py           loads the prebuilt grid and point layers; spatial lookups
│   │   ├── llm_tools.py       Gemini tool definitions + analyst system prompt
│   │   └── services/          analyze, layers, chat (Gemini function-calling loop)
│   ├── scripts/               offline pipeline
│   │   ├── download_data.py   NISAR, OPERA and WorldCover downloads (NASA Earthdata / ASF)
│   │   ├── build_grid.py      NC H3 grid: Census, FEMA NRI, Aqueduct, land cover, OSM -> scores + map layers
│   │   ├── process_insar.py   OPERA ground motion, NISAR coherence, NISAR radar image
│   │   └── pressure_model.py  Siting Pressure model: US features -> GPU XGBoost -> NC predictions
│   ├── data/                  prebuilt outputs the API serves (committed, ~50 MB)
│   │   ├── grid/              29,352 hexes x 46 columns (parquet) + county and state tables
│   │   ├── points/            substations, lines, schools, hospitals, data centers (OSM)
│   │   ├── layers/            map layers as GeoJSON
│   │   ├── overlays/          NISAR L-band radar image
│   │   └── models/            pressure model metrics (model card)
│   └── tests/                 18 pytest tests (API contract, real-data path, Gemini loop)
│
├── contract/                  API contract shared by frontend and backend
│   ├── schemas.py             Pydantic models: the single source of truth (v1.4.0)
│   ├── CONTRACT.md            human-readable API doc
│   ├── mock/                  sample responses generated from the schemas
│   └── api.js, stub_server.py helpers used to build the frontend before the backend existed
│
├── Dockerfile, render.yaml    deployment (Render Docker blueprint)
└── Readme.md
```

## AI usage disclosure

As the hackathon rules require, here is how we used AI tools:

- **In the product:** Google **Gemini** (`gemini-3.8-flash`, via the Gemini API) powers the AI analyst chat. It is instructed to use only numbers from the report and to cite their sources, and it calls our own backend tools to move or resize the site.
- **During development:** we used **Claude (Anthropic, Claude Opus 5.5, in Cowork)** as a coding assistant. It helped:
  - draft the project plan, task split and API contract
  - write parts of the backend: the FastAPI service, impact model, ETL pipeline, radar processing, Siting Pressure model training scripts and some sample tests
  - add features and bug fixes to the frontend (chat panel, map layers, Risk tab, number formatting, loading and error states)
  - write a sample README

  We chose the data sources and ran the pipelines on our own hardware. We checked outputs by hand: for example, the Wake County tax estimate was recomputed from the NCDOR rate, and the radar products were inspected and fixed when the first ground-motion rates were unrealistic. We take responsibility for the code and the numbers.
- **Frontend:** Zan built the original interface by hand (map, parameter form, tabs, layout and styling). Extra styling and certain features (tab switching and Find Better Spots button) were done with ChatGPT.

## License

MIT for our code. The datasets keep their own licenses and terms; see the links above. Map data © OpenStreetMap contributors.
