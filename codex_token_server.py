#!/usr/bin/env python3
"""Codex Token 消耗看板 v3 - 按事件时间戳归组（跨天会话正确分摊到每天）。

v3 变更:
- 每条 token 事件带时间戳，消耗按事件发生日归属（长会话跨天不再记到创建日）
- 会话级去重: 同一 session 多个 rollout 文件合并，按时间序算增量，不重复计数
- 下钻明细: 显示该会话在所选当天的消耗 + 当天活跃时间段

v3.1 变更（缓存强化）:
- 文件级增量解析: rollout 是追加写 JSONL，只读上次断点之后的新字节，活跃文件不再整文件重读
- 后台 30s 刷新线程: /api/stats 与 /api/sessions 永远命中热缓存，请求路径零聚合开销
- 兜底: 缓存年龄 >120s（刷新线程死亡）时请求才同步重算

v3.2 变更（cache 命中统计）:
- 聚合 cached_input_tokens 增量, 今日卡片/每日 tooltip/会话明细均显示命中率

v3.3 变更（实时速率仪表盘）:
- /api/rate: 1m/5m/15m 滚动窗口消耗速度 + 最近30分钟 30s 粒度 sparkline（input/cached 拆分）+ 活跃会话
- /widget: 紧凑实时仪表盘页面（1s 轮询），桌面悬浮第一版
- 后台刷新周期 30s → 5s（增量读只碰新字节，代价低，速率数据更实时）

用法:
    python3 codex_token_server.py              # 默认 127.0.0.1:8790
    python3 codex_token_server.py 9000         # 指定端口
"""

import json
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SESSIONS_DIR = Path.home() / ".codex" / "sessions"
SESSION_INDEX = Path.home() / ".codex" / "session_index.jsonl"
START_DATE = datetime(2026, 7, 28)  # 展示起点

_file_cache = {}       # path -> (mtime, size, ino, offset, result)
_title_index = None
_title_index_mtime = None
_day_cache = {"data": None, "ts": 0}
_rate_state = {"events": [], "ts": 0}   # 最近事件缓冲 (te, sid, di, dc, do)，供 /api/rate
_quota_state = {"data": None, "ts": 0}  # 官方额度进度（codex app-server account/rateLimits/read）
_lock = threading.Lock()          # 保护 _day_cache 读写
_compute_lock = threading.Lock()  # 串行化聚合计算，防冷启动惊群
REFRESH_INTERVAL = 5              # 后台刷新周期（秒）——v3.3 起 5s，服务实时速率
QUOTA_POLL_INTERVAL = 60          # 官方额度轮询周期（秒）
REQUEST_MAX_STALE = 120           # 请求兜底阈值：缓存超过该年龄才同步重算
READ_BLOCK = 8 << 20              # 增量读文件的块大小（8MB，限制内存占用）


