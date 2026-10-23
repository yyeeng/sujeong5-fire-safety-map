from __future__ import annotations

import pandas as pd
import pytest

from fire_safety_map.analysis import (
    firetruck_difficulty_score,
    parse_width_m,
    residential_density_scores,
    visibility_accessibility_score,
    weighted_score,
)
from fire_safety_map.official import official_building_category, repair_korean_mojibake


def test_width_parser_handles_units_and_invalid_ranges() -> None:
    assert parse_width_m("2.5 m") == pytest.approx(2.5)
    assert parse_width_m("10 ft") == pytest.approx(3.048)
    assert parse_width_m(["4", "2.8"]) == pytest.approx(2.8)
    assert parse_width_m("0") is None
    assert parse_width_m("45") is None


def test_firetruck_score_does_not_impute_missing_width() -> None:
    score, evidence, missing = firetruck_difficulty_score(
        near_steps=True,
        distance_to_drive_m=55,
        road_width_m=None,
        foot_only_evidence=True,
    )
    assert score == 5
    assert "도로폭" in missing
    assert any("계단" in item for item in evidence)


def test_all_missing_access_evidence_returns_no_score() -> None:
    score, evidence, missing = firetruck_difficulty_score(
        near_steps=None,
        distance_to_drive_m=None,
        road_width_m=None,
        foot_only_evidence=None,
    )
    assert score is None
    assert evidence == []
    assert len(missing) == 4


def test_density_scoring_is_tie_safe_and_all_zero_is_missing() -> None:
    scored = residential_density_scores([1, 1, 4, 9, 9])
    assert scored.iloc[0] == scored.iloc[1]
    assert scored.iloc[3] == scored.iloc[4] == 5
    zeros = residential_density_scores([0, 0, 0])
    assert zeros.isna().all()


def test_visibility_and_weighted_score() -> None:
    assert visibility_accessibility_score(3) == 4
    assert visibility_accessibility_score(4) == 5
    score = weighted_score(
        {"A": 5, "B": 3, "C": 4}, {"A": 0.5, "B": 0.3, "C": 0.2}
    )
    assert score == pytest.approx(4.2)
    assert weighted_score(
        {"A": 5, "B": pd.NA, "C": 4}, {"A": 0.5, "B": 0.3, "C": 0.2}
    ) is None


def test_official_building_use_classification_and_encoding_repair() -> None:
    assert repair_korean_mojibake("´Üµ¶ÁÖÅÃ") == "단독주택"
    assert repair_korean_mojibake("단독주택") == "단독주택"
    assert official_building_category("01000", "단독주택") == "residential"
    assert official_building_category("02000", "공동주택") == "residential"
    assert official_building_category(None, None) == "unknown"
    assert official_building_category("03000", "제1종근린생활시설") == "other"
