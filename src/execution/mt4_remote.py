"""MT4 远程下单客户端 — Mac 端

通过 VPS 中继服务器向 Windows MT4 发送下单指令。

架构:
  Mac (本模块) ──HTTP──→ VPS relay_server.py ──TCP──→ Windows MT4 EA

配置 (config/config.local.yaml):
  mt4_relay:
    url: "http://你的VPS公网IP:8080"
    enabled: true

用法:
  from src.execution.mt4_remote import MT4Remote
  mt4 = MT4Remote()
  result = mt4.sell(symbol="EURUSD", volume=0.01, sl=1.14850, tp=1.14100)
"""
import json
import logging
from typing import Optional, Dict

import httpx

from src.utils.config_loader import config

logger = logging.getLogger(__name__)


class MT4Remote:
    """MT4 远程交易客户端"""

    def __init__(self):
        # 优先读中控台设置，降级到 YAML 配置
        from src.utils.dashboard_settings import load
        ui_cfg = load().get("mt4_relay", {})
        if ui_cfg.get("url"):
            self.url = ui_cfg["url"].rstrip("/")
            self.enabled = ui_cfg.get("enabled", False)
        else:
            cfg = config.load()
            mt4_cfg = cfg.get("mt4_relay", {})
            self.url = mt4_cfg.get("url", "").rstrip("/")
            self.enabled = mt4_cfg.get("enabled", False)

    def sell(self, symbol: str = "EURUSD", volume: float = 0.01,
             sl: float = None, tp: float = None, comment: str = "auto",
             sl_pips: float = None, tp_pips: float = None) -> Dict:
        """做空下单

        sl_pips/tp_pips: 距离模式 — EA 按实际成交价换算 SL/TP,
        消除行情源(OANDA)与券商之间的点差/基差影响。None 则用绝对价格。
        """
        return self._order("SELL", symbol, volume, sl, tp, comment, sl_pips, tp_pips)

    def buy(self, symbol: str = "EURUSD", volume: float = 0.01,
            sl: float = None, tp: float = None, comment: str = "auto",
            sl_pips: float = None, tp_pips: float = None) -> Dict:
        """做多下单（sl_pips/tp_pips 同上）"""
        return self._order("BUY", symbol, volume, sl, tp, comment, sl_pips, tp_pips)

    def quote(self, symbol: str = "EURUSD") -> Optional[Dict]:
        """获取 EA 最近上报的券商实时报价(供数据校准)"""
        if not self.enabled:
            return None
        try:
            resp = httpx.get(f"{self.url}/quote?symbol={symbol}", timeout=5)
            d = resp.json()
            if d.get("success") and d.get("bid"):
                return d
        except Exception as e:
            logger.debug(f"获取券商报价失败: {e}")
        return None

    def health(self) -> Dict:
        """检查中继服务器状态"""
        if not self.enabled:
            return {"success": False, "error": "MT4远程交易未启用"}
        try:
            resp = httpx.get(f"{self.url}/health", timeout=5)
            return resp.json()
        except Exception as e:
            return {"success": False, "error": str(e)}

    def close(self, direction: str, symbol: str = "EURUSD") -> Dict:
        """平仓指定品种+方向的持仓（EA 异步执行，按 symbol+direction 全部平）"""
        if not self.enabled:
            return {"success": False, "error": "MT4远程交易未启用"}
        try:
            resp = httpx.post(
                f"{self.url}/close",
                json={"symbol": symbol, "direction": direction},
                timeout=15,
            )
            result = resp.json()
            if result.get("success"):
                logger.info(f"✅ MT4平仓指令已提交: {symbol} {direction}")
            else:
                logger.warning(f"❌ MT4平仓失败: {result.get('error')}")
            return result
        except Exception as e:
            logger.error(f"MT4平仓连接失败: {e}")
            return {"success": False, "error": str(e)}

    def _order(self, direction: str, symbol: str, volume: float,
               sl: Optional[float], tp: Optional[float], comment: str,
               sl_pips: Optional[float] = None,
               tp_pips: Optional[float] = None) -> Dict:
        """发送下单请求"""
        if not self.enabled:
            return {"success": False, "error": "MT4远程交易未启用"}

        payload = {
            "direction": direction,
            "symbol": symbol,
            "volume": volume,
            "sl": sl,
            "tp": tp,
            "comment": comment,
        }
        # 距离模式: EA 按成交价换算 SL/TP, 消除行情源与券商的点差
        if sl_pips is not None:
            payload["sl_pips"] = round(float(sl_pips), 1)
        if tp_pips is not None:
            payload["tp_pips"] = round(float(tp_pips), 1)

        try:
            resp = httpx.post(
                f"{self.url}/order",
                json=payload,
                timeout=15,
            )
            result = resp.json()
            # 双保险: 即使旧版 relay 对 EA 执行失败也返回 success=True,
            # 客户端侧仍按 result.status == "filled" 判定真实成交
            inner = result.get("result") or {}
            status = inner.get("status", "filled")
            if result.get("success") and status == "filled":
                logger.info(f"✅ MT4远程下单成功: {direction} {volume}手")
            else:
                reason = inner.get("error") or result.get("error") or f"status={status}"
                result = {**result, "success": False,
                          "error": f"EA未确认成交: {reason}"}
                logger.warning(f"❌ MT4远程下单失败: {result['error']}")
            return result
        except Exception as e:
            logger.error(f"MT4远程连接失败: {e}")
            return {"success": False, "error": str(e)}
