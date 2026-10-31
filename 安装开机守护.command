#!/usr/bin/env bash
# 安装为 macOS 开机自启服务(LaunchAgent): 登录后自动启动, 崩溃自动重启
#
# 原理: ~/Desktop 受 macOS 隐私(TCC)保护, launchd 后台进程无权读取, 会报
#       "Operation not permitted"。因此桌面项目目录作为【源码】, 本脚本把运行时
#       部署到非保护目录 ~/Library/Application Support/hf-model-downloader 运行。
#       修改桌面代码后, 重新双击本脚本即可同步并重启服务。
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
SRC="$(cd "$(dirname "$0")" && pwd)"
RT="$HOME/Library/Application Support/hf-model-downloader"
LABEL="com.hetao.hf-model-downloader"
UID_NUM="$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "[1/4] 部署运行时到: $RT"
mkdir -p "$RT"
for f in progress_server.py dashboard.html hf_download.sh hfd.sh localhost.crt localhost.key manifest.webmanifest; do
  [ -f "$SRC/$f" ] && cp -f "$SRC/$f" "$RT/$f"
done
# PWA 图标等静态资源
if [ -d "$SRC/assets" ]; then
  mkdir -p "$RT/assets"
  cp -f "$SRC/assets/"* "$RT/assets/" 2>/dev/null || true
fi
chmod +x "$RT/hf_download.sh" "$RT/hfd.sh" 2>/dev/null || true
# 首次部署时把当前任务配置带过去; 之后运行时维护自己的配置, 不覆盖
[ -f "$RT/dl_config.json" ] || { [ -f "$SRC/dl_config.json" ] && cp -f "$SRC/dl_config.json" "$RT/dl_config.json" || true; }

echo "[2/4] 生成 LaunchAgent: $PLIST"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$RT/progress_server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$RT</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$RT/server.log</string>
  <key>StandardErrorPath</key><string>$RT/server.log</string>
</dict>
</plist>
PLIST

echo "[3/4] 停止旧实例(仅 8765 监听进程, 不影响 aria2 下载)"
OLD="$(lsof -nP -iTCP:8765 -sTCP:LISTEN -t 2>/dev/null || true)"
[ -n "$OLD" ] && kill -9 $OLD 2>/dev/null || true
sleep 1
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo "[4/4] 等待启动..."
sleep 2
if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✓ 已安装并启动: 开机自启 + 崩溃自动重启 -> http://localhost:8765"
  echo "  运行时目录: $RT"
else
  echo "! 服务未监听, 查看日志: $RT/server.log"
fi
