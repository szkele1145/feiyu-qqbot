#!/data/data/com.termux/files/usr/bin/bash
# feiyu-qqbot 安卓 (Termux) 一键安装脚本
#
# 前置：在 Termux 中执行 pkg install python ffmpeg
# 用法: bash termux-install.sh
set -euo pipefail

APP_NAME="feiyu-qqbot"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/$APP_NAME"
VENV_DIR="$INSTALL_DIR/venv"

info() { echo -e "\033[36m[信息]\033[0m $*"; }
ok()   { echo -e "\033[32m[完成]\033[0m $*"; }
fail() { echo -e "\033[31m[错误]\033[0m $*" >&2; exit 1; }

command -v python >/dev/null 2>&1 || fail "未找到 python，请先执行: pkg install python"
command -v pkg >/dev/null 2>&1 || fail "请在 Termux 中运行本脚本"

info "安装系统依赖…"
pkg install -y python ffmpeg 2>/dev/null || true

info "创建虚拟环境…"
mkdir -p "$INSTALL_DIR"
python -m venv "$VENV_DIR" 2>/dev/null || fail "venv 创建失败（Termux 需 pkg install python）"

info "安装 Python 依赖…"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -q aiohttp websockets httpx pillow pyyaml cryptography numpy

info "复制程序文件…"
mkdir -p "$INSTALL_DIR/src"
[ -d "$REPO_DIR/src/feiyu_bot" ] && cp -r "$REPO_DIR/src/feiyu_bot" "$INSTALL_DIR/src/"
mkdir -p "$INSTALL_DIR/assets/voices" "$INSTALL_DIR/assets/images" "$INSTALL_DIR/logs" "$INSTALL_DIR/data"

cat > "$INSTALL_DIR/run.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
cd "$INSTALL_DIR"
export PYTHONPATH="$INSTALL_DIR/src:\$PYTHONPATH"
exec "$VENV_DIR/bin/python" -m feiyu_bot "\$@"
EOF
chmod +x "$INSTALL_DIR/run.sh"

if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
  info "生成配置模板…"
  ( cd "$INSTALL_DIR" && PYTHONPATH="$INSTALL_DIR/src" python -m feiyu_bot --init ) || true
fi

# 安卓后台保活：termux-wake-lock + 定时任务
cat > "$INSTALL_DIR/keepalive.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# 15 分钟一次保活检查
while true; do
  sleep 900
  termux-wake-lock 2>/dev/null || true
  cd "$HOME/feiyu-qqbot"
  PYTHONPATH="$HOME/feiyu-qqbot/src" "$HOME/feiyu-qqbot/venv/bin/python" \
    -c "
import asyncio
from feiyu_bot.bot import create_bot
bot = create_bot('$HOME/feiyu-qqbot/config.yaml')
asyncio.run(bot._keepalive_once())
print('保活检查完成')
" 2>&1 | tail -3
done
EOF
chmod +x "$INSTALL_DIR/keepalive.sh"

ok "安装完成！"
echo
echo "启动机器人:  $INSTALL_DIR/run.sh"
echo "后台保活:    $INSTALL_DIR/keepalive.sh &"
echo "定时保活:    (crontab -l 2>/dev/null; echo '*/15 * * * * $INSTALL_DIR/run.sh --keepalive-check') | crontab -"
echo "WebUI:       http://127.0.0.1:8720"
echo
echo "提示: 安卓后台运行请配合 termux: ./termux-wake-lock 防止系统杀后台"
