#!/data/data/com.termux/files/usr/bin/bash
# feiyu-qqbot 安卓一键安装（由 APK 调用）
set -euo pipefail

APP_NAME="feiyu-qqbot"
INSTALL_DIR="$HOME/$APP_NAME"
VERSION="0.1.0"

info() { echo -e "\\033[36m[信息]\\033[0m $*"; }
ok()   { echo -e "\\033[32m[完成]\\033[0m $*"; }
fail() { echo -e "\\033[31m[错误]\\033[0m $*" >&2; exit 1; }

command -v pkg >/dev/null 2>&1 || fail "请在 Termux 中运行"
command -v python >/dev/null 2>&1 || { info "安装 Python…"; pkg install -y python; }

info "安装系统依赖（ffmpeg 用于语音转码）…"
pkg install -y ffmpeg python-pip 2>/dev/null || true
termux-wake-lock 2>/dev/null || true

mkdir -p "$INSTALL_DIR"/{src,assets/voices,assets/images,logs,data}
info "安装目录: $INSTALL_DIR"

if [ ! -d "$INSTALL_DIR/src/feiyu_bot" ]; then
  fail "缺少程序源码。请把源码放到 $INSTALL_DIR/src/feiyu_bot 后重试，或使用 Linux 包内的源码。"
fi

info "创建虚拟环境…"
python -m venv "$INSTALL_DIR/venv" 2>/dev/null || fail "venv 创建失败，请执行 pkg install python"
VENV_PIP="$INSTALL_DIR/venv/bin/pip"

info "安装 Python 依赖（首次较慢，约 3-8 分钟）…"
"$VENV_PIP" install --upgrade pip -q
"$VENV_PIP" install -q aiohttp websockets httpx pillow pyyaml cryptography numpy

cat > "$INSTALL_DIR/run.sh" <<EOF2
#!/data/data/com.termux/files/usr/bin/bash
cd "$INSTALL_DIR"
export PYTHONPATH="$INSTALL_DIR/src:\$PYTHONPATH"
exec "$INSTALL_DIR/venv/bin/python" -m feiyu_bot "\$@"
EOF2
chmod +x "$INSTALL_DIR/run.sh"

if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
  info "生成配置模板…"
  ( cd "$INSTALL_DIR" && PYTHONPATH="$INSTALL_DIR/src" python -m feiyu_bot --init ) || true
fi

ok "安装完成（$VERSION）"
echo
echo "1) 编辑配置填写 QQ appid/secret 与 LLM Key:"
echo "   nano $INSTALL_DIR/config.yaml"
echo "2) 启动机器人:"
echo "   $INSTALL_DIR/run.sh"
echo "3) 打开手机浏览器访问控制面板:"
echo "   http://127.0.0.1:8720"
