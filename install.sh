#!/usr/bin/env bash
# feiyu-qqbot Linux / macOS 一键安装脚本
#
# 用法:
#   bash install.sh              # 安装依赖 + 创建 venv + 生成配置
#   bash install.sh --systemd    # 同时注册 systemd 服务
#   bash install.sh --no-venv    # 用系统 Python 直接跑
set -euo pipefail

APP_NAME="feiyu-qqbot"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/$APP_NAME}"
VENV_DIR="$INSTALL_DIR/venv"
USE_SYSTEMD=0
USE_VENV=1

for arg in "$@"; do
  case "$arg" in
    --systemd) USE_SYSTEMD=1 ;;
    --no-venv) USE_VENV=0 ;;
    --help|-h)
      echo "用法: bash install.sh [--systemd] [--no-venv]"
      exit 0 ;;
  esac
done

info()  { echo -e "\033[36m[信息]\033[0m $*"; }
ok()    { echo -e "\033[32m[完成]\033[0m $*"; }
warn()  { echo -e "\033[33m[警告]\033[0m $*"; }
fail()  { echo -e "\033[31m[错误]\033[0m $*" >&2; exit 1; }

# ---- 检测包管理器 ----
detect_pm() {
  if command -v apt-get >/dev/null 2>&1; then echo apt
  elif command -v dnf >/dev/null 2>&1; then echo dnf
  elif command -v yum >/dev/null 2>&1; then echo yum
  elif command -v pacman >/dev/null 2>&1; then echo pacman
  elif command -v apk >/dev/null 2>&1; then echo apk
  elif command -v zypper >/dev/null 2>&1; then echo zypper
  else echo none
  fi
}

install_deps() {
  local pm; pm=$(detect_pm)
  info "检测到包管理器: $pm"
  case "$pm" in
    apt)
      SUDO=sudo; [ "$(id -u)" = "0" ] && SUDO=""
      $SUDO apt-get update -y
      $SUDO apt-get install -y python3 python3-venv python3-pip ffmpeg
      ;;
    dnf)  SUDO=sudo; [ "$(id -u)" = "0" ] && SUDO=""
      $SUDO dnf install -y python3 python3-pip ffmpeg ;;
    yum)  SUDO=sudo; [ "$(id -u)" = "0" ] && SUDO=""
      $SUDO yum install -y python3 python3-pip ffmpeg ;;
    pacman) SUDO=sudo; [ "$(id -u)" = "0" ] && SUDO=""
      $SUDO pacman -S --noconfirm python python-pip ffmpeg ;;
    apk)  SUDO=su -c
      $SUDO "apk add --no-cache python3 py3-pip ffmpeg" ;;
    zypper) SUDO=sudo; [ "$(id -u)" = "0" ] && SUDO=""
      $SUDO zypper install -y python3 python3-pip ffmpeg ;;
    none) warn "未识别的包管理器，跳过系统依赖安装" ;;
  esac
}

# ---- 安装 ----
info "安装目录: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

if [ "$USE_VENV" = "1" ]; then
  command -v python3 >/dev/null 2>&1 || fail "未找到 python3，请先安装 Python 3.10+"
  PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  info "Python 版本: $PYVER"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
    || fail "需要 Python 3.10 及以上"
  info "创建虚拟环境…"
  python3 -m venv "$VENV_DIR" 2>/dev/null || python3 -m venv --without-pip "$VENV_DIR"
  PIP="$VENV_DIR/bin/pip"
  [ -x "$PIP" ] || PIP="python3 -m pip"
  info "安装 Python 依赖…"
  $PIP install --upgrade pip -q || true
  $PIP install -q aiohttp websockets httpx pillow pyyaml cryptography numpy || \
    $PIP install -q -r "$REPO_DIR/requirements.txt"
  ok "虚拟环境就绪: $VENV_DIR"
fi

# ---- 复制程序 ----
info "复制程序文件…"
mkdir -p "$INSTALL_DIR/src"
if [ -d "$REPO_DIR/src/feiyu_bot" ]; then
  cp -r "$REPO_DIR/src/feiyu_bot" "$INSTALL_DIR/src/"
fi
mkdir -p "$INSTALL_DIR/assets/voices" "$INSTALL_DIR/assets/images" "$INSTALL_DIR/logs" "$INSTALL_DIR/data"

# ---- 生成启动脚本 ----
cat > "$INSTALL_DIR/run.sh" <<EOF
#!/usr/bin/env bash
cd "$INSTALL_DIR"
export PYTHONPATH="$INSTALL_DIR/src:\$PYTHONPATH"
exec "$VENV_DIR/bin/python" -m feiyu_bot "\$@"
EOF
chmod +x "$INSTALL_DIR/run.sh"
ok "启动脚本: $INSTALL_DIR/run.sh"

# ---- 配置 ----
if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
  info "生成配置模板…"
  ( cd "$INSTALL_DIR" && PYTHONPATH="$INSTALL_DIR/src" python3 -m feiyu_bot --init ) || true
  warn "请编辑 $INSTALL_DIR/config.yaml 填写 QQ appid/secret 与 LLM API Key"
fi

# ---- systemd ----
if [ "$USE_SYSTEMD" = "1" ]; then
  if command -v systemctl >/dev/null 2>&1; then
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/$APP_NAME.service" <<EOF
[Unit]
Description=$APP_NAME - QQ 官方机器人 AI Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/run.sh
Restart=always
RestartSec=30
StandardOutput=append:$INSTALL_DIR/logs/stdout.log
StandardError=append:$INSTALL_DIR/logs/stderr.log

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable "$APP_NAME.service"
    ok "已注册 systemd 用户服务，执行以下命令启动:"
    echo "  systemctl --user start $APP_NAME"
    echo "  systemctl --user status $APP_NAME"
    echo "  journalctl --user -u $APP_NAME -f"
  else
    warn "未检测到 systemd，跳过服务注册"
  fi
fi

ok "安装完成！"
echo
echo "启动: $INSTALL_DIR/run.sh"
echo "WebUI: http://127.0.0.1:8720"
