"""
M1 downloads — run on the A100 server (or any machine with good bandwidth), in the background.

    pip install asf_search
    export EARTHDATA_TOKEN=...        # or EARTHDATA_USERNAME + EARTHDATA_PASSWORD
                                      # free account + token: https://urs.earthdata.nasa.gov  (Generate Token)

    python scripts/download_data.py probe                 # what NISAR/OPERA data exists over the demo area (no login)
    python scripts/download_data.py nisar --list                  # NISAR GCOV (backscatter) scenes + sizes
    python scripts/download_data.py nisar --max 1                 # newest dual-pol GCOV over the demo point
    python scripts/download_data.py nisar --product gunw --max 4  # interferograms (subsidence)
    python scripts/download_data.py nisar --product sme2 --max 1  # soil moisture
    python scripts/download_data.py opera --list          # list OPERA DISP-S1 frames over the demo area
    python scripts/download_data.py opera --frame 12345 --max 12
    python scripts/download_data.py landcover             # ESA WorldCover 10 m tiles covering NC (no login)
    nohup python scripts/download_data.py all > download.log 2>&1 &     # everything, in the background

Files land in $DATA_DIR/raw/{nisar,opera,landcover}  (default backend/data/raw/...).
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
RAW = DATA_DIR / "raw"

# W, S, E, N
DEMO_BBOX = (-79.2, 35.3, -78.2, 36.2)      # Research Triangle + Johnston County
NC_BBOX = (-84.4, 33.8, -75.4, 36.6)

NISAR_GCOV = "NISAR_L2_GCOV_PROVISIONAL_V1"
NISAR_PRODUCTS = {
    "gcov": ("NISAR_L2_GCOV_PROVISIONAL_V1", "_DHDH_"),   # prefer dual-pol (HH + HV)
    "gunw": ("NISAR_L2_GUNW_PROVISIONAL_V1", None),
    "sme2": ("NISAR_L3_SME2_PROVISIONAL_V1", None),
}
DEMO_POINT = (35.70, -78.55)   # lat, lon: between Raleigh and Clayton; scenes must cover this
PROBE_COLLECTIONS = [
    "NISAR_L2_GCOV_PROVISIONAL_V1",   # backscatter (radar view layer)
    "NISAR_L2_GCOV_BETA_V1",
    "NISAR_L2_GUNW_PROVISIONAL_V1",   # interferograms (if present -> NISAR subsidence possible)
    "NISAR_L3_SME2_PROVISIONAL_V1",   # soil moisture
    "OPERA_L3_DISP-S1_V1",            # Sentinel-1 displacement (subsidence fallback)
]
OPERA_DISP = "OPERA_L3_DISP-S1_V1"


# ------------------------------------------------------------- helpers ---

def wkt(b) -> str:
    w, s, e, n = b
    return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"


def parse_bbox(text: str):
    vals = tuple(float(x) for x in text.split(","))
    if len(vals) != 4:
        raise argparse.ArgumentTypeError("bbox = W,S,E,N")
    return vals


def size_mb(p: dict) -> float:
    b = p.get("bytes")
    if isinstance(b, dict):
        return sum((v or {}).get("bytes", 0) or 0 for v in b.values()) / 1e6
    return (b or 0) / 1e6


def session():
    import asf_search as asf

    s = asf.ASFSession()
    tok = os.getenv("EARTHDATA_TOKEN")
    user, pw = os.getenv("EARTHDATA_USERNAME"), os.getenv("EARTHDATA_PASSWORD")
    if tok:
        try:
            return s.auth_with_token(tok)
        except Exception as e:  # validation endpoint can reject a token that still works for downloads
            print(f"  note: token check failed ({e}); trying it directly as a Bearer token")
            s.headers.update({"Authorization": f"Bearer {tok}"})
            return s
    if user and pw:
        return s.auth_with_creds(user, pw)
    sys.exit("Downloads need EARTHDATA_TOKEN (or EARTHDATA_USERNAME + EARTHDATA_PASSWORD). Searching/listing does not.")


def search(short_name: str, bbox, max_results: int = 200, start: str | None = None, end: str | None = None,
           point: tuple[float, float] | None = None):
    import asf_search as asf

    geom = f"POINT({point[1]} {point[0]})" if point else wkt(bbox)
    opts = dict(shortName=short_name, intersectsWith=geom, maxResults=max_results)
    if start:
        opts["start"] = start
    if end:
        opts["end"] = end
    return asf.search(**opts)


# -------------------------------------------------------------- probe ---

def cmd_probe(a):
    print(f"bbox {a.bbox}")
    for name in PROBE_COLLECTIONS:
        try:
            r = search(name, a.bbox, max_results=500)
            dates = sorted(str(p.properties.get("startTime"))[:10] for p in r)
            span = f"{dates[0]} .. {dates[-1]}" if dates else "-"
            print(f"  {name:32s} {len(r):4d} granules   {span}")
        except Exception as e:
            print(f"  {name:32s} error: {e}")


# -------------------------------------------------------------- NISAR ---

def cmd_nisar(a):
    short, prefer = NISAR_PRODUCTS[a.product]
    r = search(short, a.bbox, max_results=200, start=a.start, point=DEMO_POINT)
    items = sorted(r, key=lambda p: str(p.properties.get("startTime")), reverse=True)
    if prefer:
        preferred = [p for p in items if prefer in (p.properties.get("fileName") or "")]
        items = preferred or items
    print(f"{len(items)} {short} granules covering {DEMO_POINT}")
    for p in items[:20]:
        pr = p.properties
        print(f"  {str(pr.get('startTime'))[:19]}  {size_mb(pr):8.0f} MB  {pr.get('fileName') or pr.get('sceneName')}")
    if a.list or not items:
        return
    out = RAW / "nisar" / a.product
    out.mkdir(parents=True, exist_ok=True)
    s = session()
    for p in items[: a.max]:
        name = p.properties.get("fileName")
        if name and (out / name).exists():
            print(f"have {name}")
            continue
        print(f"downloading {name} ({size_mb(p.properties):.0f} MB) -> {out}")
        p.download(path=str(out), session=s)
    print("done")


# -------------------------------------------------------------- OPERA ---

def _frame(p) -> int | None:
    f = p.properties.get("frameNumber")
    if f:
        return int(f)
    m = re.search(r"_F(\d{5})_", p.properties.get("fileName") or p.properties.get("sceneName") or "")
    return int(m.group(1)) if m else None


def cmd_opera(a):
    r = search(OPERA_DISP, a.bbox, max_results=2000, start=a.start, end=a.end)
    by_frame = defaultdict(list)
    for p in r:
        by_frame[_frame(p)].append(p)
    print(f"{len(r)} {OPERA_DISP} granules over {a.bbox}, {len(by_frame)} frames")
    for f, ps in sorted(by_frame.items(), key=lambda kv: -len(kv[1])):
        ds = sorted(str(p.properties.get("startTime"))[:10] for p in ps)
        mb = sum(size_mb(p.properties) for p in ps)
        print(f"  frame {f}: {len(ps):4d} granules  {ds[0]} .. {ds[-1]}  ~{mb / 1e3:.1f} GB total")
    if a.list or not r:
        if not a.list and not r:
            print("nothing found")
        return
    frame = a.frame or max(by_frame, key=lambda k: len(by_frame[k]))
    ps = sorted(by_frame[frame], key=lambda p: str(p.properties.get("startTime")))
    # evenly spaced through time so a velocity fit has a long baseline
    step = max(1, math.ceil(len(ps) / a.max))
    pick = ps[::step][: a.max]
    out = RAW / "opera" / f"F{frame}"
    out.mkdir(parents=True, exist_ok=True)
    s = session()
    for p in pick:
        print(f"downloading {p.properties.get('fileName')} ({size_mb(p.properties):.0f} MB)")
        p.download(path=str(out), session=s)
    print("done ->", out)


# ---------------------------------------------------------- land cover ---
# ESA WorldCover 2021 v200, 10 m, global, public S3 (no login). 3x3 degree tiles named by SW corner.
WC_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"


def worldcover_tiles(b) -> list[str]:
    w, s, e, n = b
    tiles = []
    for lat in range(math.floor(s / 3) * 3, math.ceil(n / 3) * 3, 3):
        for lon in range(math.floor(w / 3) * 3, math.ceil(e / 3) * 3, 3):
            ns = f"N{lat:02d}" if lat >= 0 else f"S{-lat:02d}"
            ew = f"E{lon:03d}" if lon >= 0 else f"W{-lon:03d}"
            tiles.append(ns + ew)
    return tiles


def fetch(url: str, dest: Path):
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  have {dest.name}")
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  get  {dest.name}")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.rename(dest)
    except Exception as e:
        print(f"  FAIL {dest.name}: {e}")
        tmp.unlink(missing_ok=True)


def cmd_landcover(a):
    out = RAW / "landcover"
    out.mkdir(parents=True, exist_ok=True)
    tiles = worldcover_tiles(a.bbox)
    print(f"ESA WorldCover tiles for {a.bbox}: {tiles}")
    for t in tiles:
        fetch(WC_URL.format(tile=t), out / f"ESA_WorldCover_10m_2021_v200_{t}_Map.tif")


# --------------------------------------------------------------- main ---

def cmd_all(a):
    cmd_probe(argparse.Namespace(bbox=DEMO_BBOX))
    cmd_landcover(argparse.Namespace(bbox=NC_BBOX))
    session()  # fail fast on auth before any big download
    steps = [  # small first; the ~7 GB GCOV scene last
        ("NISAR sme2", lambda: cmd_nisar(argparse.Namespace(product="sme2", bbox=DEMO_BBOX, start=None, list=False, max=1))),
        ("NISAR gunw", lambda: cmd_nisar(argparse.Namespace(product="gunw", bbox=DEMO_BBOX, start=None, list=False, max=4))),
        ("OPERA", lambda: cmd_opera(argparse.Namespace(bbox=DEMO_BBOX, start="2022-01-01", end=None, list=False, frame=None, max=12))),
        ("NISAR gcov", lambda: cmd_nisar(argparse.Namespace(product="gcov", bbox=DEMO_BBOX, start=None, list=False, max=1))),
    ]
    for name, fn in steps:
        print(f"\n=== {name} ===")
        try:
            fn()
        except Exception as e:
            print(f"{name} failed: {type(e).__name__}: {e}")
    print("\nALL DONE")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe"); p.add_argument("--bbox", type=parse_bbox, default=DEMO_BBOX); p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("nisar")
    p.add_argument("--product", choices=list(NISAR_PRODUCTS), default="gcov")
    p.add_argument("--bbox", type=parse_bbox, default=DEMO_BBOX)
    p.add_argument("--start", default=None, help="YYYY-MM-DD")
    p.add_argument("--list", action="store_true")
    p.add_argument("--max", type=int, default=1)
    p.set_defaults(fn=cmd_nisar)

    p = sub.add_parser("opera")
    p.add_argument("--bbox", type=parse_bbox, default=DEMO_BBOX)
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--frame", type=int, default=None, help="default: frame with most granules")
    p.add_argument("--list", action="store_true")
    p.add_argument("--max", type=int, default=12)
    p.set_defaults(fn=cmd_opera)

    p = sub.add_parser("landcover"); p.add_argument("--bbox", type=parse_bbox, default=NC_BBOX); p.set_defaults(fn=cmd_landcover)
    p = sub.add_parser("all"); p.set_defaults(fn=cmd_all)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
