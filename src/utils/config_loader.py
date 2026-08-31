"""配置加载 — 支持 config.yaml + config.local.yaml 覆盖 + 代理"""
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Union

import httpx
import yaml


class Config:
    """全局配置单例（线程安全）"""

    _instance = None
    _config: Dict[str, Any] = {}
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                # 双重检查：锁内再确认一次，防止竞争
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def load(self, config_dir: Union[str, Path] = None, reload: bool = False) -> Dict[str, Any]:
        # 已加载且未要求重载 → 直接返回缓存
        if self._config and not reload:
            return self._config

        if config_dir is None:
            config_dir = Path(__file__).resolve().parent.parent.parent / "config"
        config_dir = Path(config_dir)

        base_path = config_dir / "config.yaml"
        local_path = config_dir / "config.local.yaml"

        if not base_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {base_path}")

        with open(base_path) as f:
            self._config = yaml.safe_load(f)

        if local_path.exists():
            with open(local_path) as f:
                local_cfg = yaml.safe_load(f)
            self._deep_merge(self._config, local_cfg)

        return self._config

    def get(self, *keys: str, default: Any = None) -> Any:
        val = self._config
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
        return val if val is not None else default

    def get_http_client(self) -> httpx.Client:
        """获取 httpx 客户端（带代理或不带）"""
        net = self._config.get("network", {})
        if net.get("enabled"):
            proxy_url = net.get("http", "")
            return httpx.Client(proxy=proxy_url, timeout=30)
        return httpx.Client(timeout=30)

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        for key, val in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(val, dict):
                Config._deep_merge(base[key], val)
            else:
                base[key] = val


config = Config()


def setup_logging(name: str = "forex", level: str = "INFO") -> None:
    """统一日志配置 — 所有入口点调用此函数设置日志格式和级别"""
    import logging
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger(name).info(f"📊 日志初始化完成 (level={level})")
