"""外汇量化交易 — 守护进程主入口

同时启动:
  1. Flask 中控面板 (http://127.0.0.1:5001)
  2. 定时信号评估 (每 15 分钟)
  3. 自动复盘 (每 6 小时)
"""
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from src.daemon.engine import DaemonEngine
from src.daemon.gold_engine import GoldEngine
from src.daemon.aud_engine import AudEngine
from src.daemon.history import SignalHistory

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("main")

# 全局守护进程实例
daemon = DaemonEngine()
gold = GoldEngine()
aud = AudEngine()

# 调度间隔: 数据刷新15分钟(API限额); 信号评估5分钟(回测证实:
# 15分钟轮询使确认K线模式损失2/3收益, 5分钟等效逐bar回测收益翻3倍)
UNIFIED_INTERVAL = 15 * 60   # 数据刷新
EVAL_INTERVAL = 5 * 60       # 信号评估


def start_daemon():
    """在后台线程启动守护进程"""
    daemon.start()
    logger.info("✅ 守护进程已启动")


def unified_tick(refresh: bool = False):
    """一次统一评估: (refresh=True时)先拉取三品种最新数据, 再依次评估三引擎

    顺序保证: 所有引擎的评估都基于同一批数据。
    """
    daemon._last_unified_tick = datetime.now(timezone.utc)
    # ① 统一拉取三品种最新数据(OANDA主 + TwelveData备) — 15分钟一次
    if refresh:
        try:
            daemon._refresh_market_data()  # EUR/USD (含重试/fallback/面板状态回写)
        except Exception as e:
            logger.warning(f"EURUSD 数据刷新失败: {str(e)[:60]}")
        from src.data_collection.market_data import refresh_symbol_data
        for sym, tag in [("XAU_USD", "黄金"), ("AUD_USD", "澳元")]:
            try:
                refresh_symbol_data(sym)
            except Exception as e:
                logger.warning(f"{tag} 数据刷新失败: {str(e)[:60]}")

    # ② 按最新数据依次评估: 结算/反转出场 → 信号检测 → 下单
    try:
        daemon.evaluate_signal()
    except Exception as e:
        logger.error(f"EURUSD 评估失败: {e}")
    try:
        gold.evaluate()
    except Exception as e:
        logger.error(f"黄金评估失败: {e}")
    try:
        aud.evaluate()
    except Exception as e:
        logger.error(f"澳元评估失败: {e}")

    daemon._last_eval_time = datetime.now(timezone.utc)
    daemon._beat("signal", UNIFIED_INTERVAL * 2)


