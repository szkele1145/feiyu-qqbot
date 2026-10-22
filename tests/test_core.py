"""核心单元测试。

运行::

    python -m pytest tests/ -v
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from feiyu_bot.agent.context import (  # noqa: E402
    ContextManager,
    estimate_tokens,
)
from feiyu_bot.config import Config  # noqa: E402
from feiyu_bot.llm.budget import BudgetGuard  # noqa: E402
from feiyu_bot.personas import PersonaManager  # noqa: E402
from feiyu_bot.qq.constants import (  # noqa: E402
    compute_intents,
    infer_file_type,
)
from feiyu_bot.qq.parser import parse_event, strip_tags  # noqa: E402
from feiyu_bot.qq.reply_pool import ReplyPool  # noqa: E402
from feiyu_bot.runtime_guard import RuntimeGuard  # noqa: E402
from feiyu_bot.secrets import SecretVault  # noqa: E402


# ---------------------------------------------------------------- 配置


def test_config_defaults_merged():
    cfg = Config.default()
    assert cfg.get("qq.sandbox") is True
    assert cfg.get("scheduler.keepalive_seconds") == 900
    assert cfg.get("runtime.offpeak_only") is False


def test_config_set_dotted():
    cfg = Config.default()
    cfg.set("llm.model", "deepseek-reasoner")
    assert cfg.get("llm.model") == "deepseek-reasoner"
    cfg.set("a.b.c", 1)
    assert cfg.get("a.b.c") == 1


def test_config_save_encrypts(tmp_path):
    vault = SecretVault(tmp_path / "data")
    cfg = Config.default()
    cfg.set("llm.api_key", "sk-secret-value")
    cfg.set("qq.appid", "123456789")
    path = cfg.save(tmp_path / "config.yaml", vault=vault)

    raw = path.read_text(encoding="utf-8")
    assert "sk-secret-value" not in raw
    assert "123456789" not in raw
    assert "ENC:" in raw


def test_config_load_decrypts(tmp_path):
    vault = SecretVault(tmp_path / "data")
    cfg = Config.default()
    cfg.set("llm.api_key", "sk-roundtrip")
    cfg.save(tmp_path / "config.yaml", vault=vault)

    loaded = Config.load(tmp_path / "config.yaml", vault=vault)
    assert loaded.get("llm.api_key") == "sk-roundtrip"


# ---------------------------------------------------------------- 密钥


def test_vault_encrypt_decrypt(tmp_path):
    vault = SecretVault(tmp_path)
    ct = vault.encrypt("hello-key")
    assert ct.startswith("ENC:")
    assert "hello-key" not in ct
    assert vault.decrypt(ct) == "hello-key"


def test_vault_masked_hides_value(tmp_path):
    vault = SecretVault(tmp_path)
    masked = vault.masked_config({"llm": {"api_key": "sk-abcdefghijkl"}})
    assert masked["llm"]["api_key"] != "sk-abcdefghijkl"


# ---------------------------------------------------------------- QQ 协议


def test_intents_values():
    # 官方位值
    assert compute_intents(["GROUP_AND_C2C_EVENT"]) == 1 << 25
    assert compute_intents(["PUBLIC_GUILD_MESSAGES"]) == 1 << 30
    combined = compute_intents(["GROUP_AND_C2C_EVENT", "PUBLIC_GUILD_MESSAGES"])
    assert combined == (1 << 25) | (1 << 30)


def test_infer_file_type():
    assert infer_file_type("a.png") == 1
    assert infer_file_type("a.mp4") == 2
    assert infer_file_type("a.mp3") == 3
    assert infer_file_type("a.zip") == 4


def test_parse_group_event():
    d = {
        "id": "MID1",
        "group_openid": "GOP",
        "content": "<@!bot> 你好",
        "author": {"user_openid": "UOP", "username": "鱼"},
        "timestamp": "123",
    }
    msg = parse_event("GROUP_AT_MESSAGE_CREATE", d)
    assert msg is not None
    assert msg.kind == "group"
    assert msg.target == "group"
    assert msg.openid == "GOP"
    assert msg.user_openid == "UOP"
    assert msg.content == "你好"
    assert msg.msg_id == "MID1"
    assert msg.session_key == "group:GOP"


def test_parse_c2c_event():
    d = {"id": "MID2", "content": "在吗", "author": {"user_openid": "UOP2"}}
    msg = parse_event("C2C_MESSAGE_CREATE", d)
    assert msg is not None
    assert msg.kind == "c2c"
    assert msg.target == "user"
    assert msg.session_key == "c2c:UOP2"


def test_strip_tags():
    assert strip_tags("<@!123> hi <#456>") == "hi"
    assert strip_tags("<emoji:123>") == ""


def test_reply_pool_seq_increments():
    pool = ReplyPool({"max_uses_per_msg": 5})
    pool.add("group", "GOP", "MID1")
    seqs = [pool.acquire("group", "GOP") for _ in range(6)]
    assert seqs[0] == ("MID1", 1)
    assert seqs[4] == ("MID1", 5)
    assert seqs[5] is None  # 超过官方 5 条上限


def test_reply_pool_expires():
    pool = ReplyPool({"context_ttl": 0.0})
    pool.add("group", "GOP", "MID1")
    assert pool.acquire("group", "GOP") is None


# ---------------------------------------------------------------- 人格


def test_persona_packs_loaded():
    pm = PersonaManager()
    ids = {p["id"] for p in pm.list()}
    assert "feiyu" in ids
    assert len(ids) >= 4


def test_persona_prompt_contains_safety():
    pm = PersonaManager()
    p = pm.get("feiyu")
    assert p is not None
    prompt = p.build_system_prompt()
    assert "安全约束" in prompt
    assert "大肥鱼" in prompt


# ---------------------------------------------------------------- 预算 / 时段


def test_budget_daily_limit(tmp_path):
    g = BudgetGuard({"daily_call_limit": 2}, state_path=tmp_path / "b.json")
    assert g.can_call()[0] is True
    g.record_call()
    g.record_call()
    assert g.can_call()[0] is False


def test_budget_token_ceiling():
    g = BudgetGuard({"token_ceiling": 100})
    assert g.clamp_tokens(5000) == 100
    assert g.clamp_tokens(50) == 50


def test_budget_allowed_hours():
    g = BudgetGuard({"allowed_hours": [["12:00", "14:00"]]})
    assert g.in_service_window(datetime(2024, 1, 1, 13, 0)) is True
    assert g.in_service_window(datetime(2024, 1, 1, 15, 0)) is False


def test_budget_cross_midnight():
    g = BudgetGuard({"allowed_hours": [["18:00", "08:00"]]})
    assert g.in_service_window(datetime(2024, 1, 1, 20, 0)) is True
    assert g.in_service_window(datetime(2024, 1, 1, 3, 0)) is True
    assert g.in_service_window(datetime(2024, 1, 1, 12, 0)) is False


# ---------------------------------------------------------------- 谷时守卫


def test_runtime_guard_offpeak_only():
    g = RuntimeGuard({
        "offpeak_only": True,
        "offpeak_hours": [["12:00", "14:00"]],
        "offpeak_behavior": "silent",
    })
    assert g.should_handle(datetime(2024, 1, 1, 13, 0)) is True
    assert g.should_handle(datetime(2024, 1, 1, 15, 0)) is False


def test_runtime_guard_disabled_allows_all():
    g = RuntimeGuard({"offpeak_only": False})
    assert g.should_handle(datetime(2024, 1, 1, 3, 0)) is True


# ---------------------------------------------------------------- 上下文


def test_estimate_tokens_chinese():
    assert estimate_tokens("你好世界") > 0
    assert estimate_tokens("") == 0
    # 中文比英文更"贵"
    assert estimate_tokens("中文字符测试内容") >= estimate_tokens("abcdefgh")


def test_context_trims_history():
    mgr = ContextManager({"max_history": 5, "max_tokens": 100000, "keep_recent": 2})
    conv = mgr.get("s1", "sys")
    from feiyu_bot.llm import ChatMessage

    for i in range(20):
        conv.add(ChatMessage(role="user", content=f"消息 {i}"))
    import asyncio

    asyncio.run(mgr.compress_if_needed(conv))
    assert len(conv.messages) <= 5


def test_context_summary_created():
    mgr = ContextManager({"max_history": 4, "max_tokens": 50, "keep_recent": 1,
                          "summarize_ratio": 0.5})
    conv = mgr.get("s1", "sys")
    from feiyu_bot.llm import ChatMessage

    for i in range(10):
        conv.add(ChatMessage(role="user", content=f"这是一条比较长的消息内容 {i} " * 5))
    import asyncio

    asyncio.run(mgr.compress_if_needed(conv))
    assert conv.summary or len(conv.messages) <= 4


def test_context_trim_image_text():
    mgr = ContextManager({"max_image_desc_chars": 50})
    assert len(mgr.trim_image_text("x" * 500)) <= 70


# ---------------------------------------------------------------- 识图


def test_vision_pipeline_analyze_generates_report():
    from feiyu_bot.vision import VisionPipeline

    # 造一张纯红小图
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buf, format="PNG")
    data = buf.getvalue()

    pipe = VisionPipeline({"enabled": True, "use_local_ocr": False})
    report = pipe.analyze(data)
    assert report.ok is True
    assert report.width == 64 and report.height == 64
    text = report.to_prompt_text()
    assert "图片本地分析报告" in text
    assert "红色" in text or "颜色" in text


def test_vision_report_on_bad_data():
    from feiyu_bot.vision import VisionPipeline

    pipe = VisionPipeline({"enabled": True, "use_local_ocr": False})
    report = pipe.analyze(b"not-an-image")
    assert report.ok is False
    assert "失败" in report.to_prompt_text()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
