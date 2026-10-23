"""验证 /api/selfcheck 自检接口。"""
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PORT = 8901
BASE = f"http://127.0.0.1:{PORT}"


def call(path, timeout=20):
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:  # noqa: BLE001
            return e.code, {}


def main() -> int:
    import os

    from feiyu_bot.config import Config
    from feiyu_bot.webui.server import WebUIController

    results = []

    def check(name, cond, detail=""):
        results.append((name, cond))
        print(f"  {'OK  ' if cond else 'FAIL'} {name} {detail}")

    tmp = Path(tempfile.mkdtemp(prefix="feiyu-sc-"))
    cfg = Config.default()
    cfg.set("app.base_dir", str(tmp))
    cfg.set("webui.port", PORT)
    cfg.set("webui.host", "127.0.0.1")
    cfg.save(tmp / "config.yaml")
    cfg = Config.load(tmp / "config.yaml")

    class FakeBot:
        def __init__(self, cfg):
            self.cfg = cfg
            self.media = None
            self.vault = None

        def status(self):
            return {"running": False, "gateway": {"connected": False}}

    bot = FakeBot(cfg)

    # 目录用绝对路径，避免受 CWD 影响
    cfg.set("media.image_dir", str(tmp / "images"))
    cfg.set("media.voice_dir", str(tmp / "voices"))
    cfg.set("app.log_dir", str(tmp / "logs"))

    ctl = WebUIController(bot)
    ctl.serve_background("127.0.0.1", PORT, "")
    time.sleep(1.0)

    print("[自检接口]")
    st, d = call("/api/selfcheck")
    check("GET /api/selfcheck", st == 200 and "checks" in d)
    checks = d.get("checks") or []
    check("返回若干检查项", len(checks) >= 8, f"-> {len(checks)} 项")
    check("有 passed/total 汇总", "passed" in d and "total" in d,
          f"-> {d.get('passed')}/{d.get('total')}")

    by_key = {c["key"]: c for c in checks}
    # 空配置：QQ 凭证应判为未通过并给修复建议
    check("QQ appid 未填 -> 不通过", by_key.get("qq.appid", {}).get("ok") is False)
    check("QQ appid 有修复建议", bool(by_key.get("qq.appid", {}).get("fix")))

    # 默认配置里 base_url 一般是预填的，先清空再验证「未填」路径
    cfg.set("llm.base_url", "")
    st, d2 = call("/api/selfcheck")
    by_key2 = {c["key"]: c for c in (d2.get("checks") or [])}
    check("LLM base_url 未填 -> 不通过", by_key2.get("llm.base_url", {}).get("ok") is False)
    cfg.set("llm.base_url", "https://api.deepseek.com/v1")

    # 填好关键项后应转为通过
    cfg.set("qq.appid", "1234567890")
    cfg.set("qq.secret", "abcdefghijklmnop")
    cfg.set("llm.base_url", "https://api.deepseek.com/v1")
    cfg.set("llm.model", "deepseek-chat")
    cfg.set("llm.api_key", "sk-1234567890abcdef")
    st, d = call("/api/selfcheck")
    by_key = {c["key"]: c for c in (d.get("checks") or [])}
    check("填好后 QQ appid 通过", by_key.get("qq.appid", {}).get("ok") is True)
    check("AppID 已脱敏", "*" in str(by_key.get("qq.appid", {}).get("detail", "")),
          f"-> {by_key.get('qq.appid', {}).get('detail')}")
    check("API Key 已脱敏", "*" in str(by_key.get("llm.api_key", {}).get("detail", "")))
    check("base_url 未脱敏（便于核对）",
          by_key.get("llm.base_url", {}).get("detail") == "https://api.deepseek.com/v1")

    # 非法 base_url
    cfg.set("llm.base_url", "api.deepseek.com")
    st, d = call("/api/selfcheck")
    by_key = {c["key"]: c for c in (d.get("checks") or [])}
    check("非法 base_url 被抓到", by_key.get("llm.base_url", {}).get("ok") is False,
          f"-> {by_key.get('llm.base_url', {}).get('detail')}")

    # 密文未解密
    cfg.set("qq.secret", "ENC:gAAAAABfake")
    st, d = call("/api/selfcheck")
    by_key = {c["key"]: c for c in (d.get("checks") or [])}
    check("密文未解密被抓到", by_key.get("qq.secret", {}).get("ok") is False,
          f"-> {by_key.get('qq.secret', {}).get('detail')}")

    ctl.stop()

    ok = sum(1 for _, c in results if c)
    print(f"\n结果: {ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
