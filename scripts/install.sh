#!/bin/bash
# CodexGauge 安装脚本 (DMG 内: 将 CodexGauge.app 拖入 /Applications 后执行)
#   /Applications/CodexGauge.app/Contents/Resources/install.sh
# 功能: 装 python server(8790) + launchd 常驻(widget+server) + 首次拉起
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"   # .app 根
RES="$APP_DIR/Contents/Resources"
BIN="$HOME/.codexgauge"
mkdir -p "$BIN"

# 1. 服务端脚本落位
cp "$RES/codex_token_server.py" "$BIN/"

# 2. python3 探测: 系统 python3 缺 stdlib 或过老时退 homebrew
PY="$(command -v python3)"
if ! "$PY" -c "import http.server, json" 2>/dev/null; then
  PY="/opt/homebrew/bin/python3"
fi

# 3. launchd plists (指向安装位)
cat > "$HOME/Library/LaunchAgents/com.codexgauge.token-server.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.codexgauge.token-server</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$BIN/codex_token_server.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/codexgauge-server.log</string>
  <key>StandardErrorPath</key><string>/tmp/codexgauge-server.log</string>
</dict></plist>
PLIST
cat > "$HOME/Library/LaunchAgents/com.codexgauge.widget.plist" <<PLIST2
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.codexgauge.widget</string>
  <key>ProgramArguments</key><array>
    <string>$APP_DIR/Contents/MacOS/CodexGauge</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
PLIST2

# 4. 拉起 (bootout 旧实例幂等)
launchctl bootout gui/$(id -u)/com.codexgauge.token-server 2>/dev/null || true
launchctl bootout gui/$(id -u)/com.codexgauge.widget 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.codexgauge.token-server.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.codexgauge.widget.plist"

sleep 2
echo "✅ CodexGauge 已安装: server :8790 + 悬浮窗 (开机自启)"
echo "   卸载: launchctl bootout gui/$(id -u)/com.codexgauge.{widget,token-server} && rm -rf $BIN ~/Library/LaunchAgents/com.codexgauge.*.plist"
