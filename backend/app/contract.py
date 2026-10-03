"""
Bridge to ../contract so there is ONE copy of the schemas.

Everything the backend needs from the contract is re-exported here:
    from app.contract import Report, AnalyzeRequest, build_report, ...
"""
from __future__ import annotations

import sys

from .config import REPO_ROOT

_CONTRACT_DIR = REPO_ROOT / "contract"
if not (_CONTRACT_DIR / "schemas.py").exists():
    raise RuntimeError(f"contract/schemas.py not found at {_CONTRACT_DIR}; keep backend/ and contract/ side by side")
if str(_CONTRACT_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTRACT_DIR))

from schemas import *  # noqa: E402,F401,F403
from schemas import CONTRACT_VERSION  # noqa: E402,F401
from make_mock import LAYERS, build_layer, build_report, build_suggest, core_metrics  # noqa: E402,F401
