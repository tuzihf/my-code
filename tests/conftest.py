"""pytest 配置:把项目根目录加进 sys.path,让测试能 import 各模块。"""
import sys
from pathlib import Path

# 把 my-agent 根目录加到 sys.path,这样 tests/ 里的测试能 import tools 等
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
