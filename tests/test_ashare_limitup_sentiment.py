"""VECTOR layer: sentiment metrics and the config-driven tier. Descriptive, not a forecast."""
from __future__ import annotations

import copy
from datetime import datetime
import json
from pathlib import Path

import pytest

from ashare_limitup_fakes import FakeFetch, body, dt_row, payload, prev_row, session_bodies, zb_row, zt_row

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-11-16"
AFTER_CLOSE = "2026-11-16T07:30:00+00:00"


def rows(pool, raw_rows):
    from ashare_limitup import pools

    return pools.parse_pool(pool, payload(raw_rows)).rows


def session_pools(**overrides):
    parsed = {
        "limit_up": rows("limit_up", [zt_row("990001", lbc=3), zt_row("990002", lbc=2),
                                      zt_row("990003", lbc=1), zt_row("991004", lbc=1, m=1)]),
        "broken_board": rows("broken_board", [zb_row("990005")]),
        "limit_down": rows("limit_down", []),
        "yesterday_limit_up": rows("yesterday_limit_up", [
            prev_row("990001", zdp=10.0, ylbc=2), prev_row("990002", zdp=10.0),
            prev_row("990005", zdp=4.0), prev_row("990006", zdp=-2.0), prev_row("990007", zdp=-6.0)]),
    }
    parsed.update(overrides)
    return parsed


def shipped_document():
    return json.loads((ROOT / "config" / "ashare_limitup.json").read_text(encoding="utf-8"))


def shipped():
    from ashare_limitup import config

    return config.load_config()


NEUTRAL = {"limit_up_count": 50, "limit_down_count": 10, "broken_board_rate": 0.30,
           "max_consecutive_boards": 4, "promotion_rate": 0.30, "yesterday_limit_up_avg_change_pct": 1.0}
HOT = {"limit_up_count": 90, "limit_down_count": 2, "broken_board_rate": 0.10,
       "max_consecutive_boards": 7, "promotion_rate": 0.50, "yesterday_limit_up_avg_change_pct": 4.0}
COLD = {"limit_up_count": 20, "limit_down_count": 40, "broken_board_rate": 0.50,
        "max_consecutive_boards": 2, "promotion_rate": 0.10, "yesterday_limit_up_avg_change_pct": -2.0}


def metrics_scoring(score):
    """Shipped-config metrics whose bands add up to `score` (hot ones first, or cold ones first)."""
    names = list(NEUTRAL)
    source = HOT if score > 0 else COLD
    return {name: (source[name] if index < abs(score) else NEUTRAL[name]) for index, name in enumerate(names)}


def test_metrics_follow_their_stated_definitions():
    from ashare_limitup import sentiment

    assert sentiment.compute_metrics(session_pools()) == {
        "limit_up_count": 4,
        "limit_down_count": 0,
        "broken_board_count": 1,
        "broken_board_rate": pytest.approx(1 / 5),          # broken / (limit-up + broken)
        "max_consecutive_boards": 3,
        "yesterday_limit_up_count": 5,
        "promotion_rate": pytest.approx(2 / 5),             # yesterday's limit-ups that are limit-up again
        "yesterday_limit_up_avg_change_pct": pytest.approx(3.2),
        "yesterday_limit_up_median_change_pct": pytest.approx(4.0),
        "yesterday_limit_up_red_ratio": pytest.approx(3 / 5),
    }


def test_promotion_joins_on_code_so_it_cannot_exceed_yesterdays_pool():
    from ashare_limitup import sentiment

    pools = session_pools(
        limit_up=rows("limit_up", [zt_row("990001", lbc=1), zt_row("990050", lbc=2), zt_row("990051", lbc=4)]),
        yesterday_limit_up=rows("yesterday_limit_up", [prev_row("990001", zdp=10.0), prev_row("990006", zdp=-2.0)]))
    assert sentiment.compute_metrics(pools)["promotion_rate"] == pytest.approx(1 / 2)


def test_an_empty_pool_on_a_live_day_is_a_real_zero():
    from ashare_limitup import sentiment

    metrics = sentiment.compute_metrics(session_pools(limit_down=[], broken_board=[]))
    assert metrics["limit_down_count"] == 0 and metrics["limit_down_count"] is not None
    assert metrics["broken_board_count"] == 0 and metrics["broken_board_rate"] == 0.0


