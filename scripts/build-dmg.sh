#!/bin/bash
# 构建 CodexGauge.app + DMG (本地与 GitHub Actions 共用)
# 用法: ./scripts/build-dmg.sh [输出目录]   产物: CodexGauge-<version>-arm64.dmg / -x64.dmg
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-dist}"
VER="$(/usr/bin/python3 -c "import plistlib; print(plistlib.load(open('scripts/Info.plist','rb'))['CFBundleShortVersionString'])" 2>/dev/null || echo 1.0.0)"
ARCH="$(uname -m)"

echo "== build CodexGauge.app ($ARCH, v$VER) =="
rm -rf build/CodexGauge.app
mkdir -p build/CodexGauge.app/Contents/{MacOS,Resources}

# 1. 编译 widget (服务端为 python 脚本, 打进 Resources)
swiftc -O codex_widget_app.swift -o build/CodexGauge.app/Contents/MacOS/CodexGauge \
    -framework WebKit -framework Cocoa

# 2. 资源: server + 脚本 + launchd plists (安装时由 install.sh 落位)
cp codex_token_server.py codex_token_report.py codex_token_dashboard.py codex-token-monitor.sh \
   com.codexgauge.widget.plist com.codexgauge.token-server.plist build/CodexGauge.app/Contents/Resources/
cp scripts/Info.plist build/CodexGauge.app/Contents/Info.plist
cp scripts/install.sh build/CodexGauge.app/Contents/Resources/install.sh
chmod +x build/CodexGauge.app/Contents/Resources/*.sh

# 3. 签名 (adhoc: 本地分发; CI 可注入签名身份)
if [ -n "${MACOS_SIGN_IDENTITY:-}" ]; then
    codesign --force --deep --sign "$MACOS_SIGN_IDENTITY" build/CodexGauge.app
else
    codesign --force --deep -s - build/CodexGauge.app
fi

# 4. DMG (hdiutil, 不依赖 create-dmg)
mkdir -p "$OUT"
DMG="$OUT/CodexGauge-$VER-$ARCH.dmg"
rm -f "$DMG"
hdiutil create -volname CodexGauge -srcfolder build/CodexGauge.app -ov -format UDZO "$DMG"
echo "== done: $DMG =="
