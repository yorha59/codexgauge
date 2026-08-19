#!/usr/bin/env python3
"""Codex token 消耗统计 — 从本地会话日志生成报表，不受账号切换影响。

用法:
    python3 codex_token_report.py              # 本月报表
    python3 codex_token_report.py 2026-08      # 指定月份
    python3 codex_token_report.py --all        # 全部历史
"""

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = Path.home() / ".codex" / "sessions"


def parse_session_file(path: Path):
    """解析单个会话文件，返回 (session_id, date, total_usage)"""
    session_id = ""
    date_str = path.name.split("T")[0].replace("rollout-", "")
    last_usage = None
    turn_usages = []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = d.get("payload", {})
                if not isinstance(payload, dict):
                    continue

                # Capture session id from meta
                if not session_id and "session_id" in payload:
                    session_id = payload["session_id"]

                # Token usage lives in event_msg -> payload.info.total_token_usage
                info = payload.get("info")
                if isinstance(info, dict):
                    tu = info.get("total_token_usage")
                    if isinstance(tu, dict) and "input_tokens" in tu:
                        turn_usages.append({
                            "input": tu.get("input_tokens", 0),
                            "cached": tu.get("cached_input_tokens", 0),
                            "output": tu.get("output_tokens", 0),
                        })
    except Exception:
        pass

    if not turn_usages:
        return None

    # total_token_usage is cumulative within a session — take the max (last) value
    # But there may be multiple turns; sum the deltas is complex. Use max as total.
    total_input = max((u["input"] for u in turn_usages), default=0)
    total_cached = max((u["cached"] for u in turn_usages), default=0)
    total_output = max((u["output"] for u in turn_usages), default=0)

    return {
        "session_id": session_id or path.stem,
        "date": date_str,
        "input": total_input,
        "cached": total_cached,
        "output": total_output,
    }


def collect_sessions(month_filter: str = None):
    """收集所有会话数据，可按月过滤 (格式: 2026-08)"""
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions

    for year_dir in sorted(SESSIONS_DIR.iterdir()):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir() or not month_dir.name.isdigit():
                continue
            ym = f"{year_dir.name}-{month_dir.name}"
            if month_filter and not ym.startswith(month_filter):
                continue
            for day_dir in sorted(month_dir.iterdir()):
                if not day_dir.is_dir():
                    continue
                for f in day_dir.glob("*.jsonl"):
                    result = parse_session_file(f)
                    if result:
                        sessions.append(result)
    return sessions


def fmt_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def main():
    month_filter = None
    if len(sys.argv) > 1:
        if sys.argv[1] == "--all":
            month_filter = None
        else:
            month_filter = sys.argv[1]

    sessions = collect_sessions(month_filter)
    if not sessions:
        print("没有找到会话数据")
        return

    # Aggregate by date
    by_date = defaultdict(lambda: {"input": 0, "cached": 0, "output": 0, "sessions": 0})
    for s in sessions:
        d = by_date[s["date"]]
        d["input"] += s["input"]
        d["cached"] += s["cached"]
        d["output"] += s["output"]
        d["sessions"] += 1

    # Print report
    title = f"Codex Token 消耗报表（{month_filter or '全部'}）"
    print(f"\n{'='*70}")
    print(title)
    print(f"{'='*70}")
    print(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"数据源: 本地会话日志（与登录账号无关，切换账号不影响统计）")
    print()

    print(f"{'日期':12s} {'会话数':>5s} {'Input':>10s} {'Cached':>10s} {'Output':>10s} {'合计':>10s}")
    print("-" * 70)

    grand = {"input": 0, "cached": 0, "output": 0, "sessions": 0}
    for date in sorted(by_date.keys()):
        d = by_date[date]
        total = d["input"] + d["output"]
        print(f"{date:12s} {d['sessions']:5d} {fmt_num(d['input']):>10s} {fmt_num(d['cached']):>10s} {fmt_num(d['output']):>10s} {fmt_num(total):>10s}")
        grand["input"] += d["input"]
        grand["cached"] += d["cached"]
        grand["output"] += d["output"]
        grand["sessions"] += d["sessions"]

    print("-" * 70)
    gtotal = grand["input"] + grand["output"]
    print(f"{'合计':12s} {grand['sessions']:5d} {fmt_num(grand['input']):>10s} {fmt_num(grand['cached']):>10s} {fmt_num(grand['output']):>10s} {fmt_num(gtotal):>10s}")

    # Top 5 heaviest sessions
    print(f"\n{'='*70}")
    print("消耗最大的 5 个会话:")
    print(f"{'='*70}")
    top = sorted(sessions, key=lambda s: s["input"] + s["output"], reverse=True)[:5]
    for s in top:
        total = s["input"] + s["output"]
        print(f"  {s['date']} | {fmt_num(total):>8s} total | {s['session_id'][:36]}")


if __name__ == "__main__":
    main()
