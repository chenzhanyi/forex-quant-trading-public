"""FXCM REST API 实盘交易桥接

通过 FXCM ForexConnect REST API 直接下单。
不需要 MT4/MT5 开着。

安装 fxcmpy（FXCM 官方未发布到 PyPI，从 GitHub 安装）:
    pip install git+https://github.com/fxcm-mroman/FXCM-API.git
    或
    pip install fxcmpy  # 如果已经通过其他方式安装

Token 获取:
    - 登录 FXCM 账户后台 → API 管理 → 生成 Token
    - 或联系 FXCM 客服开通 API 权限
    - Token 写入 config/config.local.yaml:
        fxcm:
          access_token: "YOUR_TOKEN"
          server: "demo"  # demo/real

用法:
    from src.execution.fxcm_bridge import FXCMBridge
    fxcm = FXCMBridge()
    fxcm.sell(entry=1.14267, sl=1.14874, tp=1.13775, lots=0.01)
    fxcm.close_position("SELL")
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional, Dict

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    """下单结果"""
    success: bool
    order_id: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    lots: float
    message: str


class FXCMBridge:
    """FXCM 实盘交易桥接"""

    def __init__(self):
        self._con = None
        self._lock = threading.Lock()
        self._connected = False

    # ═══════════════════════════════════════
    # 连接管理
    # ═══════════════════════════════════════

    def connect(self, token: str = None, server: str = None) -> bool:
        """连接 FXCM 服务器

        Args:
            token: API Token（不传则从 config.local.yaml 读取）
            server: 'demo' 或 'real'
        """
        try:
            from src.utils.config_loader import config

            if token is None:
                cfg = config.load()
                token = cfg.get("fxcm", {}).get("access_token", "")
                server = server or cfg.get("fxcm", {}).get("server", "demo")

            if not token or token.startswith("YOUR_"):
                logger.warning("⚠️ FXCM Token 未配置，跳过连接")
                return False

            import fxcmpy
            self._con = fxcmpy.fxcmpy(
                access_token=token,
                server=server,
                log_level="error",
            )
            self._connected = True
            logger.info(f"✅ FXCM 已连接 (server={server})")
            return True

        except ImportError:
            logger.error(
                "❌ fxcmpy 未安装。请运行:\n"
                "   pip install git+https://github.com/fxcm-mroman/FXCM-API.git"
            )
            return False
        except Exception as e:
            logger.error(f"❌ FXCM 连接失败: {e}")
            self._connected = False
            return False

    def disconnect(self):
        """断开连接"""
        if self._con:
            try:
                self._con.close()
            except Exception:
                pass
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._con is not None

    # ═══════════════════════════════════════
    # 行情查询
    # ═══════════════════════════════════════

    def get_current_price(self, symbol: str = "EUR/USD") -> Optional[float]:
        """获取当前报价（中间价）"""
        if not self.is_connected:
            return None
        try:
            bid, ask = self._con.get_last_price(symbol)
            return round((bid + ask) / 2, 5)
        except Exception as e:
            logger.warning(f"FXCM 报价失败: {e}")
            return None

    # ═══════════════════════════════════════
    # 下单
    # ═══════════════════════════════════════

    def sell(self, entry: float, sl: float, tp: float,
             lots: float = 0.01, symbol: str = "EUR/USD") -> OrderResult:
        """做空下单

        Args:
            entry: 入场价（用 market order，市价成交）
            sl: 止损价
            tp: 止盈价
            lots: 手数（默认 0.01）
            symbol: 交易品种
        """
        units = int(lots * 100000)  # 0.01 手 = 1000 单位
        return self._place_order("SELL", symbol, units, sl, tp)

    def buy(self, entry: float, sl: float, tp: float,
            lots: float = 0.01, symbol: str = "EUR/USD") -> OrderResult:
        """做多下单"""
        units = int(lots * 100000)
        return self._place_order("BUY", symbol, units, sl, tp)

    def _place_order(self, direction: str, symbol: str, units: int,
                     sl: float, tp: float) -> OrderResult:
        """实际下单"""
        if not self.is_connected:
            # 尝试自动连接
            if not self.connect():
                return OrderResult(
                    success=False, order_id="", direction=direction,
                    entry_price=0, stop_loss=sl, take_profit=tp,
                    lots=units / 100000,
                    message="FXCM 未连接（Token 未配置或连接失败）",
                )

        with self._lock:
            try:
                if direction == "SELL":
                    order = self._con.create_market_sell_order(
                        symbol, units,
                        stop_rate=sl,
                        limit_rate=tp,
                    )
                else:
                    order = self._con.create_market_buy_order(
                        symbol, units,
                        stop_rate=sl,
                        limit_rate=tp,
                    )

                order_id = str(order.get("orderId", ""))
                executed_price = float(order.get("price", 0))

                logger.info(
                    f"✅ FXCM {direction}: {units}单位 @ {executed_price:.5f} "
                    f"SL={sl:.5f} TP={tp:.5f}"
                )

                return OrderResult(
                    success=True,
                    order_id=order_id,
                    direction=direction,
                    entry_price=executed_price,
                    stop_loss=sl,
                    take_profit=tp,
                    lots=units / 100000,
                    message=f"已成交 @ {executed_price:.5f}",
                )

            except Exception as e:
                logger.error(f"❌ FXCM 下单失败: {e}")
                return OrderResult(
                    success=False, order_id="", direction=direction,
                    entry_price=0, stop_loss=sl, take_profit=tp,
                    lots=units / 100000,
                    message=str(e)[:200],
                )

    # ═══════════════════════════════════════
    # 持仓管理
    # ═══════════════════════════════════════

    def close_position(self, direction: str = None,
                       symbol: str = "EUR/USD") -> bool:
        """平仓

        Args:
            direction: 'SELL'/'BUY' 或 None（平全部）
        """
        if not self.is_connected:
            return False
        try:
            positions = self._con.get_open_positions()
            for pos in positions:
                if pos.get("currency", "") == symbol:
                    if direction and pos.get("isBuy") != (direction == "BUY"):
                        continue
                    trade_id = pos.get("tradeId")
                    self._con.close_trade(trade_id, 100)
                    logger.info(f"✅ 已平仓: {trade_id}")
            return True
        except Exception as e:
            logger.error(f"平仓失败: {e}")
            return False

    def get_open_positions(self) -> list:
        """获取当前持仓"""
        if not self.is_connected:
            return []
        try:
            return self._con.get_open_positions()
        except Exception:
            return []

    def get_account_info(self) -> Dict:
        """获取账户信息"""
        if not self.is_connected:
            return {}
        try:
            return self._con.get_accounts()
        except Exception:
            return {}
