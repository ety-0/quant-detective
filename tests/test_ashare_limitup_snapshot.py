"""Daily pool snapshot: raw bytes stay out of Git; the manifest pins them by hash."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest

from ashare_limitup_fakes import FakeFetch, body, session_bodies, zt_row

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-11-16"  # a Monday XSHG session
AFTER_CLOSE = "2026-11-16T07:30:00+00:00"  # 15:30 Asia/Shanghai
PATHS = {"limit_up": "getTopicZTPool", "broken_board": "getTopicZBPool",
         "limit_down": "getTopicDTPool", "yesterday_limit_up": "getYesterdayZTPool"}


def run_capture(tmp_path, bodies=None, *, day=DAY, at=AFTER_CLOSE, **kwargs):
    from ashare_limitup import snapshot

    fetch = FakeFetch(session_bodies() if bodies is None else bodies, received_at=at)
    manifest = snapshot.capture(
        day, fetch=fetch, now=lambda: datetime.fromisoformat(at),
        cache_dir=tmp_path / "cache", manifest_dir=tmp_path / "manifests", **kwargs)
    return manifest, fetch


def entry(manifest, pool):
    return next(e for e in manifest["pools"] if e["pool"] == pool)


def test_capture_archives_the_exact_bytes_and_pins_them_in_a_manifest(tmp_path):
    bodies = session_bodies()
    manifest, fetch = run_capture(tmp_path, bodies)

    assert [url for url, _ in fetch.calls] == [entry(manifest, p)["source_url"] for p in PATHS]
    assert {kind for _, kind in fetch.calls} == {"bytes"}
    for pool, path in PATHS.items():
        item = entry(manifest, pool)
        raw = (tmp_path / "cache" / "raw" / item["file"]).read_bytes()
        assert raw == bodies[path]
        assert item["sha256"] == hashlib.sha256(bodies[path]).hexdigest()
        assert item["bytes"] == len(bodies[path])
        assert item["known_at"] == AFTER_CLOSE and item["source"]
        assert item["status"] == "OK"
    assert [entry(manifest, p)["rows"] for p in PATHS] == [4, 1, 0, 5]
    assert (manifest["status"], manifest["session_state"], manifest["final"]) == ("OK", "POST_CLOSE", True)
    assert manifest["trade_date"] == DAY and manifest["known_at"] == AFTER_CLOSE
    assert json.loads((tmp_path / "manifests" / f"{DAY}.json").read_text(encoding="utf-8")) == manifest


def test_manifest_carries_no_row_level_market_data(tmp_path):
    run_capture(tmp_path)
    text = (tmp_path / "manifests" / f"{DAY}.json").read_text(encoding="utf-8")
    for needle in ("990001", "991004", "合成990", "合成行业"):
        assert needle not in text


def test_default_locations_keep_raw_data_ignored_and_manifests_tracked():
    from ashare_limitup import snapshot

    ignored = {line.strip().rstrip("/") for line in (ROOT / ".gitignore").read_text().splitlines() if line.strip()}
    assert snapshot.DEFAULT_CACHE_DIR.relative_to(ROOT).parts[0] in ignored
    manifest_parts = snapshot.DEFAULT_MANIFEST_DIR.relative_to(ROOT).parts
    assert not any("/".join(manifest_parts[:i]) in ignored for i in range(1, len(manifest_parts) + 1))


@pytest.mark.parametrize("at, state", [
    ("2026-11-16T02:42:00+00:00", "INTRADAY"),    # 10:42 local, session open
    ("2026-11-16T07:03:00+00:00", "INTRADAY"),    # 15:03 local, before the configured cut-off
    ("2026-11-16T07:30:00+00:00", "POST_CLOSE"),  # 15:30 local
    ("2026-11-17T00:30:00+00:00", "POST_CLOSE"),  # next morning
])
def test_only_a_capture_after_the_close_is_final(tmp_path, at, state):
    manifest, _ = run_capture(tmp_path, at=at)
    assert manifest["session_state"] == state
    assert manifest["final"] is (state == "POST_CLOSE")


def test_a_capture_that_straddles_the_cutoff_is_not_final(tmp_path):
    from ashare_limitup import snapshot

    times = ["2026-11-16T07:04:58+00:00", "2026-11-16T07:04:59+00:00",
             "2026-11-16T07:05:00+00:00", "2026-11-16T07:05:01+00:00"]  # 15:04:58 .. 15:05:01 local
    manifest = snapshot.capture(
        DAY, fetch=FakeFetch(session_bodies(), received_at=times), now=lambda: datetime.fromisoformat(times[-1]),
        cache_dir=tmp_path / "cache", manifest_dir=tmp_path / "manifests")
    assert (manifest["session_state"], manifest["final"]) == ("INTRADAY", False)
    assert manifest["known_at"] == times[-1], "the snapshot as a whole is only known once its last pool arrived"


@pytest.mark.parametrize("day, at, code", [
    ("2026-11-15", "2026-11-16T07:30:00+00:00", "NON_SESSION"),           # Sunday
    ("2026-10-01", "2026-10-02T07:30:00+00:00", "NON_SESSION"),           # National Day
    ("2026-11-17", "2026-11-16T07:30:00+00:00", "FUTURE_DATE"),
    ("2026-10-28", "2026-11-17T07:30:00+00:00", "CAPTURE_LAG_EXCEEDED"),  # 20 days late
    ("20261116", AFTER_CLOSE, "INVALID_DATE"),
    ("2026-13-40", AFTER_CLOSE, "INVALID_DATE"),
])
def test_dates_whose_answer_could_not_be_trusted_are_refused_before_any_request(tmp_path, day, at, code):
    from ashare_limitup import snapshot

    with pytest.raises(snapshot.SnapshotRefused) as caught:
        run_capture(tmp_path, day=day, at=at)
    assert caught.value.code == code
    assert not (tmp_path / "cache").exists() and not (tmp_path / "manifests").exists()


def test_capture_lag_limit_and_close_cutoff_come_from_config(tmp_path):
    relaxed = {"final_after_local_time": "15:00:00", "max_capture_lag_days": 30}
    manifest, _ = run_capture(tmp_path, day="2026-10-28", at="2026-11-17T07:30:00+00:00", capture_config=relaxed)
    assert manifest["status"] == "OK"
    manifest, _ = run_capture(tmp_path / "b", at="2026-11-16T07:03:00+00:00", capture_config=relaxed)
    assert manifest["session_state"] == "POST_CLOSE"


def test_weekday_beyond_the_calendar_is_captured_but_flagged_unverified(tmp_path):
    from ashare_limitup import snapshot

    manifest, _ = run_capture(tmp_path, session_bodies(qdate=20990105),
                              day="2099-01-05", at="2099-01-05T07:30:00+00:00")  # a Monday
    assert manifest["calendar_status"] == "UNVERIFIED_OUT_OF_CALENDAR_BOUNDS"
    assert entry(manifest, "limit_up")["status"] == "OK"
    with pytest.raises(snapshot.SnapshotRefused) as caught:
        run_capture(tmp_path / "weekend", day="2099-01-04", at="2099-01-05T07:30:00+00:00")
    assert caught.value.code == "NON_SESSION"


def test_four_empty_pools_mean_no_provider_data_not_a_zero_day(tmp_path):
    manifest, _ = run_capture(tmp_path, {path: body([]) for path in PATHS.values()})
    assert (manifest["status"], manifest["final"]) == ("NO_PROVIDER_DATA", False)


def test_a_failed_pool_is_recorded_without_losing_the_others(tmp_path):
    bodies = session_bodies()
    bodies["getTopicDTPool"] = {"error_code": "HTTP_ERROR", "http_status": 451, "error": "HTTP 451"}
    manifest, _ = run_capture(tmp_path, bodies)

    failed = entry(manifest, "limit_down")
    assert (failed["status"], failed["http_status"], failed["error"]) == ("FETCH_FAILED", 451, "HTTP 451")
    assert failed["file"] is None and failed["sha256"] is None and failed["rows"] is None
    assert (manifest["status"], manifest["final"]) == ("INCOMPLETE", False)
    assert (tmp_path / "cache" / "raw" / entry(manifest, "limit_up")["file"]).exists()


@pytest.mark.parametrize("bad, status", [
    (b"<html>Site Unavailable</html>", "NON_JSON"),
    (body([zt_row("990009")], rc=102), "PROVIDER_ERROR"),
    (body([zt_row("990009")], tc=9), "TRUNCATED"),
    (body([zt_row("990009")], qdate=20261113), "DATE_AHEAD_OF_PROVIDER"),
])
def test_an_unusable_answer_keeps_its_bytes_and_is_not_counted(tmp_path, bad, status):
    bodies = session_bodies()
    bodies["getTopicZTPool"] = bad
    manifest, _ = run_capture(tmp_path, bodies)

    item = entry(manifest, "limit_up")
    assert item["status"] == status and item["rows"] is None
    assert item["sha256"] == hashlib.sha256(bad).hexdigest()
    assert (tmp_path / "cache" / "raw" / item["file"]).read_bytes() == bad
    assert manifest["status"] == "INCOMPLETE"


def test_only_final_manifests_reach_the_tracked_directory(tmp_path):
    from ashare_limitup import snapshot

    tracked, provisional = tmp_path / "manifests" / f"{DAY}.json", tmp_path / "cache" / "provisional" / f"{DAY}.json"
    early, _ = run_capture(tmp_path, at="2026-11-16T02:42:00+00:00")
    assert not tracked.exists(), "an intraday look must not leave a file where manifests get committed"
    assert json.loads(provisional.read_text(encoding="utf-8")) == early
    loaded = snapshot.load_snapshot(DAY, cache_dir=tmp_path / "cache", manifest_dir=tmp_path / "manifests")
    assert loaded.manifest == early and len(loaded.pools["limit_up"]) == 4

    final, _ = run_capture(tmp_path, at=AFTER_CLOSE)
    assert json.loads(tracked.read_text(encoding="utf-8")) == final
    assert not provisional.exists(), "the superseded provisional manifest is removed"


def test_a_provisional_manifest_copied_into_the_tracked_directory_is_rejected(tmp_path):
    from ashare_limitup import snapshot

    run_capture(tmp_path, at="2026-11-16T02:42:00+00:00")
    (tmp_path / "manifests").mkdir()
    (tmp_path / "cache" / "provisional" / f"{DAY}.json").rename(tmp_path / "manifests" / f"{DAY}.json")
    with pytest.raises(ValueError, match="not final"):
        snapshot.load_snapshot(DAY, cache_dir=tmp_path / "cache", manifest_dir=tmp_path / "manifests")


def test_final_snapshot_is_immutable_but_an_intraday_one_is_superseded(tmp_path):
    from ashare_limitup import snapshot

    early, _ = run_capture(tmp_path, at="2026-11-16T02:42:00+00:00")
    assert early["final"] is False
    final, _ = run_capture(tmp_path, at=AFTER_CLOSE)
    assert final["final"] is True

    changed = session_bodies()
    changed["getTopicZTPool"] = body([zt_row("990099")])
    with pytest.raises(snapshot.SnapshotRefused) as caught:
        run_capture(tmp_path, changed, at="2026-11-17T00:30:00+00:00")
    assert caught.value.code == "SNAPSHOT_LOCKED"
    assert json.loads((tmp_path / "manifests" / f"{DAY}.json").read_text(encoding="utf-8")) == final
    raw = (tmp_path / "cache" / "raw" / entry(final, "limit_up")["file"]).read_bytes()
    assert raw == session_bodies()["getTopicZTPool"]


def test_load_snapshot_returns_parsed_rows_for_verified_files(tmp_path):
    from ashare_limitup import snapshot

    manifest, _ = run_capture(tmp_path)
    loaded = snapshot.load_snapshot(DAY, cache_dir=tmp_path / "cache", manifest_dir=tmp_path / "manifests")
    assert loaded.manifest == manifest
    assert [row["code"] for row in loaded.pools["limit_up"]] == ["990001", "990002", "991004", "990003"]
    assert loaded.pools["limit_down"] == []


@pytest.mark.parametrize("damage", ["edit", "delete"])
def test_load_snapshot_rejects_raw_files_that_no_longer_match_the_manifest(tmp_path, damage):
    from ashare_limitup import snapshot

    manifest, _ = run_capture(tmp_path)
    raw = tmp_path / "cache" / "raw" / entry(manifest, "limit_up")["file"]
    raw.write_bytes(body([zt_row("990001", lbc=9)])) if damage == "edit" else raw.unlink()
    with pytest.raises(ValueError, match="INPUT_MISMATCH"):
        snapshot.load_snapshot(DAY, cache_dir=tmp_path / "cache", manifest_dir=tmp_path / "manifests")


def test_load_snapshot_leaves_unavailable_pools_absent(tmp_path):
    from ashare_limitup import snapshot

    bodies = session_bodies()
    bodies["getTopicDTPool"] = {"error_code": "TIMEOUT", "http_status": None, "error": "timeout"}
    run_capture(tmp_path, bodies)
    loaded = snapshot.load_snapshot(DAY, cache_dir=tmp_path / "cache", manifest_dir=tmp_path / "manifests")
    assert loaded.pools["limit_down"] is None and len(loaded.pools["limit_up"]) == 4


def test_load_snapshot_treats_an_all_empty_day_as_unavailable(tmp_path):
    from ashare_limitup import snapshot

    run_capture(tmp_path, {path: body([]) for path in PATHS.values()})
    loaded = snapshot.load_snapshot(DAY, cache_dir=tmp_path / "cache", manifest_dir=tmp_path / "manifests")
    assert set(loaded.pools.values()) == {None}


def test_manifest_contract_accepts_a_final_snapshot_and_rejects_smuggled_rows(tmp_path):
    from ashare_limitup import snapshot

    manifest, _ = run_capture(tmp_path)
    snapshot.validate_manifest(manifest, require_final=True)

    with_rows = json.loads(json.dumps(manifest))
    with_rows["pools"][0]["rows_data"] = [{"code": "990001"}]
    with pytest.raises(ValueError):
        snapshot.validate_manifest(with_rows)

    bad_hash = json.loads(json.dumps(manifest))
    bad_hash["pools"][0]["sha256"] = "not-a-hash"
    with pytest.raises(ValueError):
        snapshot.validate_manifest(bad_hash)


def test_manifest_contract_can_require_finality(tmp_path):
    from ashare_limitup import snapshot

    intraday, _ = run_capture(tmp_path, at="2026-11-16T02:42:00+00:00")
    snapshot.validate_manifest(intraday)
    with pytest.raises(ValueError, match="final"):
        snapshot.validate_manifest(intraday, require_final=True)

    forged = dict(intraday, final=True)
    with pytest.raises(ValueError):
        snapshot.validate_manifest(forged)


def test_every_manifest_committed_to_the_repo_is_final_and_hash_only():
    from ashare_limitup import snapshot

    for path in sorted(snapshot.DEFAULT_MANIFEST_DIR.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        snapshot.validate_manifest(document, require_final=True)
        assert path.name == f"{document['trade_date']}.json"
