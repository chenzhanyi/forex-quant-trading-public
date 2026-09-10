"""外汇量化交易 — Web 中控面板"""
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict

from flask import Flask, jsonify, render_template, request

from src.strategy.signal import SignalGenerator
from src.strategy.trend import TrendAnalyzer
from src.analysis.fundamentals import FundamentalsAnalyzer
from src.data_collection.calendar import EconomicCalendar
from src.data_collection.oanda import OandaClient
from src.utils.config_loader import config, setup_logging

setup_logging("dashboard")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["TITLE"] = "外汇量化交易 · 中控面板"


# ── API 路由 ──

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/signal")
def api_signal():
    """获取最新交易信号"""
    try:
        gen = SignalGenerator()
        sig = gen.generate()
        sentiment = {"label": "中性", "score": 0, "bullish_count": 0, "bearish_count": 0}
        fund = FundamentalsAnalyzer()
        try:
            # 方案A: 关键词打分
            fund_dir = Path(__file__).resolve().parent.parent.parent / "data" / "fundamentals"
            events = []
            for f in sorted(fund_dir.rglob("*.json"), reverse=True)[:10]:
                with open(f) as fh:
                    events.extend(json.load(fh))
            if events:
                sentiment = fund.analyze_sentiment(events, 7)
            # 方案B: Hermes LLM 评分（如有，覆盖方案A）
            h_file = Path(__file__).resolve().parent.parent.parent / "output" / "analysis" / f"sentiment_{datetime.now().strftime('%Y%m%d')}.json"
            if h_file.exists():
                with open(h_file) as fh:
                    llm = json.load(fh)
                    if llm.get("label"):
                        sentiment = {**sentiment, "label": llm["label"], "score": llm.get("score", sentiment["score"]), "source": llm.get("source", "Claude LLM"), "reason": llm.get("reason", "")}
        except Exception:
            pass
        return jsonify({
            "success": True,
            "data": {
                "can_trade": sig.can_trade,
                "timestamp": sig.timestamp,
                "trend": sig.trend_overall,
                "d1_direction": sig.trend_d1,
                "h4_direction": sig.trend_h4,
                "direction": sig.direction if sig.can_trade else "观望",
                "skip_reason": sig.skip_reason,
                "entry_price": sig.entry_price if sig.can_trade else None,
                "stop_loss": sig.stop_loss if sig.can_trade else None,
                "tp1": sig.take_profit_1 if sig.can_trade else None,
                "tp2": sig.take_profit_2 if sig.can_trade else None,
                "rr_ratio": sig.rr_ratio if sig.can_trade else None,
                "lots": sig.position_lots if sig.can_trade else None,
                "max_loss": sig.max_loss_amount if sig.can_trade else None,
                "confidence": sig.confidence,
                "reason": sig.entry_reason if sig.can_trade else "",
                "sentiment": sentiment,  # 基本面情绪
            }
        })
    except Exception as e:
        # 网络异常(如OANDA EOF)时返回可用结果, 面板不崩 —
        # daemon每15分钟自动评估会持续重试, 数据恢复后自动正常
        logger.error(f"signal api: {e}")
        return jsonify({
            "success": True,
            "data": {
                "can_trade": False,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M GMT+8"),
                "trend": "网络异常，信号生成失败",
                "d1_direction": "?",
                "h4_direction": "?",
                "direction": "观望",
                "skip_reason": f"⛔ 网络异常: {str(e)[:60]}（数据恢复后自动正常，可稍后刷新）",
                "entry_price": None, "stop_loss": None, "tp1": None, "tp2": None,
                "rr_ratio": None, "lots": None, "max_loss": None,
                "confidence": 0, "reason": "",
                "sentiment": {"label": "中性", "score": 0},
            }
        })


@app.route("/api/entry")
def api_entry():
    """获取入场检测详情（含斜率动量、形态评分）"""
    try:
        from src.strategy.entry import EntryDetector
        ed = EntryDetector()
        h4 = ed.oanda.load_parquet("H4")
        h4_sma_p = ed.cfg["strategy"]["trend"]["h4_sma_period"]
        h4 = ed.ind.add_sma(h4, h4_sma_p)
        slope = ed.ind.calculate_slope(h4[f"SMA{h4_sma_p}"], 3)
        threshold = float(ed.cfg["strategy"]["entry"].get("slope_momentum", 0.0003))
        slope_data = {"h4_sma_slope": round(slope, 8), "threshold": threshold, "momentum_ok": abs(slope) >= threshold}
        m15 = ed._prepare("M15")
        pattern_bull_m15 = ed._detect_bullish_pattern(m15) if m15 is not None else ""
        pattern_bear_m15 = ed._detect_bearish_pattern(m15) if m15 is not None else ""
        return jsonify({"success": True, "data": {"slope_momentum": slope_data, "patterns": {"m15_bullish": pattern_bull_m15, "m15_bearish": pattern_bear_m15}}})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/llm-sentiment")