def start_unified_scheduler():
    """统一调度线程: 数据刷新15分钟 / 信号评估5分钟(回测: 5分钟检测收益翻3倍)

    评估间隔5分钟: M15数据15分钟才更新一根 — 同一bar会被评估2~3次,
    三引擎已加"同一bar时间戳只处理一次"防重复下单。
    """
    logger.info(
        f"🔄 统一信号调度启动: 数据刷新每 {UNIFIED_INTERVAL // 60} 分钟, "
        f"信号评估每 {EVAL_INTERVAL // 60} 分钟 "
        f"(EURUSD/黄金/澳元 依次评估: 下单/平仓/反转)"
    )
    def _loop():
        unified_tick(refresh=True)  # 启动即刷新+评估
        last_refresh = time.time()
        while True:
            for _ in range(EVAL_INTERVAL // 10):
                time.sleep(10)
            try:
                if time.time() - last_refresh >= UNIFIED_INTERVAL:
                    unified_tick(refresh=True)
                    last_refresh = time.time()
                else:
                    unified_tick(refresh=False)  # 仅评估(用最新本地数据)
            except Exception as e:
                logger.error(f"统一调度异常: {e}")

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return thread


def register_daemon_routes(app):
    """给 Flask app 注册守护进程相关的 API 路由"""

    @app.route("/api/daemon/status")
    def api_daemon_status():
        from flask import jsonify
        return jsonify({"success": True, "data": daemon.status})

    @app.route("/api/gold/status")
    def api_gold_status():
        from flask import jsonify
        return jsonify({"success": True, "data": gold.status})

    @app.route("/api/gold/evaluate")
    def api_gold_evaluate():
        from flask import jsonify
        result = gold.evaluate(refresh=True)  # 手动评估: 先刷新行情再评估
        return jsonify({"success": True, "data": result})

    @app.route("/api/aud/status")
    def api_aud_status():
        from flask import jsonify
        return jsonify({"success": True, "data": aud.status})

    @app.route("/api/aud/evaluate")
    def api_aud_evaluate():
        from flask import jsonify
        result = aud.evaluate(refresh=True)  # 手动评估: 先刷新行情再评估
        return jsonify({"success": True, "data": result})

    @app.route("/api/market/status")
    def api_market_status():
        """三品种数据新鲜度 + 上次统一调度时间(供菜单栏状态)"""
        from flask import jsonify
        return jsonify({"success": True, "data": _market_status_data()})

    @app.route("/api/menu/status")
    def api_menu_status():
        """菜单栏聚合状态 — 一次请求拿全(demon/gold/aud/market/proxy)

        菜单栏每10秒轮询一次, 单请求避免多请求串行阻塞 AppKit 主线程。
        """
        from flask import jsonify
        from src.utils.dashboard_settings import load
        from src.web.proxy_manager import ProxyManager
        try:
            proxy_cfg = load().get("proxy", {})
        except Exception:
            proxy_cfg = {}
        return jsonify({"success": True, "data": {
            "daemon": daemon.status,
            "gold": gold.status,
            "aud": aud.status,
            "market": _market_status_data(),
            "proxy": {
                "process": ProxyManager().status(),
                "mode": proxy_cfg.get("mode", "off"),
            },
        }})


def _market_status_data() -> dict:
    """三品种数据新鲜度 + 上次统一调度时间"""
    from datetime import datetime, timezone
    from src.data_collection.oanda import OandaClient
    o = OandaClient()
    symbols = {}
    for sym in ["EUR_USD", "XAU_USD", "AUD_USD"]:
        try:
            df = o.load_parquet("M15", symbol=sym)
            last = df.index.max()
            age_min = round((datetime.now(timezone.utc) - last).total_seconds() / 60)
            symbols[sym] = {"last_ts": str(last), "age_min": age_min,
                            "fresh": age_min < 60}
        except Exception:
            symbols[sym] = {"last_ts": None, "age_min": None, "fresh": False}
    tick = getattr(daemon, "_last_unified_tick", None)
    return {"symbols": symbols,
            "last_tick": tick.isoformat() if tick else None}

    @app.route("/api/daemon/evaluate")
    def api_daemon_evaluate():
        from flask import jsonify
        result = daemon.evaluate_signal()
        return jsonify({"success": True, "data": result})

    return app


def create_app():
    """创建 Flask app 并集成守护进程"""
    from src.web.dashboard import app as flask_app

    # 注册守护进程路由
    register_daemon_routes(flask_app)

    return flask_app


def main():
    """守护进程主入口"""
    import sys

    # 启动守护进程（后台线程）
    daemon_thread = threading.Thread(target=start_daemon, daemon=True)
    daemon_thread.start()

    # 启动黄金引擎(评估由统一调度器驱动)
    gold.start()

    # 启动澳元引擎(评估由统一调度器驱动)
    aud.start()

    # 启动统一信号调度: 每15分钟统一拉取三品种数据 → 依次评估
    start_unified_scheduler()

    # 启动 Flask（后台线程，主线程留给菜单栏）
    app = create_app()
    port = 5001

    if "--no-open" not in sys.argv and "--menubar" not in sys.argv:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")

    logger.info(f"🌐 中控面板: http://127.0.0.1:{port}")

    # 菜单栏模式: Flask 放到后台线程，主线程跑 rumps
    if "--menubar" in sys.argv or "--menu" in sys.argv:
        flask_thread = threading.Thread(
            target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
            daemon=True,
        )
        flask_thread.start()
        # 主线程启动菜单栏
        from src.daemon.menubar import main as menubar_main
        menubar_main()
    else:
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
