"""qd-ashare: capture a day, report its sentiment tier, recompute it offline."""
from __future__ import annotations

from datetime import datetime
import json

from ashare_limitup_fakes import FakeFetch, session_bodies

DAY = "2026-11-16"
AFTER_CLOSE = "2026-11-16T07:30:00+00:00"


def run(tmp_path, argv, bodies=None, *, at=AFTER_CLOSE):
    from ashare_limitup import cli

    fetch = FakeFetch(session_bodies() if bodies is None else bodies, received_at=at)
    code = cli.main([*argv, "--cache-dir", str(tmp_path / "cache"), "--manifest-dir", str(tmp_path / "manifests")],
                    fetch=fetch, now=lambda: datetime.fromisoformat(at))
    return code, fetch


def test_snapshot_command_captures_the_day_and_reports_its_tier(tmp_path, capsys):
    code, fetch = run(tmp_path, ["snapshot", "--date", "20261116"])
    out = capsys.readouterr().out

    assert code == 0 and len(fetch.calls) == 4
    assert "2026-11-16" in out and "偏热" in out and "涨停 4" in out and "跌停 0" in out
    assert "UNCALIBRATED_HEURISTIC" in out, "the reader must see that thresholds are uncalibrated"
    assert (tmp_path / "manifests" / f"{DAY}.json").is_file()
    stored = json.loads((tmp_path / "cache" / "derived" / DAY / "sentiment.json").read_text(encoding="utf-8"))
    assert stored["tier"] == {"id": "WARM", "label": "偏热"} and stored["provisional"] is False


def test_sentiment_command_recomputes_offline_from_the_archived_bytes(tmp_path, capsys):
    run(tmp_path, ["snapshot", "--date", DAY])
    stored = json.loads((tmp_path / "cache" / "derived" / DAY / "sentiment.json").read_text(encoding="utf-8"))
    capsys.readouterr()

    code, fetch = run(tmp_path, ["sentiment", "--date", DAY, "--json"])
    assert code == 0 and fetch.calls == []
    assert json.loads(capsys.readouterr().out) == stored


def test_intraday_snapshot_is_reported_as_provisional(tmp_path, capsys):
    code, _ = run(tmp_path, ["snapshot", "--date", DAY], at="2026-11-16T02:42:00+00:00")
    out = capsys.readouterr().out
    assert code == 0 and "临时" in out and "盘中" in out


def test_refused_date_exits_nonzero_and_says_why(tmp_path, capsys):
    code, fetch = run(tmp_path, ["snapshot", "--date", "2026-11-15"])
    captured = capsys.readouterr()
    assert code == 2 and fetch.calls == []
    assert "NON_SESSION" in captured.err and captured.out == ""


def test_incomplete_snapshot_exits_nonzero_and_names_the_failed_pool(tmp_path, capsys):
    bodies = session_bodies()
    bodies["getTopicDTPool"] = {"error_code": "HTTP_ERROR", "http_status": 451, "error": "HTTP 451"}
    code, _ = run(tmp_path, ["snapshot", "--date", DAY], bodies)
    out = capsys.readouterr().out
    assert code == 2
    assert "不可用" in out and "跌停池" in out and "FETCH_FAILED" in out and "跌停 缺失" in out


def test_sentiment_without_a_snapshot_exits_nonzero(tmp_path, capsys):
    code, _ = run(tmp_path, ["sentiment", "--date", DAY])
    assert code == 2 and "NO_MANIFEST" in capsys.readouterr().err


def test_tampered_archive_is_reported_not_summarised(tmp_path, capsys):
    run(tmp_path, ["snapshot", "--date", DAY])
    (tmp_path / "cache" / "raw" / DAY / "limit_up.json").write_bytes(b"{}")
    capsys.readouterr()
    code, _ = run(tmp_path, ["sentiment", "--date", DAY])
    captured = capsys.readouterr()
    assert code == 2 and "INPUT_MISMATCH" in captured.err and captured.out == ""
