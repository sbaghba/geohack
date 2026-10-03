"""
POST /api/analyze and /api/suggest.

Real data when backend/data/grid/nc_h3r7.parquet exists (built by scripts/build_grid.py),
otherwise — or with USE_MOCK=true — the contract's sample builder.
"""
from __future__ import annotations

from ..config import settings
from ..contract import AnalyzeRequest, Report, SuggestRequest, SuggestResponse, build_report, build_suggest
from ..errors import out_of_coverage

# Contiguous US bounding box (rough). Tier 1 = NC grid, tier 2 = rest of US.
US_BBOX = (24.0, -125.0, 50.0, -66.0)


def real_mode() -> bool:
    if settings.use_mock or not settings.grid_path.exists():
        return False
    return True


def check_coverage(lat: float, lon: float) -> None:
    s, w, n, e = US_BBOX
    if not (s <= lat <= n and w <= lon <= e):
        raise out_of_coverage()


def analyze(req: AnalyzeRequest) -> Report:
    check_coverage(req.lat, req.lon)
    if real_mode():
        from .. import real

        return real.report(req)
    return build_report(req)


def suggest(req: SuggestRequest) -> SuggestResponse:
    check_coverage(req.lat, req.lon)
    if real_mode():
        from .. import real

        return real.suggest(req)
    return build_suggest(req, build_report(AnalyzeRequest(**req.model_dump(exclude={"radius_km", "n"}))))
