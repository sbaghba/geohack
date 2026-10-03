"""
POST /api/analyze and /api/suggest.

M1: served from the contract mock builder (real formulas, placeholder local values).
M2: replace `_real_report` with grid lookup + live point queries + impact.py.
"""
from __future__ import annotations

from ..config import settings
from ..contract import AnalyzeRequest, Report, SuggestRequest, SuggestResponse, build_report, build_suggest
from ..errors import out_of_coverage

# Contiguous US bounding box (rough). Tier 1 = NC grid, tier 2 = rest of US.
US_BBOX = (24.0, -125.0, 50.0, -66.0)


def check_coverage(lat: float, lon: float) -> None:
    s, w, n, e = US_BBOX
    if not (s <= lat <= n and w <= lon <= e):
        raise out_of_coverage()


def analyze(req: AnalyzeRequest) -> Report:
    check_coverage(req.lat, req.lon)
    if settings.use_mock or not settings.grid_path.exists():
        return build_report(req)
    return _real_report(req)


def suggest(req: SuggestRequest) -> SuggestResponse:
    check_coverage(req.lat, req.lon)
    base = AnalyzeRequest(**req.model_dump(exclude={"radius_km", "n"}))
    if settings.use_mock or not settings.grid_path.exists():
        return build_suggest(req, build_report(base))
    raise NotImplementedError("M3: radius search over grid")


def _real_report(req: AnalyzeRequest) -> Report:  # M2
    raise NotImplementedError("M2: grid lookup + impact model")