def api_llm_sentiment():
    """触发 Claude LLM 新闻情绪分析"""
    try:
        from src.analysis.llm_sentiment import LLMSentimentAnalyzer
        analyzer = LLMSentimentAnalyzer()

        # 先检查今天是否已有结果
        existing = analyzer.get_today_result()
        if existing:
            return jsonify({"success": True, "data": existing, "cached": True})

        # 执行分析
        result = analyzer.analyze()
        return jsonify({"success": True, "data": result, "cached": False})
    except Exception as e:
        logger.error(f"LLM sentiment error: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/llm-sentiment/status")
def api_llm_sentiment_status():
    """检查今日是否已有 LLM 分析结果"""
    try:
        from src.analysis.llm_sentiment import LLMSentimentAnalyzer
        analyzer = LLMSentimentAnalyzer()
        existing = analyzer.get_today_result()
        return jsonify({
            "success": True,
            "data": {"available": existing is not None, "result": existing},
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/settings/llm", methods=["GET", "POST"])
def api_settings_llm():
    """获取或更新 LLM CLI 设置"""
    from src.utils.dashboard_settings import load, save

    if request.method == "POST":
        try:
            data = request.get_json(force=True)
            current = load()
            if "llm" not in current:
                current["llm"] = {}
            if "cli_command" in data:
                current["llm"]["cli_command"] = data["cli_command"]
            if "cli_args" in data:
                current["llm"]["cli_args"] = data["cli_args"]
            save(current)
            return jsonify({"success": True, "data": current["llm"]})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
    else:
        settings = load()
        return jsonify({"success": True, "data": settings.get("llm", {})})


@app.route("/api/fundamentals")
def api_fundamentals():
    """获取基本面摘要 (支持 ?symbol=EUR/USD|XAU/USD|AUD/USD, 默认 EUR/USD)"""
    try:
        symbol = request.args.get("symbol", "EUR/USD")
        if symbol not in ("EUR/USD", "XAU/USD", "AUD/USD"):
            symbol = "EUR/USD"
        fa = FundamentalsAnalyzer()
        summary = fa.generate_summary(7, symbol=symbol)
        return jsonify({"success": True, "data": {"summary": summary, "symbol": symbol}})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/calendar")
def api_calendar():
    """获取经济日历 (支持 ?symbol=EUR/USD|XAU/USD|AUD/USD, 默认 EUR/USD; 优先线上, 降级到本地缓存)"""
    try:
        symbol = request.args.get("symbol", "EUR/USD")
        if symbol not in ("EUR/USD", "XAU/USD", "AUD/USD"):
            symbol = "EUR/USD"
        cal = EconomicCalendar()
        events = cal.fetch()
        # 线上数据没有该品种事件时，降级到本地缓存
        filtered = cal.get_high_impact(events, symbol)
        if not filtered:
            import json
            from pathlib import Path
            cal_dir = Path(__file__).resolve().parent.parent.parent / "data" / "calendar"
            for f in sorted(cal_dir.glob("calendar_*.json"), reverse=True):
                with open(f) as fh:
                    cached = json.load(fh)
                if not isinstance(cached, list):
                    continue
                check = cal.get_high_impact(cached, symbol)
                if check:
                    events = cached
                    filtered = check
                    break
        by_date: Dict[str, list] = {}
        for e in filtered:
            d = e.get("date", "")
            if d not in by_date:
                by_date[d] = []
            by_date[d].append(e)
        return jsonify({
            "success": True,
            "data": {
                "total": len(events),
                "symbol": symbol,
                "count": len(filtered),
                "high_impact": len([e for e in filtered if e.get("impact","").lower() == "high"]),
                "by_date": by_date,
            }
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/trend")
def api_trend():
    """获取大势判断详情"""
    try:
        t = TrendAnalyzer()
        r = t.analyze()
        return jsonify({
            "success": True,
            "data": {
                "d1": r.get("D1", {}),
                "h4": r.get("H4", {}),
                "overall": r.get("overall", ""),
                "can_trade": r.get("can_trade", False),
                "reason": r.get("reason", ""),
            }
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/history")
def api_history():
    """获取近期信号历史统计（含明细和按日统计）"""
    try:
        from src.daemon.history import SignalHistory
        from datetime import timezone, timedelta
        sh_tz = timezone(timedelta(hours=8))
        days = 7
        records = SignalHistory().load_recent(days)
        summary = SignalHistory().summary(days)

        # 今日(北京时区)独立统计 — 供"今日信号"卡片, 避免混入 7 天历史
        today = datetime.now(sh_tz).strftime("%Y-%m-%d")
        try:
            today_recs = [r for r in records
                          if datetime.fromisoformat(r.get("recorded_at", "")).astimezone(sh_tz).strftime("%Y-%m-%d") == today]
        except Exception:
            today_recs = []
        summary["today_total"] = len(today_recs)
        summary["today_trade_signals"] = sum(1 for r in today_recs if r.get("can_trade", False))

        return jsonify({
            "success": True,
            "data": {
                "summary": summary,
                "records": records[-20:],  # 最近 20 条
                "daily_counts": SignalHistory().get_daily_counts(30),
            }
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/review")
def api_review():
    """获取复盘报告"""
    try:
        from src.strategy.review import ReviewEngine
        engine = ReviewEngine()
        report = engine.generate_review(days=7)
        return jsonify({"success": True, "data": {"report": report}})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/refresh")
def api_refresh():
    """刷新数据 — 与统一调度同机制(三品种 OANDA主+TwelveData备+专用代理), 并完成一轮评估"""
    try:
        # 延迟导入避免循环依赖(dashboard 被 src.daemon 的 create_app 加载)
        from src.daemon import unified_tick
        unified_tick()
        return jsonify({
            "success": True,
            "data": {
                "message": "三品种数据已刷新并完成评估(下单/平仓/反转)",
                "time": datetime.now(timezone.utc).isoformat(),
            }
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/status")
def api_status():
    """系统状态"""
    try:
        data_dir = Path(__file__).resolve().parent.parent.parent / "data"
        forex_files = list((data_dir / "forex").rglob("*.parquet"))
        news_files = list((data_dir / "news").rglob("*.csv"))
        cal_files = list((data_dir / "calendar").rglob("*.json"))
        fund_files = list((data_dir / "fundamentals").rglob("*.json"))

        return jsonify({
            "success": True,
            "data": {
                "oanda": f"{len(forex_files)} 个文件",
                "news": f"{len(news_files)} 个文件",
                "calendar": f"{len(cal_files)} 个文件",
                "fundamentals": f"{len(fund_files)} 个文件",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── 交易日志（手动标注）──

@app.route("/api/journal/signals")
def api_journal_signals():
    """获取所有已推送的交易信号（含标注状态）"""
    try:
        from src.web.trade_journal import load_signal_files, merge_signals_with_journal
        signals = load_signal_files(days=60)
        merged = merge_signals_with_journal(signals)
        return jsonify({"success": True, "data": merged})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/journal/annotate", methods=["POST"])
def api_journal_annotate():
    """手动标注一条交易信号"""
    try:
        from src.web.trade_journal import update_annotation
        data = request.get_json(force=True)
        signal_file = data.get("signal_file", "")
        fields = {k: v for k, v in data.items() if k != "signal_file"}
        result = update_annotation(signal_file, fields)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/journal/summary")
def api_journal_summary():
    """交易日志统计"""
    try:
        from src.web.trade_journal import get_summary
        return jsonify({"success": True, "data": get_summary()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── 黄金设置 ──

@app.route("/api/settings/gold", methods=["GET", "POST"])
def api_settings_gold():
    """获取或更新黄金配置"""
    from src.utils.dashboard_settings import load, save

    if request.method == "POST":
        try:
            data = request.get_json(force=True)
            current = load()
            if "gold" not in current:
                current["gold"] = {}
            for key in ["enabled", "max_open", "auto_trade", "sl_atr_mult", "tp_usd", "rsi",
                        "lots",
                        "fuse_enabled", "fuse_max_loss_streak", "fuse_cooldown_hours",
                        "be_trigger_usd", "be_lock_buffer_points"]:
                if key in data:
                    current["gold"][key] = data[key]
            save(current)
            return jsonify({"success": True, "data": current["gold"]})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
    else:
        from src.utils.dashboard_settings import load
        settings = load()
        return jsonify({"success": True, "data": settings.get("gold", {})})


# ── 澳元设置 ──

@app.route("/api/settings/aud", methods=["GET", "POST"])
def api_settings_aud():
    """获取或更新澳元配置"""
    from src.utils.dashboard_settings import load, save

    if request.method == "POST":
        try:
            data = request.get_json(force=True)
            current = load()
            if "aud" not in current:
                current["aud"] = {}
            for key in ["enabled", "max_open", "auto_trade", "sl_atr_mult", "tp_pips",
                        "lots",
                        "fuse_enabled", "fuse_max_loss_streak", "fuse_cooldown_hours",
                        "flat_range", "dedup_pips"]:
                if key in data:
                    current["aud"][key] = data[key]
            save(current)
            return jsonify({"success": True, "data": current["aud"]})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
    else:
        from src.utils.dashboard_settings import load
        settings = load()
        return jsonify({"success": True, "data": settings.get("aud", {})})


# ── MT4 远程交易设置 ──

@app.route("/api/settings/mt4", methods=["GET", "POST"])
def api_settings_mt4():
    """获取或更新 MT4 中继服务器设置"""
    from src.utils.dashboard_settings import load, save

    if request.method == "POST":
        try:
            data = request.get_json(force=True)
            current = load()
            if "mt4_relay" not in current:
                current["mt4_relay"] = {}
            for key in ["url", "enabled"]:
                if key in data:
                    current["mt4_relay"][key] = data[key]
            save(current)
            return jsonify({"success": True, "data": current["mt4_relay"]})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
    else:
        settings = load()
        return jsonify({"success": True, "data": settings.get("mt4_relay", {})})


@app.route("/api/settings/proxy", methods=["GET", "POST"])
def api_settings_proxy():
    """获取/更新 OANDA 专用代理设置 (vless链接 + 使用时机 + 端口)

    POST 保存后自动启停 sing-box 进程:
      mode=auto/always → 启动专用代理; mode=off → 停止
    """
    from src.utils.dashboard_settings import load, save
    from src.web.proxy_manager import ProxyManager, DEFAULT_PORT

    if request.method == "POST":
        try:
            data = request.get_json(force=True)
            current = load()
            if "proxy" not in current:
                current["proxy"] = {}
            for key in ["mode", "vless_url", "port"]:
                if key in data:
                    current["proxy"][key] = data[key]
            save(current)

            # 按模式启停专用代理进程
            p = current["proxy"]
            pm = ProxyManager()
            mode = p.get("mode", "off")
            port = int(p.get("port", DEFAULT_PORT))
            if mode in ("auto", "always") and p.get("vless_url"):
                proc_result = pm.start(p["vless_url"], port)
            else:
                proc_result = pm.stop()
            return jsonify({"success": True, "data": p, "process": proc_result})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
    else:
        settings = load()
        from src.web.proxy_manager import ProxyManager
        return jsonify({
            "success": True,
            "data": settings.get("proxy", {}),
            "process": ProxyManager().status(),
        })


@app.route("/api/proxy/test", methods=["POST"])
def api_proxy_test():
    """测试专用代理连通性 (通用网络 + OANDA 可达)"""
    from src.web.proxy_manager import ProxyManager
    result = ProxyManager().test_connectivity()
    return jsonify({"success": True, **result})


@app.route("/api/settings/mt4/test", methods=["POST"])
def api_settings_mt4_test():
    """测试 MT4 中继服务器连通性"""
    data = request.get_json(force=True) if request.data else {}
    url = data.get("url", "").rstrip("/")
    if not url:
        return jsonify({"success": False, "error": "请先设置 MT4 中继地址"})

    import socket
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname
    http_port = parsed.port or 8080

    result = {"http": False, "tcp": False, "ea_connected": False}

    # HTTP 测试
    try:
        import httpx
        from src.execution.mt4_remote import relay_headers
        resp = httpx.get(f"{url}/health", timeout=5, headers=relay_headers())
        if resp.status_code == 200:
            data = resp.json()
            result["http"] = True
            result["ea_connected"] = data.get("ea_connected", False)
    except Exception as e:
        result["http_error"] = str(e)[:100]

    # TCP 测试 (默认 9090)
    try:
        s = socket.socket()
        s.settimeout(5)
        s.connect((host, 9090))
        s.close()
        result["tcp"] = True
    except Exception as e:
        result["tcp_error"] = str(e)[:100]

    return jsonify({"success": result["http"], "data": result})


# ── 交易设置: EURUSD 手数 ──

@app.route("/api/settings/trade", methods=["GET", "POST"])
def api_settings_trade():
    """获取/更新 EURUSD 每次交易手数 (account.default_lots)"""
    from src.utils.dashboard_settings import load, save
    from src.utils.config_loader import config
    yaml_cfg = config.load().get("account", {})

    if request.method == "POST":
        data = request.get_json(force=True)
        current = load()
        if "trade" not in current:
            current["trade"] = {}
        if "default_lots" in data:
            current["trade"]["default_lots"] = float(data["default_lots"])
        if "max_same_dir" in data:
            current["trade"]["max_same_dir"] = int(data["max_same_dir"])
        save(current)
        return jsonify({"success": True, "data": current["trade"]})

    ui = load().get("trade", {})
    return jsonify({"success": True, "data": {
        "default_lots": ui.get("default_lots", yaml_cfg.get("default_lots", 0.02)),
        "max_same_dir": ui.get("max_same_dir",
                               config.load().get("strategy", {}).get("max_same_dir", 3)),
    }})


# ── 交易设置: EURUSD RSI 过滤器 ──

@app.route("/api/settings/eurusd", methods=["GET", "POST"])
def api_settings_eurusd():
    """获取/更新 EURUSD RSI 过滤器开关与窗口 (默认关闭=原始设计)"""
    from src.utils.dashboard_settings import load, save
    from src.utils.config_loader import config
    yaml_cfg = config.load().get("strategy", {}).get("entry", {}).get("rsi", {})

    if request.method == "POST":
        data = request.get_json(force=True)
        current = load()
        if "eurusd" not in current:
            current["eurusd"] = {}
        for key in ["rsi_enabled", "rsi_lo", "rsi_hi"]:
            if key in data:
                current["eurusd"][key] = data[key]
        save(current)
        return jsonify({"success": True, "data": current["eurusd"]})

    ui = load().get("eurusd", {})
    return jsonify({"success": True, "data": {
        "rsi_enabled": ui.get("rsi_enabled", yaml_cfg.get("enabled", False)),
        "rsi_lo": ui.get("rsi_lo", yaml_cfg.get("lo", 30)),
        "rsi_hi": ui.get("rsi_hi", yaml_cfg.get("hi", 60)),
    }})


# ── 交易设置: 锁利追踪 ──

@app.route("/api/settings/trail", methods=["GET", "POST"])
def api_settings_trail():
    """获取/更新锁利追踪 (trail)"""
    from src.utils.dashboard_settings import load, save
    from src.utils.config_loader import config
    yaml_cfg = config.load().get("trail", {})

    if request.method == "POST":
        data = request.get_json(force=True)
        current = load()
        if "trail" not in current:
            current["trail"] = {}
        for key in ["enabled", "trigger", "lock_ratio", "tp_mult"]:
            if key in data:
                current["trail"][key] = data[key]
        save(current)
        return jsonify({"success": True, "data": current["trail"]})

    ui = load().get("trail", {})
    return jsonify({"success": True, "data": {**yaml_cfg, **ui}})


# ── 交易设置: 反转自动平仓 (出场模块) ──

@app.route("/api/settings/exit", methods=["GET", "POST"])
def api_settings_exit():
    """获取/更新反转自动平仓设置 (strategy.exit)"""
    from src.utils.dashboard_settings import load, save
    from src.utils.config_loader import config
    yaml_cfg = config.load().get("strategy", {}).get("exit", {})

    if request.method == "POST":
        data = request.get_json(force=True)
        current = load()
        if "exit" not in current:
            current["exit"] = {}
        for key in ["reversal_auto_close", "min_profit_pips", "reversal_tf",
                    "fuse_enabled", "fuse_max_loss_streak", "fuse_cooldown_hours"]:
            if key in data:
                current["exit"][key] = data[key]
        save(current)
        return jsonify({"success": True, "data": current["exit"]})

    ui = load().get("exit", {})
    return jsonify({"success": True, "data": {**yaml_cfg, **ui}})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("DASHBOARD_PORT", 5001))
    debug = os.environ.get("DASHBOARD_DEBUG", "").lower() in ("1", "true", "yes")
    print(f"🌐 中控面板启动: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=debug)
