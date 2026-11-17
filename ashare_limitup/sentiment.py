"""Sentiment metrics and tier for one captured day (VECTOR layer).

Deterministic arithmetic on LEDGER pools. The tier describes what the day's
limit-up pools already show (REPORTED); it is not a forecast and not a trade
instruction. Thresholds come from config and are marked uncalibrated there.

Definitions (counts are within the provider's universe, see pools.UNIVERSE_NOTE)
- limit_up_count / limit_down_count / broken_board_count: rows in each pool.
- broken_board_rate: broken / (limit-up + broken) — of the stocks that touched
  the limit, the share that did not hold it.
- max_consecutive_boards: highest 连板数 in the limit-up pool; 0 when the pool is empty.
- promotion_rate: share of yesterday's limit-up pool that is in today's limit-up
  pool, joined on code.
- yesterday_limit_up_*: today's change of yesterday's limit-up pool — mean,
  median, and share closing up.
A metric is None when a pool it needs is unavailable or its denominator is zero.
"""
from __future__ import annotations

from statistics import fmean, median

from ashare_limitup import pools as pool_layer
from ashare_limitup.config import METRICS, LoadedConfig, SentimentConfig
from ashare_limitup.snapshot import Snapshot

UNAVAILABLE = {"id": "UNAVAILABLE", "label": "不可用"}
NOTE = "描述当日已经发生的涨停板状态（REPORTED），不是预测，也不是交易指令。"


def _ratio(numerator, denominator):
    return None if numerator is None or not denominator else numerator / denominator


def compute_metrics(pools: dict) -> dict:
    """`pools` maps each pool name to its parsed rows, or to None when unavailable."""
    up, broken = pools["limit_up"], pools["broken_board"]
    down, yesterday = pools["limit_down"], pools["yesterday_limit_up"]
    touched = None if up is None or broken is None else len(up) + len(broken)
    changes = None if yesterday is None else [row["change_pct"] for row in yesterday]
    promoted = None
    if up is not None and yesterday is not None:
        promoted = len({row["code"] for row in yesterday} & {row["code"] for row in up})
    return {
        "limit_up_count": None if up is None else len(up),
        "limit_down_count": None if down is None else len(down),
        "broken_board_count": None if broken is None else len(broken),
        "broken_board_rate": None if touched is None else _ratio(len(broken), touched),
        "max_consecutive_boards": None if up is None else max((row["consecutive_boards"] for row in up), default=0),
        "yesterday_limit_up_count": None if yesterday is None else len(yesterday),
        "promotion_rate": None if yesterday is None else _ratio(promoted, len(yesterday)),
        "yesterday_limit_up_avg_change_pct": fmean(changes) if changes else None,
        "yesterday_limit_up_median_change_pct": median(changes) if changes else None,
        "yesterday_limit_up_red_ratio": _ratio(sum(change > 0 for change in changes), len(changes)) if changes else None,
    }


def _band(value, band) -> int:
    if band.direction == "higher_is_hotter":
        return -1 if value <= band.cold else 1 if value >= band.hot else 0
    return -1 if value >= band.cold else 1 if value <= band.hot else 0


def classify(metrics: dict, thresholds: SentimentConfig) -> dict:
    """Band each metric to -1/0/+1, sum them, and map the sum to a tier.

    Any missing metric makes the tier UNAVAILABLE: a neutral band for a missing
    value would be a silent zero.
    """
    missing = [name for name in METRICS if metrics.get(name) is None]
    bands = {name: _band(metrics[name], thresholds.metrics[name]) for name in METRICS if name not in missing}
    if missing:
        return {"bands": bands, "score": None, "tier": dict(UNAVAILABLE), "missing": missing}
    score = sum(bands.values())
    tier = next(tier for tier in thresholds.tiers if tier.max_score is None or score <= tier.max_score)
    return {"bands": bands, "score": score, "tier": {"id": tier.id, "label": tier.label}, "missing": []}


def build_record(snapshot: Snapshot, config: LoadedConfig) -> dict:
    """The day's sentiment record: aggregates only, traceable to hashed inputs and config."""
    manifest = snapshot.manifest
    metrics = compute_metrics(snapshot.pools)
    return {
        "kind": "ashare_limitup_sentiment", "schema_version": 1,
        "trade_date": manifest["trade_date"], "evidence_class": "REPORTED",
        "provisional": not manifest["final"], "snapshot_status": manifest["status"],
        "session_state": manifest["session_state"], "known_at": manifest["known_at"],
        "universe_note": pool_layer.UNIVERSE_NOTE,
        "inputs": [{key: entry[key] for key in ("pool", "status", "rows", "sha256", "known_at", "source")}
                   for entry in manifest["pools"]],
        "metrics": metrics, **classify(metrics, config.sentiment),
        "config_sha256": config.sha256, "calibration_status": config.calibration_status,
        "note": NOTE,
    }
