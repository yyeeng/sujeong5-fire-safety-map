"""Generate evidence-backed candidate zones and a Folium review map."""

from __future__ import annotations

import argparse
import html
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

import folium
import geopandas as gpd
import osmnx as ox
import pandas as pd
import yaml
from shapely import union_all

from analysis_core import (
    firetruck_difficulty_score,
    parse_width_m,
    residential_density_scores,
    visibility_accessibility_score,
    weighted_score,
)
from official_data import (
    find_latest_building_shp,
    load_official_buildings,
    load_official_roads,
    nearest_official_road,
)


RESIDENTIAL_BUILDING_TYPES = {
    "apartments",
    "residential",
    "house",
    "detached",
    "semidetached_house",
    "terrace",
    "bungalow",
    "dormitory",
}
FOOT_ALLEY_TYPES = {"steps", "footway", "path", "pedestrian", "living_street"}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def jsonish_values(value: object) -> list[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    text = str(value).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except json.JSONDecodeError:
            pass
    return [text]


def tag_contains(value: object, expected: set[str]) -> bool:
    return bool({item.lower() for item in jsonish_values(value)} & expected)


def clean_for_geojson(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    clean = frame.copy()
    def clean_value(value: object) -> object:
        if isinstance(value, (list, tuple, set, dict)):
            return json.dumps(value, ensure_ascii=False)
        if pd.isna(value):
            return None
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        return value

    for column in clean.columns:
        if column == clean.geometry.name:
            continue
        clean[column] = clean[column].map(clean_value)
    return clean


def profile_fire_water_status(zip_path: Path) -> dict[str, Any]:
    """Profile the supplied official fire-water ZIP without inventing locations."""
    result: dict[str, Any] = {
        "found": zip_path.exists(),
        "source_file": zip_path.name,
        "reference_year": 2025,
        "used_for_mapping": False,
        "used_for_scoring": False,
    }
    if not zip_path.exists():
        result["error"] = "file_not_found"
        return result
    try:
        frame = pd.read_csv(
            zip_path,
            compression="zip",
            encoding="utf-8-sig",
            dtype=str,
            keep_default_na=False,
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    frame.columns = [str(column).strip() for column in frame.columns]
    for column in frame.columns:
        frame[column] = frame[column].astype(str).str.strip()
    required = {
        "sn",
        "fruswtr_se_nm",
        "grnds_sgg_nm",
        "li_nm",
        "hnum_nm",
        "stts_se_nm",
        "damg_rgn_lot",
        "damg_rgn_lat",
        "road_nm_addr",
    }
    missing_columns = sorted(required - set(frame.columns))
    if missing_columns:
        result["error"] = f"missing_columns: {', '.join(missing_columns)}"
        return result

    longitude = pd.to_numeric(frame["damg_rgn_lot"], errors="coerce")
    latitude = pd.to_numeric(frame["damg_rgn_lat"], errors="coerce")
    coordinate_complete = longitude.notna() & latitude.notna()
    plausible_busan_coordinate = coordinate_complete & longitude.between(
        128.7, 129.4
    ) & latitude.between(34.8, 35.5)
    dong_gu = frame.loc[frame["grnds_sgg_nm"].eq("동구")].copy()
    dong_coordinate_complete = pd.to_numeric(
        dong_gu["damg_rgn_lot"], errors="coerce"
    ).notna() & pd.to_numeric(dong_gu["damg_rgn_lat"], errors="coerce").notna()
    address_counts = frame["road_nm_addr"].value_counts(dropna=False)
    dominant_address = "" if address_counts.empty else str(address_counts.index[0])
    dominant_address_rows = 0 if address_counts.empty else int(address_counts.iloc[0])
    type_counts = {
        str(name): int(count)
        for name, count in frame["fruswtr_se_nm"].value_counts().items()
    }
    dong_type_counts = {
        str(name): int(count)
        for name, count in dong_gu["fruswtr_se_nm"].value_counts().items()
    }
    result.update(
        {
            "rows": int(len(frame)),
            "columns": int(len(frame.columns)),
            "unique_sn": int(frame["sn"].nunique()),
            "duplicate_sn_rows": int(frame.duplicated("sn", keep=False).sum()),
            "exact_duplicate_rows": int(frame.duplicated().sum()),
            "facility_type_counts": type_counts,
            "coordinate_complete_rows": int(coordinate_complete.sum()),
            "coordinate_complete_rate": round(float(coordinate_complete.mean()), 6),
            "plausible_busan_coordinate_rows": int(plausible_busan_coordinate.sum()),
            "li_name_missing_rows": int(frame["li_nm"].eq("").sum()),
            "li_name_missing_rate": round(float(frame["li_nm"].eq("").mean()), 6),
            "distinct_road_addresses": int(frame["road_nm_addr"].nunique()),
            "dominant_road_address": dominant_address,
            "dominant_road_address_rows": dominant_address_rows,
            "dominant_road_address_rate": round(dominant_address_rows / len(frame), 6),
            "dong_gu": {
                "rows": int(len(dong_gu)),
                "facility_type_counts": dong_type_counts,
                "coordinate_complete_rows": int(dong_coordinate_complete.sum()),
                "li_name_present_rows": int(dong_gu["li_nm"].ne("").sum()),
                "parcel_number_present_rows": int(dong_gu["hnum_nm"].ne("").sum()),
                "distinct_road_addresses": int(dong_gu["road_nm_addr"].nunique()),
                "dominant_road_address_rows": int(
                    dong_gu["road_nm_addr"].eq(dominant_address).sum()
                ),
                "usable_status_rows": int(dong_gu["stts_se_nm"].eq("사용가").sum()),
            },
            "mapping_decision": (
                "excluded: 동구 레코드에 좌표가 없고 도로명주소가 단일 반복값이라 "
                "후보별 최근접 시설을 계산할 수 없음"
            ),
            "scope_note": "소화전·저수조 등 소방용수 현황이며 보이는 소화기 설치 현황이 아님",
        }
    )
    return result


def load_pnu_fire_infrastructure(
    parquet_path: Path,
    boundary: gpd.GeoDataFrame,
    metric_crs: str,
    context_margin_m: float,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    """Load only usable parcel/reference coordinates from the PNU-ANCHOR dataset.

    Fallback coordinates are retained in the quality counts but never mapped or
    used to exclude a candidate. Parcel/reference coordinates are still parcel
    representatives, not surveyed cabinet or hydrant positions.
    """
    empty = gpd.GeoDataFrame(geometry=[], crs=metric_crs)
    quality: dict[str, Any] = {
        "found": parquet_path.exists(),
        "source_file": str(parquet_path),
        "mapped_quality_tier": "parcel_or_reference",
        "fallback_used_for_mapping_or_scoring": False,
    }
    if not parquet_path.exists():
        quality["error"] = "file_not_found"
        return empty, quality
    try:
        frame = pd.read_parquet(parquet_path)
    except Exception as exc:
        quality["error"] = f"{type(exc).__name__}: {exc}"
        return empty, quality

    required = {
        "asset_type",
        "usable_flag",
        "latitude",
        "longitude",
        "coordinate_quality_tier",
        "coordinate_fallback_used",
        "candidate_jibun_address",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        quality["error"] = f"missing_columns: {', '.join(missing)}"
        return empty, quality

    frame = frame.copy()
    frame["latitude"] = pd.to_numeric(frame["latitude"], errors="coerce")
    frame["longitude"] = pd.to_numeric(frame["longitude"], errors="coerce")
    plausible = frame["longitude"].between(128.7, 129.4) & frame["latitude"].between(
        34.8, 35.5
    )
    frame = frame.loc[plausible].copy()
    all_points = gpd.GeoDataFrame(
        frame,
        geometry=gpd.points_from_xy(frame["longitude"], frame["latitude"]),
        crs="EPSG:4326",
    ).to_crs(metric_crs)
    target_geometry = boundary.geometry.iloc[0]
    context_geometry = target_geometry.buffer(float(context_margin_m))
    in_target = all_points.geometry.map(target_geometry.covers)
    in_context = all_points.geometry.map(context_geometry.covers)
    exact_usable = (
        all_points["coordinate_quality_tier"].eq("parcel_or_reference")
        & all_points["usable_flag"].fillna(False).astype(bool)
    )
    mapped = all_points.loc[in_context & exact_usable].copy()
    mapped["inside_target"] = mapped.geometry.map(target_geometry.covers)

    tier_counts = {
        str(name): int(count)
        for name, count in all_points["coordinate_quality_tier"].value_counts().items()
    }
    quality.update(
        {
            "rows": int(len(all_points)),
            "coordinate_quality_tier_counts": tier_counts,
            "fallback_coordinate_rows": int(
                all_points["coordinate_fallback_used"].fillna(False).astype(bool).sum()
            ),
            "target_rows_all_quality": int(in_target.sum()),
            "context_rows_all_quality": int(in_context.sum()),
            "target_parcel_or_reference_usable": int((in_target & exact_usable).sum()),
            "context_parcel_or_reference_usable": int((in_context & exact_usable).sum()),
            "target_asset_type_counts": {
                str(name): int(count)
                for name, count in all_points.loc[in_target & exact_usable, "asset_type"]
                .value_counts()
                .items()
            },
            "context_asset_type_counts": {
                str(name): int(count)
                for name, count in mapped["asset_type"].value_counts().items()
            },
            "scope_note": (
                "fire_water는 소방용수, emergency_fire_equipment는 비상소화장치입니다. "
                "parcel_or_reference 좌표도 실제 함체 실측점이 아니라 필지/참조 위치입니다."
            ),
        }
    )
    return mapped, quality


def load_busan_map_context(
    root: Path, metric_crs: str
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, dict[str, Any]]:
    """Load a lightweight, tile-free Busan overview for the resident map."""
    boundary_path = root / "input/data_archive/boundary/BND_ADM_DONG_PG.shp"
    roads_path = root / "input/data_archive/processed/roads_classified.parquet"
    empty = gpd.GeoDataFrame(geometry=[], crs=metric_crs)
    quality: dict[str, Any] = {
        "boundary_path": str(boundary_path),
        "roads_path": str(roads_path),
        "major_road_width_m": 12.0,
    }
    if not boundary_path.exists() or not roads_path.exists():
        missing = [
            str(path)
            for path in (boundary_path, roads_path)
            if not path.exists()
        ]
        quality["error"] = f"missing_files: {', '.join(missing)}"
        return empty, empty.copy(), quality

    try:
        all_dongs = gpd.read_file(boundary_path)
        if "ADM_CD" not in all_dongs.columns:
            raise ValueError("BND_ADM_DONG_PG.shp에 ADM_CD 필드가 없습니다.")
        busan_dongs = all_dongs.loc[
            all_dongs["ADM_CD"].astype(str).str.startswith("21"),
            [column for column in ("ADM_CD", "ADM_NM", "BASE_DATE", "geometry") if column in all_dongs.columns],
        ].copy()
        busan_dongs = busan_dongs.to_crs(metric_crs)
        busan_dongs.geometry = busan_dongs.geometry.simplify(8.0, preserve_topology=True)

        all_roads = gpd.read_parquet(roads_path).to_crs(metric_crs)
        all_roads["road_width_m"] = pd.to_numeric(
            all_roads.get("road_width_m"), errors="coerce"
        )
        busan_major_roads = all_roads.loc[
            all_roads["road_width_m"].ge(12.0),
            [
                column
                for column in ("RN", "ROA_CLS_SE", "road_width_m", "geometry")
                if column in all_roads.columns
            ],
        ].copy()
        busan_major_roads.geometry = busan_major_roads.geometry.simplify(
            5.0, preserve_topology=True
        )
        quality.update(
            {
                "status": "loaded",
                "busan_admin_dongs": int(len(busan_dongs)),
                "busan_major_roads": int(len(busan_major_roads)),
            }
        )
        return busan_dongs, busan_major_roads, quality
    except Exception as exc:
        quality["error"] = f"{type(exc).__name__}: {exc}"
        return empty, empty.copy(), quality


def graph_neighbor_counts(graph: Any) -> dict[Any, int]:
    neighbors: dict[Any, set[Any]] = defaultdict(set)
    for u, v in graph.edges():
        if u != v:
            neighbors[u].add(v)
            neighbors[v].add(u)
    return {node: len(neighbors.get(node, set())) for node in graph.nodes}


def building_category(value: object) -> str:
    tags = {item.lower() for item in jsonish_values(value)}
    if tags & RESIDENTIAL_BUILDING_TYPES:
        return "residential"
    if not tags or tags == {"yes"}:
        return "unknown"
    return "other"


def distance_to_union(geometry: Any, merged: Any | None) -> float | None:
    if merged is None or merged.is_empty:
        return None
    return float(geometry.distance(merged))


def load_optional_features(path: Path, crs: str) -> gpd.GeoDataFrame:
    if not path.exists():
        return gpd.GeoDataFrame(geometry=[], crs=crs)
    frame = gpd.read_file(path)
    if frame.crs is None:
        frame = frame.set_crs("EPSG:4326")
    return frame.to_crs(crs)


def apply_field_visibility(pool: pd.DataFrame, input_path: Path) -> tuple[pd.DataFrame, int]:
    pool = pool.copy()
    pool["C_source"] = "map_proxy"
    if not input_path.exists():
        return pool, 0
    field = pd.read_csv(input_path, dtype={"candidate_id": str})
    required = {"candidate_id", "c_score"}
    if not required.issubset(field.columns):
        raise ValueError("input/visibility_field.csv needs candidate_id,c_score columns")
    field["c_score"] = pd.to_numeric(field["c_score"], errors="coerce")
    if (~field["c_score"].dropna().between(1, 5)).any():
        raise ValueError("Every field c_score must be between 1 and 5")
    values = field.dropna(subset=["c_score"]).set_index("candidate_id")["c_score"]
    matched = pool["candidate_id"].isin(values.index)
    pool.loc[matched, "C_score"] = pool.loc[matched, "candidate_id"].map(values)
    pool.loc[matched, "C_source"] = "field_observation"
    return pool, int(matched.sum())


def select_spaced_candidates(
    pool: gpd.GeoDataFrame, count: int, min_spacing_m: float
) -> gpd.GeoDataFrame:
    if pool.empty:
        return pool.copy()
    sortable = pool.copy()
    sortable["_score_sort"] = pd.to_numeric(sortable["score_map"], errors="coerce").fillna(-1)
    sortable = sortable.sort_values(
        ["_score_sort", "A_score", "residential_buildings_50m", "C_score", "candidate_id"],
        ascending=[False, False, False, False, True],
        na_position="last",
    )
    selected_indexes: list[int] = []
    selected_geometries: list[Any] = []
    for index, row in sortable.iterrows():
        point = row.geometry
        if all(point.distance(chosen) >= min_spacing_m for chosen in selected_geometries):
            selected_indexes.append(index)
            selected_geometries.append(point)
        if len(selected_indexes) == count:
            break
    return sortable.loc[selected_indexes].drop(columns=["_score_sort"])


def road_style(feature: dict[str, Any]) -> dict[str, Any]:
    highway = str(feature.get("properties", {}).get("highway", ""))
    if "steps" in highway:
        return {"color": "#d7301f", "weight": 4, "opacity": 0.9}
    if any(value in highway for value in ("footway", "path", "pedestrian")):
        return {"color": "#f59e0b", "weight": 2.5, "opacity": 0.75}
    return {"color": "#94a3b8", "weight": 1.4, "opacity": 0.65}


def official_road_style(
    feature: dict[str, Any], narrow_threshold_m: float = 4.0
) -> dict[str, Any]:
    width = feature.get("properties", {}).get("road_width_m")
    color = "#64748b"
    try:
        if width is not None and float(width) < narrow_threshold_m:
            color = "#dc2626"
    except (TypeError, ValueError):
        pass
    return {"color": color, "weight": 2.2, "opacity": 0.8}


def local_building_style(feature: dict[str, Any]) -> dict[str, Any]:
    properties = feature.get("properties", {})
    category = str(properties.get("building_category", "unknown"))
    inside_target = bool(properties.get("inside_target"))
    if not inside_target:
        return {
            "color": "#d0ccc3",
            "weight": 0.4,
            "fillColor": "#e8e4dc",
            "fillOpacity": 0.8,
        }
    fill = {
        "residential": "#e6d8c9",
        "other": "#dddcd6",
        "unknown": "#ebe8e1",
    }.get(category, "#ebe8e1")
    return {
        "color": "#aaa59c",
        "weight": 0.55,
        "fillColor": fill,
        "fillOpacity": 0.92,
    }


def local_road_weight(feature: dict[str, Any], *, casing: bool) -> float:
    width = feature.get("properties", {}).get("road_width_m")
    try:
        width_value = float(width)
    except (TypeError, ValueError):
        width_value = 4.0
    inner = max(2.0, min(7.0, 1.4 + width_value * 0.38))
    return inner + 2.2 if casing else inner


def local_road_casing_style(feature: dict[str, Any]) -> dict[str, Any]:
    inside_target = bool(feature.get("properties", {}).get("inside_target"))
    return {
        "color": "#a9a59d" if inside_target else "#c8c4bb",
        "weight": local_road_weight(feature, casing=True),
        "opacity": 0.98,
        "lineCap": "round",
        "lineJoin": "round",
    }


def local_road_fill_style(feature: dict[str, Any]) -> dict[str, Any]:
    inside_target = bool(feature.get("properties", {}).get("inside_target"))
    return {
        "color": "#fffefa" if inside_target else "#fbfaf6",
        "weight": local_road_weight(feature, casing=False),
        "opacity": 1.0,
        "lineCap": "round",
        "lineJoin": "round",
    }


def busan_major_road_style(feature: dict[str, Any]) -> dict[str, Any]:
    width = feature.get("properties", {}).get("road_width_m")
    try:
        weight = max(0.8, min(2.4, float(width) / 10.0))
    except (TypeError, ValueError):
        weight = 1.0
    return {
        "color": "#ffffff",
        "weight": weight,
        "opacity": 0.92,
        "lineCap": "round",
        "lineJoin": "round",
    }


def dashboard_panel_html_v2(
    candidates_web: gpd.GeoDataFrame,
    *,
    map_name: str,
    marker_names: dict[int, str],
) -> str:
    cards: list[str] = []
    marker_cases: list[str] = []
    for _, row in candidates_web.sort_values("rank").iterrows():
        rank = int(row["rank"])
        road_name = (
            "도로명 미확인"
            if pd.isna(row.get("official_road_name"))
            else str(row.get("official_road_name"))
        )
        latitude = float(row.geometry.y)
        longitude = float(row.geometry.x)
        coordinate = f"{latitude:.6f}, {longitude:.6f}"
        directions_name = quote(f"보이는 소화기 {rank} {road_name}", safe="")
        directions_url = (
            f"https://map.kakao.com/link/to/{directions_name},"
            f"{latitude:.7f},{longitude:.7f}"
        )
        cards.append(
            f"""
            <article class="site-row" data-candidate-card="{rank}">
              <div class="site-card-body">
                <span class="site-rank">{rank}</span>
                <span class="site-copy">
                  <span class="site-title">{html.escape(road_name)}</span>
                  <span class="site-sub">{coordinate}</span>
                </span>
              </div>
              <div class="site-card-actions">
                <button type="button" onclick="focusCandidate({rank})">지도 보기</button>
                <a href="{directions_url}" target="_blank" rel="noopener noreferrer">길찾기 <span aria-hidden="true">↗</span></a>
              </div>
            </article>
            """
        )
        marker_cases.append(
            f"case {rank}: {map_name}.setView([{latitude:.7f}, {longitude:.7f}], 18); "
            f"{marker_names[rank]}.openPopup(); break;"
        )
    return f"""
    <style>
      :root {{--panel-width:clamp(380px,25vw,520px);--paper:#f7f6f2;--surface:#fff;--ink:#17201d;--muted:#6a746f;--line:#dce1de;--red:#c34238;--amber:#b67422;--blue:#34708a;--forest:#173d36;}}
      *,*::before,*::after {{box-sizing:border-box;}}
      html,body {{margin:0!important;overflow:hidden!important;font-family:"Segoe UI","Noto Sans KR",Arial,sans-serif;color:var(--ink);}}
      .folium-map {{width:100%!important;height:100vh!important;}}
      .leaflet-control-container {{font-family:inherit;}}
      .candidate-dashboard {{position:fixed;right:18px;top:18px;bottom:18px;width:var(--panel-width);z-index:10000;background:rgba(247,246,242,.97);border:1px solid rgba(23,61,54,.17);border-radius:16px;box-shadow:0 16px 42px rgba(33,43,39,.18);display:flex;flex-direction:column;overflow:hidden;backdrop-filter:blur(10px);}}
      .dashboard-head {{padding:22px 20px;background:var(--forest);color:#fff;}}
      .title-line {{display:flex;align-items:center;}}
      .dashboard-head h1 {{font-size:28px;line-height:1.25;margin:0;font-weight:750;letter-spacing:-.035em;}}
      .dashboard-tabs {{display:grid;grid-template-columns:1fr 1fr;gap:7px;background:#edf1ef;border-bottom:1px solid var(--line);padding:8px 12px;}}
      .dashboard-tab {{border:1px solid #cbd4d0;border-radius:9px;background:#fff;padding:14px 8px;font:700 17px inherit;color:#58645f;cursor:pointer;}}
      .dashboard-tab.is-active {{border-color:var(--forest);background:var(--forest);color:#fff;box-shadow:0 2px 7px rgba(23,61,54,.18);}}
      .dashboard-tab:focus-visible {{outline:2px solid var(--blue);outline-offset:-3px;}}
      .dashboard-scroll {{flex:1;min-height:0;overflow:auto;scrollbar-color:#b7bfbb transparent;scrollbar-width:thin;}}
      .dashboard-panel {{padding:16px 14px 26px;}}
      .dashboard-panel[hidden] {{display:none;}}
      .site-list {{display:grid;gap:8px;}}
      .site-row {{position:relative;background:#fff;border:1px solid var(--line);border-radius:11px;overflow:hidden;transition:border-color .15s,transform .15s;}}
      .site-row:hover {{border-color:#acb8b2;transform:translateY(-1px);}}
      .site-row.is-selected {{border-color:var(--forest);box-shadow:0 0 0 1px var(--forest);}}
      .site-card-body {{padding:14px 14px 13px;min-height:66px;}}
      .site-rank {{float:left;width:36px;height:36px;border-radius:9px;background:#f7e9e7;display:grid;place-items:center;font:750 14px ui-monospace,SFMono-Regular,Consolas,monospace;color:#9c3029;}}
      .site-copy {{display:block;margin-left:48px;min-width:0;position:relative;}}
      .site-title {{display:block;font-size:18px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
      .site-sub {{display:block;margin-top:5px;font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted);}}
      .site-card-actions {{display:grid;grid-template-columns:1fr 1fr;border-top:1px solid var(--line);}}
      .site-card-actions button,.site-card-actions a {{display:block;border:0;background:#fff;padding:13px 7px;text-align:center;color:#285d52;font:650 15px inherit;text-decoration:none;cursor:pointer;}}
      .site-card-actions a {{border-left:1px solid var(--line);}}
      .site-card-actions button:hover,.site-card-actions a:hover,.site-card-actions button:focus-visible,.site-card-actions a:focus-visible {{background:#edf3f0;outline:0;}}
      .section-title {{font-size:21px;margin:2px 0 15px;}}
      .video-link-card {{display:flex;align-items:center;gap:14px;margin-bottom:15px;padding:16px;border-radius:10px;background:var(--forest);color:#fff;text-decoration:none;}}
      .video-play {{width:42px;height:42px;border:1px solid rgba(255,255,255,.5);border-radius:50%;display:grid;place-items:center;font-size:16px;padding-left:2px;}}
      .video-link-copy {{flex:1;}}
      .video-link-copy b {{display:block;font-size:17px;}}
      .video-link-copy span {{display:block;margin-top:4px;color:rgba(255,255,255,.72);font-size:13px;}}
      .video-arrow {{font-size:19px;color:rgba(255,255,255,.72);}}
      .usage-steps {{display:grid;gap:10px;}}
      .usage-step {{display:grid;grid-template-columns:104px 1fr;gap:14px;align-items:center;padding:12px;background:#fff;border:1px solid var(--line);border-radius:11px;}}
      .step-art {{width:104px;height:104px;border-radius:50%;background-image:url('../assets/extinguisher_4_steps.png');background-repeat:no-repeat;background-size:400% auto;background-color:#f6f0e4;border:1px solid #d6ddd8;}}
      .step-art.step-1 {{background-position:0% 50%;}}
      .step-art.step-2 {{background-position:33.333% 50%;}}
      .step-art.step-3 {{background-position:66.667% 50%;}}
      .step-art.step-4 {{background-position:100% 50%;}}
      .step-heading {{display:flex;align-items:center;gap:7px;}}
      .usage-number {{width:28px;height:28px;border-radius:7px;background:#e8eeeb;color:var(--forest);display:grid;place-items:center;font:750 14px/1 ui-monospace,SFMono-Regular,Consolas,monospace;}}
      .usage-step b {{font-size:17px;}}
      .usage-step p {{margin:7px 0 0;color:var(--muted);font-size:15px;line-height:1.55;}}
      .safety-alert {{margin-top:15px;padding:14px;border-left:4px solid var(--red);background:#f5eeec;color:#5f2c28;font-size:15px;line-height:1.6;}}
      .action-guide {{margin-top:18px;}}
      .action-guide h3 {{margin:0 0 12px;font-size:20px;}}
      .action-list {{display:grid;grid-template-columns:1fr;gap:9px;}}
      .action-item {{min-height:118px;padding:11px;display:grid;grid-template-columns:96px 1fr;gap:13px;align-items:center;border-radius:10px;background:#fff;border:1px solid var(--line);}}
      .action-art {{width:96px;height:96px;border-radius:50%;background-image:url('../assets/fire_actions_8_steps.png');background-repeat:no-repeat;background-size:400% auto;background-color:#f6f0e4;}}
      .action-art-1 {{background-position:0 -30px;}}
      .action-art-2 {{background-position:33.333% -30px;}}
      .action-art-3 {{background-position:66.667% -30px;}}
      .action-art-4 {{background-position:100% -30px;}}
      .action-art-5 {{background-position:0 -130px;}}
      .action-art-6 {{background-position:33.333% -130px;}}
      .action-art-7 {{background-position:66.667% -130px;}}
      .action-art-8 {{background-position:100% -130px;}}
      .action-item b {{display:block;font-size:17px;}}
      .action-item p {{margin:7px 0 0;color:var(--muted);font-size:14px;line-height:1.55;}}
      .source-note {{margin-top:13px;font-size:14px;line-height:1.55;color:var(--muted);}}
      .source-note a {{color:var(--blue);}}
      .dashboard-footer {{padding:10px 14px;border-top:1px solid var(--line);background:#fff;color:#75807b;font-size:12px;}}
      .map-key {{font-size:14px!important;line-height:1.65!important;}}
      @media (max-width:900px) {{
        :root {{--panel-width:100%;}}
        .folium-map {{width:100%!important;height:58vh!important;}}
        .candidate-dashboard {{left:8px!important;right:auto!important;top:56vh;bottom:8px;width:calc(100% - 16px)!important;height:auto;border-radius:15px;}}
        .dashboard-head {{padding:10px 12px 9px;}}
        .dashboard-head h1 {{font-size:24px;}}
        .dashboard-panel {{padding:9px 10px 18px;}}
        .map-key {{top:82px!important;bottom:auto!important;left:8px!important;max-width:230px;}}
        .action-list {{grid-template-columns:1fr;}}
      }}
    </style>
    <aside class="candidate-dashboard" aria-label="우리동네 화재안전 지도">
      <header class="dashboard-head">
        <div class="title-line"><h1>우리동네 화재안전 지도</h1></div>
      </header>
      <nav class="dashboard-tabs" aria-label="대시보드 보기">
        <button type="button" class="dashboard-tab is-active" data-dashboard-tab="usage" aria-selected="true" onclick="showDashboardTab('usage')">사용·대피</button>
        <button type="button" class="dashboard-tab" data-dashboard-tab="locations" aria-selected="false" onclick="showDashboardTab('locations')">보이는 소화기</button>
      </nav>
      <div class="dashboard-scroll">
        <section class="dashboard-panel" data-dashboard-panel="locations" hidden>
          <div class="site-list">{''.join(cards)}</div>
        </section>
        <section class="dashboard-panel" data-dashboard-panel="usage">
          <h2 class="section-title">소화기 사용 순서</h2>
          <a class="video-link-card" href="https://www.youtube.com/watch?v=5fxqetu4tUY" target="_blank" rel="noopener noreferrer"><span class="video-play">▶</span><span class="video-link-copy"><b>YouTube 사용 영상 열기</b><span>새 창에서 재생</span></span><span class="video-arrow">↗</span></a>
          <div class="usage-steps">
            <div class="usage-step"><span class="step-art step-1" role="img" aria-label="안전핀을 뽑는 그림"></span><div><div class="step-heading"><span class="usage-number">1</span><b>안전핀을 뽑습니다</b></div><p>손잡이를 누르지 않도록 몸통을 잡고 안전핀을 힘껏 당깁니다.</p></div></div>
            <div class="usage-step"><span class="step-art step-2" role="img" aria-label="노즐을 불 밑으로 향하는 그림"></span><div><div class="step-heading"><span class="usage-number">2</span><b>불의 밑부분을 겨눕니다</b></div><p>출구를 등지고 노즐을 불꽃이 아니라 타는 물체의 아랫부분으로 향합니다.</p></div></div>
            <div class="usage-step"><span class="step-art step-3" role="img" aria-label="손잡이를 누르는 그림"></span><div><div class="step-heading"><span class="usage-number">3</span><b>손잡이를 움켜쥡니다</b></div><p>노즐을 잡은 채 위·아래 손잡이를 함께 강하게 눌러 약제를 방사합니다.</p></div></div>
            <div class="usage-step"><span class="step-art step-4" role="img" aria-label="좌우로 방사하는 그림"></span><div><div class="step-heading"><span class="usage-number">4</span><b>밑면을 좌우로 쓸어줍니다</b></div><p>불의 밑면 전체를 덮도록 좌우로 움직이며 재발화 여부를 확인합니다.</p></div></div>
          </div>
          <div class="safety-alert"><b>대피가 먼저입니다.</b> 연기가 많거나 불길이 커지면 진화를 멈추고 대피한 뒤 119에 신고하세요.</div>
          <section class="action-guide" aria-label="화재 국민행동요령">
            <h3>화재가 나면 이렇게 행동하세요</h3>
            <div class="action-list">
              <div class="action-item"><span class="action-art action-art-1" role="img" aria-label="주변에 화재를 알리고 비상벨을 누르는 그림"></span><div><b>주변에 알리기</b><p>“불이야”라고 외치고 비상벨을 눌러 모두에게 알립니다.</p></div></div>
              <div class="action-item"><span class="action-art action-art-2" role="img" aria-label="손등으로 문손잡이의 열을 확인하는 그림"></span><div><b>문 확인하기</b><p>손등으로 문손잡이를 확인하고 뜨거우면 문을 열지 않습니다.</p></div></div>
              <div class="action-item"><span class="action-art action-art-3" role="img" aria-label="낮은 자세로 계단을 이용해 대피하는 그림"></span><div><b>계단으로 대피</b><p>엘리베이터를 타지 말고 낮은 자세로 계단을 이용하며 출입문을 닫습니다.</p></div></div>
              <div class="action-item"><span class="action-art action-art-4" role="img" aria-label="건물 밖 안전한 곳에서 전화로 신고하는 그림"></span><div><b>안전한 곳에서 신고</b><p>신고 때문에 대피가 늦지 않도록 먼저 안전한 곳으로 나온 뒤 119에 신고합니다.</p></div></div>
              <div class="action-item"><span class="action-art action-art-5" role="img" aria-label="작은 불인지 판단하고 소화기를 준비하는 그림"></span><div><b>작은 불만 진화</b><p>불길이 천장에 닿지 않은 작은 불일 때만 소화기를 사용합니다.</p></div></div>
              <div class="action-item"><span class="action-art action-art-6" role="img" aria-label="창문에서 구조를 요청하는 그림"></span><div><b>대피가 어렵다면</b><p>창문으로 구조를 요청하거나 대피공간·경량칸막이를 이용합니다.</p></div></div>
              <div class="action-item"><span class="action-art action-art-7" role="img" aria-label="안전한 곳에 모여 인원을 확인하고 소방관에게 알리는 그림"></span><div><b>인원 확인</b><p>약속한 안전한 곳에서 인원을 확인하고 안 보이는 사람은 소방관에게 알립니다.</p></div></div>
              <div class="action-item"><span class="action-art action-art-8" role="img" aria-label="옷에 붙은 불을 끄기 위해 바닥에서 몸을 구르는 그림"></span><div><b>옷에 불이 붙으면</b><p>멈추고 얼굴을 감싼 뒤 바닥에 엎드려 몸을 굴려 불을 끕니다.</p></div></div>
            </div>
          </section>
          <p class="source-note"><a href="https://old.safekorea.go.kr/idsiSFK/neo/main_m/sot/fire.html" target="_blank" rel="noopener noreferrer">국민안전24 화재 국민행동요령 더보기 ↗</a></p>
        </section>
      </div>
      <footer class="dashboard-footer">표시 좌표는 현장 확인 전 설치 검토 중심점입니다. · OSM © contributors</footer>
    </aside>
    <script>
      window.showDashboardTab = function(name) {{
        document.querySelectorAll('[data-dashboard-tab]').forEach(function(button) {{
          var active = button.dataset.dashboardTab === name;
          button.classList.toggle('is-active', active);
          button.setAttribute('aria-selected', active ? 'true' : 'false');
        }});
        document.querySelectorAll('[data-dashboard-panel]').forEach(function(panel) {{
          panel.hidden = panel.dataset.dashboardPanel !== name;
        }});
      }};
      window.focusCandidate = function(rank) {{
        document.querySelectorAll('[data-candidate-card]').forEach(function(card) {{ card.classList.toggle('is-selected', Number(card.dataset.candidateCard) === rank); }});
        switch(rank) {{ {' '.join(marker_cases)} }}
      }};
      window.addEventListener('load', function() {{
        document.title='우리동네 화재안전 지도';
        var params=new URLSearchParams(window.location.search);
        var tab=params.get('tab');if(tab==='usage'||tab==='locations'){{showDashboardTab(tab);}}
        function updateTargetLocator() {{
          var localView={map_name}.getZoom()>13;
          document.querySelectorAll('.target-locator').forEach(function(label) {{ label.style.display=localView?'none':'block'; }});
          document.querySelectorAll('.budget-candidate-marker').forEach(function(marker) {{ marker.style.display=localView?'block':'none'; }});
          document.querySelectorAll('.local-road-label').forEach(function(label) {{ label.style.display=localView?'block':'none'; }});
        }}
        {map_name}.on('zoomend',updateTargetLocator);updateTargetLocator();
        var site=Number(params.get('site'));if(Number.isInteger(site)&&site>=1&&site<=10){{setTimeout(function(){{focusCandidate(site);}},120);}}
        setTimeout(function(){{{map_name}.invalidateSize(false);}},0);
      }});
    </script>
    """


def build_map(
    boundary: gpd.GeoDataFrame,
    busan_dongs: gpd.GeoDataFrame,
    busan_major_roads: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
    official_roads: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    candidates: gpd.GeoDataFrame,
    output_path: Path,
    *,
    boundary_label: str,
    building_label: str,
    context_margin_m: float,
    infrastructure: gpd.GeoDataFrame,
    narrow_threshold_m: float,
) -> None:
    boundary_web = boundary.to_crs("EPSG:4326")
    busan_dongs_web = clean_for_geojson(busan_dongs.to_crs("EPSG:4326"))
    busan_major_roads_web = clean_for_geojson(
        busan_major_roads.to_crs("EPSG:4326")
    )
    center = boundary_web.geometry.iloc[0].representative_point()
    fmap = folium.Map(
        location=[center.y, center.x],
        zoom_start=16,
        tiles=None,
        control_scale=True,
        prefer_canvas=True,
        zoom_snap=0.25,
        zoom_delta=0.25,
    )
    fmap.get_root().header.add_child(
        folium.Element(
            "<style>html,body,.folium-map,.leaflet-container{background:#f3f0e8!important}"
            ".budget-candidate-marker{"
            "background:transparent!important;border:0!important}.visible-extinguisher-icon{"
            "width:44px;height:44px;border-radius:50%;display:grid;place-items:center;position:relative;"
            "background:#fff;border:3px solid #dc2626;box-shadow:0 2px 8px rgba(90,20,20,.42)}"
            ".visible-extinguisher-icon svg{display:block}.local-road-label{"
            "background:rgba(255,255,255,.82)!important;border:0!important;"
            "box-shadow:none!important;color:#4b4944;font:500 13px/1.2 sans-serif;"
            "white-space:nowrap;padding:1px 3px;border-radius:2px;pointer-events:none}"
            ".target-locator{background:#173d36!important;color:#fff!important;border:2px solid #fff!important;"
            "box-shadow:0 2px 8px rgba(0,0,0,.24);border-radius:999px!important;"
            "font:700 14px/28px sans-serif!important;text-align:center;white-space:nowrap;}"
            ".fire-water-divicon{background:transparent!important;border:0!important;}"
            ".fire-water-icon{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;"
            "background:#75c9d5;border:2px solid #fff;box-shadow:0 1px 6px rgba(20,70,79,.45);}</style>"
        )
    )
    if not busan_dongs_web.empty:
        folium.GeoJson(
            busan_dongs_web,
            name="부산 행정동",
            show=True,
            style_function=lambda _feature: {
                "color": "#bbb6ad",
                "weight": 0.65,
                "fillColor": "#e8e4da",
                "fillOpacity": 0.72,
            },
            interactive=False,
        ).add_to(fmap)
    if not busan_major_roads_web.empty:
        folium.GeoJson(
            busan_major_roads_web,
            name="부산 주요도로 (12m 이상)",
            show=True,
            style_function=busan_major_road_style,
            interactive=False,
        ).add_to(fmap)
    folium.GeoJson(
        boundary_web,
        name=boundary_label,
        style_function=lambda _feature: {
            "color": "#1d4ed8",
            "weight": 3,
            "fillColor": "#f7f1e6",
            "fillOpacity": 0.32,
        },
    ).add_to(fmap)
    folium.Marker(
        location=[center.y, center.x],
        icon=folium.DivIcon(
            html="대상지역",
            icon_size=(64, 26),
            icon_anchor=(32, 13),
            class_name="target-locator",
        ),
        interactive=False,
    ).add_to(fmap)
    if "inside_target" in buildings.columns:
        buildings = buildings.loc[buildings["inside_target"].fillna(False)].copy()
    if "inside_target" in official_roads.columns:
        official_roads = official_roads.loc[
            official_roads["inside_target"].fillna(False)
        ].copy()
    if not buildings.empty:
        folium.GeoJson(
            clean_for_geojson(buildings.to_crs("EPSG:4326")),
            name=building_label,
            show=True,
            style_function=local_building_style,
            tooltip=folium.GeoJsonTooltip(
                fields=["building_use_name", "building_category"],
                aliases=["건물 용도", "분류"],
                localize=True,
            )
            if {"building_use_name", "building_category"}.issubset(buildings.columns)
            else None,
        ).add_to(fmap)
    if not official_roads.empty:
        official_roads_web = clean_for_geojson(official_roads.to_crs("EPSG:4326"))
        local_roads = folium.FeatureGroup(
            name="대상지역 도로", show=True
        ).add_to(fmap)
        folium.GeoJson(
            official_roads_web,
            style_function=local_road_casing_style,
        ).add_to(local_roads)
        folium.GeoJson(
            official_roads_web,
            style_function=local_road_fill_style,
        ).add_to(local_roads)

        candidate_road_names = set(
            candidates.get("official_road_name", pd.Series(dtype=object)).dropna().astype(str)
        )
        road_labels = official_roads.loc[
            official_roads["RN"].notna() & official_roads["RN"].astype(str).str.strip().ne(""),
            ["RN", "geometry"],
        ].copy()
        if not road_labels.empty:
            road_labels = road_labels.dissolve(by="RN").reset_index()
            road_labels["_length_m"] = road_labels.geometry.length
            road_labels["_candidate_road"] = road_labels["RN"].astype(str).isin(
                candidate_road_names
            )
            road_labels = road_labels.sort_values(
                ["_candidate_road", "_length_m"], ascending=False
            ).head(20)
            for _, label_row in road_labels.to_crs("EPSG:4326").iterrows():
                label_geometry = label_row.geometry
                if hasattr(label_geometry, "geoms"):
                    label_geometry = max(label_geometry.geoms, key=lambda part: part.length)
                label_point = label_geometry.interpolate(0.5, normalized=True)
                folium.Marker(
                    location=[label_point.y, label_point.x],
                    icon=folium.DivIcon(
                        html=html.escape(str(label_row["RN"])),
                        icon_size=(110, 16),
                        icon_anchor=(55, 8),
                        class_name="local-road-label",
                    ),
                    interactive=False,
                ).add_to(local_roads)

        folium.GeoJson(
            official_roads_web,
            name="좁은 도로 확인용",
            show=True,
            style_function=lambda feature: official_road_style(
                feature, narrow_threshold_m=narrow_threshold_m
            ),
            tooltip=folium.GeoJsonTooltip(
                fields=["RN", "road_width_m"],
                aliases=["도로명", "등록 도로폭(m)"],
                localize=True,
            ),
        ).add_to(fmap)

    candidate_layer = folium.FeatureGroup(name="보이는 소화기", show=True).add_to(fmap)

    if not infrastructure.empty:
        if "inside_target" in infrastructure.columns:
            infrastructure = infrastructure.loc[
                infrastructure["inside_target"].fillna(False)
            ].copy()
        infra_web = infrastructure.loc[
            infrastructure.get(
                "asset_type", pd.Series(index=infrastructure.index, dtype=object)
            ).eq("fire_water")
        ].to_crs("EPSG:4326")
        water_layer = folium.FeatureGroup(
            name="소방용수", show=True
        ).add_to(fmap)
        for _, infra_row in infra_web.iterrows():
            address = str(infra_row.get("candidate_jibun_address", "")).strip() or "주소 미상"
            water_icon = """
            <div class="fire-water-icon" aria-label="소방용수">
              <svg width="23" height="23" viewBox="0 0 20 20" aria-hidden="true">
                <path fill="#ffffff" d="M8 3h4v2h-1v1.1A4.5 4.5 0 0 1 14.5 10H17v4h-2.5v2H16v2H4v-2h1.5v-2H3v-4h2.5A4.5 4.5 0 0 1 9 6.1V5H8V3Zm-.5 7h5a2.5 2.5 0 0 0-5 0Zm0 2v4h5v-4h-5Z"/>
              </svg>
            </div>
            """
            folium.Marker(
                location=[infra_row.geometry.y, infra_row.geometry.x],
                icon=folium.DivIcon(
                    html=water_icon,
                    icon_size=(36, 36),
                    icon_anchor=(18, 18),
                    class_name="fire-water-divicon",
                ),
                tooltip=f"소방용수 · {address}",
                popup=folium.Popup(
                    f"<b>소방용수</b><br>{html.escape(address)}<br>참고 위치 · 현장 확인 필요",
                    max_width=300,
                ),
                z_index_offset=100,
            ).add_to(water_layer)
    folium.GeoJson(
        roads.to_crs("EPSG:4326"),
        name="보행로·계단",
        show=True,
        style_function=road_style,
        tooltip=folium.GeoJsonTooltip(
            fields=["highway", "width_m"],
            aliases=["OSM highway", "관측 폭(m)"],
            localize=True,
        ),
    ).add_to(fmap)
    residential_buildings = buildings.loc[
        buildings.get("building_category", pd.Series(index=buildings.index, dtype=object)).eq(
            "residential"
        )
    ].copy()
    if not residential_buildings.empty:
        folium.GeoJson(
            clean_for_geojson(residential_buildings.to_crs("EPSG:4326")),
            name="주거용 건물만 강조",
            show=False,
            style_function=lambda _feature: {
                "color": "#be123c",
                "weight": 0.8,
                "fillColor": "#fb7185",
                "fillOpacity": 0.34,
            },
        ).add_to(fmap)

    candidates_web = candidates.to_crs("EPSG:4326")
    marker_names: dict[int, str] = {}
    for _, row in candidates_web.iterrows():
        road_name_text = (
            "인접 도로명 미확인"
            if pd.isna(row.get("official_road_name"))
            else str(row.get("official_road_name"))
        )
        rank = int(row["rank"])
        popup = f"""
        <div style="width:280px;font:14px/1.6 sans-serif">
          <b>보이는 소화기 {rank} · {html.escape(road_name_text)}</b><br>
          <span style="color:#5f6a65">{row.geometry.y:.6f}, {row.geometry.x:.6f}</span><br>
          <span style="font-size:13px;color:#7a4a42">설치 확정지가 아닌 설치 검토 중심점입니다. 현장에서 정확한 설치 지점을 확인해야 합니다.</span>
        </div>
        """
        marker_html = f"""
        <div class="visible-extinguisher-icon" aria-label="보이는 소화기 {rank}">
          <svg width="28" height="32" viewBox="0 0 28 32" aria-hidden="true">
            <path fill="#7f1d1d" d="M10 3h9v3h-3v2h-4V6h-2V3Zm9 2h4v2h-4V5Z"/>
            <path fill="#dc2626" d="M9 9h10a3 3 0 0 1 3 3v16H6V12a3 3 0 0 1 3-3Z"/>
            <path fill="#991b1b" d="M6 25h16v4H6z"/>
            <path fill="none" stroke="#7f1d1d" stroke-width="2" stroke-linecap="round" d="M19 10c5 1 6 4 4 8"/>
            <text x="14" y="22" text-anchor="middle" fill="#fff" font-family="Arial,sans-serif" font-size="9" font-weight="700">{rank}</text>
          </svg>
        </div>
        """
        candidate_marker = folium.Marker(
            location=[row.geometry.y, row.geometry.x],
            icon=folium.DivIcon(
                html=marker_html,
                icon_size=(44, 44),
                icon_anchor=(22, 22),
                class_name="budget-candidate-marker",
            ),
            tooltip=f"보이는 소화기 {rank} · {road_name_text}",
            popup=folium.Popup(popup, max_width=300),
            z_index_offset=1000,
        )
        candidate_marker.add_to(candidate_layer)
        marker_names[rank] = candidate_marker.get_name()

    if busan_dongs_web.empty:
        busan_bounds = tuple(boundary_web.total_bounds)
    else:
        busan_bounds = tuple(busan_dongs_web.total_bounds)
    minx, miny, maxx, maxy = busan_bounds
    fmap.fit_bounds(
        [[miny, minx], [maxy, maxx]],
        padding_top_left=(20, 20),
        padding_bottom_right=(490, 20),
    )
    legend = """
    <div class="map-key" style="position:fixed;bottom:26px;left:24px;z-index:9999;background:rgba(255,255,255,.96);
    border:1px solid #cfcac1;border-radius:10px;padding:13px 15px;font:14px sans-serif;color:#34322f;box-shadow:0 6px 18px rgba(0,0,0,.10)">
      <strong style="display:block;margin-bottom:8px;font-size:16px">범례</strong>
      <span style="display:inline-block;width:24px;border-top:4px solid #1d4ed8;vertical-align:middle"></span> 대상지역 경계<br>
      <span style="display:inline-block;width:24px;border-top:4px solid #d7301f;vertical-align:middle"></span> 계단·좁은 도로&nbsp;&nbsp;
      <span style="display:inline-block;width:24px;border-top:4px solid #f59e0b;vertical-align:middle"></span> 보행로<br>
      <span style="display:inline-grid;width:28px;height:28px;border-radius:50%;background:#fff;border:2px solid #dc2626;place-items:center;vertical-align:middle">
        <svg width="17" height="20" viewBox="0 0 28 32" aria-hidden="true"><path fill="#7f1d1d" d="M10 3h9v3h-3v2h-4V6h-2V3Zm9 2h4v2h-4V5Z"/><path fill="#dc2626" d="M9 9h10a3 3 0 0 1 3 3v16H6V12a3 3 0 0 1 3-3Z"/><path fill="#991b1b" d="M6 25h16v4H6z"/><path fill="none" stroke="#7f1d1d" stroke-width="2" stroke-linecap="round" d="M19 10c5 1 6 4 4 8"/></svg>
      </span> 보이는 소화기&nbsp;&nbsp;
      <span style="display:inline-grid;width:24px;height:24px;border-radius:50%;background:#75c9d5;border:1px solid #fff;box-shadow:0 0 0 1px #4a9aa7;place-items:center;vertical-align:middle">
        <svg width="16" height="16" viewBox="0 0 20 20" aria-hidden="true"><path fill="#fff" d="M8 3h4v2h-1v1.1A4.5 4.5 0 0 1 14.5 10H17v4h-2.5v2H16v2H4v-2h1.5v-2H3v-4h2.5A4.5 4.5 0 0 1 9 6.1V5H8V3Zm-.5 7h5a2.5 2.5 0 0 0-5 0Zm0 2v4h5v-4h-5Z"/></svg>
      </span> 소방용수<br>
      <span style="display:inline-block;width:24px;border-top:3px solid #fff;box-shadow:0 0 0 1px #d1cec6;vertical-align:middle"></span> 부산 주요도로
    </div>
    """
    fmap.get_root().html.add_child(folium.Element(legend))
    fmap.get_root().html.add_child(
        folium.Element(
            dashboard_panel_html_v2(
                candidates_web,
                map_name=fmap.get_name(),
                marker_names=marker_names,
            )
        )
    )
    folium.LayerControl(collapsed=True, position="topleft").add_to(fmap)
    fmap.save(output_path)
    rendered_html = output_path.read_text(encoding="utf-8")
    output_path.write_text(
        "\n".join(line.rstrip() for line in rendered_html.splitlines()) + "\n",
        encoding="utf-8",
    )


def failure_outputs(output_dir: Path, manifest: dict[str, Any], failures: list[str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    quality = {
        "status": "collection_failed",
        "failures": failures,
        "download_manifest": manifest,
        "fabricated_results": False,
    }
    (output_dir / "data_quality.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "report.md").write_text(
        "# 수정제5동 보이는 소화기 후보지 분석\n\n"
        "## 결과\n외부 원자료 수집이 완료되지 않아 후보지와 점수를 생성하지 않았습니다. "
        "임의 좌표나 데모 점수는 사용하지 않았습니다.\n\n"
        "## 실패 원인\n" + "\n".join(f"- {item}" for item in failures) + "\n",
        encoding="utf-8",
    )


def analyze(config_path: Path) -> int:
    root = config_path.resolve().parent
    cache_dir = root / "cache"
    output_dir = root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    settings = config["screening"]
    pnu_context = config.get("upstream_analysis", {})
    fire_water_zip = root / config.get("sources", {}).get(
        "fire_water_status_zip",
        "부산광역시소방재난본부_부산소방재난본부_소방용수 현황.zip",
    )
    fire_water_quality = profile_fire_water_status(fire_water_zip)
    manifest_path = cache_dir / "download_manifest.json"
    if not manifest_path.exists():
        failure_outputs(output_dir, {}, ["cache/download_manifest.json이 없습니다. download_data.py를 먼저 실행하세요."])
        return 2
    manifest = read_json(manifest_path)
    official_building_path = find_latest_building_shp(root / "input")
    required = ["boundary", "walk_network", "drive_network", "path_features"]
    if official_building_path is None:
        required.append("buildings")
    failures = [
        f"{name}: {manifest.get('datasets', {}).get(name, {}).get('error', 'not downloaded')}"
        for name in required
        if not manifest.get("datasets", {}).get(name, {}).get("success")
    ]
    if failures:
        failure_outputs(output_dir, manifest, failures)
        return 2

    metric_crs = config["project"]["projection_metric"]
    boundary = gpd.read_file(cache_dir / "boundary.geojson").to_crs(metric_crs)
    busan_dongs, busan_major_roads, busan_context_quality = load_busan_map_context(
        root, metric_crs
    )
    if busan_dongs.empty:
        failure_outputs(
            output_dir,
            manifest,
            [
                "부산 전체 배경 데이터를 만들지 못했습니다: "
                + str(busan_context_quality.get("error", "행정동 데이터 없음"))
            ],
        )
        return 2
    walk_graph = ox.io.load_graphml(cache_dir / "walk_network.graphml")
    drive_graph = ox.io.load_graphml(cache_dir / "drive_network.graphml")
    walk_metric = ox.projection.project_graph(walk_graph, to_crs=metric_crs)
    drive_metric = ox.projection.project_graph(drive_graph, to_crs=metric_crs)
    walk_nodes, walk_edges = ox.convert.graph_to_gdfs(walk_metric)
    _, drive_edges = ox.convert.graph_to_gdfs(drive_metric)
    degree = graph_neighbor_counts(walk_metric)

    paths = load_optional_features(cache_dir / "path_features.geojson", metric_crs)
    osm_buildings = load_optional_features(cache_dir / "buildings.geojson", metric_crs)
    exclusions = load_optional_features(cache_dir / "exclusions.geojson", metric_crs)
    steps = paths.loc[paths.get("highway", pd.Series(index=paths.index, dtype=object)).map(
        lambda value: tag_contains(value, {"steps"})
    )].copy()
    step_union = union_all(steps.geometry.values) if not steps.empty else None
    drive_union = union_all(drive_edges.geometry.values) if not drive_edges.empty else None

    if official_building_path is not None:
        try:
            polygon_buildings, building_meta = load_official_buildings(
                root / "input", boundary, metric_crs
            )
        except Exception as exc:
            failure_outputs(
                output_dir,
                manifest,
                [f"공식 GIS건물통합정보 처리 실패: {type(exc).__name__}: {exc}"],
            )
            return 2
        building_source = "official_gis_building_integrated"
        building_label = "전체 건물 (GIS건물통합정보 2026-10-09)"
        building_keep = [
            column
            for column in (
                "building_id",
                "A2",
                "A4",
                "A8",
                "A9",
                "building_use_name",
                "building_category",
                "source_file",
                "geometry",
            )
            if column in polygon_buildings.columns
        ]
        clean_for_geojson(polygon_buildings[building_keep].to_crs("EPSG:4326")).to_file(
            cache_dir / "buildings_official.geojson", driver="GeoJSON"
        )
    else:
        if "building" not in osm_buildings.columns:
            osm_buildings["building"] = None
        osm_buildings["building_category"] = osm_buildings["building"].map(building_category)
        polygon_buildings = osm_buildings.loc[
            osm_buildings.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        ].copy()
        building_source = "openstreetmap"
        building_label = "전체 건물 (OSM 저장 자료)"
        building_meta = {
            "source": "OpenStreetMap",
            "path": str(cache_dir / "buildings.geojson"),
            "version": None,
            "target_features": int(len(polygon_buildings)),
        }
    residential = polygon_buildings.loc[
        polygon_buildings["building_category"].eq("residential")
    ].copy()
    building_union = union_all(polygon_buildings.geometry.values) if not polygon_buildings.empty else None

    try:
        official_roads, official_road_meta = load_official_roads(
            root / "input", boundary, metric_crs
        )
    except FileNotFoundError:
        official_roads = gpd.GeoDataFrame(geometry=[], crs=metric_crs)
        official_road_meta = {
            "source": None,
            "path": None,
            "version": None,
            "target_or_margin_features": 0,
            "valid_width_features": 0,
        }
    except Exception as exc:
        failure_outputs(
            output_dir,
            manifest,
            [f"공식 도로폭 데이터 처리 실패: {type(exc).__name__}: {exc}"],
        )
        return 2
    map_context_margin_m = float(settings.get("map_context_margin_m", 400))
    fire_infrastructure_path = root / config.get("sources", {}).get(
        "fire_infrastructure_parquet",
        "input/data_archive/processed/fire_infra_clean.parquet",
    )
    infrastructure, infrastructure_quality = load_pnu_fire_infrastructure(
        fire_infrastructure_path,
        boundary,
        metric_crs,
        map_context_margin_m,
    )
    source_existing_emergency = infrastructure.loc[
        infrastructure.get("asset_type", pd.Series(index=infrastructure.index, dtype=object)).eq(
            "emergency_fire_equipment"
        )
    ].copy()
    use_existing_emergency_equipment_exclusion = bool(
        settings.get("use_existing_emergency_equipment_exclusion", False)
    )
    existing_emergency = (
        source_existing_emergency
        if use_existing_emergency_equipment_exclusion
        else source_existing_emergency.iloc[0:0].copy()
    )
    existing_emergency_union = (
        union_all(existing_emergency.geometry.values) if not existing_emergency.empty else None
    )
    existing_equipment_exclusion_m = float(
        settings.get("existing_emergency_equipment_exclusion_m", 75)
    )
    map_context_error: str | None = None
    map_buildings = polygon_buildings.copy()
    map_official_roads = official_roads.copy()
    if official_building_path is not None and map_context_margin_m > 0:
        try:
            map_buildings, _map_building_meta = load_official_buildings(
                root / "input",
                boundary,
                metric_crs,
                search_margin_m=map_context_margin_m,
            )
            map_official_roads, _map_road_meta = load_official_roads(
                root / "input",
                boundary,
                metric_crs,
                search_margin_m=map_context_margin_m,
            )
        except Exception as exc:
            map_context_error = f"{type(exc).__name__}: {exc}"
            map_buildings = polygon_buildings.copy()
            map_official_roads = official_roads.copy()
    target_boundary_geom = boundary.geometry.iloc[0]
    map_buildings["inside_target"] = map_buildings.geometry.intersects(target_boundary_geom)
    map_official_roads["inside_target"] = map_official_roads.geometry.intersects(
        target_boundary_geom
    )
    map_buildings.geometry = map_buildings.geometry.simplify(0.35, preserve_topology=True)
    exclusion_polygons = exclusions.loc[
        exclusions.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
    ]
    exclusion_union = union_all(exclusion_polygons.geometry.values) if not exclusion_polygons.empty else None

    incident_widths: dict[Any, list[float]] = defaultdict(list)
    incident_highways: dict[Any, set[str]] = defaultdict(set)
    for (u, v, _key), row in walk_edges.iterrows():
        width = parse_width_m(row.get("width"))
        if width is not None:
            incident_widths[u].append(width)
            incident_widths[v].append(width)
        for tag in jsonish_values(row.get("highway")):
            incident_highways[u].add(tag.lower())
            incident_highways[v].add(tag.lower())

    records: list[dict[str, Any]] = []
    excluded_near_existing_equipment = 0
    boundary_geom = boundary.geometry.iloc[0]
    for node_id, node_row in walk_nodes.iterrows():
        point = node_row.geometry
        if not boundary_geom.covers(point):
            continue
        if building_union is not None and building_union.contains(point):
            continue
        if exclusion_union is not None and exclusion_union.contains(point):
            continue
        street_degree = degree.get(node_id, 0)
        distance_drive = distance_to_union(point, drive_union)
        distance_steps = distance_to_union(point, step_union)
        # A successful query with zero step features is an observed absence, not missing data.
        near_steps = False if step_union is None else distance_steps <= float(settings["stairs_near_m"])
        highway_tags = incident_highways.get(node_id, set())
        foot_alley = bool(highway_tags & FOOT_ALLEY_TYPES)
        foot_only = None if distance_drive is None else (foot_alley and distance_drive > 15)
        if not (street_degree >= 3 or near_steps is True or foot_only is True):
            continue
        distance_existing_emergency = distance_to_union(point, existing_emergency_union)
        if (
            distance_existing_emergency is not None
            and distance_existing_emergency <= existing_equipment_exclusion_m
        ):
            excluded_near_existing_equipment += 1
            continue
        osm_width = min(incident_widths[node_id]) if incident_widths.get(node_id) else None
        official_match = nearest_official_road(
            point,
            official_roads,
            max_distance_m=float(settings.get("official_road_match_max_m", 20)),
        )
        if official_match and official_match["road_width_m"] is not None:
            width = official_match["road_width_m"]
            width_source = official_match["source"]
            official_road_name = official_match["road_name"]
            official_road_distance = official_match["distance_m"]
        else:
            width = osm_width
            width_source = "osm_width_tag" if osm_width is not None else "missing"
            official_road_name = None
            official_road_distance = None
        a_score, a_evidence, missing = firetruck_difficulty_score(
            near_steps=near_steps,
            distance_to_drive_m=distance_drive,
            road_width_m=width,
            foot_only_evidence=foot_only,
            narrow_threshold_m=float(settings["road_width_narrow_m"]),
        )
        if residential.empty:
            residential_count: int | None = None
        else:
            buffer = point.buffer(float(settings["density_buffer_m"]))
            residential_count = int(len(residential.sindex.query(buffer, predicate="intersects")))
        minimum_residential = int(settings.get("minimum_residential_buildings_50m", 1))
        if residential_count is not None and residential_count < minimum_residential:
            continue
        candidate_reason = []
        if street_degree >= 3:
            candidate_reason.append(f"{street_degree}방향 보행 교차/분기")
        if near_steps:
            candidate_reason.append("계단 연결점 인접")
        if foot_only:
            candidate_reason.append("차량망 이격 보행골목")
        if residential_count is not None:
            candidate_reason.append(f"50m 내 주거건물 {residential_count}동")
        records.append(
            {
                "candidate_id": f"S5-{node_id}",
                "osm_node_id": str(node_id),
                "candidate_reason": "; ".join(candidate_reason),
                "street_degree": street_degree,
                "near_steps": near_steps,
                "distance_to_drive_m": None if distance_drive is None else round(distance_drive, 2),
                "foot_only_evidence": foot_only,
                "distance_to_existing_emergency_m": None
                if distance_existing_emergency is None
                else round(distance_existing_emergency, 2),
                "road_width_m": width,
                "road_width_source": width_source,
                "osm_width_m": osm_width,
                "official_road_name": official_road_name,
                "distance_to_official_road_m": None
                if official_road_distance is None
                else round(official_road_distance, 2),
                "A_score": a_score,
                "A_evidence": "; ".join(a_evidence),
                "missing_items": "; ".join(missing),
                "residential_buildings_50m": residential_count,
                "C_score": visibility_accessibility_score(street_degree),
                "visibility_proxy": True,
                "needs_field_check": True,
                "geometry": point,
            }
        )

    pool = gpd.GeoDataFrame(records, geometry="geometry", crs=metric_crs)
    if pool.empty:
        failure_outputs(output_dir, manifest, ["후보 조건을 만족하는 실제 OSM 보행망 노드가 없습니다."])
        return 2
    pool["B_score"] = residential_density_scores(pool["residential_buildings_50m"])
    candidate_pool_before_density_screen = int(len(pool))
    minimum_density_score = int(settings.get("minimum_residential_density_score", 2))
    low_density_mask = pool["B_score"].lt(minimum_density_score)
    excluded_low_density_candidates = int(low_density_mask.sum())
    pool = pool.loc[~low_density_mask].copy()
    if pool.empty:
        failure_outputs(
            output_dir,
            manifest,
            [
                "주거밀도 최저등급 제외 후 남은 후보가 없습니다. "
                "실제 인구·보행량 자료를 확보한 뒤 후보 조건을 재검토해야 합니다."
            ],
        )
        return 2
    pool, field_override_count = apply_field_visibility(
        pool, root / "input" / "visibility_field.csv"
    )
    weights = {
        "A": float(config["weights"]["firetruck_access_difficulty"]),
        "B": float(config["weights"]["residential_building_density"]),
        "C": float(config["weights"]["visibility_accessibility_proxy"]),
    }
    pool["score_map"] = pool.apply(
        lambda row: weighted_score(
            {"A": row["A_score"], "B": row["B_score"], "C": row["C_score"]},
            weights,
        ),
        axis=1,
    )
    pool["confidence"] = pool.apply(
        lambda row: "low" if row["missing_items"] else "low_to_medium", axis=1
    )
    selected = select_spaced_candidates(
        pool,
        int(settings["candidates_to_export"]),
        float(settings["candidate_min_spacing_m"]),
    ).copy()
    selected = selected.sort_values(
        ["score_map", "A_score", "residential_buildings_50m", "C_score"],
        ascending=False,
        na_position="last",
    ).reset_index(drop=True)
    selected["rank"] = range(1, len(selected) + 1)
    complete_scores = selected["score_map"].notna().sum()
    can_recommend = len(selected) >= int(settings["candidates_to_export"]) and complete_scores >= int(
        settings["recommended_sites"]
    )
    selected["recommended"] = False
    if can_recommend:
        complete_index = selected.index[selected["score_map"].notna()][
            : int(settings["recommended_sites"])
        ]
        selected.loc[complete_index, "recommended"] = True
    selected["budget_scenario_tier"] = selected["rank"].map(
        lambda rank: "핵심 3개소안"
        if int(rank) <= 3
        else ("5개소안 추가분" if int(rank) <= 5 else "10개소안 추가분")
    )

    selected_web = selected.to_crs("EPSG:4326")
    selected_web["longitude"] = selected_web.geometry.x
    selected_web["latitude"] = selected_web.geometry.y
    csv_columns = [
        "rank",
        "candidate_id",
        "recommended",
        "budget_scenario_tier",
        "latitude",
        "longitude",
        "score_map",
        "A_score",
        "B_score",
        "C_score",
        "C_source",
        "residential_buildings_50m",
        "near_steps",
        "distance_to_drive_m",
        "foot_only_evidence",
        "distance_to_existing_emergency_m",
        "road_width_m",
        "road_width_source",
        "osm_width_m",
        "official_road_name",
        "distance_to_official_road_m",
        "street_degree",
        "candidate_reason",
        "A_evidence",
        "missing_items",
        "confidence",
        "visibility_proxy",
        "needs_field_check",
    ]
    selected_web[csv_columns].to_csv(
        output_dir / "candidates.csv", index=False, encoding="utf-8-sig"
    )
    selected_web[csv_columns].to_csv(
        output_dir / "budget_candidates.csv", index=False, encoding="utf-8-sig"
    )
    sensitivity_rows: list[dict[str, Any]] = []
    for threshold in settings.get("road_width_sensitivity_m", [3.5, 4.0, 6.0]):
        threshold_value = float(threshold)
        threshold_scores: list[dict[str, Any]] = []
        for _, candidate in selected.iterrows():
            a_sensitivity, evidence_sensitivity, _missing_sensitivity = (
                firetruck_difficulty_score(
                    near_steps=bool(candidate["near_steps"]),
                    distance_to_drive_m=candidate["distance_to_drive_m"],
                    road_width_m=candidate["road_width_m"],
                    foot_only_evidence=candidate["foot_only_evidence"],
                    narrow_threshold_m=threshold_value,
                )
            )
            score_sensitivity = weighted_score(
                {
                    "A": a_sensitivity,
                    "B": candidate["B_score"],
                    "C": candidate["C_score"],
                },
                weights,
            )
            threshold_scores.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "road_width_threshold_m": threshold_value,
                    "A_score": a_sensitivity,
                    "B_score": candidate["B_score"],
                    "C_score": candidate["C_score"],
                    "residential_buildings_50m": candidate[
                        "residential_buildings_50m"
                    ],
                    "score_map": score_sensitivity,
                    "A_evidence": "; ".join(evidence_sensitivity),
                }
            )
        ranked = pd.DataFrame(threshold_scores).sort_values(
            [
                "score_map",
                "A_score",
                "residential_buildings_50m",
                "C_score",
                "candidate_id",
            ],
            ascending=[False, False, False, False, True],
        )
        ranked["rank_within_selected_10"] = range(1, len(ranked) + 1)
        sensitivity_rows.extend(ranked.to_dict("records"))
    pd.DataFrame(sensitivity_rows).to_csv(
        output_dir / "road_width_sensitivity.csv", index=False, encoding="utf-8-sig"
    )
    clean_for_geojson(selected_web).to_file(
        output_dir / "candidates.geojson", driver="GeoJSON"
    )

    roads = walk_edges.reset_index().copy()
    roads["width_m"] = roads.get("width", pd.Series(index=roads.index, dtype=object)).map(parse_width_m)
    roads["highway"] = roads.get("highway", pd.Series(index=roads.index, dtype=object)).map(
        lambda value: "|".join(jsonish_values(value))
    )
    roads["_geom_key"] = roads.geometry.to_wkb()
    roads = roads.drop_duplicates("_geom_key").drop(columns="_geom_key")
    roads = gpd.clip(roads, boundary.geometry.iloc[0], keep_geom_type=True)
    road_keep = [column for column in ["osmid", "highway", "name", "width_m", "access", "motor_vehicle", "length", "geometry"] if column in roads.columns]
    roads_out = clean_for_geojson(roads[road_keep].to_crs("EPSG:4326"))
    roads_out.to_file(output_dir / "roads.geojson", driver="GeoJSON")
    official_road_keep = [
        column
        for column in ("RN", "RN_CD", "ROAD_BT", "road_width_m", "geometry")
        if column in official_roads.columns
    ]
    if not official_roads.empty:
        clean_for_geojson(official_roads[official_road_keep].to_crs("EPSG:4326")).to_file(
            output_dir / "official_roads.geojson", driver="GeoJSON"
        )
    if not infrastructure.empty:
        mapped_fire_water = infrastructure.loc[
            infrastructure.get(
                "asset_type", pd.Series(index=infrastructure.index, dtype=object)
            ).eq("fire_water")
            & infrastructure.get(
                "inside_target", pd.Series(False, index=infrastructure.index)
            ).fillna(False)
        ].copy()
        infrastructure_keep = [
            column
            for column in (
                "asset_type",
                "candidate_jibun_address",
                "coordinate_quality_tier",
                "geocode_quality",
                "inside_target",
                "geometry",
            )
            if column in mapped_fire_water.columns
        ]
        clean_for_geojson(mapped_fire_water[infrastructure_keep].to_crs("EPSG:4326")).to_file(
            output_dir / "existing_fire_infrastructure.geojson", driver="GeoJSON"
        )
    clean_for_geojson(busan_dongs.to_crs("EPSG:4326")).to_file(
        output_dir / "busan_admin_dongs.geojson", driver="GeoJSON"
    )
    clean_for_geojson(busan_major_roads.to_crs("EPSG:4326")).to_file(
        output_dir / "busan_major_roads.geojson", driver="GeoJSON"
    )
    boundary_meta = manifest["datasets"]["boundary"]
    boundary_version = (
        boundary_meta.get("base_date")
        or boundary_meta.get("version")
        or boundary_meta.get("year")
        or "기준일 미상"
    )
    map_building_keep = [
        column
        for column in ("building_use_name", "building_category", "inside_target", "geometry")
        if column in map_buildings.columns
    ]
    map_road_keep = [
        column
        for column in ("RN", "RN_CD", "ROAD_BT", "road_width_m", "inside_target", "geometry")
        if column in map_official_roads.columns
    ]
    build_map(
        boundary,
        busan_dongs,
        busan_major_roads,
        roads[["highway", "width_m", "geometry"]],
        map_official_roads[map_road_keep],
        map_buildings[map_building_keep],
        selected,
        output_dir / "map.html",
        boundary_label="대상지역 경계",
        building_label="대상지역 건물",
        context_margin_m=map_context_margin_m,
        infrastructure=infrastructure,
        narrow_threshold_m=float(settings["road_width_narrow_m"]),
    )

    width_valid = int(roads["width_m"].notna().sum())
    official_width_valid = int(official_roads.get("road_width_m", pd.Series(dtype=float)).notna().sum())
    candidate_official_width_matches = int(selected["road_width_source"].eq("official_ROAD_BT").sum())
    osm_cache_reused = any(
        bool(manifest.get("datasets", {}).get(name, {}).get("cache_reused"))
        for name in ("walk_network", "drive_network", "path_features")
    )
    fresh_osm_attempt = manifest.get("fresh_osm_attempt", {})
    building_total = int(len(polygon_buildings))
    building_unknown = int(polygon_buildings["building_category"].eq("unknown").sum())
    fire_water_dong_gu = fire_water_quality.get("dong_gu", {})
    target_infrastructure_counts = infrastructure_quality.get("target_asset_type_counts", {})
    target_existing_equipment = int(
        target_infrastructure_counts.get("emergency_fire_equipment", 0)
    )
    target_fire_water = int(target_infrastructure_counts.get("fire_water", 0))
    if use_existing_emergency_equipment_exclusion:
        existing_equipment_decision = (
            f"자료상 비상소화장치 {target_existing_equipment}건을 "
            f"{existing_equipment_exclusion_m:.0f}m 중복 검토에 적용했습니다."
        )
    else:
        existing_equipment_decision = (
            f"자료상 비상소화장치 {target_existing_equipment}건은 현장 정보와 일치하지 않아 "
            "지도 표시와 후보 제외에 사용하지 않았습니다."
        )
    if fire_water_quality.get("found") and not fire_water_quality.get("error"):
        fire_water_limitation = (
            "별도 2025 부산 소방용수 ZIP은 동구 위치정보가 없어 공간분석에서 제외했습니다. "
            "PNU-ANCHOR 전처리 자료의 필지/참조 좌표 중 소방용수는 참고 레이어로 표시했습니다. "
            + existing_equipment_decision
        )
    elif fire_water_quality.get("error"):
        fire_water_limitation = (
            "소방용수 현황 파일을 읽지 못해 기존 시설과의 거리를 계산하지 않았습니다: "
            + str(fire_water_quality["error"])
        )
    else:
        fire_water_limitation = "기존 소화기·소화전·비상소화장치 위치 자료가 없어 중복 여부를 확인하지 못했습니다."
    quality = {
        "status": "complete" if can_recommend else "insufficient_for_three_recommendations",
        "fabricated_results": False,
        "map_presentation": {
            "external_tile_requests": 0,
            "background": "busan_admin_dongs_major_roads_with_sujeong5_detail",
            "default_extent": "Busan",
            "candidate_scope": "Sujeong 5-dong only",
            "busan_admin_dongs": int(len(busan_dongs)),
            "busan_major_roads": int(len(busan_major_roads)),
            "building_features": int(len(map_buildings)),
            "official_road_features": int(len(map_official_roads)),
            "context_load_error": map_context_error,
        },
        "boundary": manifest["datasets"]["boundary"],
        "fresh_osm_attempt": fresh_osm_attempt,
        "fire_water_status": fire_water_quality,
        "fire_infrastructure": infrastructure_quality,
        "upstream_analysis": pnu_context,
        "sources": {
            "buildings": building_meta,
            "official_roads": official_road_meta,
            "fire_infrastructure": {
                "path": str(fire_infrastructure_path),
                "mapped_quality_tier": "parcel_or_reference",
            },
            "busan_map_context": busan_context_quality,
            "osm": {
                "walk_network": manifest["datasets"].get("walk_network"),
                "drive_network": manifest["datasets"].get("drive_network"),
                "path_features": manifest["datasets"].get("path_features"),
            },
        },
        "counts": {
            "walk_nodes": int(len(walk_nodes)),
            "walk_edges_directed": int(len(walk_edges)),
            "drive_edges_directed": int(len(drive_edges)),
            "step_path_features": int(len(steps)),
            "building_polygons": building_total,
            "residential_buildings_identified": int(len(residential)),
            "unknown_use_buildings": building_unknown,
            "official_road_segments_near_boundary": int(len(official_roads)),
            "official_road_segments_with_valid_width": official_width_valid,
            "selected_candidates_with_official_width": candidate_official_width_matches,
            "candidate_pool_before_residential_density_screen": candidate_pool_before_density_screen,
            "excluded_low_density_candidates": excluded_low_density_candidates,
            "eligible_candidate_pool": int(len(pool)),
            "spaced_candidates": int(len(selected)),
            "complete_wsm_scores": int(complete_scores),
            "field_visibility_overrides": field_override_count,
            "excluded_near_existing_emergency_equipment": excluded_near_existing_equipment,
            "mapped_existing_fire_infrastructure": int(len(infrastructure)),
            "source_existing_emergency_equipment_records": int(
                len(source_existing_emergency)
            ),
            "existing_emergency_equipment_records_used": int(len(existing_emergency)),
        },
        "rates": {
            "building_use_unknown_rate": None
            if building_total == 0
            else round(building_unknown / building_total, 4),
            "road_width_observed_rate": round(width_valid / len(roads), 4) if len(roads) else None,
            "official_road_width_valid_rate": round(official_width_valid / len(official_roads), 4)
            if len(official_roads)
            else None,
            "selected_candidate_official_width_match_rate": round(
                candidate_official_width_matches / len(selected), 4
            )
            if len(selected)
            else None,
        },
        "checks": {
            "boundary_valid": bool(boundary.geometry.is_valid.all()),
            "boundary_area_km2": round(float(boundary.area.iloc[0] / 1_000_000), 6),
            "all_candidates_inside_boundary": bool(
                selected.geometry.map(boundary.geometry.iloc[0].covers).all()
            ),
            "minimum_spacing_m": None
            if len(selected) < 2
            else round(
                min(
                    selected.geometry.iloc[i].distance(selected.geometry.iloc[j])
                    for i in range(len(selected))
                    for j in range(i + 1, len(selected))
                ),
                2,
            ),
            "requested_spacing_m": float(settings["candidate_min_spacing_m"]),
            "candidate_count_requirement_met": len(selected)
            >= int(settings["candidates_to_export"]),
            "all_selected_candidates_meet_residential_density_floor": bool(
                selected["B_score"].ge(minimum_density_score).all()
            ),
            "minimum_residential_density_score": minimum_density_score,
            "narrow_road_threshold_m": float(settings["road_width_narrow_m"]),
            "existing_emergency_equipment_exclusion_m": existing_equipment_exclusion_m,
            "existing_emergency_equipment_exclusion_enabled": (
                use_existing_emergency_equipment_exclusion
            ),
            "recommendation_count": int(selected["recommended"].sum()),
        },
        "limitations": [
            "2024-10-01 경계는 2026년 현행 경계로 검증되지 않은 참고용 자료입니다."
            if manifest["datasets"]["boundary"].get("outdated")
            else None,
            (
                "2026-10-22 신규 Overpass 호출은 로컬 프록시 연결 거부로 실패하여, "
                f"이전 성공 캐시를 재사용했습니다. 이전 요청 경계는 현재 경계의 "
                f"{fresh_osm_attempt.get('current_boundary_overlap_pct', '미상')}%와 중첩합니다."
            )
            if osm_cache_reused
            else None,
            "OSM 보행전용 경로는 공식 소방차 진입불가 판정이 아닙니다.",
            "GIS건물통합정보의 용도 미상 건물은 주거건물 수에서 제외했습니다."
            if building_source == "official_gis_building_integrated"
            else "OSM 건물 용도 미상은 주거건물 수에서 제외했습니다.",
            "ROAD_BT는 등록 도로폭이며 주정차 등을 반영한 실제 유효폭이 아닙니다."
            if not official_roads.empty
            else "공식 ROAD_BT 도로폭 자료가 없어 OSM 폭 태그만 사용했습니다.",
            "C는 현장 시야가 아닌 지도 네트워크 접근방향 대리지표입니다.",
            "기존 시설의 parcel_or_reference 좌표는 필지/참조점이며 실제 함체 실측점이 아닙니다.",
            "fallback_approximate 시설 좌표는 지도·거리·후보 제외에 사용하지 않았습니다.",
            fire_water_limitation,
        ],
    }
    quality["limitations"] = [item for item in quality["limitations"] if item]
    (output_dir / "data_quality.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "fire_water_data_quality.json").write_text(
        json.dumps(fire_water_quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "fire_infrastructure_data_quality.json").write_text(
        json.dumps(infrastructure_quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    top_rows = selected.loc[selected["recommended"]]
    if top_rows.empty:
        top_section = (
            "추천 3곳은 제시하지 않았습니다. 완전한 A/B/C 점수 또는 75m 간격의 10개 후보가 "
            "충족되지 않았기 때문입니다."
        )
    else:
        lines = ["|순위|후보 ID|잠정 적합도|A|B|C|선정 근거|", "|---:|---|---:|---:|---:|---:|---|"]
        for _, row in top_rows.iterrows():
            lines.append(
                f"|{int(row['rank'])}|{row['candidate_id']}|{row['score_map']:.2f}|"
                f"{row['A_score']}|{int(row['B_score'])}|{row['C_score']}|"
                f"{row['candidate_reason']}; {row['A_evidence']}|"
            )
        top_section = "\n".join(lines)

    all_lines = [
        "|순위|예산 단계|인접 도로명|위도|경도|점수|A/B/C|50m 주거건물|등록폭|",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in selected_web.iterrows():
        road_name = "미확인" if pd.isna(row.get("official_road_name")) else row["official_road_name"]
        width = "미상" if pd.isna(row.get("road_width_m")) else f"{row['road_width_m']:.1f}m"
        all_lines.append(
            f"|{int(row['rank'])}|{row['budget_scenario_tier']}|{road_name}|"
            f"{row['latitude']:.6f}|{row['longitude']:.6f}|{row['score_map']:.2f}|"
            f"{row['A_score']}/{int(row['B_score'])}/{row['C_score']}|"
            f"{int(row['residential_buildings_50m'])}동|{width}|"
        )
    all_candidates_section = "\n".join(all_lines)
    scenario_3 = ", ".join(selected_web.loc[selected_web["rank"].le(3), "official_road_name"])
    scenario_5 = ", ".join(selected_web.loc[selected_web["rank"].le(5), "official_road_name"])
    scenario_10 = ", ".join(selected_web.loc[selected_web["rank"].le(10), "official_road_name"])

    report = f"""# 부산 동구 수정제5동 보이는 소화기 설치 우선검토

## 결론

실제 공개 원자료에서 {len(selected)}개 후보 구역을 75m 이상 이격해 추렸습니다. 이 결과는 설치 확정이나 공식 소방차 진입불가 판정이 아니라 **현장조사 순서를 정하는 지도 기반 잠정 적합도**입니다. 추천 표시는 데이터 요구조건을 충족한 경우에만 상위 {settings['recommended_sites']}곳에 부여했습니다.

{top_section}

## 예산 규모별 검토 위치

- **3개소안:** {scenario_3}
- **5개소안:** {scenario_5}
- **10개소안:** {scenario_10}

아래 10곳은 모두 검증된 동일 후보 풀입니다. 예산 단계는 설치비 단가가 제공되지 않은 상태에서 순위별 개소 수만 나눈 것이며, 실제 사업비 추정은 아닙니다.

{all_candidates_section}

## PNU-ANCHOR와의 연결

PNU-ANCHOR는 부산 주택화재 {int(pnu_context.get('busan_residential_fire_events', 9792)):,}건을 이용해 **어느 지역을 먼저 볼 것인가**를 검토한 상위 분석입니다. 수정5동은 거리 보정 출동지연 순위 {int(pnu_context.get('distance_adjusted_delay_rank', 7))}/{int(pnu_context.get('compared_areas', 206))}, q={float(pnu_context.get('q_value', 0.034)):.3f}, 화재의 {int(pnu_context.get('target_fires_near_under_4m_pct', 86))}%가 폭 4m 미만 도로 50m 이내라는 근거로 선정됐습니다. 이 프로젝트는 그 결과를 받아 **수정5동 안에서 어디를 현장조사할 것인가**를 50:30:20 가중합으로 좁힙니다. 따라서 이 점수는 도시 전체 취약지수가 아니라 설치 후보 선별 점수입니다.

- 연계 저장소: {pnu_context.get('repository_url', 'https://github.com/PPPP98/PNU-ANCHOR')}
- 참조 커밋: `{pnu_context.get('reference_commit', '미상')}`
- 협소도로 기본값: 등록폭 4m 미만. 3.5m와 6m 결과는 `output/road_width_sensitivity.csv`에서 같은 10개 후보의 점수 민감도를 비교합니다.

## 수집 결과와 데이터 품질

- 행정경계: {boundary_meta.get('source')} / 기준 {boundary_meta.get('base_date') or boundary_meta.get('version') or boundary_meta.get('year')} / 면적 {boundary_meta.get('area_km2')} km² / 원본 CRS {boundary_meta.get('raw_crs')}
- 경계 경고: {'2024-10-01 참고용 경계로 2026년 현행 여부가 검증되지 않았습니다.' if boundary_meta.get('outdated') else 'SGIS 또는 입력 공식 경계를 사용했습니다.'}
- OSM 보행망: 노드 {len(walk_nodes):,}개, 방향성 에지 {len(walk_edges):,}개
- OSM 차량망: 방향성 에지 {len(drive_edges):,}개
- 계단 피처: {len(steps):,}개
- OSM 갱신 상태: {'2026-10-22 신규 호출 실패(로컬 프록시 연결 거부), 이전 성공 캐시 재사용. 이전 요청 경계와 현재 경계의 중첩률 ' + str(fresh_osm_attempt.get('current_boundary_overlap_pct')) + '%' if osm_cache_reused else '이번 실행에서 신규 호출 성공'}
- 건물: {building_meta.get('source')} / 기준 {building_meta.get('version')} / 수정5동 교차 폴리곤 {building_total:,}개, 주거용 식별 {len(residential):,}개, 용도미상 {building_unknown:,}개 ({(building_unknown / building_total * 100) if building_total else 0:.1f}%)
- 공식 도로폭: {official_road_meta.get('source')} / 기준 {official_road_meta.get('version')} / 경계 주변 {len(official_roads):,}개 구간 중 유효폭 {official_width_valid:,}개. 최종 후보 {len(selected)}곳 중 {candidate_official_width_matches}곳에 20m 이내 최근접 `ROAD_BT`를 연결했습니다.
- 기존 시설: PNU-ANCHOR 전처리 자료 13,151건 중 `parcel_or_reference`이면서 사용 가능한 소방용수 {target_fire_water}건을 참고 레이어로 표시했습니다. {existing_equipment_decision} 근사 좌표는 지도·점수·후보 제외에 사용하지 않았습니다.
- 별도 소방용수 현황 ZIP(2025): 부산 {fire_water_quality.get('rows', 0):,}건, 동구 {fire_water_dong_gu.get('rows', 0):,}건을 확인했으나 동구 좌표 완전 건수가 {fire_water_dong_gu.get('coordinate_complete_rows', 0):,}건이라 공간분석에서 제외했습니다.
- OSM 폭 태그: 중복 제거 보행도로 {len(roads):,}개 중 {width_valid:,}개 ({(width_valid / len(roads) * 100) if len(roads) else 0:.1f}%). 공식 폭 연결이 없을 때만 보조로 사용했고, 폭 미상은 임의로 채우지 않았습니다.
- 기존 비상소화장치 후보 제외 {'적용: ' + str(excluded_near_existing_equipment) + '개 제외' if use_existing_emergency_equipment_exclusion else '미적용'} → 1차 후보 풀 {candidate_pool_before_density_screen:,}개 → 주거밀도 최저등급(B=1) {excluded_low_density_candidates:,}개 제외 → 적격 후보 풀 {len(pool):,}개 → 75m 공간분산 후 {len(selected):,}개, 완전한 WSM 점수 {complete_scores:,}개
- API 원본·호출 기록: `cache/download_manifest.json`, OSMnx HTTP 응답 캐시: `cache/osmnx_http/`

## 평가법

소방관 인터뷰 녹취 1건과 주민 2명 인터뷰를 정성적으로 코딩해 연구팀의 **주관적 가중치** A 50%, B 30%, C 20%를 적용했습니다. 소방관의 실제 차량 미진입·호스 도보 전개 경험과 주민들의 협소골목 우려를 근거로 A를 가장 높게 두고, 주택 밀집 노출을 B, 발견·이용 용이성을 C로 반영했습니다. 통계적으로 추정된 가중치는 아니며 상세 근거는 `interview_evidence.md`에 정리했습니다.

- A(소방차 접근 어려움 대리점수): 기본 1점에서 25m 이내 계단 +2, 차량망과 15m 초과 이격된 보행골목 +1, 차량망 거리 25m 초과 +1 또는 50m 초과 +2, 관측폭 {float(settings['road_width_narrow_m']):.1f}m 미만 +2 후 최대 5점. PNU-ANCHOR의 기본 협소도로 노출값과 맞춘 것이며 법적·공식 진입불가 기준은 아닙니다. 폭은 20m 이내 공식 `ROAD_BT`를 우선하고 없으면 OSM 폭 태그를 사용했습니다. 폭 미상은 가감하지 않았습니다.
- B(주거건물 밀집): 2026-10-09 GIS건물통합정보에서 주용도코드 `01000`(단독주택), `02000`(공동주택)인 건물을 식별하고, 각 후보 50m 이내 개수를 후보 풀의 동률 보정 분위순위로 1~5점화했습니다. 용도 미상은 제외했습니다.
- 주거 희박 지점 방지: 50m 안에 식별된 주거건물이 1동도 없는 도로망 노드를 먼저 제외하고, 전체 1차 후보 중 주거밀도 최저등급(B=1)도 최종 후보 산정 전에 제외했습니다. B점수는 이 제외 전 1차 후보 풀을 기준으로 산정해 문턱을 반복 적용하지 않았습니다.
- C(가시성/접근성 대리점수): 보행망 접근방향 1개=1, 2개=2, 3개=4, 4개 이상=5. 실제 시야가 아니며 `visibility_proxy=true`, `needs_field_check=true`입니다. `input/visibility_field.csv`의 `candidate_id,c_score`(1~5)로 현장값을 넣으면 대체됩니다.
- 종합점수: `0.5*A + 0.3*B + 0.2*C`. A/B/C 중 하나라도 계산할 수 없으면 종합점수를 만들지 않았습니다.

## 해석상 한계

- OSM 보행전용 경로는 소방차 진입불가를 공식 판정하지 않습니다. 불법주정차와 실제 유효폭도 현장에서 확인해야 합니다.
- 자료상 비상소화장치 필지/참조 좌표는 사용자 현장 정보와 일치하지 않아 지도 표시와 후보 제외에 사용하지 않았습니다.
- 후보는 도로망 노드의 **후보 구역 중심점**입니다. 실제 벽면, 설치 권한, 전기·통신·관리주체는 확인되지 않았습니다.
- 주택 밀집은 건물 수 대리지표이며 노후배선이나 실제 화재위험을 뜻하지 않습니다.
- 화면 배경지도: 외부 지도 타일 없이 부산 행정동 {len(busan_dongs):,}개와 폭 12m 이상 주요도로 {len(busan_major_roads):,}개를 전체 배경으로 표시하고, 수정5동 안에만 상세 건물·도로·추천 후보를 표시했습니다. 따라서 타일 서버 403과 무관하게 `output/map.html`을 직접 열 수 있습니다.
- 주민 안내 패널: 후보 10곳의 중심좌표·인접 도로명·등록폭·50m 주거건물 수와 우선 추천 3곳을 목록화하고, 후보 선택 시 지도를 확대하도록 연결했습니다. 오류가 발생하던 YouTube 임베드는 제거하고 새 창 링크와 로컬 4단계 그림·설명을 제공합니다.
- 분석 피처 중 OSM 보행망: © OpenStreetMap contributors. 공식 경계·도로·건물 자료는 위 수집 결과에 기재한 버전 자료를 사용했습니다.

## 현장점검 체크리스트

1. 후보 반경의 기존 소화기·소화전·비상소화장치 및 관리주체 확인
2. 소방차/호스 실제 접근 동선, 상시 주정차, 계단 폭과 경사 확인
3. 도로 실유효폭과 회차 가능성 측정(등록폭·OSM 태그와 구분)
4. 여러 접근방향에서 표지·함체가 보이는지 주야간 확인 후 C점수 입력
5. 설치 벽면·기초, 사유지 동의, 방수·동파·훼손 위험과 점검 동선 확인

## 산출물

- `output/map.html`: 경계·도로·계단·후보 10곳·조건 충족 시 추천 3곳
- `output/candidates.csv`, `output/candidates.geojson`: 점수와 근거·결측 포함
- `output/budget_candidates.csv`: 3·5·10개소 예산 시나리오 단계 포함
- `output/roads.geojson`: 분석에 사용한 OSM 보행도로
- `output/official_roads.geojson`: 후보 도로폭 연결에 사용한 2026-06 도로명주소 도로구간
- `output/existing_fire_infrastructure.geojson`: 대상지역 안에서 위치 품질을 통과한 소방용수 참고 레이어
- `output/road_width_sensitivity.csv`: 3.5m·4m·6m 기준에 따른 선택 후보 10곳의 점수·순위 비교
- `output/data_quality.json`: 수집 개수, 누락률, 공간 검증 결과
- `output/fire_water_data_quality.json`: 제공된 2025 부산 소방용수 현황의 좌표·주소·중복 품질 점검
- `output/fire_infrastructure_data_quality.json`: PNU-ANCHOR 시설 좌표 등급과 사용 범위
"""
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    budget_report = f"""# 수정5동 보이는 소화기 예산 검토 위치안

## 예산 규모별 위치

- **3개소안:** {scenario_3}
- **5개소안:** {scenario_5}
- **10개소안:** {scenario_10}

단가·부대공사비가 제공되지 않아 금액은 계산하지 않았습니다. 5개소안은 3개소안에 4~5순위를 더하고, 10개소안은 1~10순위를 모두 포함하는 누적 시나리오입니다.

## 전체 후보 10개소

{all_candidates_section}

## 보고 시 주의 문구

- 설치 확정지가 아니라 **현장조사 전 예산 검토 후보 구역 중심점**입니다.
- 점수는 인터뷰 기반 주관적 가중치 A 50%, B 30%, C 20%를 적용한 **현장조사 우선순위용 지도 기반 잠정 적합도**입니다. 실제 인구·보행량·기존 소방시설·토지권리는 포함되지 않았습니다.
- 기존 시설 중복, 실제 유효 도로폭, 시야, 설치 벽면과 토지·관리 권한은 현장에서 확인해야 합니다.
- 2026-10-22 신규 Overpass 호출은 프록시 연결 거부로 실패하여 앞선 성공 캐시를 재사용했습니다. 이전 요청 경계와 2025-06-30 공식 경계의 중첩률은 {fresh_osm_attempt.get('current_boundary_overlap_pct')}%입니다.
"""
    (output_dir / "budget_sites.md").write_text(budget_report, encoding="utf-8")
    print(f"Created {len(selected)} candidates; recommendations={int(selected['recommended'].sum())}")
    print(f"Map: {output_dir / 'map.html'}")
    return 0 if can_recommend else 4


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml", type=Path)
    args = parser.parse_args()
    raise SystemExit(analyze(args.config))


if __name__ == "__main__":
    main()
