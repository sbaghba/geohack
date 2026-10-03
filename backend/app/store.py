"""Loads the prebuilt grid + point layers once and answers spatial lookups (no GIS stack needed at runtime)."""
from __future__ import annotations

import logging
from functools import cached_property, lru_cache

import h3
import numpy as np
import pandas as pd

from .config import settings

log = logging.getLogger("sitesense.store")
EARTH_R = 6371.0088
RES = 7


def haversine_km(lat, lon, plat, plon):
    lat, lon, plat, plon = map(np.radians, (lat, lon, plat, plon))
    a = np.sin((plat - lat) / 2) ** 2 + np.cos(lat) * np.cos(plat) * np.sin((plon - lon) / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(a))


class Store:
    @property
    def available(self) -> bool:
        return settings.grid_path.exists()

    @cached_property
    def grid(self) -> pd.DataFrame:
        g = pd.read_parquet(settings.grid_path).set_index("hex_id")
        log.info("grid loaded: %d hexes", len(g))
        return g

    @cached_property
    def counties(self) -> pd.DataFrame:
        p = settings.grid_path.parent / "counties_nc.parquet"
        return pd.read_parquet(p).set_index("county") if p.exists() else pd.DataFrame()

    @cached_property
    def states(self):
        from shapely import STRtree, from_wkb

        p = settings.grid_path.parent / "us_states.parquet"
        if not p.exists():
            return None
        df = pd.read_parquet(p)
        geoms = from_wkb(df.wkb.values)
        return df.stusps.tolist(), geoms, STRtree(geoms)

    def points(self, name: str) -> pd.DataFrame:
        return _points(str(settings.data_dir / "points" / f"{name}.parquet"))

    def row(self, hex_id: str):
        try:
            return self.grid.loc[hex_id]
        except KeyError:
            return None

    def hex_of(self, lat: float, lon: float) -> str:
        return h3.latlng_to_cell(lat, lon, RES)

    def state_of(self, lat: float, lon: float) -> str | None:
        if self.states is None:
            return None
        from shapely import Point

        names, geoms, tree = self.states
        for i in tree.query(Point(lon, lat), predicate="intersects"):
            return names[int(i)]
        return None

    def nearest(self, name: str, lat: float, lon: float, min_kv: float | None = None):
        df = self.points(name)
        if df.empty:
            return None, None, None
        if min_kv is not None and "kv" in df:
            big = df[df.kv >= min_kv]
            df = big if len(big) >= 50 or name == "lines" else df
            if df.empty:
                return None, None, None
        d = haversine_km(lat, lon, df.lat.values, df.lon.values)
        i = int(np.argmin(d))
        r = df.iloc[i]
        return float(d[i]), (None if pd.isna(r.get("kv", np.nan)) else float(r["kv"])), r

    def within(self, name: str, lat: float, lon: float, km: float) -> list[tuple[float, pd.Series]]:
        df = self.points(name)
        if df.empty:
            return []
        d = haversine_km(lat, lon, df.lat.values, df.lon.values)
        idx = np.where(d <= km)[0]
        return sorted(((float(d[i]), df.iloc[i]) for i in idx), key=lambda t: t[0])


@lru_cache(maxsize=16)
def _points(path: str) -> pd.DataFrame:
    try:
        return pd.read_parquet(path)
    except FileNotFoundError:
        return pd.DataFrame(columns=["lat", "lon", "name", "kv"])


store = Store()
