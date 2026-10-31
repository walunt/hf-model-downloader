#!/usr/bin/env bash
# 卸载开机自启服务(LaunchAgent); 卸载后可用「启动下载器.command」手动启动
set -euo pipefail
LABEL="com.hetao.hf-model-downloader"
UID_NUM="$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "✓ 已卸载开机自启服务: $LABEL"
