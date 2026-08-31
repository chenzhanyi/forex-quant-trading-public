"""MT4 交易中继服务器 v3 — 干净版

Mac ──HTTP──→ VPS (本服务) ──轮询──→ MT4 EA

启动: python3 relay_server.py --host 0.0.0.0 --port 8080
"""
import json, logging, threading, time
from datetime import datetime
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [relay] %(message)s")
logger = logging.getLogger("relay")

app = Flask(__name__)

# ── 状态 ──
ea_positions = {}       # {ticket: {...}} EA 上报的最新持仓
pending_orders = {}     # {order_id: {...}} 待处理订单
order_results = {}      # {order_id: {...}} 已完成订单
broker_quotes = {}      # {symbol: {"bid":.., "ask":.., "ts":..}} EA 轮询时捎带上报
order_counter = 0
order_lock = threading.Lock()

# 派单独占认领租约: 订单发给 EA 后 N 秒内不再重复派发
# (防止两个图表/EA 实例同时抢同一订单; 确认失败时租约到期后重新可派)
CLAIM_LEASE_SECONDS = 45


# ═══════════════════════ HTTP API ═══════════════════════

ea_last_seen = 0  # EA 最后一次上报时间戳

@app.route("/health")
def health():
    import time
    alive = (time.time() - ea_last_seen) < 120  # 2分钟内上报过=在线
    return jsonify({"status": "ok", "ea_connected": alive, "positions": len(ea_positions)})


@app.route("/order", methods=["POST"])
def place_order():
    """下单"""
    global order_counter
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"success": False, "error": "invalid JSON"}), 400
    with order_lock:
        order_counter += 1
        oid = f"ord_{order_counter}_{int(time.time())}"
        order_data = {
            "id": oid, "time": datetime.now().isoformat(),
            "action": "open",
            "direction": data["direction"], "symbol": data.get("symbol", "EURUSD"),
            "volume": data.get("volume", 0.01),
            "sl": data.get("sl"), "tp": data.get("tp"),
            # 距离模式字段必须透传 — EA 按成交价换算SL/TP, 消除OANDA与券商点差
            "sl_pips": data.get("sl_pips"), "tp_pips": data.get("tp_pips"),
            "comment": data.get("comment", "auto"),
            "_created_at": time.time(),
        }
        pending_orders[oid] = order_data
    logger.info(f"📩 下单: {data['direction']} SL={data.get('sl')} TP={data.get('tp')}")
    # 等 EA 处理
    for _ in range(100):
        if oid in order_results:
            r = order_results.pop(oid)
            pending_orders.pop(oid, None)
            # EA 回报 status=error(OrderSend 失败)也必须返回 success=False,
            # 否则调用方会把失败单当作已成交, 记录幽灵持仓
            ok = r.get("status") == "filled"
            return jsonify({"success": ok, "order_id": oid, "result": r})
        time.sleep(0.1)
    return jsonify({"success": False, "order_id": oid, "error": "EA 超时未响应"})


@app.route("/close", methods=["POST"])
def close_positions():
    """平仓"""
    global order_counter
    data = request.get_json(force=True) if request.data else {}
    with order_lock:
        order_counter += 1
        oid = f"close_{order_counter}_{int(time.time())}"
        pending_orders[oid] = {
            "id": oid, "time": datetime.now().isoformat(),
            "action": "close",
            "direction": data.get("direction", ""),
            "symbol": data.get("symbol", "EURUSD"),
            "ticket": data.get("ticket", 0),
            "_created_at": time.time(),
        }
    logger.info(f"📤 平仓: {data.get('direction','全部')}")
    return jsonify({"success": True, "order_id": oid})


@app.route("/modify", methods=["POST"])
def modify_order():
    """修改订单"""
    global order_counter
    data = request.get_json(force=True) if request.data else {}
    with order_lock:
        order_counter += 1
        oid = f"mod_{order_counter}_{int(time.time())}"
        pending_orders[oid] = {
            "id": oid, "time": datetime.now().isoformat(),
            "action": "modify",
            "ticket": data.get("ticket", 0),
            "sl": data.get("sl"), "tp": data.get("tp"),
            "_created_at": time.time(),
        }
    logger.info(f"📝 修改: ticket={data.get('ticket')} SL={data.get('sl')} TP={data.get('tp')}")
    return jsonify({"success": True, "order_id": oid})


# ═══════════════════════ EA 轮询 ═══════════════════════

