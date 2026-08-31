"""外汇量化交易 — 守护进程主入口

同时启动:
  1. Flask 中控面板 (http://127.0.0.1:5001)
  2. 定时信号评估 (每 15 分钟)
  3. 自动复盘 (每 6 小时)
"""
import logging
import threading
from pathlib import Path

from src.daemon.engine import DaemonEngine
from src.daemon.gold_engine import GoldEngine
from src.daemon.history import SignalHistory

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("main")

# 全局守护进程实例
daemon = DaemonEngine()
gold = GoldEngine()


def start_daemon():
    """在后台线程启动守护进程"""
    daemon.start()
    logger.info("✅ 守护进程已启动")


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
        result = gold.evaluate()
        return jsonify({"success": True, "data": result})

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

    # 启动黄金引擎
    gold.start()

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
