"""轻量 .env 加载器:解析项目根目录的 .env,填充尚未设置的环境变量。

不引入 python-dotenv 依赖,只覆盖最常见的 KEY=VALUE 格式:
- 忽略空行与 # 注释
- 支持 KEY=value、KEY="value"、KEY='value'
- 不覆盖已经存在的环境变量(系统环境优先级更高)
"""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path | None = None) -> bool:
    """加载 .env 文件。默认读当前工作目录下的 .env。返回是否成功加载。"""
    target = Path(path) if path else Path.cwd() / ".env"
    if not target.is_file():
        return False

    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 去掉首尾配对引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        # 不覆盖已存在的环境变量
        if key and key not in os.environ:
            os.environ[key] = value
    return True
