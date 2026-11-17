"""qd-ashare: capture one day's limit-up pools and report its sentiment tier.

    qd-ashare snapshot --date 2026-11-16     # four public GETs, then the tier
    qd-ashare sentiment --date 2026-11-16    # recompute offline from the archived bytes

Research output only: no orders, no trade instructions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from ashare_limitup import pools, sentiment, snapshot
from ashare_limitup.config import DEFAULT_CONFIG_PATH, load_config


def _day(text: str) -> str:
    return f"{text[:4]}-{text[4:6]}-{text[6:]}" if re.fullmatch(r"\d{8}", text) else text


def _count(value) -> str:
    return "缺失" if value is None else str(value)


def _share(value) -> str:
    return "缺失" if value is None else f"{value * 100:.1f}%"


def _change(value) -> str:
    return "缺失" if value is None else f"{value:+.2f}%"


def render(record: dict) -> str:
    metrics, tier = record["metrics"], record["tier"]
    if record["missing"]:
        verdict = f"{tier['label']}（{tier['id']}）｜缺失指标：{'、'.join(record['missing'])}"
    else:
        verdict = f"{tier['label']}（{tier['id']}，分数 {record['score']:+d}）"
    if not record["provisional"]:
        standing = "正式：收盘后完整快照"
    elif record["snapshot_status"] != "OK":
        standing = f"临时：快照状态 {record['snapshot_status']}"
    else:
        standing = "临时：盘中快照，收盘后需重抓"
    lines = [
        f"{record['trade_date']} 涨停板情绪：{verdict}｜{standing}",
        f"涨停 {_count(metrics['limit_up_count'])}｜跌停 {_count(metrics['limit_down_count'])}｜"
        f"炸板 {_count(metrics['broken_board_count'])}（炸板率 {_share(metrics['broken_board_rate'])}）｜"
        f"最高连板 {_count(metrics['max_consecutive_boards'])}｜晋级率 {_share(metrics['promotion_rate'])}",
        f"昨日涨停 {_count(metrics['yesterday_limit_up_count'])} 只，今日均值 "
        f"{_change(metrics['yesterday_limit_up_avg_change_pct'])}，中位数 "
        f"{_change(metrics['yesterday_limit_up_median_change_pct'])}，红盘率 {_share(metrics['yesterday_limit_up_red_ratio'])}",
    ]
    if any(entry["status"] != "OK" for entry in record["inputs"]):
        lines.append("数据池：" + "｜".join(f"{pools.POOLS[e['pool']].label} {e['status']}" for e in record["inputs"]))
    lines += [
        f"口径：{record['universe_note']}",
        f"阈值：{record['calibration_status']}（config sha256 {record['config_sha256'][:12]}）｜数据已知时间：{record['known_at'] or '无'}",
        record["note"],
    ]
    return "\n".join(lines)


def main(argv=None, *, fetch=None, now=None) -> int:
    parser = argparse.ArgumentParser(prog="qd-ashare", description="A 股涨停池快照与情绪档位；只读研究，不下单。")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("snapshot", "抓取当日四个股池并给出情绪档位"), ("sentiment", "用已归档的原始响应离线重算情绪档位")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--date", required=True, help="交易日，YYYY-MM-DD 或 YYYYMMDD")
        command.add_argument("--cache-dir", type=Path, default=snapshot.DEFAULT_CACHE_DIR, help="原始响应目录（不进 Git）")
        command.add_argument("--manifest-dir", type=Path, default=snapshot.DEFAULT_MANIFEST_DIR, help="清单目录（进 Git）")
        command.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
        command.add_argument("--json", action="store_true", help="输出完整情绪记录")
    args = parser.parse_args(argv)
    trade_date = _day(args.date)
    try:
        config = load_config(args.config)
        if args.command == "snapshot":
            if fetch is None:
                from market_data.transport import fetch
            snapshot.capture(trade_date, fetch=fetch, now=now, cache_dir=args.cache_dir,
                             manifest_dir=args.manifest_dir, capture_config=config.capture)
        loaded = snapshot.load_snapshot(trade_date, cache_dir=args.cache_dir, manifest_dir=args.manifest_dir)
        record = sentiment.build_record(loaded, config)
    except (ValueError, OSError) as exc:
        print(f"未完成：{exc}", file=sys.stderr)
        return 2
    text = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
    derived = args.cache_dir / "derived" / trade_date / "sentiment.json"
    derived.parent.mkdir(parents=True, exist_ok=True)
    derived.write_text(text + "\n", encoding="utf-8")
    print(text if args.json else render(record))
    return 0 if record["tier"]["id"] != "UNAVAILABLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