@app.route("/pending")
def get_pending():
    """EA 轮询: 返回最早待处理订单（过期自动丢弃）

    查询参数:
      symbol  EA 所在图表的品种(如 EURUSD) — 只派发该品种的订单,
              防止黄金图表执行 EURUSD 订单(及反向)
      bid/ask EA 捎带上报的券商实时报价 — 存入 broker_quotes 供 Mac 端校准

    派单独占认领: 订单一经下发即标记 _claimed_at, 租约期内不重复派发;
    确认失败/EA 崩溃时租约到期后重新可派。
    """
    import time
    EXPIRY_SECONDS = 600  # 10分钟过期

    # 捎带报价上报(无需加锁, 单键写入)
    sym = request.args.get("symbol", "").upper()
    bid = request.args.get("bid", "")
    ask = request.args.get("ask", "")
    if sym and bid and ask:
        try:
            b, a = float(bid), float(ask)
            # 周末/休市时 MT4 报价可能为 0 — 无效报价不入库
            if b > 0 and a > 0:
                broker_quotes[sym] = {
                    "bid": b, "ask": a,
                    "symbol": sym, "ts": time.time(),
                }
        except ValueError:
            pass

    with order_lock:
        now = time.time()
        # 清理过期订单
        expired = [k for k, v in pending_orders.items()
                   if now - v.get("_created_at", 0) > EXPIRY_SECONDS]
        for k in expired:
            logger.info(f"🗑 过期订单丢弃: {k}")
            pending_orders.pop(k)
        # 认领租约到期 → 重新可派
        for k, v in pending_orders.items():
            if v.get("_claimed_at") and now - v["_claimed_at"] > CLAIM_LEASE_SECONDS:
                v.pop("_claimed_at", None)
        # 找第一个未认领且品种匹配的订单
        for v in pending_orders.values():
            if v.get("_claimed_at"):
                continue
            if sym and v.get("symbol", "EURUSD").upper() != sym:
                continue
            v["_claimed_at"] = now
            return jsonify(v)
        return jsonify({})


@app.route("/quote")
def get_quote():
    """返回 EA 最近上报的券商报价(供 Mac 端数据校准)"""
    sym = request.args.get("symbol", "EURUSD").upper()
    q = broker_quotes.get(sym)
    if not q:
        return jsonify({"success": False, "error": "no quote yet"})
    import time
    if time.time() - q.get("ts", 0) > 120:  # 报价超过2分钟视为失效
        return jsonify({"success": False, "error": "quote stale"})
    return jsonify({"success": True, **q})


@app.route("/confirm")
def confirm_order():
    """EA 回报结果"""
    oid = request.args.get("order_id", "")
    ticket = request.args.get("ticket", "")
    status = request.args.get("status", "filled")
    with order_lock:
        if oid in pending_orders:
            pending_orders.pop(oid)
        order_results[oid] = {
            "order_id": oid, "ticket": ticket, "status": status,
            "price": request.args.get("price", ""),
            "error": request.args.get("error", ""),
        }
    global ea_last_seen
    import time
    ea_last_seen = time.time()
    logger.info(f"✅ EA确认: {oid} {status}")
    return jsonify({"success": True})


# ═══════════════════════ 持仓上报 ═══════════════════════

@app.route("/rp")
def rp():
    """EA 逐单上报持仓

    clear=1 时可带 sym=XXX 只清理该品种缓存 —
    避免多个图表 EA 各自上报时互相清空对方的持仓。
    """
    global ea_positions, ea_last_seen
    import time
    ea_last_seen = time.time()
    if request.args.get("clear") == "1":
        sym = request.args.get("sym", "").upper()
        if sym:
            ea_positions = {k: v for k, v in ea_positions.items()
                            if v.get("symbol", "EURUSD").upper() != sym}
        else:
            ea_positions = {}  # 不带 sym: 全清(兼容旧 EA)
        return jsonify({"success": True})
    ticket = request.args.get("t")
    if ticket:
        ea_positions[int(ticket)] = {
            "ticket": int(ticket),
            "type": "BUY" if request.args.get("type") == "0" else "SELL",
            "lots": float(request.args.get("lots", 0)),
            "open": float(request.args.get("open", 0)),
            "sl": float(request.args.get("sl", 0)),
            "tp": float(request.args.get("tp", 0)),
            "profit": float(request.args.get("profit", 0)),
            "symbol": request.args.get("sym", "EURUSD"),
        }
    return jsonify({"success": True})


@app.route("/positions")
def get_positions():
    return jsonify({"success": True, "positions": list(ea_positions.values())})


# ═══════════════════════ 管理 ═══════════════════════

@app.route("/admin")
def admin():
    """查看所有状态"""
    return jsonify({
        "pending": len(pending_orders),
        "results": len(order_results),
        "positions": len(ea_positions),
    })


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()
    logger.info(f"🌐 Relay v3 启动: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
