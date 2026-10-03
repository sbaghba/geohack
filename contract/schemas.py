"""
SiteSense API contract — single source of truth.

Both teammates build against these models. FastAPI turns them into live docs
at /docs and /openapi.json. The mock files in mock/ are generated from these
models by make_mock.py, so they always match.

Conventions (see CONTRACT.md):
  * snake_case everywhere; units live in the field name (_km, _mwh, _usd_yr, _m3_yr, _pct)
  * scores are 0-100; percentages are 0-100 (not 0-1)
  * missing data = null AND its dotted path is listed in Report.missing
  * coordinates are always lat, lon (WGS84 degrees)

Change rules: adding a field is fine anytime. Renaming/removing a field needs
the other person's OK. No renames after 3 PM Saturday.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

CONTRACT_VERSION = "1.2.0"  # 1.1: county_households, county_homes_share_pct, field_sources | 1.2: hazards.nisar_*, nisar_coherence layer, /api/overlays

# ----------------------------------------------------------------- enums ---

Cooling = Literal["evaporative", "air", "liquid"]
Power = Literal["grid", "grid_solar", "gas"]
Workload = Literal["ai", "mixed"]
Quadrant = Literal["good", "tradeoff", "poor", "avoid"]
# good     = high suitability, low burden
# tradeoff = high suitability, high burden
# poor     = low suitability, low burden
# avoid    = low suitability, high burden
Flag = Literal[
    "in_floodplain",      # FEMA 100-yr zone (A*, V*)
    "in_wetland",         # NWI
    "protected_area",     # PAD-US
    "school_within_500m",
    "outside_nc",         # tier 2: coarse data only
]
MitigationKey = Literal["closed_loop_cooling", "onsite_solar", "larger_setback", "smaller_campus"]
LayerName = Literal["suitability", "burden", "pressure", "subsidence", "water_stress", "nisar_coherence"]


# -------------------------------------------------------------- requests ---

class AnalyzeRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, examples=[35.655])
    lon: float = Field(..., ge=-180, le=180, examples=[-78.462])
    mw: float = Field(100, ge=1, le=2000, description="IT load in MW")
    cooling: Cooling = "evaporative"
    power: Power = "grid"
    workload: Workload = "ai"


class SuggestRequest(AnalyzeRequest):
    radius_km: float = Field(50, ge=5, le=200)
    n: int = Field(3, ge=1, le=5)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1, description="Full history, oldest first; backend is stateless")
    report: Optional["Report"] = Field(None, description="The report currently on screen")
    config: Optional[AnalyzeRequest] = None


# --------------------------------------------------------- report pieces ---

class Site(BaseModel):
    lat: float
    lon: float
    hex_id: Optional[str] = Field(None, description="H3 res-7 cell; null outside the NC grid")
    label: Optional[str] = Field(None, examples=["near Clayton, NC"])
    county: Optional[str] = None
    state: Optional[str] = Field(None, description="2-letter code")
    tier: Literal[1, 2] = Field(..., description="1 = full NC detail, 2 = coarse US")


class Driver(BaseModel):
    key: str = Field(..., examples=["near_230kv_line"])
    label: str = Field(..., examples=["230 kV line 1.8 km away"])
    direction: Literal["+", "-"] = Field(..., description="+ raises the score, - lowers it")


class Scores(BaseModel):
    suitability: float = Field(..., ge=0, le=100, description="Developer view; higher = better site")
    burden: float = Field(..., ge=0, le=100, description="Community view; higher = worse for residents")
    quadrant: Quadrant
    pressure: Optional[float] = Field(None, ge=0, le=100, description="Siting Pressure model: relative likelihood developers target this hex")
    pressure_drivers: list[Driver] = Field(default_factory=list, description="Top SHAP drivers, max 3")


class Energy(BaseModel):
    pue: float
    annual_mwh: float
    peak_grid_mw: float = Field(..., description="mw x PUE")
    homes_equiv: float
    county_share_pct: Optional[float] = Field(None, description="Site use as % of county electricity use")
    county_households: Optional[float] = None
    county_homes_share_pct: Optional[float] = Field(None, description="homes_equiv as % of the county's households")
    grid_region: Optional[str] = Field(None, examples=["SRVC"], description="eGRID subregion")
    price_usd_per_mwh: Optional[float] = None
    annual_cost_usd: Optional[float] = None
    nearest_substation_km: Optional[float] = None
    nearest_substation_kv: Optional[float] = None
    nearest_line_km: Optional[float] = None
    nearest_line_kv: Optional[float] = None


class Carbon(BaseModel):
    grid_lb_per_mwh: Optional[float] = None
    tons_co2_yr: Optional[float] = None
    cars_equiv: Optional[float] = None


class Water(BaseModel):
    wue_l_per_kwh: float
    onsite_m3_yr: float
    offsite_m3_yr: Optional[float] = None
    households_equiv: float = Field(..., description="On-site use / avg US household use")
    aqueduct_stress: Optional[float] = Field(None, ge=0, le=5)
    aqueduct_label: Optional[str] = Field(None, examples=["Medium-high (20-40%)"])
    drought_category: Optional[Literal["none", "D0", "D1", "D2", "D3", "D4"]] = None


class Economy(BaseModel):
    capex_usd: float
    construction_jobs: float
    permanent_jobs: float
    county_unemployed: Optional[float] = None
    county_unemployment_pct: Optional[float] = None
    property_tax_usd_yr: Optional[float] = None
    county_levy_share_pct: Optional[float] = Field(None, description="Tax as % of county's total property-tax levy")


class NriScores(BaseModel):
    """FEMA National Risk Index scores, 0-100 (null if not available)."""
    overall: Optional[float] = None
    hurricane: Optional[float] = None
    heat_wave: Optional[float] = None
    riverine_flooding: Optional[float] = None
    coastal_flooding: Optional[float] = None
    tornado: Optional[float] = None


class Hazards(BaseModel):
    fema_zone: Optional[str] = Field(None, examples=["X", "AE"])
    in_floodplain: Optional[bool] = None
    nri: NriScores
    nri_rating: Optional[str] = Field(None, examples=["Relatively Moderate"])
    subsidence_mm_yr: Optional[float] = Field(None, description="OPERA DISP-S1 line-of-sight velocity 2021-2025, mm/yr; negative = moving away from the satellite (sinking)")
    nisar_coherence: Optional[float] = Field(None, ge=0, le=1, description="NISAR 12-day interferometric coherence: 1 = stable ground/structures, 0 = changing surface")
    nisar_motion_12d_mm: Optional[float] = Field(None, description="NISAR 12-day line-of-sight motion snapshot (noisy; atmosphere not removed)")


class Poi(BaseModel):
    name: str
    kind: Literal["school", "hospital", "data_center", "substation"]
    lat: float
    lon: float
    distance_km: float


class Community(BaseModel):
    pop_1km: Optional[float] = None
    pop_3km: Optional[float] = None
    pop_5km: Optional[float] = None
    homes_1km: Optional[float] = None
    svi_pct: Optional[float] = Field(None, ge=0, le=100, description="CDC SVI percentile; higher = more vulnerable")
    median_income_usd: Optional[float] = None
    schools_1km: list[Poi] = Field(default_factory=list)
    hospitals_1km: list[Poi] = Field(default_factory=list)


class LandConverted(BaseModel):
    forest: float = 0
    cropland: float = 0
    pasture: float = 0
    wetland: float = 0
    developed: float = 0
    other: float = 0


class Land(BaseModel):
    acres: float
    converted_acres: LandConverted
    flags: list[Flag] = Field(default_factory=list)


class Delta(BaseModel):
    field: str = Field(..., examples=["water.onsite_m3_yr"], description="Dotted path into Report")
    before: Optional[float] = None
    after: Optional[float] = None
    pct_change: Optional[float] = None


class Mitigation(BaseModel):
    key: MitigationKey
    label: str
    deltas: list[Delta]


class Source(BaseModel):
    key: str = Field(..., examples=["egrid"])
    name: str
    url: str
    vintage: Optional[str] = Field(None, examples=["2023"])


# ---------------------------------------------------------------- report ---

class Report(BaseModel):
    contract_version: str = CONTRACT_VERSION
    mock: bool = Field(False, description="True when served from mock/ — UI shows a 'sample data' badge")
    generated_at: str = Field(..., description="ISO 8601 UTC")
    request: AnalyzeRequest
    site: Site
    scores: Scores
    energy: Energy
    carbon: Carbon
    water: Water
    economy: Economy
    hazards: Hazards
    community: Community
    land: Land
    mitigations: list[Mitigation] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list, description="Dotted paths of fields that are null for lack of data")
    field_sources: dict[str, str] = Field(default_factory=dict, description="Dotted path (or section) -> Source.key, e.g. 'water.aqueduct_stress': 'aqueduct', 'water': 'model'")


# --------------------------------------------------------------- suggest ---

class Candidate(BaseModel):
    rank: int
    lat: float
    lon: float
    hex_id: Optional[str] = None
    label: Optional[str] = None
    distance_km: float = Field(..., description="From the original pin")
    scores: Scores
    deltas: list[Delta] = Field(..., description="vs. the original site, most important first")
    reason: str = Field(..., examples=["Same grid access, 60% less water stress, 2,100 fewer residents within 3 km"])


class SuggestResponse(BaseModel):
    origin: Site
    origin_scores: Scores
    radius_km: float
    candidates: list[Candidate]


# ---------------------------------------------------------------- layers ---

class LayerInfo(BaseModel):
    name: LayerName
    label: str
    unit: str = Field(..., examples=["score 0-100", "mm/yr"])
    min: float
    max: float
    higher_is: Literal["better", "worse", "neutral"]


class LayerList(BaseModel):
    layers: list[LayerInfo]


class OverlayInfo(BaseModel):
    """A georeferenced image for L.imageOverlay(url, bounds)."""
    name: str = Field(..., examples=["nisar_hv"])
    label: str
    url: str = Field(..., description="Absolute URL of the PNG")
    bounds: list[list[float]] = Field(..., description="[[south, west], [north, east]]")
    date: Optional[str] = None
    source: str = Field(..., description="Source.key")
    legend: dict[str, str] = Field(default_factory=dict)


class OverlayList(BaseModel):
    overlays: list[OverlayInfo]

# GET /api/layers/{name} returns plain GeoJSON (not modeled here):
# FeatureCollection of hex Polygons, properties = {"hex_id": str, "value": float | null}


# ---------------------------------------------------------------- health ---

class Health(BaseModel):
    ok: bool
    llm_ok: bool
    model: Optional[str] = Field(None, examples=["gemini-3.8-flash"])
    grid_rows: int
    contract_version: str = CONTRACT_VERSION


# ---------------------------------------------------------------- errors ---

class ApiError(BaseModel):
    error: Literal["invalid_request", "out_of_coverage", "upstream_unavailable", "rate_limited", "internal"]
    detail: str


# ------------------------------------------------------- chat SSE events ---
# POST /api/chat responds with text/event-stream. Each event is:
#   event: <name>\ndata: <json>\n\n
# Event payloads:

class TokenEvent(BaseModel):          # event: token
    text: str


class ToolCallEvent(BaseModel):       # event: tool_call   (UI can show "Moving site...")
    id: str
    name: Literal["geocode", "move_site", "set_config", "find_better_sites", "compare"]
    args: dict


class DoneEvent(BaseModel):           # event: done
    finish_reason: Literal["stop", "length", "error"] = "stop"


class ErrorEvent(BaseModel):          # event: error
    message: str

# event: report       data = Report            (backend already ran analyze; UI moves pin + re-renders)
# event: suggestions  data = SuggestResponse   (UI drops ghost pins)


ChatRequest.model_rebuild()
