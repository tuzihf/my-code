"""pytest 配置:sys.path + 沙箱兼容的临时目录处理。

背景:DSH 的 workspace-write 沙箱有两个限制——
1) 不允许写系统临时目录,导致 pytest 默认的 tmp_path 报 PermissionError;
2) Path.mkdir(mode=0o700) 创建的目录无法 os.scandir(WinError 5),
   而 pytest 的 tmp_path 内部恰好都用 mode=0o700 建目录。

因此这里做两件事:
- monkeypatch Path.mkdir,忽略 mode 参数(用默认权限);
- 用 --basetemp 把临时目录基目录指定到项目内的可写目录。
"""
import sys
import pathlib
from pathlib import Path

# 把 my-agent 根目录加到 sys.path,这样 tests/ 里的测试能 import tools 等
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 忽略 Path.mkdir 的 mode 参数(沙箱下 0o700 会导致目录不可 scandir)
_orig_mkdir = pathlib.Path.mkdir


def _sandbox_safe_mkdir(self, mode=0o777, parents=False, exist_ok=False):
    return _orig_mkdir(self, parents=parents, exist_ok=exist_ok)


pathlib.Path.mkdir = _sandbox_safe_mkdir


def pytest_configure(config):
    """把 pytest 临时目录基目录设为项目内可写目录,绕过系统临时目录。"""
    config.option.basetemp = str(ROOT / ".test_tmp2")
