"""Synthetic Eastmoney-shaped topic-pool payloads for tests. Not market data.

Row keys mirror the provider payload shape (c/m/n/p/zdp/...), but every code,
name and number here is invented.
"""
from __future__ import annotations

import json


def zt_row(code, *, lbc=1, zdp=10.0, p=10000, fbt=93000, lbt=93000, fund=5_000_000,
           zbc=0, m=0, n=None, hybk="合成行业", days=None, ct=None):
    return {
        "c": code, "m": m, "n": n or f"合成{code}", "p": p, "zdp": zdp,
        "amount": 100_000_000, "ltsz": 2_000_000_000.0, "tshare": 3_000_000_000.0,
        "hs": 5.5, "lbc": lbc, "fbt": fbt, "lbt": lbt, "fund": fund, "zbc": zbc,
        "hybk": hybk, "zttj": {"days": days or lbc, "ct": ct or lbc},
    }


def zb_row(code, *, zdp=4.0, p=9500, ztp=10000, fbt=100000, zbc=1, m=0, n=None):
    return {
        "c": code, "m": m, "n": n or f"合成{code}", "p": p, "ztp": ztp, "zdp": zdp,
        "amount": 80_000_000, "ltsz": 1_500_000_000.0, "tshare": 2_500_000_000.0,
        "hs": 7.0, "fbt": fbt, "zbc": zbc, "zf": 9.0, "zs": 0.1,
        "zttj": {"days": 1, "ct": 0}, "hybk": "合成行业",
    }


def dt_row(code, *, zdp=-10.0, p=9000, lbt=100000, days=1, oc=0, m=1, n=None):
    return {
        "c": code, "m": m, "n": n or f"合成{code}", "p": p, "zdp": zdp,
        "amount": 60_000_000, "ltsz": 1_000_000_000.0, "tshare": 2_000_000_000.0,
        "pe": 20.0, "hs": 3.0, "fund": 4_000_000, "lbt": lbt, "fba": 1_000_000,
        "days": days, "oc": oc, "hybk": "合成行业",
    }


def prev_row(code, *, zdp, ylbc=1, p=10000, ztp=11000, yfbt=93000, m=0, n=None):
    return {
        "c": code, "m": m, "n": n or f"合成{code}", "p": p, "ztp": ztp, "zdp": zdp,
        "amount": 90_000_000, "ltsz": 2_000_000_000.0, "tshare": 3_000_000_000.0,
        "hs": 6.0, "zf": 8.0, "zs": 0.0, "yfbt": yfbt, "ylbc": ylbc,
        "hybk": "合成行业", "zttj": {"days": ylbc, "ct": ylbc},
    }


def payload(rows, *, rc=0, tc=None, qdate=20261116):
    """Provider envelope as a parsed object."""
    return {
        "rc": rc, "rt": 110, "svr": 1, "lt": 2, "full": 0,
        "data": {"tc": len(rows) if tc is None else tc, "qdate": qdate, "pool": rows},
    }


def body(rows, **kwargs) -> bytes:
    """Provider envelope as response bytes."""
    return json.dumps(payload(rows, **kwargs), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class FetchFailure(Exception):
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__(receipt.get("error"))


class FakeFetch:
    """Stands in for market_data.transport.fetch: one GET per call, with a receipt."""

    def __init__(self, bodies, *, received_at="2026-11-16T07:30:00+00:00"):
        self.bodies = dict(bodies)  # endpoint path -> bytes, or a receipt dict to fail with
        self.received_at = received_at  # one timestamp for every call, or a list with one per call
        self.calls = []

    def __call__(self, url, kind="json"):
        at = self.received_at if isinstance(self.received_at, str) else self.received_at[len(self.calls)]
        self.calls.append((url, kind))
        path = url.split("?", 1)[0].rsplit("/", 1)[-1]
        receipt = {
            "url": url, "requested_at_utc": at, "received_at_utc": at,
            "http_status": 200, "ok": True, "error": None, "error_code": None, "timeout_seconds": 12,
        }
        outcome = self.bodies[path]
        if isinstance(outcome, dict):
            receipt.update(ok=False, **outcome)
            raise FetchFailure(receipt)
        return {"data": outcome, "receipt": receipt}


def session_bodies(qdate=20261116):
    """A small synthetic session: 4 limit-ups, 1 broken board, 0 limit-downs, 5 limit-ups yesterday."""
    return {
        "getTopicZTPool": body([
            zt_row("990001", lbc=3, fbt=92500),
            zt_row("990002", lbc=2, fbt=93100),
            zt_row("990003", lbc=1, fbt=94500),
            zt_row("991004", lbc=1, fbt=93200, m=1),
        ], qdate=qdate),
        "getTopicZBPool": body([zb_row("990005")], qdate=qdate),
        "getTopicDTPool": body([], qdate=qdate),
        "getYesterdayZTPool": body([
            prev_row("990001", zdp=10.0, ylbc=2),
            prev_row("990002", zdp=10.0, ylbc=1),
            prev_row("990005", zdp=4.0),
            prev_row("990006", zdp=-2.0),
            prev_row("990007", zdp=-6.0),
        ], qdate=qdate),
    }
