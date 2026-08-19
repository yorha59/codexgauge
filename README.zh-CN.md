# codexgauge 📊

[English](README.md) | 中文

> 所有 Codex 用量工具都告诉你已经烧了多少。这个告诉你**现在烧多快** —— 以及什么时候该拆会话。

![macOS 13+](https://img.shields.io/badge/macOS-13%2B-000?logo=apple&logoColor=white)
![Python 3.9+ 仅标准库](https://img.shields.io/badge/python-3.9%2B%20stdlib%20only-3776AB?logo=python&logoColor=white)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![零遥测](https://img.shields.io/badge/network-localhost%20only-3fb950)

![demo](docs/demo.gif)

macOS 悬浮仪表盘，专为 [OpenAI Codex](https://developers.openai.com/codex/) 用量设计：**实时消耗速率、缓存 TTL 倒计时、基于你自己会话历史重放的拆分建议** —— 毛玻璃悬浮窗，不打扰，但一直在。

---

## 为什么

额度类组件回答"还剩多少"。长任务跑着的时候，你真正想问的是这三个：

### 🔥 实时烧速

![burn rate](docs/widget-rate.jpg)

- **1m / 5m / 15m 滚动窗口**消耗速度，每 **5 秒**刷新
- **30 分钟 sparkline**（30s 粒度），input / cached 拆分显示
- 活跃会话指示 —— 看清*哪个*会话在烧
- 增量解析只读日志**新增字节**，常驻无感

### ⏳ 缓存还活着吗？

![cache TTL](docs/cache-ttl.jpg)

Prompt cache 对命中输入打 1 折，但前缀 **30 分钟**无命中即老化。codexgauge 单独拆账 cached tokens，并为当前会话计算 **TTL 倒计时**：
> 挂机 20 分钟？悬浮窗告诉你缓存还剩 ~10 分钟 —— 要么赶在过期前回来，要么接受下一轮全量上下文原价重付。

### ✂️ 这个会话该拆吗？

![split advisor](docs/split-reason.jpg)

不拍脑袋 —— **用你自己的历史做重放扫描**：

- 把本地全部会话历史作为时间序列重放
- 模拟"水位 X 处 compact 后继续"，扫多个阈值测净节省
- 按边际收益论证。我的存档（**840 个会话**）：

| 拆分水位 | input 节省 | credits/周 |
|---|---|---|
| 200K | 14.2% | 39.0K |
| **150K** | **30.1%** | **82.6K** |
| 120K | 36.8% | 101.0K |
| 100K | 40.2% | 110.2K |

  从 150K 压到 100K 只多省 ~10pp，打断频率却近乎翻倍 —— **150K 是拐点**。（装到你机器上会按你的会话重算。）
- 计费机制背书：每轮重发全量上下文，水位 × 轮数才是真实账单。我的存档里 Top10 巨型会话占总消耗 **45.5%**（最大 1.23B tokens / 7,838 轮）。

---

## 悬浮窗

![widget in context](docs/widget-context.jpg)

- 无边框 `NSPanel` + `WKWebView`，原生毛玻璃，亮色壁纸自动压暗
- 顶部把手拖拽；其余区域点击全部透传网页 —— 柱状图下钻、按钮都好用
- 右键菜单 · 位置记忆 · 所有空间（含全屏）可见
- `127.0.0.1:8790` 本地服务 —— 不联网、无遥测、不碰任何 API key

## 工作原理

1. **数据源**：`~/.codex/sessions/*.jsonl`（含归档会话）—— Codex CLI 本来就在写的文件，不读别的。
2. **增量解析**：每文件缓存 `(mtime, size, inode, offset)`，只解析上次断点之后的新字节；结尾不完整的行留 buffer 下轮续读。后台线程 5s 刷新，API 请求永远命中热缓存（刷新线程挂了才同步兜底重算）。
3. **事件时间归账**：每条 token 事件归属到*发生*那天 —— 跨午夜的长会话不再把账全记到创建日。同一会话多个 rollout 文件按时间序去重算增量。
4. **重放引擎**：拆分建议重放全部历史，模拟多阈值 compact，测各水位净节省。

附带：

![full dashboard](docs/dashboard.jpg)

- 完整 Web 看板（每日柱状图下钻、会话明细 + 活跃时段）
- CLI 报表：`codex_token_report.py [YYYY-MM|--all]` —— 月度 / 全部历史
- 终端聚合监控：`codex-token-monitor.sh [watch [seconds]]`

## 安装

**前置**：macOS 13+，Python 3.9+（纯标准库，无需 pip），本机有 Codex CLI 会话日志。

```bash
git clone https://github.com/yorha59/codexgauge && cd codexgauge

# 1. 看板服务（Web UI + API）
python3 codex_token_server.py          # → http://127.0.0.1:8790

# 2. 悬浮窗 —— 无需 Xcode 工程
swiftc -O -framework WebKit -framework Cocoa \
      -o codex_widget codex_widget_app.swift
./codex_widget

# 3. （可选）开机自启 —— launchd plist 是模板：
#    把 /path/to/codexgauge 替换成你的 clone 目录，然后：
sed -i '' 's|/path/to/codexgauge|'"$PWD"'|g' com.codexgauge.*.plist
cp com.codexgauge.token-server.plist ~/Library/LaunchAgents/ \
  && launchctl load ~/Library/LaunchAgents/com.codexgauge.token-server.plist
```

> 一键 `install.sh`（含 Homebrew cask）在路线图上。

## 对比

|  | codexgauge | [CodexBar](https://github.com/steipete/CodexBar) | [codeburn](https://github.com/getagentseal/codeburn) |
|---|---|---|---|
| 实时烧速（1m/5m/15m + sparkline） | ✅ | — | — |
| 缓存命中率 | ✅ 实时 | — | ✅ 月度 |
| 缓存 TTL 倒计时 | ✅ | — | — |
| 拆分建议 | ✅ 重放扫描阈值 | — | 规则启发式 |
| 形态 | 悬浮窗 | 菜单栏 | TUI / web / 菜单栏 |
| 覆盖 | Codex | 69 家 | 41 工具 |
| 依赖 | Python stdlib | Swift 原生 | Node 22+ |

定位：codeburn 是月度审计报告，CodexBar 是额度表。**codexgauge 是驾驶舱** —— 盯着*当前这个*会话、*此刻*发生了什么。

## FAQ

**数据从哪来？** 仅本地 Codex 会话日志。服务只绑 127.0.0.1，绝无出站请求。

**会拖慢 Codex 吗？** 不会。会话文件是追加写，解析器只读上次之后的新字节，且不在请求路径上。

**需要 OpenAI 凭据吗？** 不需要。无 API key、无 OAuth、无 cookie。

## 许可证

MIT
