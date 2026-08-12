"""模型配置持久化:存到 settings.json,重启保留。

配置结构:
{
  "model": {
    "type": "openai_compat" | "deepseek" | "mock",
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
    "api_key": "sk-xxx"
  }
}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def settings_path() -> Path:
    return Path.home() / ".my-agent" / "settings.json"


def load_settings() -> dict[str, Any]:
    p = settings_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_settings(data: dict[str, Any]) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_model_config() -> dict[str, Any]:
    """取模型配置。默认用 DeepSeek。"""
    settings = load_settings()
    config = settings.get("model") or {}
    if not config:
        config = {
            "type": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
        }
    return config


def set_model_config(config: dict[str, Any]) -> None:
    settings = load_settings()
    settings["model"] = config
    save_settings(settings)
