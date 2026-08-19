#!/bin/bash
# Codex Token Monitor - 跨账号聚合本地监控工具
# 用法: ./codex-token-monitor.sh          # 单次输出
#       ./codex-token-monitor.sh watch    # 持续监控(每5分钟刷新)
#       ./codex-token-monitor.sh watch 60 # 持续监控(每60秒刷新)

# 脚本旁边的 dashboard 脚本(纯 stdlib, 任意 python3 可跑)
DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$DIR/codex_token_dashboard.py"
PY="$(command -v python3)"

case "${1:-}" in
  watch)
    INTERVAL="${2:-300}"
    exec "$PY" "$SCRIPT" watch "$INTERVAL"
    ;;
  *)
    exec "$PY" "$SCRIPT"
    ;;
esac
