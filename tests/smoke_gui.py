"""GUI 冒烟测试：开窗口 3 秒后自动关闭。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from feiyu_bot.panel.gui import ControlPanelGUI  # noqa: E402

g = ControlPanelGUI(config_path=Path(__file__).resolve().parents[1] / "config.yaml")
g.root.after(3000, g.root.destroy)
g.run()
print("GUI smoke ok")
