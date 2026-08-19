#!/usr/bin/env python3
"""Codex 整体 token 消耗监控 — 跨账号聚合，实时/定时自动输出。

聚合所有账号的本地会话日志，输出整体消耗（当日/本周/累计）。
- watch 模式：每隔 N 分钟自动刷新输出
- once 模式：输出一次退出（适合 cron）
"""

import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CACHE_FILE = Path.home() / ".hermes" / "scripts" / ".codex_token_cache.json"


def fmt_num(n):
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def collect_all():
    """扫描全部会话日志，按日期聚合。返回 {date: {input, cached, output, sessions}}"""
    by_date = defaultdict(lambda: {"input": 0, "cached": 0, "output": 0, "sessions": 0})
    if not SESSIONS_DIR.exists():
        return by_date
    for year_dir in SESSIONS_DIR.iterdir():
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir() or not month_dir.name.isdigit():
                continue
            for day_dir in month_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                date_str = f"{year_dir.name}-{month_dir.name}-{day_dir.name}"
                for f in day_dir.glob("*.jsonl"):
                    usage = parse_file(f)
                    if usage:
                        d = by_date[date_str]
                        d["input"] += usage["input"]
                        d["cached"] += usage["cached"]
                        d["output"] += usage["output"]
                        d["sessions"] += 1
    return by_date


def parse_file(path):
    """单个会话文件 -> 最大累计 usage（跨账号无关）"""
    best = {"input": 0, "cached": 0, "output": 0}
    found = False
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "total_token_usage" not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                info = (d.get("payload") or {}).get("info")
                if not isinstance(info, dict):
                    continue
                tu = info.get("total_token_usage")
                if isinstance(tu, dict) and "input_tokens" in tu:
                    found = True
                    # total_token_usage 是会话累计值，取最后一次（最大）
                    if tu["input_tokens"] >= best["input"]:
                        best = {
                            "input": tu.get("input_tokens", 0),
                            "cached": tu.get("cached_input_tokens", 0),
                            "output": tu.get("output_tokens", 0),
                        }
    except Exception:
        pass
    return best if found else None


def week_dates(today=None):
    """本周一到今天的日期列表"""
    today = today or datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    dates = []
    d = monday
    while d <= today:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return dates


def print_dashboard(by_date):
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    week = week_dates()

    today_d = by_date.get(today_str, {"input": 0, "cached": 0, "output": 0, "sessions": 0})
    week_d = {"input": 0, "cached": 0, "output": 0, "sessions": 0}
    for dstr in week:
        if dstr in by_date:
            for k in ("input", "cached", "output", "sessions"):
                week_d[k] += by_date[dstr][k]

    total_d = {"input": 0, "cached": 0, "output": 0, "sessions": 0}
    for d in by_date.values():
        for k in ("input", "cached", "output", "sessions"):
            total_d[k] += d[k]

    print()
    print("┌─────────────────────────────────────────────────┐")
    print("│       Codex 整体 Token 消耗（跨账号聚合）        │")
    print("├─────────────────────────────────────────────────┤")
    today_total = today_d["input"] + today_d["output"]
    week_total = week_d["input"] + week_d["output"]
    all_total = total_d["input"] + total_d["output"]
    print(f"│  今日: {fmt_num(today_total):>10}  ({today_d['sessions']} 会话)".ljust(50) + "│")
    print(f"│  本周: {fmt_num(week_total):>10}  ({week_d['sessions']} 会话)".ljust(50) + "│")
    print(f"│  累计: {fmt_num(all_total):>10}  ({total_d['sessions']} 会话)".ljust(50) + "│")
    print("├─────────────────────────────────────────────────┤")
    print(f"│  今日明细: input {fmt_num(today_d['input'])} / cached {fmt_num(today_d['cached'])} / output {fmt_num(today_d['output'])}".ljust(50) + "│")
    print("├─────────────────────────────────────────────────┤")
    print("│  最近 7 天:".ljust(50) + "│")
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        if d in by_date:
            v = by_date[d]
            t = v["input"] + v["output"]
            bar_len = min(int(t / 5_000_000), 20)
            bar = "█" * bar_len
            print(f"│   {d} {fmt_num(t):>9} {bar}".ljust(50) + "│")
        else:
            print(f"│   {d}         -".ljust(50) + "│")
    print("└─────────────────────────────────────────────────┘")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 300
        while True:
            subprocess.run(["clear"]) if False else print("\033[2J\033[H", end="")
            by_date = collect_all()
            print(f"\n刷新时间: {datetime.now().strftime('%H:%M:%S')}  (每 {interval}s 自动刷新)")
            print_dashboard(by_date)
            time.sleep(interval)
    else:
        by_date = collect_all()
        print_dashboard(by_date)


if __name__ == "__main__":
    main()
