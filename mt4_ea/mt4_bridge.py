"""MT4 Windows 桥接脚本 — 轮询 VPS 并自动下单

在 Windows 上运行（需要 Python 3 + MT4 终端已打开）。
不需要 EA，通过 MT4 COM 接口直接下单。

用法:
    pip install requests
    python mt4_bridge.py --relay http://43.134.95.147:8080 --poll 5
"""
import json
import sys
import time
import threading
from datetime import datetime

try:
    import requests
except ImportError:
    print("请先安装: pip install requests")
    sys.exit(1)


class MT4Bridge:
    """MT4 下单桥接"""

    def __init__(self, relay_url: str, poll_seconds: int = 5):
        self.relay_url = relay_url.rstrip("/")
        self.poll_seconds = poll_seconds
        self.last_order_id = ""
        self._connected = False

    def connect(self) -> bool:
        """检查 VPS 连通性"""
        try:
            resp = requests.get(f"{self.relay_url}/health", timeout=5)
            if resp.status_code == 200:
                print(f"✅ 已连接 VPS: {self.relay_url}")
                self._connected = True
                return True
        except Exception as e:
            print(f"❌ VPS 连接失败: {e}")
        return False

    def run(self):
        """主循环"""
        print(f"🚀 MT4 Bridge 启动 (轮询间隔={self.poll_seconds}s)")
        while True:
            try:
                self._check_and_execute()
            except Exception as e:
                print(f"⚠️ 轮询异常: {e}")
            time.sleep(self.poll_seconds)

    def _check_and_execute(self):
        """检查待处理订单并执行"""
        try:
            resp = requests.get(f"{self.relay_url}/pending", timeout=5)
            if resp.status_code != 200:
                return
            data = resp.json()
            if not data or not data.get("id"):
                return

            order_id = data["id"]
            if order_id == self.last_order_id:
                return
            self.last_order_id = order_id

            direction = data.get("direction", "")
            symbol = data.get("symbol", "EURUSD")
            volume = float(data.get("volume", 0.01))
            sl = data.get("sl")
            tp = data.get("tp")

            if sl is not None:
                sl = float(sl)
            if tp is not None:
                tp = float(tp)

            print(f"\n📩 收到订单: {direction} {volume}手 SL={sl} TP={tp}")

            # 通过 MT4 COM 接口下单
            result = self._place_order_mt4(direction, symbol, volume, sl, tp)

            # 回报 VPS
            params = f"order_id={order_id}"
            if result.get("ticket"):
                params += f"&ticket={result['ticket']}&status=filled&price={result.get('price','')}"
                print(f"✅ 下单成功: Ticket={result['ticket']}")
            else:
                params += f"&status=error&error={result.get('error','unknown')}"
                print(f"❌ 下单失败: {result.get('error')}")

            requests.get(f"{self.relay_url}/confirm?{params}", timeout=5)

        except requests.exceptions.ConnectionError:
            pass  # VPS 暂时不可达，静默跳过
        except Exception as e:
            print(f"⚠️ 处理异常: {e}")

    @staticmethod
    def _place_order_mt4(direction: str, symbol: str, volume: float,
                          sl: float = None, tp: float = None) -> dict:
        """通过 COM 接口在 MT4 下单

        MT4 终端必须正在运行且已登录账户。
        使用 Python win32com 调用 MT4 COM API。
        """
        try:
            import win32com.client

            # 连接 MT4
            mt4 = win32com.client.Dispatch("MetaTrader4.MT4")
            if not mt4.IsConnected():
                return {"error": "MT4 未连接账户"}

            # 下单
            cmd = 0 if direction == "BUY" else 1  # OP_BUY=0, OP_SELL=1
            price = mt4.Ask(symbol) if direction == "BUY" else mt4.Bid(symbol)
            if price <= 0:
                return {"error": "无法获取报价"}

            ticket = mt4.OrderSend(
                symbol, cmd, volume, price,
                30,      # slippage
                sl or 0,
                tp or 0,
                "auto",  # comment
                0,       # magic
                0,       # expiration
            )

            if ticket > 0:
                return {"ticket": ticket, "price": price}
            else:
                return {"error": f"OrderSend 失败, ticket={ticket}"}

        except ImportError:
            return {"error": "pywin32 未安装。请运行: pip install pywin32"}
        except Exception as e:
            return {"error": f"COM 错误: {e}"}


if __name__ == "__main__":
    relay = sys.argv[1] if len(sys.argv) > 1 else "http://43.134.95.147:8080"
    poll = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    bridge = MT4Bridge(relay, poll)
    if bridge.connect():
        bridge.run()
    else:
        print("VPS 连接失败，请检查地址和网络")
        sys.exit(1)
