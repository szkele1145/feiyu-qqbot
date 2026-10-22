"""验证：敏感字段保存 -> 加密落盘 -> 重新加载 -> 解密回明文。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from feiyu_bot.config import Config
from feiyu_bot.secrets import SecretVault

tmp = Path(tempfile.mkdtemp(prefix="feiyu-sec-"))
cfg_path = tmp / "config.yaml"

# 1. 创建并写入密钥
cfg = Config.default()
cfg.set("qq.appid", "102999999")
cfg.set("qq.secret", "SuperSecretValue123")
cfg.set("llm.api_key", "sk-abcdef123456")
vault = SecretVault(tmp / "data")
cfg.save(cfg_path, vault=vault)

raw = cfg_path.read_text(encoding="utf-8")
print("【落盘检查】")
for secret in ("SuperSecretValue123", "sk-abcdef123456"):
    print(f"  明文 {secret[:8]}... 是否出现在磁盘: {secret in raw}")
print(f"  含 ENC: 前缀: {'ENC:' in raw}")

# 2. 不带 vault 加载（模拟 create_bot 的原始路径）
cfg2 = Config.load(cfg_path)
print("\n【不带 vault 加载】")
print(f"  qq.appid  = {cfg2.get('qq.appid')!r}")
print(f"  llm.api_key = {cfg2.get('llm.api_key')!r}")
ok2 = (cfg2.get("qq.appid") == "102999999"
       and cfg2.get("llm.api_key") == "sk-abcdef123456")
print(f"  解密成功: {ok2}")

# 3. 显式传 vault
cfg3 = Config.load(cfg_path, vault=SecretVault(tmp / "data"))
print("\n【显式传 vault】")
ok3 = cfg3.get("qq.secret") == "SuperSecretValue123"
print(f"  qq.secret 解密: {ok3}")

# 4. 掩码显示
masked = cfg3.masked(SecretVault(tmp / "data"))
print(f"  掩码 appid = {masked.get('qq', {}).get('appid')}")
print(f"  掩码 secret = {masked.get('qq', {}).get('secret')}")

print("\n结果:", "全部通过" if (ok2 and ok3) else "失败")
sys.exit(0 if (ok2 and ok3) else 1)
