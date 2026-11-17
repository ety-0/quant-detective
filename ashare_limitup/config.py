"""Capture limits and sentiment thresholds live in config/ashare_limitup.json, not in code."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from time_machine.hashutil import canonical_json, sha256_text

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "ashare_limitup.json"
# The six metrics that are banded. Each one needs a threshold pair in the config.
METRICS = (
    "limit_up_count", "limit_down_count", "broken_board_rate",
    "max_consecutive_boards", "promotion_rate", "yesterday_limit_up_avg_change_pct",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptureConfig(_Strict):
    final_after_local_time: time
    max_capture_lag_days: int = Field(ge=0)


class Band(_Strict):
    """Cut points are inclusive: at or beyond `cold` scores -1, at or beyond `hot` scores +1."""
    direction: Literal["higher_is_hotter", "lower_is_hotter"]
    cold: float
    hot: float

    @model_validator(mode="after")
    def cold_and_hot_do_not_overlap(self):
        if (self.cold >= self.hot) if self.direction == "higher_is_hotter" else (self.cold <= self.hot):
            raise ValueError(f"cold={self.cold} and hot={self.hot} overlap for {self.direction}")
        return self


class Tier(_Strict):
    id: str
    label: str
    max_score: Optional[int]


class SentimentConfig(_Strict):
    metrics: dict[str, Band]
    tiers: list[Tier]

    @model_validator(mode="after")
    def covers_every_metric_and_every_score(self):
        if set(self.metrics) != set(METRICS):
            raise ValueError(f"metrics must be exactly {sorted(METRICS)}")
        bounds = [tier.max_score for tier in self.tiers]
        if not bounds or bounds[-1] is not None or None in bounds[:-1]:
            raise ValueError("exactly the last tier must be unbounded (max_score: null)")
        if bounds[:-1] != sorted(set(bounds[:-1])) or len({tier.id for tier in self.tiers}) != len(self.tiers):
            raise ValueError("tiers need unique ids and strictly ascending max_score")
        return self


class _Document(_Strict):
    schema_version: Literal[1]
    calibration_status: str
    calibration_note: str
    capture: CaptureConfig
    sentiment: SentimentConfig


@dataclass(frozen=True)
class LoadedConfig:
    capture: CaptureConfig
    sentiment: SentimentConfig
    calibration_status: str
    sha256: str


def from_document(document: dict) -> LoadedConfig:
    parsed = _Document.model_validate(document)
    return LoadedConfig(parsed.capture, parsed.sentiment, parsed.calibration_status,
                        sha256_text(canonical_json(document)))


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> LoadedConfig:
    return from_document(json.loads(Path(path).read_text(encoding="utf-8")))
