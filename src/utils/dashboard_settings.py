"""面板设置 — 可通过 UI 编辑的配置项，存储为 JSON

config/dashboard_settings.json 会覆盖 config.yaml 中的对应值。
UI 中修改后立即生效，重启后保持。
"""
import json
import logging
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "dashboard_settings.json"

DEFAULTS: Dict[str, Any] = {
    "llm": {
        "cli_command": "claude-doubao",
        "cli_args": ["-p", "--dangerously-skip-permissions", "--print", "--output-format", "json"],
    }
}


def load() -> Dict[str, Any]:
    """加载面板设置，文件不存在则用默认值"""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            # 深度合并：保存的值覆盖默认值
            merged = _deep_merge(DEFAULTS.copy(), saved)
            return merged
        except Exception as e:
            logger.warning(f"读取面板设置失败: {e}，使用默认值")
    return DEFAULTS.copy()


def save(settings: Dict[str, Any]) -> None:
    """保存面板设置"""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    logger.info("✅ 面板设置已保存")


def get_llm_config() -> Dict[str, Any]:
    """获取 LLM CLI 配置（合并面板设置 > YAML 默认）"""
    from src.utils.config_loader import config
    yaml_cfg = config.load().get("strategy", {}).get("llm", {})
    ui_cfg = load().get("llm", {})
    # UI 设置优先
    return {**yaml_cfg, **ui_cfg}


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典"""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result