@pytest.mark.parametrize("unavailable, missing", [
    ("limit_down", {"limit_down_count"}),
    ("broken_board", {"broken_board_count", "broken_board_rate"}),
    ("yesterday_limit_up", {"yesterday_limit_up_count", "promotion_rate", "yesterday_limit_up_avg_change_pct",
                            "yesterday_limit_up_median_change_pct", "yesterday_limit_up_red_ratio"}),
    ("limit_up", {"limit_up_count", "max_consecutive_boards", "broken_board_rate", "promotion_rate"}),
])
def test_an_unavailable_pool_leaves_its_metrics_missing_not_zero(unavailable, missing):
    from ashare_limitup import sentiment

    metrics = sentiment.compute_metrics(session_pools(**{unavailable: None}))
    assert {name for name, value in metrics.items() if value is None} == missing


def test_ratios_without_a_denominator_are_missing():
    from ashare_limitup import sentiment

    metrics = sentiment.compute_metrics(session_pools(limit_up=[], broken_board=[], yesterday_limit_up=[]))
    assert metrics["limit_up_count"] == 0 and metrics["max_consecutive_boards"] == 0
    assert metrics["broken_board_rate"] is None
    assert metrics["promotion_rate"] is None and metrics["yesterday_limit_up_avg_change_pct"] is None


@pytest.mark.parametrize("metric, value, band", [
    ("limit_up_count", 30, -1), ("limit_up_count", 31, 0), ("limit_up_count", 69, 0), ("limit_up_count", 70, 1),
    ("limit_down_count", 20, -1), ("limit_down_count", 19, 0), ("limit_down_count", 6, 0), ("limit_down_count", 5, 1),
    ("broken_board_rate", 0.40, -1), ("broken_board_rate", 0.20, 1),
    ("yesterday_limit_up_avg_change_pct", 0.0, -1), ("yesterday_limit_up_avg_change_pct", 2.5, 1),
])
def test_bands_respect_direction_and_inclusive_cut_points(metric, value, band):
    from ashare_limitup import sentiment

    result = sentiment.classify(dict(NEUTRAL, **{metric: value}), shipped().sentiment)
    assert result["bands"][metric] == band
    assert result["score"] == band


@pytest.mark.parametrize("score, tier", [
    (-6, "ICE"), (-4, "ICE"), (-3, "COLD"), (-2, "COLD"), (-1, "NEUTRAL"), (0, "NEUTRAL"),
    (1, "NEUTRAL"), (2, "WARM"), (3, "WARM"), (4, "CLIMAX"), (6, "CLIMAX"),
])
def test_score_maps_to_the_configured_tier(score, tier):
    from ashare_limitup import sentiment

    result = sentiment.classify(metrics_scoring(score), shipped().sentiment)
    assert (result["score"], result["tier"]["id"], result["missing"]) == (score, tier, [])


def test_thresholds_are_read_from_config():
    from ashare_limitup import config, sentiment

    document = shipped_document()
    document["sentiment"]["metrics"]["limit_up_count"] = {"direction": "higher_is_hotter", "cold": 10, "hot": 40}
    document["sentiment"]["tiers"] = [{"id": "LOW", "label": "低", "max_score": 0},
                                      {"id": "HIGH", "label": "高", "max_score": None}]
    custom = config.from_document(document)

    assert sentiment.classify(NEUTRAL, shipped().sentiment)["tier"] == {"id": "NEUTRAL", "label": "中性"}
    result = sentiment.classify(NEUTRAL, custom.sentiment)
    assert result["bands"]["limit_up_count"] == 1 and result["tier"] == {"id": "HIGH", "label": "高"}
    assert custom.sha256 != shipped().sha256


def test_a_missing_metric_makes_the_tier_unavailable_instead_of_neutral():
    from ashare_limitup import sentiment

    result = sentiment.classify(dict(HOT, limit_down_count=None, promotion_rate=None), shipped().sentiment)
    assert result["tier"] == {"id": "UNAVAILABLE", "label": "不可用"}
    assert result["score"] is None
    assert result["missing"] == ["limit_down_count", "promotion_rate"]
    assert "limit_down_count" not in result["bands"] and result["bands"]["limit_up_count"] == 1


def test_shipped_config_is_complete_and_declares_itself_uncalibrated():
    from ashare_limitup import config

    loaded = shipped()
    assert loaded.calibration_status == "UNCALIBRATED_HEURISTIC"
    assert set(loaded.sentiment.metrics) == set(config.METRICS) == set(NEUTRAL)
    assert loaded.sentiment.tiers[-1].max_score is None


def _set(path, value):
    def mutate(document):
        target = document
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


