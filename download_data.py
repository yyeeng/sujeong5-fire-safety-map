"""Download the Sujeong 5-dong boundary and OpenStreetMap source data.

The script never substitutes demo coordinates. Every successful source response
is cached, and every failure is recorded in cache/download_manifest.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import geopandas as gpd
import osmnx as ox
import requests
import yaml
from dotenv import load_dotenv


GITHUB_BOUNDARY_URL = (
    "https://raw.githubusercontent.com/schwa456/hangjeongdong/master/"
    "ver20241001/HangJeongDong_ver20241001.geojson"
)
SGIS_STAGE_URL = "https://sgisapi.mods.go.kr/OpenAPI3/addr/stage.json"
TARGET_NORMALIZED = "부산광역시동구수정5동"
TARGET_NAME_VARIANTS = {TARGET_NORMALIZED, "수정5동", "수정제5동"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_name(value: object) -> str:
    return str(value).replace(" ", "").replace("제", "").strip()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def request_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 3,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, params=params, timeout=(15, 180))
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # network/API failures must be preserved verbatim
            last_error = exc
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last_error}")


def infer_crs(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if frame.crs:
        return frame
    minx, miny, maxx, maxy = frame.total_bounds
    if 120 <= minx <= 140 and 120 <= maxx <= 140 and 30 <= miny <= 45:
        return frame.set_crs("EPSG:4326")
    if 500_000 <= minx <= 1_500_000 and 1_000_000 <= miny <= 2_500_000:
        return frame.set_crs("EPSG:5179")
    raise ValueError(f"Cannot infer CRS from bounds {(minx, miny, maxx, maxy)}")


def select_target_boundary(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    candidates = frame.copy()
    exact_mask = None
    for column in candidates.columns:
        if column == candidates.geometry.name:
            continue
        normalized = candidates[column].astype(str).map(normalize_name)
        column_mask = normalized.isin(TARGET_NAME_VARIANTS)
        exact_mask = column_mask if exact_mask is None else (exact_mask | column_mask)
    if exact_mask is None or not exact_mask.any():
        names: list[str] = []
        for column in candidates.columns:
            if column != candidates.geometry.name:
                values = candidates[column].dropna().astype(str)
                names.extend(values[values.str.contains("수정", na=False)].head(20).tolist())
        raise ValueError(f"Exact 부산 동구 수정제5동 feature not found; nearby values={names[:20]}")
    selected = candidates.loc[exact_mask].copy()
    if len(selected) != 1:
        raise ValueError(f"Expected one exact boundary, found {len(selected)}")
    selected = infer_crs(selected).to_crs("EPSG:4326")
    selected.geometry = selected.geometry.make_valid()
    if selected.geometry.is_empty.any() or not selected.geometry.is_valid.all():
        raise ValueError("Selected boundary geometry is empty or invalid")
    minx, miny, maxx, maxy = selected.total_bounds
    if not (128.9 < minx < 129.3 and 128.9 < maxx < 129.3 and 34.9 < miny < 35.4):
        raise ValueError(f"Boundary is not in Busan: bounds={(minx, miny, maxx, maxy)}")
    area_km2 = float(selected.to_crs("EPSG:5179").area.iloc[0] / 1_000_000)
    if not 0.05 < area_km2 < 20:
        raise ValueError(f"Implausible administrative-dong area: {area_km2:.4f} km2")
    return selected


def find_input_boundary(input_dir: Path) -> tuple[gpd.GeoDataFrame, Path] | None:
    if not input_dir.exists():
        return None
    paths = [
        path
        for path in input_dir.rglob("*")
        if path.suffix.lower() in {".shp", ".geojson", ".json", ".gpkg"}
    ]
    paths.sort(
        key=lambda path: (
            0 if "BND_ADM_DONG" in path.stem.upper() else 1,
            0 if "boundary" in str(path).lower() else 1,
            str(path),
        )
    )
    for path in paths:
        try:
            frame = gpd.read_file(path)
            return select_target_boundary(frame), path
        except Exception:
            continue
    return None


def sgis_result_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if int(payload.get("errCd", -1)) != 0:
        raise ValueError(f"SGIS error {payload.get('errCd')}: {payload.get('errMsg')}")
    result = payload.get("result", [])
    if isinstance(result, dict):
        result = result.get("resultdata", [])
    if not isinstance(result, list):
        raise ValueError("Unexpected SGIS stage response structure")
    return result


def find_sgis_adm_cd(session: requests.Session, token: str) -> str:
    levels = [
        (None, lambda row: "부산" in str(row.get("addr_name", ""))),
        ("province", lambda row: normalize_name(row.get("addr_name")) == "동구"),
        ("district", lambda row: normalize_name(row.get("addr_name")) == "수정5동"),
    ]
    parent_code: str | None = None
    for label, matcher in levels:
        params: dict[str, Any] = {"accessToken": token, "pg_yn": 0}
        if parent_code:
            params["cd"] = parent_code
        rows = sgis_result_list(request_json(session, SGIS_STAGE_URL, params=params))
        matches = [row for row in rows if matcher(row)]
        if len(matches) != 1:
            raise ValueError(f"SGIS {label or 'province'} lookup produced {len(matches)} matches")
        parent_code = str(matches[0].get("cd") or matches[0].get("adm_cd") or "")
        if not parent_code:
            raise ValueError(f"SGIS {label or 'province'} row has no code")
    if len(parent_code) != 8:
        raise ValueError(f"SGIS returned non-8-digit adm_cd={parent_code!r}")
    return parent_code


def boundary_from_sgis(
    session: requests.Session,
    config: dict[str, Any],
    raw_dir: Path,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    key = os.getenv("SGIS_CONSUMER_KEY")
    secret = os.getenv("SGIS_CONSUMER_SECRET")
    if not key or not secret:
        raise RuntimeError("SGIS_CONSUMER_KEY/SGIS_CONSUMER_SECRET are not configured")
    auth = request_json(
        session,
        config["sources"]["sgis_auth"],
        params={"consumer_key": key, "consumer_secret": secret},
    )
    if int(auth.get("errCd", -1)) != 0:
        raise ValueError(f"SGIS authentication failed: {auth.get('errMsg')}")
    token = auth.get("result", {}).get("accessToken")
    if not token:
        raise ValueError("SGIS authentication response had no accessToken")
    adm_cd = find_sgis_adm_cd(session, token)
    year = int(config["project"]["boundary_year"])
    payload = request_json(
        session,
        config["sources"]["sgis_boundary"],
        params={"accessToken": token, "year": year, "adm_cd": adm_cd, "low_search": 0},
    )
    if int(payload.get("errCd", -1)) != 0 or not payload.get("features"):
        raise ValueError(f"SGIS boundary failed: {payload.get('errCd')} {payload.get('errMsg')}")
    raw_path = raw_dir / f"sgis_boundary_{year}_{adm_cd}.geojson"
    write_json(raw_path, payload)
    frame = gpd.GeoDataFrame.from_features(payload["features"], crs="EPSG:5179")
    selected = select_target_boundary(frame)
    return selected, {
        "source": "SGIS",
        "source_url": config["sources"]["sgis_boundary"],
        "year": year,
        "adm_cd": adm_cd,
        "raw_crs": "EPSG:5179",
        "raw_path": str(raw_path),
        "outdated": False,
    }


def boundary_from_github(
    session: requests.Session, raw_dir: Path
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    response = session.get(GITHUB_BOUNDARY_URL, timeout=(15, 240))
    response.raise_for_status()
    raw_path = raw_dir / "HangJeongDong_ver20241001.geojson"
    raw_path.write_bytes(response.content)
    frame = gpd.read_file(raw_path)
    selected = select_target_boundary(frame)
    return selected, {
        "source": "GitHub hangjeongdong fallback",
        "source_url": GITHUB_BOUNDARY_URL,
        "year": 2024,
        "version": "ver20241001",
        "raw_crs": "EPSG:4326",
        "raw_path": str(raw_path),
        "sha256": sha256_bytes(response.content),
        "outdated": True,
        "warning": "2024-10-01 경계는 2026년 현행 경계로 검증되지 않은 참고용 자료입니다.",
    }


def acquire_boundary(
    session: requests.Session,
    config: dict[str, Any],
    input_dir: Path,
    raw_dir: Path,
) -> tuple[gpd.GeoDataFrame, dict[str, Any], list[str]]:
    failures: list[str] = []
    local = find_input_boundary(input_dir)
    if local:
        selected, path = local
        raw = gpd.read_file(path)
        base_date = None
        if "BASE_DATE" in selected.columns and selected["BASE_DATE"].notna().any():
            base_date = str(selected["BASE_DATE"].dropna().iloc[0])
        adm_cd = None
        if "ADM_CD" in selected.columns and selected["ADM_CD"].notna().any():
            adm_cd = str(selected["ADM_CD"].dropna().iloc[0])
        return selected, {
            "source": "행정동 경계 SHP (입력 파일)",
            "source_url": None,
            "year": int(base_date[:4]) if base_date and base_date[:4].isdigit() else None,
            "base_date": base_date,
            "adm_cd": adm_cd,
            "raw_crs": str(raw.crs),
            "raw_path": str(path),
            "outdated": False,
        }, failures
    try:
        selected, metadata = boundary_from_sgis(session, config, raw_dir)
        return selected, metadata, failures
    except Exception as exc:
        failures.append(f"SGIS: {type(exc).__name__}: {exc}")
    try:
        selected, metadata = boundary_from_github(session, raw_dir)
        return selected, metadata, failures
    except Exception as exc:
        failures.append(f"GitHub fallback: {type(exc).__name__}: {exc}")
    raise RuntimeError("No valid boundary source. " + " | ".join(failures))


def with_retry(label: str, operation: Callable[[], Any], attempts: int = 2) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(5 * attempt)
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last_error}")


def write_feature_geojson(frame: gpd.GeoDataFrame, path: Path) -> None:
    clean = frame.copy()
    for column in clean.columns:
        if column != clean.geometry.name:
            clean[column] = clean[column].map(
                lambda value: json.dumps(value, ensure_ascii=False)
                if isinstance(value, (list, tuple, set, dict))
                else value
            )
    clean.to_file(path, driver="GeoJSON")


def download_osm(
    polygon: Any,
    cache_dir: Path,
    manifest: dict[str, Any],
) -> None:
    ox.settings.use_cache = True
    ox.settings.cache_folder = cache_dir / "osmnx_http"
    ox.settings.requests_timeout = 180
    ox.settings.overpass_rate_limit = True
    ox.settings.log_console = True
    ox.settings.http_user_agent = "sujeong5-visible-extinguisher-research/1.0"
    extra_tags = ["width", "access", "motor_vehicle", "service", "surface"]
    ox.settings.useful_tags_way = sorted(set(ox.settings.useful_tags_way + extra_tags))

    calls: list[tuple[str, Callable[[], Any]]] = [
        (
            "walk_network",
            lambda: ox.graph.graph_from_polygon(
                polygon, network_type="walk", simplify=True, retain_all=True
            ),
        ),
        (
            "drive_network",
            lambda: ox.graph.graph_from_polygon(
                polygon, network_type="drive", simplify=True, retain_all=True
            ),
        ),
        (
            "path_features",
            lambda: ox.features.features_from_polygon(
                polygon, {"highway": ["steps", "footway", "path"]}
            ),
        ),
        (
            "buildings",
            lambda: ox.features.features_from_polygon(polygon, {"building": True}),
        ),
        (
            "exclusions",
            lambda: ox.features.features_from_polygon(
                polygon,
                {
                    "amenity": ["school", "kindergarten", "college", "university"],
                    "landuse": ["railway"],
                    "railway": True,
                },
            ),
        ),
    ]

    for label, operation in calls:
        started = utc_now()
        try:
            result = with_retry(label, operation)
            if label.endswith("network"):
                path = cache_dir / f"{label}.graphml"
                ox.io.save_graphml(result, filepath=path)
                manifest["datasets"][label] = {
                    "success": True,
                    "started_at": started,
                    "finished_at": utc_now(),
                    "path": str(path),
                    "nodes": int(len(result.nodes)),
                    "edges": int(len(result.edges)),
                    "source": "OpenStreetMap via Overpass API/OSMnx",
                }
            else:
                path = cache_dir / f"{label}.geojson"
                write_feature_geojson(result.reset_index(), path)
                manifest["datasets"][label] = {
                    "success": True,
                    "started_at": started,
                    "finished_at": utc_now(),
                    "path": str(path),
                    "features": int(len(result)),
                    "source": "OpenStreetMap via Overpass API/OSMnx",
                }
        except Exception as exc:
            manifest["datasets"][label] = {
                "success": False,
                "started_at": started,
                "finished_at": utc_now(),
                "error": f"{type(exc).__name__}: {exc}",
                "source": "OpenStreetMap via Overpass API/OSMnx",
            }
        write_json(cache_dir / "download_manifest.json", manifest)


def register_reused_osm_cache(cache_dir: Path, manifest: dict[str, Any]) -> None:
    """Validate and register files from a previous successful Overpass run."""

    graph_names = ["walk_network", "drive_network"]
    feature_names = ["path_features", "buildings", "exclusions"]
    for label in graph_names:
        path = cache_dir / f"{label}.graphml"
        if not path.exists():
            manifest["datasets"][label] = {
                "success": False,
                "error": f"Reusable cache is missing: {path}",
            }
            continue
        try:
            graph = ox.io.load_graphml(path)
            manifest["datasets"][label] = {
                "success": True,
                "path": str(path),
                "nodes": int(len(graph.nodes)),
                "edges": int(len(graph.edges)),
                "source": "OpenStreetMap via previous successful Overpass/OSMnx cache",
                "cache_reused": True,
                "freshness_unverified": True,
            }
        except Exception as exc:
            manifest["datasets"][label] = {
                "success": False,
                "path": str(path),
                "error": f"Cached graph validation failed: {type(exc).__name__}: {exc}",
            }
    for label in feature_names:
        path = cache_dir / f"{label}.geojson"
        if not path.exists():
            manifest["datasets"][label] = {
                "success": False,
                "error": f"Reusable cache is missing: {path}",
            }
            continue
        try:
            frame = gpd.read_file(path)
            manifest["datasets"][label] = {
                "success": True,
                "path": str(path),
                "features": int(len(frame)),
                "source": "OpenStreetMap via previous successful Overpass/OSMnx cache",
                "cache_reused": True,
                "freshness_unverified": True,
            }
        except Exception as exc:
            manifest["datasets"][label] = {
                "success": False,
                "path": str(path),
                "error": f"Cached GeoJSON validation failed: {type(exc).__name__}: {exc}",
            }


def run(config_path: Path, *, reuse_osm_cache: bool = False) -> int:
    root = config_path.resolve().parent
    cache_dir = root / "cache"
    raw_dir = cache_dir / "raw"
    input_dir = root / "input"
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    input_dir.mkdir(parents=True, exist_ok=True)
    load_dotenv(root / ".env")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    session = requests.Session()
    session.headers.update({"User-Agent": "sujeong5-visible-extinguisher-research/1.0"})
    manifest: dict[str, Any] = {
        "started_at": utc_now(),
        "target": config["project"]["area_name"],
        "datasets": {},
        "boundary_attempt_failures": [],
    }

    try:
        boundary, boundary_meta, failures = acquire_boundary(
            session, config, input_dir, raw_dir
        )
        boundary_path = cache_dir / "boundary.geojson"
        boundary.to_file(boundary_path, driver="GeoJSON")
        area_km2 = float(boundary.to_crs("EPSG:5179").area.iloc[0] / 1_000_000)
        boundary_meta.update(
            {
                "success": True,
                "path": str(boundary_path),
                "feature_count": 1,
                "output_crs": "EPSG:4326",
                "area_km2": round(area_km2, 6),
            }
        )
        manifest["datasets"]["boundary"] = boundary_meta
        manifest["boundary_attempt_failures"] = failures
        write_json(cache_dir / "download_manifest.json", manifest)
    except Exception as exc:
        manifest["datasets"]["boundary"] = {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        manifest["finished_at"] = utc_now()
        write_json(cache_dir / "download_manifest.json", manifest)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if reuse_osm_cache:
        manifest["fresh_osm_attempt"] = {
            "success": False,
            "attempted_at": utc_now(),
            "error": (
                "Fresh Overpass request failed in this environment: local proxy "
                "127.0.0.1:9 refused the connection. Reusing files from the earlier "
                "successful Overpass/OSMnx run."
            ),
        }
        previous_boundary_path = raw_dir / "HangJeongDong_ver20241001.geojson"
        if previous_boundary_path.exists():
            try:
                previous_boundary = select_target_boundary(gpd.read_file(previous_boundary_path))
                current_metric = boundary.to_crs("EPSG:5179").geometry.iloc[0]
                previous_metric = previous_boundary.to_crs("EPSG:5179").geometry.iloc[0]
                overlap = current_metric.intersection(previous_metric).area
                manifest["fresh_osm_attempt"].update(
                    {
                        "cached_request_boundary": str(previous_boundary_path),
                        "cached_boundary_version": "ver20241001",
                        "current_boundary_overlap_pct": round(
                            overlap / current_metric.area * 100, 4
                        ),
                    }
                )
            except Exception as exc:
                manifest["fresh_osm_attempt"]["boundary_overlap_check_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
        register_reused_osm_cache(cache_dir, manifest)
    else:
        download_osm(boundary.geometry.iloc[0], cache_dir, manifest)
    manifest["finished_at"] = utc_now()
    required = ["boundary", "walk_network", "drive_network", "path_features", "buildings"]
    manifest["required_success"] = all(
        manifest["datasets"].get(name, {}).get("success") for name in required
    )
    write_json(cache_dir / "download_manifest.json", manifest)

    for name in required + ["exclusions"]:
        item = manifest["datasets"].get(name, {})
        status = "OK" if item.get("success") else "FAILED"
        detail = item.get("path") or item.get("error", "")
        print(f"[{status}] {name}: {detail}")
    if manifest["datasets"]["boundary"].get("outdated"):
        print("WARNING: 2024-10-01 boundary fallback is outdated for a 2026 decision.")
    return 0 if manifest["required_success"] else 3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml", type=Path)
    parser.add_argument(
        "--reuse-osm-cache",
        action="store_true",
        help="Validate and reuse an earlier successful OSM download without a fresh request",
    )
    args = parser.parse_args()
    raise SystemExit(run(args.config, reuse_osm_cache=args.reuse_osm_cache))


if __name__ == "__main__":
    main()
