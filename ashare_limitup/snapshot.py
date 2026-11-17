"""Capture and reload one trade date's limit-up pools.

Raw provider bytes go to the git-ignored cache. The manifest holds only request
receipts, row counts and SHA-256 hashes. A final manifest is written to the
tracked manifest directory and is the part meant for Git; a manifest that is
not final (intraday or incomplete) stays in the cache next to the raw bytes.

What a capture refuses or downgrades, and why (provider behaviour observed 2026-11-17):
- A future date returns the provider's live pool for today, so it is refused.
- A date older than about three weeks returns four empty pools, exactly like a
  date with nothing in them, so captures are limited to a short lag and four
  empty pools are recorded as NO_PROVIDER_DATA rather than as zeros.
- A non-session date also returns empty pools, so it is refused by calendar.
- During the session the pools are still changing, so a capture is final only
  when every pool was received after the configured post-close time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ashare_limitup import pools
from ashare_limitup.config import CaptureConfig, load_config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = ROOT / "market_cache" / "ashare_limitup"
DEFAULT_MANIFEST_DIR = ROOT / "research_outputs" / "ashare_limitup" / "manifests"
MARKET_TZ = ZoneInfo("Asia/Shanghai")
PROVIDER = "eastmoney_push2ex_topic_pools"
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

PoolStatus = Literal["OK", "FETCH_FAILED", "NON_JSON", "PROVIDER_ERROR", "SCHEMA_MISMATCH",
                     "TRUNCATED", "DATE_AHEAD_OF_PROVIDER"]


class SnapshotRefused(ValueError):
    """The request was not made, or the stored snapshot cannot be used."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PoolEntry(_Strict):
    pool: Literal["limit_up", "broken_board", "limit_down", "yesterday_limit_up"]
    label: str
    status: PoolStatus
    source: str
    source_url: str
    requested_at_utc: Optional[str]
    known_at: Optional[str]
    http_status: Optional[int]
    file: Optional[str]
    sha256: Optional[str] = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: Optional[int] = Field(ge=0)
    rows: Optional[int] = Field(ge=0)
    provider: Optional[dict[str, Optional[int]]]
    error: Optional[str]

    @model_validator(mode="after")
    def usable_pools_are_fully_pinned(self):
        pinned = (self.file, self.sha256, self.bytes, self.known_at)
        if self.status == "OK" and (None in pinned or self.rows is None):
            raise ValueError(f"{self.pool}: an OK pool needs file, sha256, bytes, known_at and rows")
        if self.status != "OK" and self.rows is not None:
            raise ValueError(f"{self.pool}: rows are only counted for an OK pool")
        if self.status == "FETCH_FAILED" and any(value is not None for value in pinned):
            raise ValueError(f"{self.pool}: a failed fetch has nothing to pin")
        return self


class SnapshotManifest(_Strict):
    kind: Literal["ashare_limitup_snapshot_manifest"]
    schema_version: Literal[1]
    trade_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    provider: str
    universe_note: str
    calendar_status: Literal["XSHG_SESSION", "UNVERIFIED_OUT_OF_CALENDAR_BOUNDS"]
    session_state: Literal["POST_CLOSE", "INTRADAY"]
    status: Literal["OK", "INCOMPLETE", "NO_PROVIDER_DATA"]
    final: bool
    known_at: Optional[str]
    pools: list[PoolEntry]

    @model_validator(mode="after")
    def flags_agree_with_the_pools(self):
        if [entry.pool for entry in self.pools] != list(pools.POOLS):
            raise ValueError("manifest must list each pool exactly once, in order")
        for entry in self.pools:
            if entry.file is not None and entry.file != f"{self.trade_date}/{entry.pool}.json":
                raise ValueError(f"{entry.pool}: unexpected raw file label {entry.file!r}")
        if self.status == "OK" and any(entry.status != "OK" for entry in self.pools):
            raise ValueError("status OK requires every pool to be OK")
        if self.final != (self.status == "OK" and self.session_state == "POST_CLOSE"):
            raise ValueError("final must equal (status == OK and session_state == POST_CLOSE)")
        return self