@pytest.mark.parametrize("mutate", [
    _set(("sentiment", "metrics", "limit_up_count", "cold"), 70),            # cold meets hot
    _set(("sentiment", "metrics", "limit_down_count", "hot"), 25),           # lower-is-hotter, inverted
    _set(("sentiment", "metrics", "limit_up_count", "direction"), "up"),
    _set(("sentiment", "metrics", "surprise_metric"), {"direction": "higher_is_hotter", "cold": 0, "hot": 1}),
    lambda d: d["sentiment"]["metrics"].pop("promotion_rate"),
    _set(("sentiment", "tiers", 4, "max_score"), 9),                         # no catch-all tier
    _set(("sentiment", "tiers", 1, "max_score"), -5),                        # not ascending
    _set(("sentiment", "tiers", 1, "id"), "ICE"),                            # duplicate id
    _set(("sentiment", "thresholds_elsewhere"), {}),
    _set(("capture", "max_capture_lag_days"), -1),
])
def test_config_that_cannot_classify_every_day_is_rejected(mutate):
    from ashare_limitup import config

    document = copy.deepcopy(shipped_document())
    mutate(document)
    with pytest.raises(ValueError):
        config.from_document(document)


def captured_record(tmp_path, bodies=None, *, at=AFTER_CLOSE):
    from ashare_limitup import sentiment, snapshot

    fetch = FakeFetch(session_bodies() if bodies is None else bodies, received_at=at)
    manifest = snapshot.capture(DAY, fetch=fetch, now=lambda: datetime.fromisoformat(at),
                                cache_dir=tmp_path / "cache", manifest_dir=tmp_path / "manifests")
    loaded = snapshot.load_snapshot(DAY, cache_dir=tmp_path / "cache", manifest_dir=tmp_path / "manifests")
    return sentiment.build_record(loaded, shipped()), manifest


def test_record_is_traceable_to_hashed_inputs_and_config(tmp_path):
    record, manifest = captured_record(tmp_path)

    assert record["trade_date"] == DAY and record["known_at"] == AFTER_CLOSE
    assert record["evidence_class"] == "REPORTED" and record["provisional"] is False
    assert record["inputs"] == [
        {"pool": e["pool"], "status": "OK", "rows": e["rows"], "sha256": e["sha256"],
         "known_at": e["known_at"], "source": e["source"]} for e in manifest["pools"]]
    assert record["config_sha256"] == shipped().sha256
    assert record["calibration_status"] == "UNCALIBRATED_HEURISTIC"
    assert record["metrics"]["limit_up_count"] == 4 and record["metrics"]["promotion_rate"] == pytest.approx(0.4)
    # limit-ups 4 (cold), limit-downs 0 (hot), broken 0.2 (hot), boards 3 (cold), promotion 0.4 (hot), avg 3.2 (hot)
    assert record["bands"] == {"limit_up_count": -1, "limit_down_count": 1, "broken_board_rate": 1,
                               "max_consecutive_boards": -1, "promotion_rate": 1,
                               "yesterday_limit_up_avg_change_pct": 1}
    assert (record["score"], record["tier"]) == (2, {"id": "WARM", "label": "偏热"})


def test_record_holds_aggregates_only(tmp_path):
    record, _ = captured_record(tmp_path)
    text = json.dumps(record, ensure_ascii=False)
    for needle in ("990001", "991004", "合成990", "合成行业"):
        assert needle not in text


def test_intraday_snapshot_gives_a_provisional_record(tmp_path):
    record, _ = captured_record(tmp_path, at="2026-11-16T02:42:00+00:00")
    assert record["provisional"] is True and record["session_state"] == "INTRADAY"
    assert record["tier"]["id"] == "WARM"


def test_day_without_provider_data_has_no_tier(tmp_path):
    empty = {path: body([]) for path in session_bodies()}
    record, _ = captured_record(tmp_path, empty)
    assert record["snapshot_status"] == "NO_PROVIDER_DATA" and record["provisional"] is True
    assert set(record["metrics"].values()) == {None}
    assert record["tier"]["id"] == "UNAVAILABLE" and record["score"] is None


def test_failed_pool_makes_the_tier_unavailable(tmp_path):
    bodies = session_bodies()
    bodies["getTopicDTPool"] = {"error_code": "TIMEOUT", "http_status": None, "error": "timeout"}
    record, _ = captured_record(tmp_path, bodies)
    assert record["metrics"]["limit_down_count"] is None and record["metrics"]["limit_up_count"] == 4
    assert record["tier"]["id"] == "UNAVAILABLE" and record["missing"] == ["limit_down_count"]


def test_limit_down_rows_count_toward_the_cold_side():
    from ashare_limitup import sentiment

    pools = session_pools(limit_down=rows("limit_down", [dt_row(f"99{2000 + i}") for i in range(25)]))
    metrics = sentiment.compute_metrics(pools)
    assert metrics["limit_down_count"] == 25
    assert sentiment.classify(metrics, shipped().sentiment)["bands"]["limit_down_count"] == -1
