"""风险控制 — 简化为 R:R 验证 + 仓位计算

止损止盈由 EntryDetector 直接计算（H4 ATR×2.5 / TP40）。
这里只验证 R:R ≥ 1.0 并计算仓位。
"""
import logging
from dataclasses import dataclass

from src.utils.config_loader import config

logger = logging.getLogger(__name__)


@dataclass
class RiskResult:
    passed: bool
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    rr_ratio: float
    sl_method: str
    position_units: int
    position_lots: float
    max_loss_amount: float
    max_loss_pips: float
    reason: str


class RiskManager:
    """简化的风险管理器"""

    def __init__(self):
        self.cfg = config.load()
        self.balance = float(self.cfg["account"]["balance"])
        self.default_lots = self._resolve_default_lots()

    def _resolve_default_lots(self) -> float:
        """每次实时读取手数: 中控台设置优先 > YAML 兜底
        放 calculate 里每次都读, 避免仅重启 daemon 才生效"""
        try:
            from src.utils.dashboard_settings import load
            ui_lots = load().get("trade", {}).get("default_lots")
            if ui_lots is not None:
                return float(ui_lots)
        except Exception:
            pass
        return float(self.cfg["account"].get("default_lots", 0.01))

    def calculate(self, direction: str, entry: float,
                  stop_loss: float, h4_targets: dict = None) -> RiskResult:
        """验证 R:R 并计算仓位

        SL 和 TP 已由 EntryDetector 确定，这里只做验证和仓位计算。
        """
        self.default_lots = self._resolve_default_lots()   # 实时读, 面板改动立即生效
        sl_pips = abs(entry - stop_loss) * 10000
        tp_pips = 40  # 固定 40 pips

        tp1 = entry - tp_pips / 10000 if direction == "SELL" else entry + tp_pips / 10000
        tp2 = tp1

        rr = tp_pips / sl_pips if sl_pips > 0 else 0
        if rr < 1.0:
            return RiskResult(
                passed=False, stop_loss=stop_loss, take_profit_1=tp1,
                take_profit_2=tp2, rr_ratio=round(rr, 2), sl_method="ATR2.5x",
                position_units=0, position_lots=0, max_loss_amount=0,
                max_loss_pips=round(sl_pips, 1),
                reason=f"R:R {rr:.1f} < 1.0，放弃"
            )

        # 仓位: 小资金固定 default_lots 手; 大资金按风险缩放
        if self.balance < 500:
            lots = self.default_lots            # 来自 config account.default_lots
            units = int(round(lots * 100000))
        else:
            units = int(max(1000, round(self.balance * 0.02 / (sl_pips * 0.10 / 1000) / 1000) * 1000))
            lots = units / 100000

        actual_loss = sl_pips * 0.10 * (units / 1000)
        return RiskResult(
            passed=True, stop_loss=stop_loss, take_profit_1=tp1, take_profit_2=tp2,
            rr_ratio=round(rr, 2), sl_method="ATR2.5x",
            position_units=units, position_lots=round(lots, 4),
            max_loss_amount=round(actual_loss, 2), max_loss_pips=round(sl_pips, 1),
            reason=f"✅ SL={sl_pips:.0f}p TP={tp_pips}p R:R={rr:.1f}"
        )
