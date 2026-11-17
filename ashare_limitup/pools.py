"""Eastmoney 涨停板行情 topic pools: request shape and row parsing (LEDGER layer).

Provenance
- Output field names and the board-ladder ordering are adapted from
  ZhuLinsen/daily_stock_analysis, data_provider/akshare_fetcher.py::get_limit_up_pool (MIT).
- Endpoint paths, query parameters and column meanings were read from AKShare,
  akshare/stock/quote/stock_ztb_em.py (MIT).
Neither project is imported or installed; reviewed commits are pinned in
connectors/upstream_review_pins.json and the licences are in THIRD_PARTY_NOTICES.md.

Deliberate differences from the reference implementation
- The whole pool is kept; there is no top-N cut.
- Absent values stay None. The reference coerces missing counts to 0.
- No User-Agent rotation and no retry: one bounded GET per pool.
- An unusable payload raises PoolError with a status instead of being swallowed.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from urllib.parse import urlencode

HOST = "https://push2ex.eastmoney.com"
# Static value the public 涨停板行情 page sends as `ut`. The endpoint answers rc=102
# without it. It identifies the page, not a user or an account.
PUBLIC_PAGE_TOKEN = "7eea3edcaed734bea9cbfc24409ed989"
PAGE_SIZE = 10000
UNIVERSE_NOTE = "东方财富涨停板行情口径：不含 ST 与科创板；涨停池、昨日涨停池不含未中断连续一字板的新股。"
_CODE = re.compile(r"\d{6}")


class PoolError(ValueError):
    """The provider answered, but the answer cannot be used as this pool."""

    def __init__(self, status: str, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}")


@dataclass(frozen=True)
class ParsedPool:
    rows: list
    provider: dict


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _price(value):
    number = _number(value)
    return None if number is None else number / 1000


def _clock(value):
    count = _count(value)
    return None if count is None or not 0 <= count <= 235959 else f"{count:06d}"


def _text(value):
    return value.strip() or None if isinstance(value, str) else None


def _exchange(value):
    return {0: "SZ", 1: "SH"}.get(_count(value))


def _limit_stat(value):
    if not isinstance(value, dict):
        return None
    days, boards = _count(value.get("days")), _count(value.get("ct"))
    return None if days is None or boards is None else f"{days}/{boards}"


_COMMON = {
    "code": ("c", _text), "name": ("n", _text), "exchange": ("m", _exchange),
    "change_pct": ("zdp", _number), "price": ("p", _price), "amount": ("amount", _number),
    "float_market_cap": ("ltsz", _number), "total_market_cap": ("tshare", _number),
    "turnover_rate": ("hs", _number), "industry": ("hybk", _text),
}


@dataclass(frozen=True)
class PoolSpec:
    label: str
    path: str
    sort: str
    fields: dict
    required: dict  # provider key -> converter that must not return None


POOLS = {
    "limit_up": PoolSpec("涨停池", "/getTopicZTPool", "fbt:asc", {
        **_COMMON, "seal_amount": ("fund", _number), "first_limit_time": ("fbt", _clock),
        "last_limit_time": ("lbt", _clock), "break_count": ("zbc", _count),
        "limit_stat": ("zttj", _limit_stat), "consecutive_boards": ("lbc", _count),
    }, {"lbc": _count}),
    "broken_board": PoolSpec("炸板池", "/getTopicZBPool", "fbt:asc", {
        **_COMMON, "limit_price": ("ztp", _price), "first_limit_time": ("fbt", _clock),
        "break_count": ("zbc", _count), "amplitude": ("zf", _number), "speed": ("zs", _number),
        "limit_stat": ("zttj", _limit_stat),
    }, {}),
    "limit_down": PoolSpec("跌停池", "/getTopicDTPool", "fund:asc", {
        **_COMMON, "pe_dynamic": ("pe", _number), "seal_amount": ("fund", _number),
        "last_limit_time": ("lbt", _clock), "on_board_amount": ("fba", _number),
        "consecutive_limit_downs": ("days", _count), "open_count": ("oc", _count),
    }, {}),
    "yesterday_limit_up": PoolSpec("昨日涨停池", "/getYesterdayZTPool", "zs:desc", {
        **_COMMON, "limit_price": ("ztp", _price), "amplitude": ("zf", _number), "speed": ("zs", _number),
        "yesterday_first_limit_time": ("yfbt", _clock), "yesterday_consecutive_boards": ("ylbc", _count),
        "limit_stat": ("zttj", _limit_stat),
    }, {"zdp": _number}),
}


def source(pool: str) -> str:
    spec = POOLS[pool]
    return f"东方财富 涨停板行情 {spec.label} {spec.path.lstrip('/')}（公开网页端点，无账户）"


def pool_url(pool: str, trade_date: str) -> str:
    """URL for one whole pool on one trade date (YYYY-MM-DD)."""
    spec = POOLS[pool]
    params = {"ut": PUBLIC_PAGE_TOKEN, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": PAGE_SIZE,
              "sort": spec.sort, "date": trade_date.replace("-", "")}
    return HOST + spec.path + "?" + urlencode(params)


def parse_pool(pool: str, document) -> ParsedPool:
    """Turn one provider response into rows, or raise PoolError."""
    spec = POOLS[pool]
    if not isinstance(document, dict):
        raise PoolError("SCHEMA_MISMATCH", "response is not a JSON object")
    data = document.get("data")
    if document.get("rc") != 0 or data is None:
        raise PoolError("PROVIDER_ERROR", f"rc={document.get('rc')!r}, data={'null' if data is None else 'present'}")
    raw_rows = data.get("pool") if isinstance(data, dict) else None
    if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
        raise PoolError("SCHEMA_MISMATCH", "data.pool is not a list of objects")
    provider = {"rc": document["rc"], "tc": _count(data.get("tc")), "qdate": _count(data.get("qdate"))}
    if provider["tc"] is not None and provider["tc"] != len(raw_rows):
        raise PoolError("TRUNCATED", f"provider reports {provider['tc']} rows, response holds {len(raw_rows)}")

    # Details name the row position, never a security: they end up in the tracked manifest.
    rows, seen = [], set()
    for position, raw in enumerate(raw_rows):
        code = raw.get("c")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise PoolError("SCHEMA_MISMATCH", f"row {position} has no six-digit code")
        if code in seen:
            raise PoolError("SCHEMA_MISMATCH", f"row {position} repeats an earlier code")
        seen.add(code)
        for key, convert in spec.required.items():
            if convert(raw.get(key)) is None:
                raise PoolError("SCHEMA_MISMATCH", f"row {position}: required field {key!r} is missing or not numeric")
        rows.append({name: convert(raw.get(key)) for name, (key, convert) in spec.fields.items()})
    if pool == "limit_up":
        rows.sort(key=lambda r: (-r["consecutive_boards"], r["first_limit_time"] or "999999", r["code"]))
    return ParsedPool(rows, provider)
