#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
INSTALL_DIR="$HOME/feiyu-qqbot"
termux-wake-lock 2>/dev/null || true
if [ ! -x "$INSTALL_DIR/run.sh" ]; then
  echo "尚未安装，请先运行安装脚本。"
  exit 1
fi
cd "$INSTALL_DIR"
exec "$INSTALL_DIR/run.sh" "$@"
