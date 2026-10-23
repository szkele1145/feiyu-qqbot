"""验证聊天内置指令（帮助 / 重置记忆 / 人格 / 状态 / 权限）。

不连 QQ，用假的 sender 与最小 Agent 依赖。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from feiyu_bot.agent.core import Agent  # noqa: E402
from feiyu_bot.config import Config  # noqa: E402
from feiyu_bot.qq.parser import ParsedMessage  # noqa: E402


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send_text(self, target, text, msg_id=""):
        self.sent.append((target, text, msg_id))


class FakeCtx:
    def __init__(self):
        self.dropped = []
        self.got = []

    def get(self, key, system=""):
        self.got.append(key)
        return object()

    def drop(self, key):
        self.dropped.append(key)

    def clear_all(self):
        pass


class FakePersonas:
    def list(self):
        return [
            {"id": "feiyu", "name": "大肥鱼"},
            {"id": "default", "name": "默认助手"},
        ]

    def get(self, pid):
        return type("P", (), {"id": pid, "name": pid})()

    def default(self):
        return self.get("feiyu")


class FakeRuntime:
    behavior = "silent"

    def should_handle(self):
        return True


def make_agent(cfg, sender, ctx):
    agent = Agent.__new__(Agent)
    agent.cfg = cfg
    agent.sender = sender
    agent.ctx = ctx
    agent.personas = FakePersonas()
    agent.runtime = FakeRuntime()
    agent.current_persona_id = "feiyu"
    from feiyu_bot.agent.core import AgentStats

    agent.stats = AgentStats()
    return agent


def msg(text, kind="c2c", user="u1", openid="o1"):
    m = ParsedMessage(kind=kind, content=text, msg_id="m1", openid=openid,
                      user_openid=user, user_id=user)
    return m


def run(agent, m):
    return asyncio.run(agent._maybe_handle_command(m))


def main() -> int:
    results = []

    def check(name, cond, detail=""):
        results.append((name, cond))
        print(f"  {'OK  ' if cond else 'FAIL'} {name} {detail}")

    tmp = Path(tempfile.mkdtemp(prefix="feiyu-cmd-"))
    cfg = Config.default()
    cfg.set("app.base_dir", str(tmp))
    cfg.save(tmp / "config.yaml")
    cfg = Config.load(tmp / "config.yaml")

    sender, ctx = FakeSender(), FakeCtx()
    agent = make_agent(cfg, sender, ctx)

    # 1) 帮助
    check("识别「帮助」", run(agent, msg("帮助")))
    check("帮助里有指令清单", "重置记忆" in sender.sent[-1][1], sender.sent[-1][1][:30])

    # 2) 重置记忆
    check("识别「重置记忆」", run(agent, msg("重置记忆")))
    check("调用了 ctx.drop", ctx.dropped == ["c2c:u1"], f"-> {ctx.dropped}")

    # 3) 人格列表
    check("识别「人格列表」", run(agent, msg("人格列表")))
    check("列出两套人格", "大肥鱼" in sender.sent[-1][1] and "默认助手" in sender.sent[-1][1])

    # 4) 状态
    check("识别「状态」", run(agent, msg("状态")))
    check("状态含用量", "今日" in sender.sent[-1][1])

    # 5) 私聊可切换人格
    check("私聊切换人格", run(agent, msg("切换人格 default", kind="c2c")))
    check("人格已切换", agent.current_persona_id == "default",
          f"-> {agent.current_persona_id}")

    # 6) 群里非管理员不能切换
    agent.current_persona_id = "feiyu"
    check("群聊切换人格被拦", run(agent, msg("切换人格 default", kind="group")))
    check("人格未被改", agent.current_persona_id == "feiyu")
    check("给出权限提示", "管理员" in sender.sent[-1][1])

    # 7) 配置管理员后群聊也能切
    cfg.set("qq.admin_users", ["boss"])
    agent2 = make_agent(cfg, sender, ctx)
    check("管理员群聊可切", run(agent2, msg("切换人格 default", kind="group", user="boss")))
    check("管理员切换生效", agent2.current_persona_id == "default")

    # 8) 普通对话不应被当成指令
    check("普通消息不拦截", not run(agent, msg("今天天气怎么样")))
    check("带 @ 前缀的指令仍识别", run(agent, msg("@机器人 帮助")))

    # 9) 关闭指令开关
    cfg.set("qq.commands_enabled", False)
    agent3 = make_agent(cfg, sender, ctx)
    check("开关关闭后不拦截", not run(agent3, msg("帮助")))

    ok = sum(1 for _, c in results if c)
    print(f"\n结果: {ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
