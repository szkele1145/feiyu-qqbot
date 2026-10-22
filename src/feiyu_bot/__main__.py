"""命令行入口。

用法::

    feiyu-qqbot                    # 用默认配置启动（双面板）
    feiyu-qqbot --config a.yaml    # 指定配置文件
    feiyu-qqbot --init             # 生成配置模板并退出
    feiyu-qqbot --webui            # 只开 WebUI
    feiyu-qqbot --no-panel         # 不开内置面板
    feiyu-qqbot --port 9000        # 指定 WebUI 端口
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import webbrowser
from pathlib import Path

from .bot import create_bot
from .config import Config, load_config
from .logging_setup import get_logger, setup_logging
from .panel import ConsolePanel
from .secrets import SecretVault
from .webui import WebUIController


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="feiyu-qqbot", description="QQ 官方机器人 AI Agent"
    )
    parser.add_argument("--config", "-c", default=None, help="配置文件路径")
    parser.add_argument("--init", action="store_true", help="生成配置模板后退出")
    parser.add_argument("--webui-only", action="store_true", help="只启动 WebUI，不连 QQ")
    parser.add_argument("--no-panel", action="store_true", help="不启动内置控制台面板")
    parser.add_argument("--no-webui", action="store_true", help="不启动 WebUI")
    parser.add_argument("--host", default=None, help="WebUI 监听地址")
    parser.add_argument("--port", type=int, default=None, help="WebUI 监听端口")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--log-level", default=None, help="日志级别")
    parser.add_argument("--stop", action="store_true", help="尝试停止正在运行的实例后退出")
    return parser.parse_args(argv)


def _stop_running_instance() -> int:
    """停止正在运行的实例（供卸载程序调用）。

    Windows 用 taskkill；其他平台按 pid 文件终止。
    """
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    pid_file = Path("data") / "feiyu.pid"
    if sys.platform.startswith("win"):
        subprocess.run(
            ["taskkill", "/F", "/IM", "feiyu-qqbot.exe"],
            capture_output=True, check=False,
        )
    elif pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
            print(f"已请求停止进程 {pid}")
        except (OSError, ValueError) as exc:
            print(f"停止失败: {exc}")
    return 0


def _write_pid_file() -> None:
    """记录当前进程 pid，供停止脚本使用。"""
    try:
        pid_file = Path("data") / "feiyu.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def _init_config_template(path: Path) -> None:
    """生成配置模板（含说明注释）。"""
    from .config import DEFAULT_CONFIG  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    if path.exists():
        print(f"配置已存在，未覆盖: {path}")
        return
    data = dict(DEFAULT_CONFIG)
    # 敏感项用环境变量占位，避免明文落盘
    data["qq"]["appid"] = "${QQ_APPID}"
    data["qq"]["secret"] = "${QQ_SECRET}"
    data["llm"]["api_key"] = "${LLM_API_KEY}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    print(f"已生成配置模板: {path}")
    print("请编辑其中的 appid / secret / api_key，或设置同名环境变量。")


async def _run(args: argparse.Namespace) -> int:
    if args.stop:
        return _stop_running_instance()

    # ---- 配置 ----
    config_path = Path(args.config) if args.config else Path("config.yaml")
    if args.init:
        _init_config_template(config_path)
        return 0

    cfg: Config = load_config(config_path)
    setup_logging(
        args.log_level or str(cfg.get("app.log_level", "INFO")),
        cfg.log_dir,
    )
    log = get_logger("feiyu.main")

    cfg.expand_env()

    # ---- 密钥保险箱 ----
    vault = SecretVault(cfg.data_dir)

    bot = create_bot(config_path)
    bot.vault = vault
    _write_pid_file()

    # 未配置凭证时给出明确提示
    if not cfg.get("qq.appid") or not cfg.get("qq.secret"):
        log.warning("=" * 56)
        log.warning("QQ appid/secret 未配置，机器人将无法连接 QQ。")
        log.warning("请编辑 %s 或设置环境变量 QQ_APPID / QQ_SECRET", config_path)
        log.warning("=" * 56)

    # ---- 面板 ----
    panel = None
    if not args.no_panel and cfg.get("panel.enabled", True) and not args.webui_only:
        panel = ConsolePanel(bot)
        panel.start_background()

    webui = None
    if not args.no_webui and cfg.get("webui.enabled", True):
        webui = WebUIController(bot)
        bot.webui = webui
        host = args.host or str(cfg.get("webui.host", "127.0.0.1"))
        port = int(args.port or cfg.get("webui.port", 8720))
        token = str(cfg.get("webui.token", "") or "")
        webui.serve_background(host, port, token)
        url = f"http://{host}:{port}" + (f"?token={token}" if token else "")
        log.info("WebUI 地址: %s", url)
        if args.open or cfg.get("webui.open_browser", False):
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass

    # ---- 运行 ----
    try:
        if args.webui_only:
            log.info("仅 WebUI 模式，按 Ctrl+C 退出")
            while True:
                await asyncio.sleep(3600)
        else:
            await bot.run()
    except KeyboardInterrupt:
        log.info("收到中断，正在退出…")
    finally:
        await bot.stop()
        if webui:
            webui.stop()
        if panel:
            panel.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001
        print(f"启动失败: {exc}", file=sys.stderr)
        import traceback  # noqa: PLC0415

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