@dataclass(frozen=True)
class Snapshot:
    manifest: dict
    pools: dict  # pool name -> parsed rows, or None when the pool is unavailable


def validate_manifest(document: dict, *, require_final: bool = False) -> None:
    """Raise ValueError unless the document is a well-formed, hash-only manifest."""
    manifest = SnapshotManifest.model_validate(document)
    if require_final and not manifest.final:
        raise ValueError(f"manifest {manifest.trade_date} is not final "
                         f"(status={manifest.status}, session_state={manifest.session_state})")


def _aware(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("receipt timestamps must include a timezone")
    return moment


def _parse_day(trade_date: str) -> date:
    try:
        if not isinstance(trade_date, str) or not _DAY.fullmatch(trade_date):
            raise ValueError
        return date.fromisoformat(trade_date)
    except ValueError:
        raise SnapshotRefused("INVALID_DATE", f"{trade_date!r} is not a YYYY-MM-DD date") from None


@lru_cache(maxsize=1)
def _xshg():
    import exchange_calendars

    return exchange_calendars.get_calendar("XSHG")


def _calendar_status(day: date) -> str:
    calendar = _xshg()
    if calendar.first_session.date() <= day <= calendar.last_session.date():
        if calendar.is_session(day.isoformat()):
            return "XSHG_SESSION"
    elif day.weekday() < 5:
        # The pinned calendar has no data for this date; holidays cannot be ruled out here.
        return "UNVERIFIED_OUT_OF_CALENDAR_BOUNDS"
    raise SnapshotRefused("NON_SESSION", f"{day} is not an XSHG session; the provider would answer with empty pools")


def _manifest_paths(trade_date: str, cache_dir: Path, manifest_dir: Path) -> tuple[Path, Path]:
    """(tracked path for a final manifest, cache path for a provisional one)."""
    name = f"{trade_date}.json"
    return Path(manifest_dir) / name, Path(cache_dir) / "provisional" / name


def _capture_pool(pool: str, trade_date: str, fetch, raw_dir: Path) -> dict:
    url = pools.pool_url(pool, trade_date)
    entry = {"pool": pool, "label": pools.POOLS[pool].label, "status": "FETCH_FAILED",
             "source": pools.source(pool), "source_url": url, "requested_at_utc": None,
             "known_at": None, "http_status": None, "file": None, "sha256": None,
             "bytes": None, "rows": None, "provider": None, "error": None}
    raw_path = raw_dir / f"{pool}.json"
    try:
        response = fetch(url, kind="bytes")
    except Exception as exc:
        receipt = getattr(exc, "receipt", None)
        if receipt is None:
            raise
        raw_path.unlink(missing_ok=True)  # never leave an older capture behind an entry that pins nothing
        entry.update(requested_at_utc=receipt.get("requested_at_utc"), http_status=receipt.get("http_status"),
                     error=receipt.get("error") or receipt.get("error_code") or "fetch failed")
        return entry

    raw, receipt = response["data"], response["receipt"]
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(raw)
    entry.update(requested_at_utc=receipt.get("requested_at_utc"), known_at=receipt["received_at_utc"],
                 http_status=receipt.get("http_status"), file=f"{trade_date}/{pool}.json",
                 sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    try:
        document = json.loads(raw.decode("utf-8"))
    except ValueError:  # includes UnicodeDecodeError
        entry.update(status="NON_JSON", error="HTTP 200, but the body is not JSON")
        return entry
    try:
        parsed = pools.parse_pool(pool, document)
    except pools.PoolError as exc:
        entry.update(status=exc.status, error=exc.detail)
        return entry
    entry["provider"] = parsed.provider
    query_date = parsed.provider["qdate"]
    if query_date is not None and int(trade_date.replace("-", "")) > query_date:
        entry.update(status="DATE_AHEAD_OF_PROVIDER",
                     error=f"provider query date is {query_date}; for a later date it serves its live pool")
        return entry
    entry.update(status="OK", rows=len(parsed.rows))
    return entry


def capture(trade_date: str, *, fetch, now=None, cache_dir: Path = DEFAULT_CACHE_DIR,
            manifest_dir: Path = DEFAULT_MANIFEST_DIR, capture_config=None) -> dict:
    """Fetch the four pools once each, archive the bytes, write and return the manifest.

    `capture_config` is a CaptureConfig or an equivalent dict; by default the shipped config is used.
    """
    limits = load_config().capture if capture_config is None else CaptureConfig.model_validate(capture_config)
    day = _parse_day(trade_date)
    moment = now() if now is not None else datetime.now(timezone.utc)
    today = moment.astimezone(MARKET_TZ).date()
    if day > today:
        raise SnapshotRefused("FUTURE_DATE", f"{trade_date} is after {today} in Asia/Shanghai")
    if (today - day).days > limits.max_capture_lag_days:
        raise SnapshotRefused("CAPTURE_LAG_EXCEEDED",
                              f"{trade_date} is {(today - day).days} days old; limit is {limits.max_capture_lag_days}. "
                              "The provider answers old dates with empty pools.")
    calendar_status = _calendar_status(day)
    final_path, provisional_path = _manifest_paths(trade_date, cache_dir, manifest_dir)
    if final_path.exists():
        raise SnapshotRefused("SNAPSHOT_LOCKED", f"{final_path.name} is final and will not be replaced")

    raw_dir = Path(cache_dir) / "raw" / trade_date
    entries = [_capture_pool(pool, trade_date, fetch, raw_dir) for pool in pools.POOLS]
    if any(entry["status"] != "OK" for entry in entries):
        status = "INCOMPLETE"
    elif all(entry["rows"] == 0 for entry in entries):
        status = "NO_PROVIDER_DATA"
    else:
        status = "OK"
    received = sorted((_aware(entry["known_at"]), entry["known_at"]) for entry in entries if entry["known_at"])
    cutoff = datetime.combine(day, limits.final_after_local_time, MARKET_TZ)
    session_state = "POST_CLOSE" if (received[0][0] if received else moment) >= cutoff else "INTRADAY"
    manifest = {
        "kind": "ashare_limitup_snapshot_manifest", "schema_version": 1, "trade_date": trade_date,
        "provider": PROVIDER, "universe_note": pools.UNIVERSE_NOTE, "calendar_status": calendar_status,
        "session_state": session_state, "status": status,
        "final": status == "OK" and session_state == "POST_CLOSE",
        "known_at": received[-1][1] if received else None, "pools": entries,
    }
    validate_manifest(manifest)
    target = final_path if manifest["final"] else provisional_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if manifest["final"]:
        provisional_path.unlink(missing_ok=True)
    return manifest


def load_snapshot(trade_date: str, *, cache_dir: Path = DEFAULT_CACHE_DIR,
                  manifest_dir: Path = DEFAULT_MANIFEST_DIR) -> Snapshot:
    """Reload a captured day. Rows are returned only for raw files whose hash still matches."""
    _parse_day(trade_date)
    final_path, provisional_path = _manifest_paths(trade_date, cache_dir, manifest_dir)
    manifest_path = final_path if final_path.exists() else provisional_path
    if not manifest_path.exists():
        raise SnapshotRefused("NO_MANIFEST", f"no manifest for {trade_date}; run a snapshot first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_manifest(manifest, require_final=manifest_path == final_path)
    loaded = {}
    for entry in manifest["pools"]:
        if entry["status"] != "OK" or manifest["status"] == "NO_PROVIDER_DATA":
            loaded[entry["pool"]] = None
            continue
        raw_path = Path(cache_dir) / "raw" / entry["file"]
        raw = raw_path.read_bytes() if raw_path.is_file() else None
        if raw is None or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"INPUT_MISMATCH: {entry['file']} is missing or differs from the manifest hash")
        loaded[entry["pool"]] = pools.parse_pool(entry["pool"], json.loads(raw.decode("utf-8"))).rows
    return Snapshot(manifest, loaded)
