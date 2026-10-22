"""Pure scoring helpers for the visible-extinguisher site screening analysis."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping

import pandas as pd


def parse_width_m(value: object) -> float | None:
    """Parse one plausible OSM/road width value in metres.

    Lists use the minimum valid width because the screening question concerns the
    narrowest observed value. Values outside (0, 40] are treated as invalid.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        parsed = [parse_width_m(item) for item in value]
        valid = [item for item in parsed if item is not None]
        return min(valid) if valid else None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip().lower().replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    width = float(match.group())
    if "ft" in text or "feet" in text:
        width *= 0.3048
    return width if 0 < width <= 40 else None


def firetruck_difficulty_score(
    *,
    near_steps: bool | None,
    distance_to_drive_m: float | None,
    road_width_m: float | None,
    foot_only_evidence: bool | None,
    narrow_threshold_m: float = 4.0,
) -> tuple[int | None, list[str], list[str]]:
    """Return a 1-5 proxy score, evidence strings, and missing-input labels.

    This is a screening proxy, never an official fire-engine access finding.
    Unknown width does not automatically raise or lower the score.
    """
    observed = [near_steps, distance_to_drive_m, road_width_m, foot_only_evidence]
    if all(value is None for value in observed):
        return None, [], ["계단", "차량도로 거리", "도로폭", "보행전용 증거"]

    score = 1
    evidence: list[str] = []
    missing: list[str] = []

    if near_steps is None:
        missing.append("계단 근접 여부")
    elif near_steps:
        score += 2
        evidence.append("25m 이내 OSM 계단")

    if foot_only_evidence is None:
        missing.append("보행 네트워크 단독 연결 여부")
    elif foot_only_evidence:
        score += 1
        evidence.append("보행망에는 있으나 차량망과 15m 초과 이격")

    if distance_to_drive_m is None or math.isnan(float(distance_to_drive_m)):
        missing.append("차량도로까지 거리")
    elif distance_to_drive_m > 50:
        score += 2
        evidence.append(f"차량망까지 {distance_to_drive_m:.1f}m (>50m)")
    elif distance_to_drive_m > 25:
        score += 1
        evidence.append(f"차량망까지 {distance_to_drive_m:.1f}m (>25m)")

    if road_width_m is None or math.isnan(float(road_width_m)):
        missing.append("도로폭")
    elif road_width_m < narrow_threshold_m:
        score += 2
        evidence.append(f"관측 도로폭 {road_width_m:.1f}m (<{narrow_threshold_m:.1f}m)")

    if not evidence:
        evidence.append("계단·차량망 이격·협소폭 가점 없음")
    return min(score, 5), evidence, missing


def residential_density_scores(values: Iterable[int | float | None]) -> pd.Series:
    """Map building counts to a tie-safe 1-5 empirical rank scale.

    If all usable counts are zero, the result is missing rather than a fabricated
    low score. If every positive count is tied, all receive the neutral score 3.
    """
    series = pd.Series(list(values), dtype="Float64")
    usable = series.dropna()
    if usable.empty or float(usable.max()) <= 0:
        return pd.Series([pd.NA] * len(series), dtype="Float64")
    positive = series.notna()
    if usable.nunique(dropna=True) == 1:
        result = pd.Series([pd.NA] * len(series), dtype="Float64")
        result.loc[positive] = 3.0
        return result
    ranks = series.rank(method="average", pct=True)
    scores = (ranks * 5).apply(lambda x: math.ceil(x) if pd.notna(x) else pd.NA)
    return scores.clip(lower=1, upper=5).astype("Float64")


def visibility_accessibility_score(street_degree: int | float | None) -> int | None:
    """Map network approach directions to a 1-5 map-based visibility proxy."""
    if street_degree is None or pd.isna(street_degree):
        return None
    degree = int(street_degree)
    if degree <= 1:
        return 1
    if degree == 2:
        return 2
    if degree == 3:
        return 4
    return 5


def weighted_score(
    components: Mapping[str, int | float | None],
    weights: Mapping[str, float],
) -> float | None:
    """Compute the declared WSM only when every required component is present."""
    if set(components) != set(weights):
        raise ValueError("components and weights must have identical keys")
    if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
        raise ValueError("weights must sum to 1.0")
    if any(value is None or pd.isna(value) for value in components.values()):
        return None
    return round(sum(float(components[key]) * weights[key] for key in weights), 3)
