#!/usr/bin/env bash
# 全屏播放 InkTime 展示页，并在播放期间阻止系统空闲息屏/锁屏。
#
# 原理：用 gnome-session-inhibit 注册 idle 抑制，告诉 GNOME「正在播放内容，
# 不要把我算作空闲」。这和视频播放器播放时阻止息屏是同一套系统机制，
# 不修改任何系统设置，脚本退出后抑制自动解除。
#
# 相比在浏览器里用 Screen Wake Lock 的好处：不要求安全上下文，
# 用局域网 IP 访问也有效。
#
# 用法：
#   ./scripts/display_kiosk.sh                       # 默认 127.0.0.1 + .env 里的端口
#   ./scripts/display_kiosk.sh http://127.0.0.1:8888/display
#   BROWSER=google-chrome ./scripts/display_kiosk.sh
#
# 退出：关闭浏览器窗口，或在本终端 Ctrl+C。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# 端口取 .env 的 FLASK_PORT
PORT=8888
if [[ -f .env ]]; then
  p="$(grep -E '^FLASK_PORT=' .env | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
  [[ -n "${p:-}" ]] && PORT="$p"
fi

# 默认走 127.0.0.1：属于安全上下文，浏览器里的 Screen Wake Lock 也能生效，双保险
URL="${1:-http://127.0.0.1:${PORT}/display}"

if ! command -v gnome-session-inhibit >/dev/null; then
  echo "未找到 gnome-session-inhibit（属于 gnome-session-bin 包）。" >&2
  echo "可改用：systemd-inhibit --what=idle --why='InkTime' <浏览器命令>" >&2
  exit 1
fi

# 选浏览器：优先环境变量，其次按可用性挑
pick_browser() {
  if [[ -n "${BROWSER:-}" ]]; then echo "$BROWSER"; return; fi
  for b in google-chrome chromium chromium-browser microsoft-edge firefox; do
    command -v "$b" >/dev/null && { echo "$b"; return; }
  done
  echo ""
}
BROWSER_BIN="$(pick_browser)"
if [[ -z "$BROWSER_BIN" ]]; then
  echo "未找到可用浏览器，请用 BROWSER=<命令> 指定。" >&2
  exit 1
fi

# 全屏参数：Chromium 系用 --kiosk，Firefox 用 --kiosk（较新版本支持）
case "$BROWSER_BIN" in
  firefox*) ARGS=(--kiosk "$URL") ;;
  *)        ARGS=(--kiosk --new-window "$URL") ;;
esac

echo "URL      : $URL"
echo "浏览器   : $BROWSER_BIN ${ARGS[*]}"
echo "空闲抑制 : 播放期间生效，脚本退出后自动解除"
echo "退出方式 : 关闭浏览器窗口，或本终端 Ctrl+C"
echo

exec gnome-session-inhibit \
  --inhibit idle \
  --reason "InkTime 相册展示中" \
  "$BROWSER_BIN" "${ARGS[@]}"