def load_title_index():
    global _title_index, _title_index_mtime
    try:
        mtime = SESSION_INDEX.stat().st_mtime
    except OSError:
        mtime = None
    # 文件变化（新会话登记 thread_name）或首次调用时重载
    if _title_index is not None and mtime == _title_index_mtime:
        return _title_index
    idx = {}
    if mtime is not None:
        try:
            with open(SESSION_INDEX, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        if d.get("id") and d.get("thread_name"):
                            idx[d["id"]] = d["thread_name"]
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass
    _title_index = idx
    _title_index_mtime = mtime
    return idx


def _extract(result, need_meta, line):
    """解析一行 JSONL，原地更新 result（头部 meta / token 事件）。返回新的 need_meta。
    口径与 v3 全量解析严格一致：meta（session_id+cwd）未找齐的阶段只找 meta、跳过 usage
    （meta 缺失的文件往往是同会话重复 rollout，计入会与权威文件双重计数）。
    """
    if need_meta:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return need_meta
        p = d.get("payload") or {}
        if isinstance(p, dict):
            if not result["session_id"] and p.get("session_id"):
                result["session_id"] = str(p["session_id"])
            if not result["cwd"] and p.get("cwd"):
                result["cwd"] = str(p["cwd"])
            need_meta = not (result["session_id"] and result["cwd"])
        return need_meta
    if "total_token_usage" not in line:
        return need_meta  # 快路径：非 usage 行不进 JSON 解析
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return need_meta
    p = d.get("payload") or {}
    ts = d.get("timestamp", "")
    info = p.get("info") if isinstance(p, dict) else None
    if not isinstance(info, dict):
        return need_meta
    tu = info.get("total_token_usage")
    if isinstance(tu, dict) and "input_tokens" in tu and ts:
        result["events"].append((
            str(ts),
            int(tu.get("input_tokens", 0)),
            int(tu.get("cached_input_tokens", 0)),
            int(tu.get("output_tokens", 0)),
        ))
    return need_meta


def parse_session_file(path: Path):
    """解析一个 rollout 文件（增量：只读上次断点之后追加的字节）。
    返回 {"session_id", "cwd", "events": [(ts, input_cum, cached_cum, output_cum)]}
    events 按文件内出现顺序；ts 为 ISO 字符串。
    """
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _file_cache.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[4]

    # 增量续读条件：同一 inode 且文件未收缩；否则从头整读
    result = {"session_id": "", "cwd": "", "events": []}
    start_offset = 0
    if cached and st.st_ino == cached[2] and st.st_size >= cached[3]:
        result = cached[4]
        start_offset = cached[3]

    need_meta = not (result["session_id"] and result["cwd"])
    offset = start_offset
    try:
        with open(path, "rb") as f:
            f.seek(start_offset)
            buf = b""
            while True:
                block = f.read(READ_BLOCK)
                if not block:
                    break
                buf += block
                last_nl = buf.rfind(b"\n")
                if last_nl < 0:
                    continue  # 块内没有完整行，等下一块拼上
                for line in buf[: last_nl + 1].decode("utf-8", "ignore").splitlines():
                    need_meta = _extract(result, need_meta, line)
                offset += last_nl + 1
                buf = buf[last_nl + 1:]
            # 结尾不完整的行留在 buf 不消费，下次从 offset 续读
    except Exception:
        pass
    _file_cache[key] = (st.st_mtime, st.st_size, st.st_ino, offset, result)
    return result


def collect_all(max_stale=REQUEST_MAX_STALE):
    """读聚合缓存。max_stale=0 强制重算（后台刷新线程用）。
    返回 (by_date_usage, by_date_sessions, sessions_meta)
    """
    with _lock:
        if max_stale and _day_cache["data"] is not None and time.time() - _day_cache["ts"] < max_stale:
            return _day_cache["data"]

    with _compute_lock:
        # 双检：等锁期间别的线程可能刚刷新完
        with _lock:
            if _day_cache["data"] is not None and time.time() - _day_cache["ts"] < REFRESH_INTERVAL:
                return _day_cache["data"]
        result = _compute_aggregate()
        with _lock:
            _day_cache["data"] = result
            _day_cache["ts"] = time.time()
        return result


def _compute_aggregate():
    """全量聚合（文件级增量读，未变化的文件直接走缓存）。
    返回 (by_date_usage, by_date_sessions, sessions_meta)
    """
    # 会话 -> 事件按文件组织；每会话取累计值最大的文件作为权威时间线
    # （多 rollout 文件计数器互相重叠，跨文件合并 delta 会重复计算）
    sessions_files = defaultdict(list)  # sid -> [parsed]
    # 数据源: 活跃 sessions(YYYY/MM/DD 三层嵌套) + archived_sessions(平铺 jsonl)。
    # 两目录零重名(已实测 comm 校验), sid 相同也会在下方 max() 权威文件选择时去重。
    source_dirs = [SESSIONS_DIR, Path.home() / ".codex" / "archived_sessions"]
    for src_dir in source_dirs:
        if not src_dir.exists():
            continue
        jsonl_files = []
        if src_dir.name == "archived_sessions":
            jsonl_files = list(src_dir.glob("*.jsonl"))          # 平铺
        else:
            for year_dir in src_dir.iterdir():                     # 三层嵌套
                if not year_dir.is_dir() or not year_dir.name.isdigit():
                    continue
                for month_dir in year_dir.iterdir():
                    if not month_dir.is_dir() or not month_dir.name.isdigit():
                        continue
                    for day_dir in month_dir.iterdir():
                        if not day_dir.is_dir():
                            continue
                        jsonl_files.extend(day_dir.glob("*.jsonl"))
        for f in jsonl_files:
            parsed = parse_session_file(f)
            if not parsed or not parsed.get("events"):
                continue
            sid = parsed["session_id"] or f.stem.split("-")[-1]
            sessions_files[sid].append(parsed)

    by_date_usage = defaultdict(lambda: {"input": 0, "cached": 0, "output": 0, "sessions": 0})
    by_date_sessions = defaultdict(dict)
    sessions_meta = {}
    now_epoch = time.time()
    rate_cutoff = now_epoch - 2400            # 实时速率数据：保留最近 40 分钟事件（覆盖 15m 窗口 + 30m sparkline）
    rate_all = []                             # 本轮重算收集的实时事件，最后整体替换缓冲（防重复累加）

    for sid, parsed_list in sessions_files.items():
        # 权威文件 = 最后事件累计值最大的文件（包含最完整历史）
        def last_cum(p):
            evs = p["events"]
            return evs[-1][1] if evs else 0
        auth = max(parsed_list, key=last_cum)
        meta = {"cwd": "", "files": len(parsed_list)}
        for p in parsed_list:
            if not meta["cwd"] and p.get("cwd"):
                meta["cwd"] = p["cwd"]
        sessions_meta[sid] = meta

        prev = None
        day_agg = defaultdict(lambda: {"input": 0, "cached": 0, "output": 0, "first_ts": "", "last_ts": ""})
        for ts, ci, cc, co in auth["events"]:
            if prev is None:
                di, dc, do = ci, cc, co   # 权威文件首事件的全额累计归当日（近似会话此前消耗）
            else:
                di, dc, do = max(ci - prev[0], 0), max(cc - prev[1], 0), max(co - prev[2], 0)
            prev = (ci, cc, co)
            day = ts[:10]
            a = day_agg[day]
            a["input"] += di
            a["cached"] += dc
            a["output"] += do
            t = ts[11:16] if len(ts) > 16 else ""
            if not a["first_ts"]:
                a["first_ts"] = t
            a["last_ts"] = t
            try:
                te = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                if te >= rate_cutoff:
                    rate_all.append((te, sid, di, dc, do))
            except (ValueError, OverflowError):
                pass
        for day, a in day_agg.items():
            if a["input"] == 0 and a["output"] == 0:
                continue
            u = by_date_usage[day]
            u["input"] += a["input"]
            u["cached"] += a["cached"]
            u["output"] += a["output"]
            u["sessions"] += 1
            by_date_sessions[day][sid] = a

    # 实时事件缓冲：整体替换（重算遍历的是全量事件列表，增量 append 会每轮重复累计）
    with _lock:
        _rate_state["events"] = rate_all
        _rate_state["ts"] = time.time()

    return (by_date_usage, by_date_sessions, sessions_meta)


def _find_codex_bin():
    """定位 codex 可执行文件。launchd 环境 PATH 极简，需兜底搜索 nvm/homebrew。"""
    import shutil, glob
    p = shutil.which("codex")
    if p:
        return p
    for pat in ("~/.nvm/versions/node/*/bin/codex", "/opt/homebrew/bin/codex", "/usr/local/bin/codex"):
        hits = sorted(glob.glob(os.path.expanduser(pat)))
        if hits:
            return hits[-1]
    return None


def fetch_quota():
    """通过 codex app-server JSON-RPC 读官方额度进度。
    注意: app-server 需 stdin 保持开启逐条写入并等待响应（run 一次性 input 会零输出）。
    返回 {"plan", "used_pct", "window_mins", "resets_at", "fetched_at"} 或 None。
    """
    import subprocess
    binpath = _find_codex_bin()
    if not binpath:
        return None
    # codex CLI 是 node 脚本（#!/usr/bin/env node），launchd 极简 PATH 下找不到 node
    env_path = os.path.dirname(binpath) + ":/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"
    env = dict(os.environ, PATH=env_path)
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": {"name": "codex-token-server", "title": "q", "version": "3.3"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"},
    ]
    try:
        p = subprocess.Popen([binpath, "app-server"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        try:
            for r in reqs:
                p.stdin.write(json.dumps(r) + "\n")
                p.stdin.flush()
                time.sleep(1)
            time.sleep(6)
            p.stdin.close()
            out = p.stdout.read()
        finally:
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
        for line in out.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("id") == 2:
                rl = (d.get("result") or {}).get("rateLimits") or {}
                pr = rl.get("primary") or {}
                if pr.get("usedPercent") is not None:
                    return {"plan": rl.get("planType"), "used_pct": pr.get("usedPercent"),
                            "window_mins": pr.get("windowDurationMins"),
                            "resets_at": pr.get("resetsAt"), "fetched_at": int(time.time())}
                break
    except Exception:
        pass
    return None


def quota_worker():
    while True:
        d = fetch_quota()
        with _lock:
            if d:
                _quota_state["data"] = d
                _quota_state["ts"] = time.time()
        time.sleep(QUOTA_POLL_INTERVAL)


def build_rate():
    """实时速率: 滚动窗口 1m/5m/15m + 30 分钟 30s 粒度 sparkline + 活跃会话。

    注意: JSONL usage 事件的时间戳粒度较粗（会话爆发时多轮同秒），
    tokens_per_sec 只作宏观速率参考；活跃状态用文件 mtime 判断。
    """
    with _lock:
        events = list(_rate_state["events"])
    now = time.time()
    windows = {}
    for w, sec in (("1m", 60), ("5m", 300), ("15m", 900)):
        ev = [e for e in events if e[0] >= now - sec]
        ti = sum(e[2] for e in ev); tc = sum(e[3] for e in ev); to = sum(e[4] for e in ev)
        windows[w] = {
            "tokens_per_sec": round((ti + to) / sec, 1),
            "input": ti, "cached": tc, "output": to,
            "cached_pct": round(tc / ti * 100, 1) if ti else 0.0,
        }
    # sparkline: 30 分钟, 30s 桶
    buckets = [{"fresh": 0, "cached": 0, "output": 0} for _ in range(60)]
    b0 = now - 1800
    for e in events:
        idx = int((e[0] - b0) // 30)
        if 0 <= idx < 60:
            buckets[idx]["fresh"] += e[2] - e[3]
            buckets[idx]["cached"] += e[3]
            buckets[idx]["output"] += e[4]
    # 活跃会话: 事件最近 5 分钟 + 该会话任一 rollout 文件 mtime 距今 < 60s（真实活跃）
    # 水位/cache 建议: 最近一轮 delta input 即该会话当前上下文水位（codex 每轮重发全量上下文）。
    # cache TTL=30 分钟（GPT-5.6 系，官方 prompt-caching 文档）: 闲置超 30 分钟缓存过期，
    # 重启一轮需全价重读 ctx（fresh 计价 125 cr/1M，cached 12.5）→ restart_cost = ctx*0.9*125/1e6。
    SPLIT_WM = 150e3          # 建议拆分水位: 150K（阈值扫描边际收益拐点，省 30% input）
    last_turn = {}
    active = defaultdict(lambda: {"input": 0, "cached": 0, "last": 0})
    for e in events:
        if e[0] >= now - 300:
            a = active[e[1]]
            a["input"] += e[2]; a["cached"] += e[3]
            a["last"] = max(a["last"], e[0])
        # 最近 40 分钟内最后事件水位（含活跃但暂无 5m 事件的会话）
        last_turn[e[1]] = (e[0], e[2])
    wm_ctx = {sid: di for sid, (te, di) in last_turn.items() if te >= now - 2400}
    live = set()
    try:
        for year_dir in SESSIONS_DIR.iterdir():
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir() or not month_dir.name.isdigit():
                    continue
                for day_dir in month_dir.iterdir():
                    if not day_dir.is_dir():
                        continue
                    for fp in day_dir.glob("*.jsonl"):
                        try:
                            if now - fp.stat().st_mtime < 60:
                                # 文件名尾部即 session_id（rollout-...-<sid>.jsonl）
                                live.add(fp.stem.split("-")[-1])
                        except OSError:
                            pass
    except OSError:
        pass
    titles = load_title_index()
    sessions = []
    advice = {"split": [], "cache": []}
    for sid, a in sorted(active.items(), key=lambda kv: -kv[1]["input"])[:8]:
        ctx = wm_ctx.get(sid, 0)
        idle = int(now - a["last"])
        # 闲置 >30min 且水位高 → cache 已过期，重启首需全价重读
        expired = idle > 1800
        restart_cr = round(ctx * 0.9 * 125 / 1e6, 1) if (expired and ctx > 50e3) else 0.0
        sessions.append({
            "session_id": sid[:8],
            "title": titles.get(sid, "") or sid[:8],
            "input_5m": a["input"], "cached_pct": round(a["cached"] / a["input"] * 100, 1) if a["input"] else 0.0,
            "idle_sec": idle,
            "live": any(sid.endswith(t) or t.endswith(sid) for t in live),
            "ctx_kb": int(ctx / 1e3),
            "split_hint": ctx >= SPLIT_WM,
            "cache_expired": expired and ctx > 50e3,
            "restart_cost_cr": restart_cr,
        })
        t = titles.get(sid, "") or sid[:8]
        if ctx >= SPLIT_WM:
            advice["split"].append(t)
        if restart_cr > 0:
            advice["cache"].append({"title": t, "cr": restart_cr, "idle_min": idle // 60})
    return {"now": int(now), "windows": windows, "sparkline": buckets, "active_sessions": sessions,
            "live_count": len(live),
            "last_event_ts": int(max((e[0] for e in events), default=0)),
            "quota": _quota_state["data"],
            "advice": advice}


def build_stats():
    by_date, _, _ = collect_all()
    with _lock:
        cache_ts = _day_cache["ts"] or time.time()
    now = datetime.now()
    out_days = []
    d = START_DATE
    while d <= now:
        ds = d.strftime("%Y-%m-%d")
        v = by_date.get(ds, {"input": 0, "cached": 0, "output": 0, "sessions": 0})
        out_days.append({
            "date": ds,
            "input": v["input"], "cached": v["cached"],
            "output": v["output"], "sessions": v["sessions"],
            "total": v["input"] + v["output"],
        })
        d += timedelta(days=1)

    today_str = now.strftime("%Y-%m-%d")
    today = by_date.get(today_str, {"input": 0, "cached": 0, "output": 0, "sessions": 0})

    total = {"input": 0, "cached": 0, "output": 0, "sessions": 0}
    for v in by_date.values():
        total["input"] += v["input"]
        total["cached"] += v["cached"]
        total["output"] += v["output"]
        total["sessions"] += v["sessions"]

    return {
        "updated_at": datetime.fromtimestamp(cache_ts).strftime("%Y-%m-%d %H:%M:%S") + f"（缓存 {max(int(time.time() - cache_ts), 0)}s）",
        "today": {**today, "total": today["input"] + today["output"]},
        "all_time": {**total, "total": total["input"] + total["output"]},
        "days": out_days,
    }


def build_sessions_for_date(date_str: str):
    by_date, by_date_sessions, sessions_meta = collect_all()
    day_sessions = by_date_sessions.get(date_str, {})
    titles = load_title_index()

    out = []
    for sid, a in day_sessions.items():
        meta = sessions_meta.get(sid, {})
        cwd = meta.get("cwd", "")
        project = cwd.rstrip("/").split("/")[-1] if cwd else ""
        out.append({
            "time": a["first_ts"] + ("~" + a["last_ts"] if a["last_ts"] > a["first_ts"] else ""),
            "session_id": sid[:8],
            "project": project,
            "title": titles.get(sid, "") or f"session {sid[:8]}",
            "files": meta.get("files", 1),
            "input": a["input"], "cached": a.get("cached", 0), "output": a["output"],
            "total": a["input"] + a["output"],
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return {"date": date_str, "count": len(out), "sessions": out}


WIDGET_PAGE = """<!DOCTYPE html><html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Codex 实时消耗</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { background:transparent; }
  /* 完全透明底: 无 body 背景/边框/阴影, 毛玻璃底由原生 NSVisualEffectView 提供 */
  body { font-family:-apple-system,"SF Pro Text","PingFang SC",sans-serif; color:#e6edf3;
         width:320px; padding:12px 14px; -webkit-user-select:none; cursor:default; }
  body, body * { text-shadow: 0 1px 3px rgba(0,0,0,0.9); }
  .hdr { display:flex; justify-content:space-between; align-items:baseline; }
  .hdr .t { font-size:12px; font-weight:600; color:#8b949e; letter-spacing:.5px; cursor:grab; }
  .btn { font-size:11px; color:#58a6ff; padding:0 2px; }
  .hdr .acts { cursor:pointer; padding:2px 0; margin:-2px 0; }   /* 整块热区 */
  .hdr .acts:hover .btn { color:#79c0ff; }
  .dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:#3fb950; margin-right:5px; }
  .dot.off { background:#484f58; }
  .clock { font-size:10px; color:#484f58; font-variant-numeric:tabular-nums; }
  .rate { display:flex; align-items:baseline; gap:6px; margin:6px 0 2px; }
  .rate .v { font-size:30px; font-weight:700; font-variant-numeric:tabular-nums; color:#58a6ff; }
  .rate .u { font-size:11px; color:#8b949e; }
  .rate.hot .v { color:#f0883e; }
  .sub { font-size:10.5px; color:#8b949e; margin-bottom:8px; font-variant-numeric:tabular-nums; }
  .spark { display:flex; align-items:flex-end; gap:1px; height:44px; margin-bottom:8px; }
  .spark div { flex:1; min-height:1px; border-radius:1px 1px 0 0; background:#1f6feb88; position:relative; }
  .spark div.f { background:#f0883e; }
  .spark .tip { display:none; position:absolute; bottom:calc(100% + 3px); left:50%; transform:translateX(-50%);
    background:#21262d; border:1px solid #30363d; border-radius:4px; padding:3px 5px; font-size:9px; white-space:nowrap; z-index:9; }
  .spark div:hover .tip { display:block; }
  .win { display:flex; gap:6px; margin-bottom:8px; }
  .win div { flex:1; padding:5px 7px; }  /* 无底板: 透出原生毛玻璃 */
  .win .l { font-size:9px; color:#8b949e; }
  .win .w { font-size:13px; font-weight:600; font-variant-numeric:tabular-nums; }
  .ses { border-top:1px solid #21262d; padding-top:6px; }
  .ses .row { display:flex; gap:6px; font-size:10px; color:#8b949e; padding:2px 0; align-items:baseline; }
  .ses .hint { display:flex; gap:6px; font-size:10px; padding:1px 0 3px 0; align-items:center; flex-wrap:wrap; }
  .ses .hint .tag { font-size:9px; border-radius:4px; padding:0 4px; white-space:nowrap; }
  .tag.wm { color:#f0b649; border:1px solid rgba(240,182,73,.45); background:rgba(240,182,73,.08); cursor:pointer; }
  .tag.wm .bang { font-weight:700; }
  .reason { display:none; font-size:9.5px; color:#8b949e; line-height:1.65; padding:4px 6px 2px 8px;
            border-left:2px solid rgba(240,182,73,.4); margin:2px 0 3px 0; }
  .reason.open { display:block; }
  .reason b { color:#c9d1d9; font-weight:600; }
  .reason .src { color:#6e7681; font-size:8.5px; margin-top:2px; }
  .tag.cx { color:#e5534b; border:1px solid rgba(229,83,75,.4); background:rgba(229,83,75,.08); }
  .tag.ck { color:#3fb950; border:1px solid rgba(63,185,80,.35); background:rgba(63,185,80,.07); }
  .quota { margin:8px 0 2px; }
  .qhead { display:flex; justify-content:space-between; font-size:10px; color:#8b949e; margin-bottom:3px; }
  .qtrack { height:6px; background:#21262d; border-radius:3px; overflow:hidden; }
  .bar { height:100%; background:linear-gradient(90deg,#2f81f7,#58a6ff); border-radius:3px; }
  .bar.hot { background:linear-gradient(90deg,#d29922,#f0883e); }
  .ses .row .n { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#c9d1d9; }
  .ses .row .r { font-variant-numeric:tabular-nums; white-space:nowrap; }
  .ses .row .pct { color:#3fb950; }
  .idle { color:#484f58 !important; }
</style>
</head>
<body>
  <div class="hdr"><div class="t"><span class="dot" id="dot"></span>Codex 实时消耗</div><div class="acts" id="btn-expand"><span class="btn">历史综合统计 ⤢</span> <span class="clock" id="clock"></span></div></div>
  <div class="rate" id="ratebox"><div class="v" id="v-rate">--</div><div class="u">tokens/s（1m 窗口）</div></div>
  <div class="sub" id="sub"></div>
  <div class="spark" id="spark"></div>
  <div class="win">
    <div><div class="l">5m 平均</div><div class="w" id="w-5">--</div></div>
    <div><div class="l">15m 平均</div><div class="w" id="w-15">--</div></div>
    <div><div class="l">cache 命中</div><div class="w" style="color:#3fb950" id="w-pct">--</div></div>
  </div>
  <div class="ses" id="ses"></div>
  <div id="qbox"></div>
<script>
function fmt(n){ if(n>=1e9)return(n/1e9).toFixed(2)+'B'; if(n>=1e6)return(n/1e6).toFixed(1)+'M';
  if(n>=1e3)return(n/1e3).toFixed(1)+'K'; return String(Math.round(n)); }
function fmtR(n){ if(n>=1e6)return(n/1e6).toFixed(1)+'M'; if(n>=1e3)return(n/1e3).toFixed(0)+'K'; return n.toFixed(0); }
let lastNow = 0;
async function tick(){
  try{
    const r = await fetch('/api/rate'); const d = await r.json();
    const w1 = d.windows['1m'], w5 = d.windows['5m'], w15 = d.windows['15m'];
    document.getElementById('v-rate').textContent = fmtR(w1.tokens_per_sec);
    document.getElementById('ratebox').className = 'rate' + (w1.tokens_per_sec > w15.tokens_per_sec*1.5 && w1.tokens_per_sec>1000 ? ' hot' : '');
    document.getElementById('sub').textContent = w1.input>0
      ? '1m: '+fmt(w1.input)+' in / '+fmt(w1.output)+' out'
      : (d.last_event_ts ? '空闲 · 最近事件 '+Math.max(0,d.now-d.last_event_ts)+'s 前' : '空闲 · 近 40 分钟无事件');
    document.getElementById('w-5').textContent = fmtR(w5.tokens_per_sec)+'/s';
    document.getElementById('w-15').textContent = fmtR(w15.tokens_per_sec)+'/s';
    document.getElementById('w-pct').textContent = w1.input>0 ? w1.cached_pct+'%' : '--';
    const act = d.active_sessions.length;
    document.getElementById('dot').className = 'dot' + (act?'':' off');
    const sp = document.getElementById('spark'); sp.innerHTML='';
    const mx = Math.max(...d.sparkline.map(b=>b.fresh+b.cached+b.output), 1);
    d.sparkline.forEach((b,i)=>{
      const tot = b.fresh+b.cached+b.output, h = Math.max(1, tot/mx*100);
      const el = document.createElement('div');
      el.style.height = h+'%';
      el.className = b.fresh*4 > b.cached ? 'f' : '';
      const mm = new Date((d.now-1800+i*30)*1000);
      el.innerHTML = '<div class="tip">'+String(mm.getHours()).padStart(2,'0')+':'+String(mm.getMinutes()).padStart(2,'0')
        +' · 总'+fmt(tot)+'<br>cached '+fmt(b.cached)+' / fresh '+fmt(b.fresh)+'</div>';
      sp.appendChild(el);
    });
    let sh = '';
    d.active_sessions.forEach(s=>{
      const idle = s.idle_sec<90 ? '' : ' idle';
      const liveMark = s.live ? ' ●' : '';
      const rTxt = s.input_5m>0 ? fmt(s.input_5m)+'<span class="pct"> '+s.cached_pct+'%</span>' : '<span style="color:#484f58">--</span>';
      sh += '<div class="row'+idle+'"><div class="n">'+s.title.replace(/</g,'&lt;').slice(0,26)+liveMark+'</div>'
        +'<div class="r">'+rTxt+'</div></div>';
      // 水位/缓存建议行（点击黄色标签展开理由: 事件委托, 见 ses click 监听）
      let hints = '';
      if (s.split_hint) hints += '<span class="tag wm" data-ctx="'+s.ctx_kb+'">⚠ 水位 '+s.ctx_kb+'K ≥150K · 建议拆分 <span class="bang">!</span></span>'
        + '<div class="reason"><b>为什么是 150K · 拆分理由</b><br>'
        + '1. Codex 每轮重发全量上下文：当前水位 '+s.ctx_kb+'K，即每轮按此量计费（你的账户 97% 缓存命中，有效价≈13.7 cr/100K）。<br>'
        + '2. 阈值扫描（840 个本地会话全量重放）：150K 处拆分省 30% input；200K 只省 14%，100K 仅再省 10pp 且打断翻倍——150K 是边际收益拐点。<br>'
        + '3. 你的实测：巨型会话在第 23~43 轮即破 150K，其后 5000+ 轮顶着高水位跑，Top10 会话占全部消耗 45.5%。<br>'
        + '<span class="src">来源：本地 ~/.codex/sessions + archived_sessions 全量解析（2,2xx 文件）；计价 developers.openai.com/codex/pricing（Sol 125/12.5/750 cr/1M）；缓存 30m TTL 见 platform.openai.com/docs/guides/prompt-caching</span></div>';
      if (s.cache_expired) hints += '<span class="tag cx">闲置超30m 缓存过期 · 重启≈'+s.restart_cost_cr+' cr</span>';
      else if (s.ctx_kb >= 50) {
        const min = Math.max(0, 30 - Math.floor(s.idle_sec/60));
        hints += '<span class="tag ck" data-ctx="'+s.ctx_kb+'" data-idle="'+s.idle_sec+'">cache ✓ 剩 ' + min + 'm' + (s.live ? ' · 活跃' : '') + '</span>';
      }
      if (hints) sh += '<div class="hint">'+hints+'</div>';
    });
    document.getElementById('ses').onclick = e => {
      const wm = e.target.closest('.tag.wm');
      // 点击黄标 → 原生弹窗展示拆分理由 (点击时实时水位快照)
      if (wm) {
        window.webkit.messageHandlers.popreason.postMessage(String(wm.dataset.ctx || '?'));
        return;
      }
      const ck = e.target.closest('.tag.ck');
      // 点击绿标 → 原生弹窗展示缓存状态依据 (水位+闲置秒数快照)
      if (ck) {
        window.webkit.messageHandlers.popreason.postMessage('ck:' + (ck.dataset.ctx || '?') + ':' + (ck.dataset.idle || '0'));
      }
    };
    document.getElementById('ses').innerHTML = sh || '<div class="row"><div class="n" style="color:#484f58">无活跃会话</div></div>';
    document.getElementById('dot').className = 'dot' + (d.live_count>0?'':' off');
    let qh = '';
    if (d.quota) {
      const pct = d.quota.used_pct, plan = d.quota.plan || '';
      const hrs = Math.max(0, Math.floor((d.quota.resets_at - d.now)/3600));
      const rst = hrs >= 48 ? Math.floor(hrs/24)+'天' : hrs+'小时';
      const bar = pct>=80 ? 'bar hot' : 'bar';
      qh = '<div class="quota"><div class="qhead"><span>额度 · '+plan+'</span><span>'+pct+'% · 重置还剩 '+rst+'</span></div>'
        +'<div class="qtrack"><div class="'+bar+'" style="width:'+Math.min(100,pct)+'%"></div></div></div>';
    }
    document.getElementById('qbox').innerHTML = qh;
    lastNow = d.now;
  }catch(e){ document.getElementById('dot').className='dot off'; }
}
tick(); setInterval(tick, 2000);
// 拖动防误触: mousedown 记录起点, 位移>5px 的 mouseup 不触发按钮 click
let _dx0 = null, _dy0 = null;
['mousedown','touchstart'].forEach(ev => document.addEventListener(ev, e => {
  _dx0 = e.clientX; _dy0 = e.clientY;
}, true));
['mouseup','touchend'].forEach(ev => document.addEventListener(ev, e => {
  if (_dx0 !== null && (Math.abs(e.clientX-_dx0) > 5 || Math.abs(e.clientY-_dy0) > 5)) {
    e.stopPropagation(); e.preventDefault(); _suppressClick = true;
  }
  _dx0 = null; _dy0 = null;
}, true));
let _suppressClick = false;
document.addEventListener('click', e => {
  if (_suppressClick) { e.stopPropagation(); e.preventDefault(); _suppressClick = false; }
}, true);
// 历史综合统计按钮: 通知原生壳开新窗口 (主悬浮窗保持不动)
document.getElementById('btn-expand').addEventListener('click', e => {
  e.stopPropagation();
  try { window.webkit.messageHandlers.expand.postMessage('expand'); } catch(err) {}
});
</script>
</body>
</html>"""


# 拆分理由独立页: 原生弹窗加载, ctx 由 query 传入 (点击黄标时的实时水位)
REASON_PAGE = """<!DOCTYPE html><html lang="zh-CN">
<head><meta charset="UTF-8"><title>拆分理由</title>
<style>
  html,body { background:transparent; margin:0; padding:10px 12px;
              font-family:-apple-system,'PingFang SC',sans-serif; }
  h3 { margin:0 0 6px 0; font-size:12px; color:#f0b649; font-weight:600; }
  .ctx { font-size:10.5px; color:#c9d1d9; margin-bottom:8px; }
  .ctx b { color:#f0b649; }
  ol { margin:0; padding-left:18px; }
  li { font-size:10.5px; color:#8b949e; line-height:1.7; margin-bottom:6px; }
  li b { color:#c9d1d9; font-weight:600; }
  .tbl { margin:6px 0 2px 0; border-collapse:collapse; }
  .tbl td,.tbl th { font-size:9.5px; padding:1px 7px; color:#8b949e; text-align:right;
                    border-bottom:1px solid rgba(255,255,255,.08); }
  .tbl th { color:#6e7681; font-weight:500; }
  .tbl td:first-child,.tbl th:first-child { text-align:left; }
  .tbl .hl td { color:#f0b649; }
  .src { font-size:9px; color:#6e7681; line-height:1.6; margin-top:8px;
         padding-top:6px; border-top:1px solid rgba(255,255,255,.08); }
  .src a { color:#58a6ff; text-decoration:none; }
</style></head>
<body>
<h3>为什么建议在 150K 拆分</h3>
<div class="ctx">当前水位 <b id="ctx">?</b> —— 每轮请求都按这个量计费</div>
<ol>
<li><b>计费机制</b>：Codex 每轮重发全量上下文。水位=每轮计费量，轮数×水位才是总消耗。账户 97% 缓存命中，有效价 ≈13.7 cr/100K——省的是单价，省不掉量。</li>
<li><b>阈值扫描</b>：840 个本地会话全量时间序列重放，在不同水位拆分再重放的净节省：
<table class="tbl">
<tr><th>拆分水位</th><th>input 节省</th><th>credits/周</th></tr>
<tr><td>200K</td><td>14.2%</td><td>39.0K</td></tr>
<tr class="hl"><td>150K</td><td>30.1%</td><td>82.6K</td></tr>
<tr><td>120K</td><td>36.8%</td><td>101.0K</td></tr>
<tr><td>100K</td><td>40.2%</td><td>110.2K</td></tr>
</table>
200K 太松（只省 14%）；压到 100K 只比 150K 多省 10pp，但打断频率约翻倍——<b>150K 是边际收益拐点</b>。</li>
<li><b>实测样本</b>：巨型会话第 23~43 轮即破 150K，之后 5000~7800 轮顶着高水位跑；Top10 会话占全部消耗 45.5%（最大 1.23B / 7838 轮）。</li>
<li><b>怎么做</b>：/compact 压缩上下文，或开新会话并贴任务摘要。</li>
</ol>
<div class="src">数据来源：本地 ~/.codex/sessions + archived_sessions 全量重放（840 会话）· 计价
<a href="https://developers.openai.com/codex/pricing">developers.openai.com/codex/pricing</a>（Sol 125/12.5/750 cr/1M，cached 10:1 折扣）·
缓存 30m TTL 见 <a href="https://platform.openai.com/docs/guides/prompt-caching">platform.openai.com/docs/guides/prompt-caching</a></div>
<script>
  const q = new URLSearchParams(location.search);
  document.getElementById('ctx').textContent = (q.get('ctx')||'?') + 'K';
</script>
</body></html>"""

# 缓存状态依据独立页: 原生弹窗加载, ctx/idle 由 query 传入 (点击绿标时快照)
CACHE_PAGE = """<!DOCTYPE html><html lang="zh-CN">
<head><meta charset="UTF-8"><title>cache 状态依据</title>
<style>
  html,body { background:transparent; margin:0; padding:10px 12px;
    font-family:-apple-system,"PingFang SC",sans-serif; color:#c9d1d9; }
  .card { background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.09);
    border-radius:10px; padding:10px 12px; margin-bottom:8px; }
  h2 { font-size:13px; margin:0 0 6px; color:#e6edf3; }
  p { font-size:11px; line-height:1.7; margin:4px 0; color:#8b949e; }
  b { color:#e6edf3; font-weight:600; }
  .g { color:#3fb950; }
  .y { color:#f0b649; }
  .src { font-size:9.5px; color:#6e7681; margin-top:6px; line-height:1.6; }
  a { color:#58a6ff; }
</style></head>
<body>
<div class="card">
  <h2>cache 剩余时间是怎么算的</h2>
  <p><b>TTL 30 分钟</b>（OpenAI 官方，GPT-5 系）：前缀命中一次即<b>刷新计时</b>。会话闲置时缓存逐段老化，满 30 分钟未命中即过期。</p>
  <p>当前会话已闲置 <b><span id="idle"></span></b>，水位 <b><span id="ctx"></span>K</b>：</p>
  <p>· 剩余 = 30m − 闲置时长（分钟取整，保守估计）<br>
     · 闲置期间若你再发一轮，缓存命中即重置回 30m<br>
     · 过期后重启 = 水位全量重读，≈ <b><span id="cost"></span> cr</b></p>
</div>
<div class="card">
  <h2>过期损失怎么算</h2>
  <p>过期重启成本 = 水位 × (125 − 12.5) cr/1M<br>
  = 水位 × 112.5 cr/1M（Sol input 原价 125、缓存价 12.5 cr/1M，差价即损失）</p>
  <p>例：150K 水位 → ≈16.9 cr；76K → ≈8.6 cr。</p>
</div>
<div class="card">
  <h2>来源</h2>
  <p class="src">· TTL 与命中刷新：OpenAI 官方文档 <a href="https://platform.openai.com/docs/guides/prompt-caching">platform.openai.com/docs/guides/prompt-caching</a><br>
  · 计价（Sol input 125 / cached 12.5 cr/1M）：developers.openai.com/codex/pricing<br>
  · 会话水位/闲置：本地 ~/.codex/sessions 实时解析（与登录账号无关）</p>
</div>
<script>
  const q = new URLSearchParams(location.search);
  const ctxK = parseInt(q.get('ctx')||'0', 10);
  const idleS = parseInt(q.get('idle')||'0', 10);
  document.getElementById('ctx').textContent = (q.get('ctx')||'?');
  document.getElementById('idle').textContent = idleS < 60 ? idleS + ' 秒' : Math.floor(idleS/60) + ' 分钟';
  document.getElementById('cost').textContent = (ctxK * 112.5 / 1000).toFixed(1);
</script>
</body></html>"""


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Codex Token 看板</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { background:transparent; }  /* 玻璃底由原生 NSVisualEffectView 提供 */
  body { font-family:-apple-system,"SF Pro Text","PingFang SC",sans-serif; color:#e6edf3; padding:26px 30px;
         -webkit-user-select:none; }
  body, body * { text-shadow: 0 1px 3px rgba(0,0,0,0.9); }  /* 与悬浮窗一致的文字阴影 */
  html { scrollbar-width: none; }
  body::-webkit-scrollbar { display:none; }
  .header { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:22px; }
  h1 { font-size:20px; font-weight:600; }
  .meta { color:#8b949e; font-size:13px; }
  .cards { display:grid; grid-template-columns:repeat(2,1fr); gap:14px; margin-bottom:24px; }
  .card { background:rgba(255,255,255,0.045); border:1px solid rgba(255,255,255,0.09); border-radius:10px; padding:16px 18px; }
  .card .label { color:#8b949e; font-size:12px; margin-bottom:7px; }
  .card .value { font-size:25px; font-weight:700; font-variant-numeric:tabular-nums; }
  .card .sub { color:#8b949e; font-size:12px; margin-top:5px; }
  .accent-blue{color:#58a6ff;} .accent-purple{color:#bc8cff;} .accent-orange{color:#d29922;} .accent-green{color:#3fb950;}
  .adv { border-radius:10px; padding:10px 14px; font-size:12.5px; margin-bottom:10px; line-height:1.8; }
  .adv.wm { color:#f0b649; border:1px solid rgba(240,182,73,.4); background:rgba(240,182,73,.07); }
  .adv .why { color:#58a6ff; font-size:11.5px; margin-left:4px; }
  .adv .whybox { display:none; margin-top:8px; padding:10px 12px; border-left:2px solid rgba(240,182,73,.5);
                 color:#c9d1d9; font-size:12px; line-height:1.9; background:rgba(0,0,0,0.18); border-radius:0 6px 6px 0; }
  .adv .whybox.open { display:block; }
  .adv .whybox .src { color:#8b949e; font-size:11px; }
  .adv.cx { color:#ff8182; border:1px solid rgba(255,129,130,.35); background:rgba(255,129,130,.06); margin-top:-4px; }
  .panel { background:rgba(255,255,255,0.045); border:1px solid rgba(255,255,255,0.09); border-radius:10px; padding:20px; margin-bottom:20px; }
  .chart-title { font-size:15px; font-weight:600; margin-bottom:16px; }
  .chart { display:flex; align-items:flex-end; gap:3px; height:200px; }
  .bar-wrap { flex:1; display:flex; flex-direction:column; align-items:center; gap:5px; height:100%; justify-content:flex-end; cursor:pointer; }
  .bar { width:100%; border-radius:3px 3px 0 0; background:linear-gradient(180deg,#58a6ff,#1f6feb); min-height:2px; transition:height .3s; position:relative; }
  .bar-wrap:hover .bar { background:linear-gradient(180deg,#79c0ff,#388bfd); }
  .bar-wrap.selected .bar { background:linear-gradient(180deg,#f0883e,#bc4c00); outline:1px solid #ffa657; }
  .bar .tip { display:none; position:absolute; bottom:calc(100% + 4px); left:50%; transform:translateX(-50%); background:rgba(255,255,255,0.10); border:1px solid rgba(255,255,255,0.14); border-radius:6px; padding:7px 9px; font-size:11px; white-space:nowrap; z-index:10; line-height:1.7; }
  .bar-wrap:hover .tip { display:block; }
  .bar-label { color:#8b949e; font-size:9.5px; }
  .bar-label.today { color:#58a6ff; font-weight:600; }
  .legend { display:flex; gap:16px; margin-top:12px; font-size:12px; color:#8b949e; }
  .footer-note { color:#8b949e; font-size:12px; margin-top:10px; }
  #detail { display:none; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th { text-align:left; color:#8b949e; font-weight:500; padding:8px 10px; border-bottom:1px solid rgba(255,255,255,0.12); font-size:11.5px; }
  td { padding:8px 10px; border-bottom:1px solid rgba(255,255,255,0.07); font-variant-numeric:tabular-nums; vertical-align:top; }
  tr:hover td { background:rgba(255,255,255,0.05); }
  .num { text-align:right; white-space:nowrap; }
  .msg { color:#c9d1d9; max-width:520px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .proj { color:#79c0ff; white-space:nowrap; max-width:150px; overflow:hidden; text-overflow:ellipsis; }
  .sid { color:#8b949e; font-family:ui-monospace,monospace; font-size:11px; max-width:96px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .time { white-space:nowrap; }
  .loading { color:#8b949e; padding:20px; text-align:center; }
</style>
</head>
<body>
  <div class="header">
    <h1>Codex Token 看板 <span style="color:#8b949e;font-size:13px;font-weight:400">跨账号聚合</span></h1>
    <div class="meta">更新于 <span id="updated"></span> · 自动刷新 60s</div>
  </div>

  <div class="cards">
    <div class="card"><div class="label">今日消耗</div><div class="value accent-blue" id="v-today">-</div><div class="sub" id="s-today"></div></div>
    <div class="card"><div class="label">区间合计（07-28 起）</div><div class="value accent-purple" id="v-30d">-</div><div class="sub" id="s-30d"></div></div>
    <div class="card"><div class="label">区间日均</div><div class="value accent-green" id="v-avg">-</div><div class="sub" id="s-avg"></div></div>
    <div class="card"><div class="label">累计（全部历史）</div><div class="value accent-orange" id="v-all">-</div><div class="sub" id="s-all"></div></div>
  </div>
  <div id="advice"></div>

  <div class="panel">
    <div class="chart-title">每日消耗（2026-07-28 起） <span style="color:#8b949e;font-size:12px;font-weight:400">（按事件实际发生时间归属 · 点击柱子看当天会话明细）</span></div>
    <div class="chart" id="chart"></div>
    <div class="legend"><span><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#58a6ff;margin-right:5px"></span>总消耗（input+output）</span><span>悬停看汇总 · 点击下钻会话级</span></div>
    <div class="footer-note">数据源：本地会话日志 ~/.codex/sessions/ · 消耗按 token 事件时间戳归属到当天（长会话跨天正确分摊）· 与登录账号无关</div>
  </div>

  <div class="panel" id="detail">
    <div class="chart-title" id="detail-title"></div>
    <div id="detail-body"><div class="loading">加载中…</div></div>
  </div>

<script>
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
  return String(n);
}
function esc(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function pct(part, whole) { return whole > 0 ? (part/whole*100).toFixed(1) + '%' : '0%'; }

let selectedDate = null;

async function refreshAdvice() {
  // 水位/缓存建议横幅: 复用 /api/rate 的 advice 结构
  try {
    const r = await fetch('/api/rate'); const d = await r.json();
    const box = document.getElementById('advice');
    let h = '';
    if (d.advice && d.advice.split && d.advice.split.length) {
      h += '<div class="adv wm">⚠ 水位预警 · ' + d.advice.split.map(esc).join('、')
        + ' 上下文已过 150K，继续跑每轮全量重发，建议 /compact 或开新会话贴任务摘要'
        + ' <a href="javascript:void(0)" class="why">依据 ▾</a>'
        + '<div class="whybox"><b>150K 阈值依据</b><br>'
        + '· Codex 每轮重发全量上下文，水位即每轮计费量（当前账户 97% 缓存命中，有效价 ≈13.7 cr/100K）<br>'
        + '· 阈值扫描（840 个本地会话全量时间序列重放）：150K 处拆分省 30% input（Sol 等效 82.6K credits）；200K 仅省 14%，压到 100K 只再多省 10pp 且打断频率翻倍<br>'
        + '· 实测样本：巨型会话在第 23~43 轮即破 150K，其后 5000~7800 轮顶着高水位跑；Top10 会话占全部消耗 45.5%（最大单会话 1.23B / 7838 轮）<br>'
        + '<span class="src">数据来源：本地 ~/.codex/sessions + archived_sessions 全量解析；官方计价 developers.openai.com/codex/pricing（Sol 125/12.5/750 cr/1M，cached 10:1）；cache TTL 30 分钟：platform.openai.com/docs/guides/prompt-caching</span></div></div>';
    }
    if (d.advice && d.advice.cache && d.advice.cache.length) {
      h += '<div class="adv cx">⏱ 缓存过期 · ' + d.advice.cache.map(c => esc(c.title) + '（闲置' + c.idle_min + '分钟，重启约 ' + c.cr + ' cr）').join('；')
        + ' — 大会话闲置超 30 分钟 cache TTL 已过，重启需全价重读</div>';
    }
    box.innerHTML = h;
    // 「依据 ▾」点击展开/收起 (事件委托, 避免内嵌 onclick 引号地狱)
    box.onclick = e => {
      const w = e.target.closest('.why');
      if (w && w.nextElementSibling) w.nextElementSibling.classList.toggle('open');
    };
  } catch (e) {}
}
refreshAdvice(); setInterval(refreshAdvice, 10000);

async function refresh() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('updated').textContent = d.updated_at;
    document.getElementById('v-today').textContent = fmt(d.today.total);
    document.getElementById('s-today').textContent = d.today.sessions + ' 个活跃会话 · cache命中 ' + pct(d.today.cached, d.today.input) + ' · input ' + fmt(d.today.input) + ' / output ' + fmt(d.today.output);
    const t30 = d.days.reduce((a,b)=>a+b.total,0);
    const s30 = d.days.reduce((a,b)=>a+b.sessions,0);
    const nDays = d.days.length;
    document.getElementById('v-30d').textContent = fmt(t30);
    document.getElementById('s-30d').textContent = s30 + ' 个会话日';
    document.getElementById('v-avg').textContent = fmt(Math.round(t30/nDays));
    document.getElementById('s-avg').textContent = nDays + ' 天平均 / 天';
    document.getElementById('v-all').textContent = fmt(d.all_time.total);
    document.getElementById('s-all').textContent = d.all_time.sessions + ' 个会话日 · ' + fmt(d.all_time.input) + ' input / ' + fmt(d.all_time.output) + ' output';

    const max = Math.max(...d.days.map(x=>x.total), 1);
    const today = new Date().toISOString().slice(0,10);
    const chart = document.getElementById('chart');
    chart.innerHTML = '';
    d.days.forEach(day => {
      const wrap = document.createElement('div');
      wrap.className = 'bar-wrap' + (day.date===selectedDate?' selected':'');
      const h = Math.max(2, Math.round(day.total / max * 100));
      wrap.innerHTML = '<div class="bar" style="height:' + h + '%">' +
        '<div class="tip"><b>' + day.date + '</b><br>总计 ' + fmt(day.total) +
        '<br>input ' + fmt(day.input) + '（cache命中 ' + pct(day.cached, day.input) + '）' +
        '<br>output ' + fmt(day.output) + ' · ' + day.sessions + ' 会话</div></div>' +
        '<div class="bar-label' + (day.date===today?' today':'') + '">' + day.date.slice(5) + '</div>';
      wrap.onclick = () => loadDetail(day.date);
      chart.appendChild(wrap);
    });
  } catch (e) { console.error(e); }
}

async function loadDetail(date) {
  selectedDate = date;
  document.querySelectorAll('.bar-wrap').forEach(w=>w.classList.remove('selected'));
  document.getElementById('detail').style.display = 'block';
  document.getElementById('detail-title').textContent = date + ' 会话明细';
  document.getElementById('detail-body').innerHTML = '<div class="loading">加载中…</div>';
  try {
    const r = await fetch('/api/sessions?date=' + date);
    const d = await r.json();
    let html = '<table><tr><th>当天活跃时段</th><th>项目</th><th>会话</th><th>标题</th>' +
      '<th class="num">当天 input</th><th class="num">cache命中</th><th class="num">当天 output</th><th class="num">当天总计</th></tr>';
    d.sessions.forEach(s => {
      html += '<tr><td>' + s.time + '</td><td class="proj">' + esc(s.project) + '</td>' +
        '<td class="sid">' + s.session_id + '</td><td class="msg" title="' + esc(s.title) + '">' + esc(s.title) + '</td>' +
        '<td class="num">' + fmt(s.input) + '</td>' +
        '<td class="num" style="color:#3fb950">' + fmt(s.cached) + ' (' + pct(s.cached, s.input) + ')</td>' +
        '<td class="num">' + fmt(s.output) + '</td><td class="num"><b>' + fmt(s.total) + '</b></td></tr>';
    });
    html += '</table>';
    document.getElementById('detail-title').innerHTML = date + ' 会话明细 <span>· 共 ' + d.count + ' 个活跃会话（当天消耗，按总量排序）</span>';
    document.getElementById('detail-body').innerHTML = html;
    // 点柱子后自动滚到明细表, 免去手动下翻
    document.getElementById('detail').scrollIntoView({behavior:'smooth', block:'start'});
  } catch (e) {
    document.getElementById('detail-body').innerHTML = '<div class="loading">加载失败: ' + e + '</div>';
  }
}

refresh();
setInterval(refresh, 60000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index"):
            self._send(200, "text/html; charset=utf-8", HTML_PAGE.encode("utf-8"))
        elif parsed.path == "/widget":
            self._send(200, "text/html; charset=utf-8", WIDGET_PAGE.encode("utf-8"))
        elif parsed.path == "/reason":
            self._send(200, "text/html; charset=utf-8", REASON_PAGE.encode("utf-8"))
        elif parsed.path == "/cache":
            self._send(200, "text/html; charset=utf-8", CACHE_PAGE.encode("utf-8"))
        elif parsed.path == "/api/rate":
            payload = json.dumps(build_rate(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", payload)
        elif parsed.path == "/api/stats":
            payload = json.dumps(build_stats(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", payload)
        elif parsed.path == "/api/sessions":
            qs = parse_qs(parsed.query)
            date = (qs.get("date") or [""])[0]
            if not date:
                self._send(400, "application/json; charset=utf-8", b'{"error":"date required"}')
                return
            payload = json.dumps(build_sessions_for_date(date), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", payload)
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


def _refresher():
    """后台常驻刷新：增量重算，让请求路径永远读到热缓存。"""
    while True:
        try:
            collect_all(max_stale=0)
        except Exception:
            pass
        time.sleep(REFRESH_INTERVAL)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    threading.Thread(target=_refresher, daemon=True, name="refresher").start()
    threading.Thread(target=quota_worker, daemon=True, name="quota").start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Codex Token 看板 v3.3 已启动: http://127.0.0.1:{port}")
    print("增量解析 · 后台 5s 刷新 · 实时速率 /api/rate · 悬浮窗 /widget")
    server.serve_forever()


if __name__ == "__main__":
    main()
