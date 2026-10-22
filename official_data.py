"""Load and normalize official building and road inputs supplied by the user."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import pyogrio


RESIDENTIAL_USE_CODES = {"01000", "02000"}
RESIDENTIAL_USE_NAMES = {"단독주택", "공동주택"}


def repair_korean_mojibake(value: object) -> object:
    """Repair CP949 bytes that a malformed DBF declaration exposed as Latin-1.

    Correct Unicode values and non-string values are returned unchanged.
    """

    if not isinstance(value, str):
        return value
    try:
        repaired = value.encode("latin1").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return repaired


def official_building_category(use_code: object, use_name: object) -> str:
    code = "" if pd.isna(use_code) else str(use_code).strip()
    name = "" if pd.isna(use_name) else str(use_name).strip()
    if code in RESIDENTIAL_USE_CODES or name in RESIDENTIAL_USE_NAMES:
        return "residential"
    if not code and not name:
        return "unknown"
    return "other"


def _dated_candidates(input_dir: Path, patterns: tuple[str, ...]) -> list[Path]:
    candidates: set[Path] = set()
    for pattern in patterns:
        candidates.update(input_dir.rglob(pattern))
    return sorted(
        candidates,
        key=lambda path: (
            max((int(item) for item in re.findall(r"(?<!\d)(\d{8})(?!\d)", path.name)), default=0),
            str(path),
        ),
        reverse=True,
    )


def find_latest_building_shp(input_dir: Path) -> Path | None:
    candidates = _dated_candidates(input_dir, ("AL_D010_26_*.shp", "AL_26_D010_*.shp"))
    return candidates[0] if candidates else None


def load_official_buildings(
    input_dir: Path,
    boundary: gpd.GeoDataFrame,
    metric_crs: str,
    *,
    search_margin_m: float = 0.0,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    """Read the target dong, optionally including a surrounding context margin."""

    path = find_latest_building_shp(input_dir)
    if path is None:
        raise FileNotFoundError("No AL_D010 GIS building integrated-information SHP found")
    info = pyogrio.read_info(path)
    source_crs = info.get("crs")
    if not source_crs:
        raise ValueError(f"Official building file has no CRS: {path}")
    available = set(map(str, info.get("fields", [])))
    required = {"A1", "A8", "A9"}
    if not required.issubset(available):
        raise ValueError(f"Official building schema lacks {sorted(required - available)}: {path}")
    wanted = [
        column
        for column in ("A0", "A1", "A2", "A3", "A4", "A8", "A9", "A22", "A28")
        if column in available
    ]
    boundary_metric = boundary.to_crs(metric_crs)
    clip_mask = boundary_metric.geometry.iloc[0].buffer(float(search_margin_m))
    source_boundary = gpd.GeoSeries([clip_mask], crs=metric_crs).to_crs(source_crs)
    frame = pyogrio.read_dataframe(
        path,
        bbox=tuple(map(float, source_boundary.total_bounds)),
        columns=wanted,
    )
    bbox_features = len(frame)
    if frame.crs is None:
        frame = frame.set_crs(source_crs)
    for column in ("A4", "A9"):
        if column in frame.columns:
            frame[column] = frame[column].map(repair_korean_mojibake)
    frame = frame.loc[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
    frame.geometry = frame.geometry.make_valid()
    frame = frame.loc[frame.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].to_crs(
        metric_crs
    )
    frame = frame.loc[frame.geometry.intersects(clip_mask)].copy()
    frame = gpd.clip(frame, clip_mask, keep_geom_type=True)
    frame["building_use_name"] = frame["A9"]
    frame["building_category"] = frame.apply(
        lambda row: official_building_category(row.get("A8"), row.get("A9")), axis=1
    )
    frame["building_id"] = frame["A1"].astype(str)
    frame["source_file"] = str(path)
    match = re.search(r"(?<!\d)(\d{8})(?!\d)", path.name)
    version = match.group(1) if match else None
    metadata = {
        "source": "국토교통부 GIS건물통합정보",
        "path": str(path),
        "version": version,
        "source_crs": str(source_crs),
        "source_features": int(info.get("features") or 0),
        "bbox_features": int(bbox_features),
        "target_features": int(len(frame)),
        "residential_features": int(frame["building_category"].eq("residential").sum()),
        "unknown_use_features": int(frame["building_category"].eq("unknown").sum()),
        "search_margin_m": float(search_margin_m),
    }
    return frame, metadata


def load_official_roads(
    input_dir: Path,
    boundary: gpd.GeoDataFrame,
    metric_crs: str,
    *,
    search_margin_m: float = 25.0,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    """Load the classified 2026 road-name sections containing ROAD_BT."""

    candidates = sorted(input_dir.rglob("roads_classified.parquet"))
    if not candidates:
        raise FileNotFoundError("No roads_classified.parquet with ROAD_BT found")
    path = candidates[0]
    frame = gpd.read_parquet(path)
    required = {"ROAD_BT", "road_width_m", "geometry"}
    if not required.issubset(frame.columns):
        raise ValueError(f"Official road schema lacks {sorted(required - set(frame.columns))}: {path}")
    if frame.crs is None:
        raise ValueError(f"Official road dataset has no CRS: {path}")
    frame = frame.to_crs(metric_crs)
    frame["road_width_m"] = pd.to_numeric(frame["road_width_m"], errors="coerce")
    valid_width = frame["road_width_m"].gt(0) & frame["road_width_m"].le(40)
    frame.loc[~valid_width, "road_width_m"] = pd.NA
    boundary_geom = boundary.to_crs(metric_crs).geometry.iloc[0]
    clip_mask = boundary_geom.buffer(search_margin_m)
    frame = frame.loc[frame.geometry.intersects(clip_mask)].copy()
    frame = gpd.clip(frame, clip_mask, keep_geom_type=True)
    metadata = {
        "source": "도로명주소 도로구간(ROAD_BT) 분류본",
        "path": str(path),
        "version": "202606",
        "source_crs": str(frame.crs),
        "target_or_margin_features": int(len(frame)),
        "valid_width_features": int(frame["road_width_m"].notna().sum()),
    }
    return frame, metadata


def nearest_official_road(
    point: Any,
    roads: gpd.GeoDataFrame,
    *,
    max_distance_m: float,
) -> dict[str, Any] | None:
    if roads.empty:
        return None
    indexes, distances = roads.sindex.nearest(
        point,
        return_all=False,
        max_distance=max_distance_m,
        return_distance=True,
    )
    if indexes.shape[1] == 0:
        return None
    row = roads.iloc[int(indexes[1, 0])]
    width = pd.to_numeric(pd.Series([row.get("road_width_m")]), errors="coerce").iloc[0]
    return {
        "road_width_m": None if pd.isna(width) else float(width),
        "road_name": None if pd.isna(row.get("RN")) else str(row.get("RN")),
        "distance_m": float(distances[0]),
        "source": "official_ROAD_BT",
    }
