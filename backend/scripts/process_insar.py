"""
M3: ground movement and radar layers from the downloaded NISAR + OPERA files (run on the A100 server).

    python scripts/process_insar.py inspect data/raw/nisar/gunw/<file>.h5    # print an HDF5 tree (debugging)
    python scripts/process_insar.py all                                       # everything below
    python scripts/process_insar.py opera | nisar | overlay

What it produces (then run:  python scripts/build_grid.py --only score,layers):
    data/interim/feat_insar.parquet   per hex:
        subsidence_mm_yr      OPERA DISP-S1 local line-of-sight velocity 2021-2025, relative to the surrounding ~12 km
                              (negative = moving away from satellite, e.g. sinking). Long-wavelength atmosphere removed.
        opera_coherence       OPERA temporal coherence (quality)
        nisar_coherence       mean NISAR 12-day interferometric coherence (1 = stable ground/structures, 0 = changing surface)
    data/overlays/nisar_hv.png + overlays.json   NISAR L-band HV backscatter image for the map (EPSG:4326 bounds)

Physics: NISAR L-band wavelength 0.2384 m; LOS displacement = -lambda/(4*pi) * unwrapped phase.
Why OPERA for the rate: one 12-day pair is dominated by atmospheric delay (cm-level), so a yearly rate needs a multi-year series.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import h3
import h5py
import numpy as np
import pandas as pd

DATA = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
RAW, INTERIM, OVERLAYS = DATA / "raw", DATA / "interim", DATA / "overlays"
OVERLAYS.mkdir(parents=True, exist_ok=True)
INTERIM.mkdir(parents=True, exist_ok=True)
RES = 7
L_BAND_WAVELENGTH_M = 0.2384
NC_BOUNDS = (33.7, -84.5, 36.7, -75.3)  # S W N E


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------- HDF5 helpers ---

def datasets(f: h5py.File) -> list[tuple[str, tuple, str]]:
    out = []
    f.visititems(lambda name, obj: out.append((name, obj.shape, str(obj.dtype))) if isinstance(obj, h5py.Dataset) else None)
    return out


def find(f: h5py.File, name: str, prefer: tuple[str, ...] = (), min_ndim: int = 0) -> str | None:
    """Path of a dataset whose last path component == name (case-insensitive), preferring paths containing `prefer`."""
    hits = [p for p, shape, _ in datasets(f) if p.split("/")[-1].lower() == name.lower() and len(shape) >= min_ndim]
    if not hits:
        return None
    hits.sort(key=lambda p: (-sum(s.lower() in p.lower() for s in prefer), len(p)))
    return hits[0]


def _parents(path: str):
    parts = path.split("/")[:-1]
    for i in range(len(parts), -1, -1):
        yield "/".join(parts[:i])


def coords(f: h5py.File, dpath: str):
    for grp in _parents(dpath):
        g = f[grp] if grp else f
        for xn, yn in (("xCoordinates", "yCoordinates"), ("x", "y"), ("x_coordinates", "y_coordinates")):
            if xn in g and yn in g and isinstance(g[xn], h5py.Dataset):
                return g[xn][()], g[yn][()]
    raise RuntimeError(f"no x/y coordinate arrays near {dpath}")


def crs_of(f: h5py.File, dpath: str, x: np.ndarray, lon_hint: float = -78.6):
    from pyproj import CRS

    ds = f[dpath]
    gm = ds.attrs.get("grid_mapping")
    cands = []
    if gm is not None:
        cands.append(gm.decode() if isinstance(gm, bytes) else str(gm))
    for grp in _parents(dpath):
        g = f[grp] if grp else f
        for n in ("projection", "spatial_ref", "crs"):
            if n in g:
                cands.append(f"{grp}/{n}" if grp else n)
    for c in cands:
        try:
            a = f[c].attrs
        except KeyError:
            continue
        for key in ("epsg_code", "epsg"):
            if key in a:
                return CRS.from_epsg(int(np.atleast_1d(a[key])[0]))
        for key in ("crs_wkt", "spatial_ref"):
            if key in a:
                v = a[key]
                return CRS.from_wkt(v.decode() if isinstance(v, bytes) else str(v))
        try:
            v = f[c][()]
            if np.issubdtype(np.asarray(v).dtype, np.integer) and int(v) > 1000:
                return CRS.from_epsg(int(v))
        except Exception:
            pass
    if np.nanmax(np.abs(x)) <= 360:
        return CRS.from_epsg(4326)
    zone = int((lon_hint + 180) // 6) + 1
    log(f"  WARNING: no CRS metadata for {dpath}; assuming UTM zone {zone}N")
    return CRS.from_epsg(32600 + zone)


def to_hex(values: np.ndarray, x: np.ndarray, y: np.ndarray, crs, stride: int) -> pd.DataFrame:
    """Median of valid pixels per H3 res-7 cell (pixels subsampled by `stride`)."""
    from pyproj import Transformer

    v = values[::stride, ::stride]
    xs, ys = x[::stride], y[::stride]
    X, Y = np.meshgrid(xs, ys)
    ok = np.isfinite(v)
    if not ok.any():
        return pd.DataFrame(columns=["hex_id", "value"])
    lon, lat = Transformer.from_crs(crs, 4326, always_xy=True).transform(X[ok], Y[ok])
    s, w, n, e = NC_BOUNDS
    inside = (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
    lat, lon, vals = lat[inside], lon[inside], v[ok][inside]
    cells = np.fromiter((h3.latlng_to_cell(a, b, RES) for a, b in zip(lat, lon)), dtype=object, count=len(lat))
    return pd.DataFrame({"hex_id": cells, "value": vals}).groupby("hex_id").value.median().reset_index()


def read2d(ds, stride: int = 1) -> np.ndarray:
    a = ds[::stride, ::stride] if stride > 1 else ds[()]
    a = a.astype("float32")
    fill = ds.attrs.get("_FillValue")
    if fill is not None:
        a[a == np.float32(np.atleast_1d(fill)[0])] = np.nan
    return a


def highpass(a: np.ndarray, block: int) -> np.ndarray:
    """Remove long-wavelength signal (atmosphere, orbit ramps): subtract a block-median surface (~12 km)."""
    h, w = a.shape
    bh, bw = -(-h // block), -(-w // block)
    pad = np.full((bh * block, bw * block), np.nan, dtype="float32")
    pad[:h, :w] = a
    med = np.nanmedian(pad.reshape(bh, block, bw, block).transpose(0, 2, 1, 3).reshape(bh, bw, -1), axis=2)
    med = np.where(np.isfinite(med), med, np.nanmedian(a))
    surf = np.repeat(np.repeat(med, block, 0), block, 1)[:h, :w]
    return a - surf


# ------------------------------------------------------------------- NISAR ---

def step_nisar():
    files = sorted((RAW / "nisar" / "gunw").glob("*.h5"))
    if not files:
        raise RuntimeError("no GUNW files in data/raw/nisar/gunw (run download_data.py nisar --product gunw)")
    disp, coh = [], []
    for fp in files:
        log(f"NISAR GUNW {fp.name[:60]}...")
        with h5py.File(fp, "r") as f:
            p_unw = find(f, "unwrappedPhase", prefer=("frequencyA", "unwrappedInterferogram", "HH"), min_ndim=2)
            if not p_unw:
                raise RuntimeError("unwrappedPhase not found; run `inspect` on this file and send the output")
            grp = p_unw.rsplit("/", 1)[0]
            p_coh = f"{grp}/coherenceMagnitude" if f"{grp}/coherenceMagnitude" in f else find(f, "coherenceMagnitude", prefer=(grp,), min_ndim=2)
            p_cc = f"{grp}/connectedComponents" if f"{grp}/connectedComponents" in f else None
            log(f"  phase: {p_unw}\n  coherence: {p_coh}\n  components: {p_cc}")
            x, y = coords(f, p_unw)
            crs = crs_of(f, p_unw, x)
            phase = read2d(f[p_unw])
            c = read2d(f[p_coh]) if p_coh else np.full_like(phase, np.nan)
            if p_cc:
                cc = f[p_cc][()]
                phase[cc == 0] = np.nan
            if c.shape != phase.shape:  # coherence on a different grid -> skip it for masking
                log(f"  coherence grid {c.shape} != phase grid {phase.shape}; using phase only")
                cx, cy = coords(f, p_coh)
                coh.append(to_hex(c, cx, cy, crs_of(f, p_coh, cx), max(1, c.shape[0] // 2500)))
                c = np.full_like(phase, np.nan)
            else:
                coh.append(to_hex(c, x, y, crs, max(1, phase.shape[0] // 2500)))
                phase[c < 0.3] = np.nan
            # A single 12-day L-band pair is dominated by ionospheric/tropospheric delay (we saw +-100s of mm),
            # so we keep coherence (robust) and do not publish 12-day motion.
            log(f"  CRS {crs.to_epsg()}, coherence median {np.nanmedian(c) if np.isfinite(c).any() else float('nan'):.2f}")
    C = pd.concat(coh).groupby("hex_id").value.mean().rename("nisar_coherence")
    out = C.reset_index()
    out.to_parquet(INTERIM / "insar_nisar.parquet")
    log(f"NISAR: {len(out):,} hexes with coherence/motion")
    return out


# ------------------------------------------------------------------- OPERA ---

DATE_PAIR = re.compile(r"_(\d{8})T\d{6}Z_(\d{8})T\d{6}Z_")


def step_opera():
    files = sorted((RAW / "opera").rglob("*.nc"))
    if not files:
        raise RuntimeError("no OPERA DISP-S1 files in data/raw/opera")
    frames: dict[str, list] = {}
    for fp in files:                                   # download_data.py puts each frame in data/raw/opera/F<frame>/
        frames.setdefault(fp.parent.name, []).append(fp)
    outs = []
    for name, fs in sorted(frames.items()):
        log(f"OPERA frame {name}: {len(fs)} files")
        try:
            outs.append(_opera_frame(fs))
        except RuntimeError as e:
            log(f"  frame {name} skipped: {e}")
    if not outs:
        raise RuntimeError("no usable OPERA frames")
    out = pd.concat(outs).groupby("hex_id", as_index=False).mean()   # overlapping frames: average per hex
    out.to_parquet(INTERIM / "insar_opera.parquet")
    log(f"OPERA: {len(out):,} hexes with a velocity from {len(outs)} frame(s)")
    return out


def _opera_frame(files):
    rates, weights, cohs, grid = [], [], [], None
    for fp in files:
        m = DATE_PAIR.search(fp.name)
        if not m:
            log(f"  skip {fp.name} (no dates in name)")
            continue
        ref, sec = (datetime.strptime(s, "%Y%m%d") for s in m.groups())
        dt_yr = (sec - ref).days / 365.25
        if dt_yr < 45 / 365.25:
            log(f"  skip {fp.name[:60]} ({(sec - ref).days} d pair: too short for a rate)")
            continue
        with h5py.File(fp, "r") as f:
            if grid is None:
                log("  datasets: " + ", ".join(p.split("/")[-1] for p, shp, _ in datasets(f) if len(shp) == 2))
            p_sw = find(f, "short_wavelength_displacement", min_ndim=2)
            p = p_sw or find(f, "displacement", min_ndim=2)
            if not p:
                raise RuntimeError(f"'displacement' not in {fp.name}; run inspect")
            d = read2d(f[p]) * 1000.0                                   # m -> mm
            pc = find(f, "connected_component_labels", min_ndim=2)
            if pc and f[pc].shape == d.shape:
                d[f[pc][()] == 0] = np.nan
            for mname in ("recommended_mask", "water_mask"):
                pm = find(f, mname, min_ndim=2)
                if pm and f[pm].shape == d.shape:
                    d[f[pm][()] == 0] = np.nan
            pt = find(f, "temporal_coherence", min_ndim=2)
            tc = read2d(f[pt]) if pt and f[pt].shape == d.shape else None
            if tc is not None:
                d[tc < 0.5] = np.nan
            if grid is None:
                x, y = coords(f, p)
                grid = (x, y, crs_of(f, p, x), d.shape)
                log(f"  using '{p}'" + ("" if p_sw else " + 12 km high-pass"))
            elif d.shape != grid[3]:
                log(f"  skip {fp.name}: grid {d.shape} != {grid[3]}")
                continue
        d -= np.nanmedian(d)                                            # common reference per pair
        if not p_sw:
            d = highpass(d, max(8, int(12000 / abs(float(grid[0][1] - grid[0][0])))))
        rates.append(d / dt_yr)
        weights.append(dt_yr ** 2)      # rate error ~ 1/dt  ->  weight dt^2
        if tc is not None:
            cohs.append(tc)
        log(f"  {fp.name[:70]}  {ref:%Y-%m-%d}->{sec:%Y-%m-%d} ({dt_yr * 365.25:.0f} d)")
    if not rates:
        raise RuntimeError("no usable OPERA files")
    R = np.stack(rates)
    W = np.array(weights)[:, None, None] * np.isfinite(R)
    v = np.nansum(np.nan_to_num(R) * W, axis=0) / np.where(W.sum(0) > 0, W.sum(0), np.nan)
    v[np.isfinite(R).sum(0) < 3] = np.nan          # need >= 3 pairs per pixel
    x, y, crs, shape = grid
    stride = max(1, shape[0] // 2500)
    log(f"OPERA: {len(rates)} pairs; LOS velocity p5/p50/p95 = {np.nanpercentile(v, 5):.1f}/{np.nanpercentile(v, 50):.1f}/"
        f"{np.nanpercentile(v, 95):.1f} mm/yr (CRS {crs.to_epsg()})")
    out = to_hex(v, x, y, crs, stride).rename(columns={"value": "subsidence_mm_yr"})
    if cohs:
        out = out.merge(to_hex(np.nanmean(np.stack(cohs), 0), x, y, crs, stride).rename(columns={"value": "opera_coherence"}),
                        on="hex_id", how="left")
    log(f"  {len(out):,} hexes")
    return out


# ----------------------------------------------------------------- overlay ---

def step_overlay():
    import rasterio
    from PIL import Image
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    files = sorted((RAW / "nisar" / "gcov").glob("*.h5"))
    if not files:
        raise RuntimeError("no GCOV file in data/raw/nisar/gcov")
    fp = files[0]
    log(f"NISAR GCOV overlay from {fp.name[:60]}...")
    with h5py.File(fp, "r") as f:
        p = find(f, "HVHV", prefer=("frequencyA",), min_ndim=2) or find(f, "HHHH", prefer=("frequencyA",), min_ndim=2)
        if not p:
            raise RuntimeError("HVHV/HHHH not found; run inspect")
        ds = f[p]
        stride = max(1, ds.shape[0] // 3000)
        hv = read2d(ds, stride)
        x, y = coords(f, p)
        crs = crs_of(f, p, x)
        x, y = x[::stride], y[::stride]
        m = re.search(r"_(\d{8})T\d{6}_", fp.name)
        date = datetime.strptime(m.group(1), "%Y%m%d").strftime("%Y-%m-%d") if m else None
    log(f"  {p} {hv.shape} (stride {stride}), CRS {crs.to_epsg()}")
    db = 10 * np.log10(np.where(hv > 0, hv, np.nan))
    lo, hi = np.nanpercentile(db, 2), np.nanpercentile(db, 98)
    dx, dy = float(x[1] - x[0]), float(y[1] - y[0])
    src_tr = from_origin(float(x[0]) - dx / 2, float(y[0]) - dy / 2, abs(dx), abs(dy)) if dy < 0 else None
    if src_tr is None:  # y ascending -> flip
        db, y, dy = db[::-1], y[::-1], -dy
        src_tr = from_origin(float(x[0]) - dx / 2, float(y[0]) - dy / 2, abs(dx), abs(dy))
    h, w = db.shape
    west_s, south_s, east_s, north_s = rasterio.transform.array_bounds(h, w, src_tr)
    dst_tr, dw, dh = calculate_default_transform(crs.to_wkt(), "EPSG:4326", w, h, west_s, south_s, east_s, north_s)
    out = np.full((dh, dw), np.nan, dtype="float32")
    reproject(db.astype("float32"), out, src_transform=src_tr, src_crs=crs.to_wkt(), dst_transform=dst_tr,
              dst_crs="EPSG:4326", resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan)
    g = np.nan_to_num(np.clip((out - lo) / (hi - lo), 0, 1))
    rgba = np.zeros((dh, dw, 4), dtype="uint8")
    rgba[..., 1] = (g * 255).astype("uint8")                 # vegetation-sensitive HV in green tones
    rgba[..., 0] = (g * 120).astype("uint8")
    rgba[..., 2] = (g * 90).astype("uint8")
    rgba[..., 3] = np.where(np.isfinite(out), 210, 0)
    Image.fromarray(rgba, "RGBA").save(OVERLAYS / "nisar_hv.png", optimize=True)
    west, north = dst_tr.c, dst_tr.f
    east, south = west + dst_tr.a * dw, north + dst_tr.e * dh
    meta = [{"name": "nisar_hv", "label": "NISAR L-band radar (HV, vegetation/structure)", "url": "/static/overlays/nisar_hv.png",
             "bounds": [[round(south, 5), round(west, 5)], [round(north, 5), round(east, 5)]], "date": date, "source": "nisar",
             "legend": {"low": f"{lo:.0f} dB (smooth: water, pavement, bare)", "high": f"{hi:.0f} dB (forest, structures)"}}]
    (OVERLAYS / "overlays.json").write_text(json.dumps(meta, indent=2))
    log(f"  wrote nisar_hv.png {dw}x{dh}, bounds {meta[0]['bounds']}")


# -------------------------------------------------------------------- main ---

def merge_features():
    parts = [pd.read_parquet(p) for p in (INTERIM / "insar_opera.parquet", INTERIM / "insar_nisar.parquet") if p.exists()]
    if not parts:
        return
    out = parts[0]
    for p in parts[1:]:
        out = out.merge(p, on="hex_id", how="outer")
    out.to_parquet(INTERIM / "feat_insar.parquet")
    log(f"wrote feat_insar.parquet: {len(out):,} hexes, columns {list(out.columns)[1:]}")
    log("next: python scripts/build_grid.py --only score,layers")


def inspect(path: str):
    with h5py.File(path, "r") as f:
        for p, shape, dt in datasets(f):
            if len(shape) >= 1 or any(k in p.lower() for k in ("projection", "epsg", "spatial_ref", "wavelength")):
                attrs = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in f[p].attrs.items()
                         if k in ("units", "epsg_code", "_FillValue", "grid_mapping", "description")}
                print(f"{p}  {shape}  {dt}  {attrs if attrs else ''}"[:220])


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "inspect":
        return inspect(sys.argv[2])
    steps = {"opera": step_opera, "nisar": step_nisar, "overlay": step_overlay}
    todo = list(steps) if cmd == "all" else [cmd]
    for name in todo:
        try:
            steps[name]()
            log(f"OK {name}")
        except Exception as e:
            log(f"FAILED {name}: {type(e).__name__}: {e}")
    merge_features()


if __name__ == "__main__":
    main()
