"""锁利追踪规则 — 纯函数 + 可开关配置（默认关闭）

config.yaml → trail:
  enabled:    false  # true 才启用锁利追踪
  trigger:    0.70   # 浮盈达到止盈的 70% 触发锁利
  lock_ratio: 0.80   # 触发后止损锁在止盈的 80% (TP40 → 锁 +32p)
  tp_mult:    2.0    # 触发后止盈翻倍

用法:
  from src.strategy.trail import compute_trail_plan
  p = compute_trail_plan("BUY", entry=1.1500, tp=1.1540, tp_pips=40)
  if p.enabled:  # MT4/EA 侧: 价格到 p.trigger_price → 改止损 p.lock_price / 止盈 p.new_tp
"""
from dataclasses import dataclass
from typing import Optional

from src.utils.config_loader import config


@dataclass
class TrailPlan:
    enabled: bool
    trigger_price: Optional[float]   # 浮盈到达该价位 → 激活锁利
    lock_price: Optional[float]      # 激活后止损移到该价（锁定利润）
    new_tp: Optional[float]          # 激活后止盈位置（TP 翻倍）
    trigger_ratio: float
    lock_ratio: float
    tp_mult: float


def get_trail_cfg() -> dict:
    """中控台设置优先, YAML 兜底"""
    yaml_cfg = config.load().get("trail", {})
    try:
        from src.utils.dashboard_settings import load
        ui = load().get("trail", {})
        if ui:
            return {**yaml_cfg, **ui}
    except Exception:
        pass
    return yaml_cfg


def compute_trail_plan(direction: str, entry: float, tp: float,
                       tp_pips: float, cfg: Optional[dict] = None) -> TrailPlan:
    """按配置生成锁利追踪计划（enabled=false 时全空）"""
    cfg = cfg if cfg is not None else get_trail_cfg()
    if not cfg.get("enabled", False):
        return TrailPlan(False, None, None, None, 0.0, 0.0, 1.0)

    trigger = float(cfg.get("trigger", 0.70))
    lock_ratio = float(cfg.get("lock_ratio", 0.80))
    tp_mult = float(cfg.get("tp_mult", 2.0))
    move = abs(tp - entry)                     # 止盈距离（价格单位, 如 0.004）

    if direction == "BUY":
        trigger_price = entry + move * trigger
        lock_price = entry + move * lock_ratio
        new_tp = entry + move * tp_mult
    elif direction == "SELL":
        trigger_price = entry - move * trigger
        lock_price = entry - move * lock_ratio
        new_tp = entry - move * tp_mult
    else:
        return TrailPlan(False, None, None, None, trigger, lock_ratio, tp_mult)

    return TrailPlan(True, round(trigger_price, 5), round(lock_price, 5),
                     round(new_tp, 5), trigger, lock_ratio, tp_mult)