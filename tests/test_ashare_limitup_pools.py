"""LEDGER layer: Eastmoney limit-up topic pools — request shape and row parsing."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_limitup_fakes import dt_row, payload, prev_row, zb_row, zt_row


def test_transport_allows_exactly_the_four_readonly_pool_endpoints():
    from ashare_limitup import pools
    from market_data import transport

    for pool in pools.POOLS:
        transport.validate_url(pools.pool_url(pool, "2026-11-16"))
    assert set(pools.POOLS) == {"limit_up", "broken_board", "limit_down", "yesterday_limit_up"}
    assert transport.ENDPOINTS["push2ex.eastmoney.com"] == {
        "/getTopicZTPool", "/getTopicZBPool", "/getTopicDTPool", "/getYesterdayZTPool"}
    with pytest.raises(ValueError):
        transport.validate_url("https://push2ex.eastmoney.com/getTopicQSPool?date=20261116")


def test_pool_url_requests_the_whole_pool_for_the_given_date():
    from ashare_limitup import pools

    url = pools.pool_url("limit_up", "2026-11-16")
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).path == "/getTopicZTPool"
    assert query["date"] == ["20261116"]
    assert int(query["pagesize"][0]) >= 5000, "a snapshot needs every row, not a top-N page"


def test_limit_up_rows_use_the_upstream_field_names_and_units():
    from ashare_limitup import pools

    raw = zt_row("990001", lbc=3, zdp=10.02, p=8750, fbt=92500, lbt=140200, fund=26_000_000,
                 zbc=2, m=0, n="合成甲", hybk="合成通信", days=4, ct=3)
    parsed = pools.parse_pool("limit_up", payload([raw], qdate=20261117))
    assert parsed.provider == {"rc": 0, "tc": 1, "qdate": 20261117}
    assert parsed.rows == [{
        "code": "990001", "name": "合成甲", "exchange": "SZ", "change_pct": 10.02, "price": 8.75,
        "amount": 100_000_000.0, "float_market_cap": 2_000_000_000.0, "total_market_cap": 3_000_000_000.0,
        "turnover_rate": 5.5, "seal_amount": 26_000_000.0, "first_limit_time": "092500",
        "last_limit_time": "140200", "break_count": 2, "limit_stat": "4/3",
        "consecutive_boards": 3, "industry": "合成通信",
    }]


def test_absent_optional_fields_stay_missing_instead_of_becoming_zero():
    from ashare_limitup import pools

    raw = zt_row("990001", lbc=1)
    for key in ("zbc", "fund", "fbt", "hybk", "zttj", "p"):
        del raw[key]
    row = pools.parse_pool("limit_up", payload([raw])).rows[0]
    assert row["break_count"] is None and row["seal_amount"] is None
    assert row["first_limit_time"] is None and row["industry"] is None
    assert row["limit_stat"] is None and row["price"] is None


def test_limit_up_rows_are_ordered_as_a_board_ladder():
    from ashare_limitup import pools

    rows = [zt_row("990003", lbc=1, fbt=93000), zt_row("990001", lbc=3, fbt=100000),
            zt_row("990002", lbc=3, fbt=92500), zt_row("990004", lbc=1, fbt=92500)]
    parsed = pools.parse_pool("limit_up", payload(rows))
    assert [r["code"] for r in parsed.rows] == ["990002", "990001", "990004", "990003"]


def test_other_pools_expose_their_own_fields():
    from ashare_limitup import pools

    broken = pools.parse_pool("broken_board", payload([zb_row("990005", p=9500, ztp=10000, zbc=2)])).rows[0]
    assert (broken["price"], broken["limit_price"], broken["break_count"]) == (9.5, 10.0, 2)

    down = pools.parse_pool("limit_down", payload([dt_row("991009", days=2, oc=1, m=1)])).rows[0]
    assert (down["exchange"], down["consecutive_limit_downs"], down["open_count"]) == ("SH", 2, 1)

    prev = pools.parse_pool("yesterday_limit_up", payload([prev_row("990001", zdp=-3.5, ylbc=2, yfbt=93100)])).rows[0]
    assert (prev["change_pct"], prev["yesterday_consecutive_boards"], prev["yesterday_first_limit_time"]) == (-3.5, 2, "093100")


def test_an_empty_pool_is_a_valid_answer():
    from ashare_limitup import pools

    parsed = pools.parse_pool("limit_down", payload([]))
    assert parsed.rows == [] and parsed.provider["tc"] == 0


@pytest.mark.parametrize("mutate, status", [
    (lambda doc: doc.update(rc=102, data=None), "PROVIDER_ERROR"),
    (lambda doc: doc.update(data=None), "PROVIDER_ERROR"),
    (lambda doc: doc["data"].update(tc=7), "TRUNCATED"),
    (lambda doc: doc["data"].pop("pool"), "SCHEMA_MISMATCH"),
    (lambda doc: doc["data"]["pool"][0].pop("c"), "SCHEMA_MISMATCH"),
    (lambda doc: doc["data"]["pool"][0].pop("lbc"), "SCHEMA_MISMATCH"),
    (lambda doc: doc["data"]["pool"][0].update(lbc="3"), "SCHEMA_MISMATCH"),
    (lambda doc: doc["data"]["pool"].append(dict(doc["data"]["pool"][0])), "SCHEMA_MISMATCH"),
])
def test_unusable_provider_payloads_are_rejected_with_a_status(mutate, status):
    from ashare_limitup import pools

    doc = payload([zt_row("990001"), zt_row("990002")])
    mutate(doc)
    if status == "SCHEMA_MISMATCH" and "pool" in doc["data"] and len(doc["data"]["pool"]) == 3:
        doc["data"]["tc"] = 3  # duplicate code, count itself is consistent
    with pytest.raises(pools.PoolError) as caught:
        pools.parse_pool("limit_up", doc)
    assert caught.value.status == status


def test_yesterday_pool_requires_a_numeric_change_for_every_row():
    from ashare_limitup import pools

    doc = payload([prev_row("990001", zdp=1.0), prev_row("990002", zdp=None)])
    with pytest.raises(pools.PoolError) as caught:
        pools.parse_pool("yesterday_limit_up", doc)
    assert caught.value.status == "SCHEMA_MISMATCH"
