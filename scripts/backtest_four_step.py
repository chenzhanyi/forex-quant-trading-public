#!/usr/bin/env python3
"""6条件叠加法回测 (H4 200SMA + EMA + K线 + RSI + 欧美盘 + ATR止损)

用法:
    python scripts/backtest_four_step.py --days 30
    python scripts/backtest_four_step.py --days 60 --json
    python scripts/backtest_four_step.py --days 30 --session=15,23     # 默认欧美盘
    python scripts/backtest_four_step.py --days 30 --session=15,5      # 加凌晨纽约尾盘(跨午夜)
    python scripts/backtest_four_step.py --days 30 --session=0,23      # 全天不限时段
    python scripts/backtest_four_step.py --days 30 --rr=2.0            # R:R 门槛 > 2 (默认 1.0)
    python scripts/backtest_four_step.py --days 30 --tp=80             # 止盈 pips (默认 40)

时段为北京时间小时,支持跨午夜(start>end 表示次日凌晨)。
"""
import sys, json, pandas as pd
sys.path.insert(0, '.')
from datetime import timezone, timedelta
from src.analysis.indicators import Indicators
from src.data_collection.oanda import OandaClient
from src.utils.config_loader import config

BACKTEST_SYMBOL = None  # 回测品种(如 AUD_USD), None=配置默认(EUR_USD)
# pip 价格换算: EUR/AUD类=10000, JPY类=100 (USDJPY 1pip=0.01)
PIP_SCALE = 10000

ind = Indicators(); o = OandaClient()

# 配置
TP_PIPS = 40
SL_ATR_MULT = 2.5
RSI_LO, RSI_HI = 30, 60
SESSION_START, SESSION_END = 15, 23
RR_MIN = 1.0   # R:R 门槛 (可 --rr 覆盖)

# 简单锁利追踪 (可 --trail 启用, 否则固定止盈)
TRAIL_TRIGGER = 0.0   # 浮盈达到止盈的 40%~100% 触发 (如 0.6=60%)
LOCK_PIPS = 4.0       # 触发后止损移到赚 N 点 (锁定利润)
LOCK_RATIO = 0.0      # 或按止盈比例锁利 (如 0.8 = 锁在止盈的80%)
TP_MULT = 2.0         # 触发后止盈翻倍
TRAIL_BARS = 480      # 追踪窗口 (5天, TP翻倍需要更长观察)

# 可选过滤 (0=关闭, 见 --dedup / --flat / --bear)
DEDUP_PIPS = 0.0      # 重复价位: 距48h内上一个同向信号 <N pips → 跳过(防同价位扎堆开仓)
FLAT_RANGE = 0.0      # 横盘区间: 近24根H4区间 <N pips → 跳过
BEAR_BAR = 0.0        # 大阴/大阳动能: 近6根H4出现 >N pips 反向实体 → 跳过

# 趋势质量过滤 (1=开启, 见 --struct / --h1guard)
STRUCT_GUARD = 0      # 结构确认: 近12根H4低点低于前12根低点(做多, LL结构破坏) → 跳过
H1_GUARD = 0          # H1 200SMA哨兵: H1周期收盘方向与开仓方向分歧 → 跳过(观望)

# 降级模式: 趋势转弱(结构破坏或H1分歧)时不拦信号, 降级处理 (见 --degrade)
#   half_lot = 手数减半(pip×0.5)   tight_sl = 止损收紧(ATR×2.5→1.5)
#   both = 两者同时
DEGRADE_MODE = ""     # ""=关闭
SL_TIGHT_MULT = 1.5   # 降级单的 ATR 止损倍数

# 同方向最大同时持仓(0=不限) — 模拟实盘限单机制, 超限信号跳过 (见 --maxpos)
MAX_POS = 0

# 确认K线(--confirm-bar=1): 形态出现后的第二根同向K线确认后才进场 —
# 看多需第二根阳线(close>open), 看空需第二根阴线; 入场价=确认bar收盘价
CONFIRM_BAR = 0

# 检测频率(--detect-every=N): 每N根M15检测一次 — 模拟实盘轮询间隔
# 1=逐bar(等效5分钟级实时检测/收盘即检) 3=每3根(等效15分钟轮询)
DETECT_EVERY = 1

# 结算窗口(M15根数): 实盘持仓不限时长, 持有到 SL/TP/反转触发 —
# 窗口过短会把"仍在持仓的单"按截断收盘价结算, 与实盘结果偏差大(低波动品种尤其)
# 默认192(48h)保持历史口径; 建议长窗口回测用 --maxbars=960(10天)/1920(20天)
SETTLE_BARS = 192

# 反转出场模块 (--exit-rev=H4/H1/M15 或组合, 与实盘出场模块同款双重确认)
# 反转形态 + 收盘突破该周期200SMA + 浮盈>=min → 按当前收盘价平仓
EXIT_REV_TF = ""              # 检测周期: ""=关闭, "H4"/"H1"/"M15"/"H4+H1"/"H4+M15"/"H1+M15"/"H4+H1+M15"
EXIT_REV_MIN_PROFIT = 0.0     # 最小浮盈(pips), 0=只要浮盈就平
EXIT_REV_CONFIRM = 0          # 连续确认根数(该周期自身K线): 0=1根即触发, 1=需连续2根
FUSE_MAX_LOSS_STREAK = 0      # 熔断: 同方向连续SL N次后暂停 FUSE_HOURS(0=关闭)
FUSE_HOURS = 48               # 熔断冷却小时


def _in_session(hour, start, end):
    """北京时间小时是否在 [start,end] 窗口内,支持跨午夜(start>end=次日凌晨)"""
    if start <= end:
        return start <= hour <= end
    return hour >= start or hour <= end


def detect_reversal_any(df, direction):
    """反转形态检测 — 与实盘 position_manager._detect_h4_*_reversal 同款

    direction: 持仓方向。SELL持仓→查多头反转(启明星/锤子/阳吞阴/看涨孕线);
               BUY持仓→查空头反转(黄昏星/射击之星/阴吞阳/看跌孕线)
    """
    w = df.iloc[-4:] if len(df) >= 4 else df
    if direction == "SELL":
        if ind.is_morning_star(w):
            return "启明星"
        if ind.is_bullish_reversal(w):
            last = w.iloc[-1]
            cl, op = float(last["close"]), float(last["open"])
            lower = min(op, cl) - float(last["low"])
            body = abs(cl - op)
            if lower >= body * 2 and cl > op:
                return "锤子线"
        if len(w) >= 2:
            pv, ls = w.iloc[-2], w.iloc[-1]
            if (float(ls["close"]) > float(ls["open"]) and
                float(pv["close"]) < float(pv["open"]) and
                float(ls["close"]) > float(pv["open"]) and
                float(ls["open"]) < float(pv["close"])):
                return "阳吞阴"
        if ind.is_bullish_harami(w):
            return "看涨孕线"
    else:
        if ind.is_evening_star(w):
            return "黄昏星"
        if ind.is_bearish_reversal(w):
            last = w.iloc[-1]
            cl, op = float(last["close"]), float(last["open"])
            upper = float(last["high"]) - max(op, cl)
            body = abs(cl - op)
            if upper >= body * 2 and cl < op:
                return "射击之星"
        if len(w) >= 2:
            pv, ls = w.iloc[-2], w.iloc[-1]
            if (float(ls["close"]) < float(ls["open"]) and
                float(pv["close"]) > float(pv["open"]) and
                float(ls["close"]) < float(pv["open"]) and
                float(ls["open"]) > float(pv["close"])):
                return "阴吞阳"
        if ind.is_bearish_harami(w):
            return "看跌孕线"
    return ""


def sim_reversal_exit(m15, tf_map, i, direction, entry, sl, tp,
                      rev_tfs, min_profit_pips, confirm, max_bars=192):
    """反转出场模拟 — 逐 M15 bar: SL/TP 优先, 再查反转(浮盈>=min → 按收盘平)

    rev_tfs: 周期列表(单周期或多时段共振, 全部满足才触发)
    confirm: 每个周期需连续 N+1 根自身K线确认(0=1根即触发)
    返回 (oc, pip, exit_time): oc ∈ SL/TP/REV/OPEN, exit_time=平仓bar时间(限单模拟用)
    """
    is_sell = direction == "SELL"
    # 每周期状态: {pos: 上次检测的周期bar位置, count: 连续确认数, ok: 当前满足}
    state = {tf: {"pos": -1, "count": 0, "ok": False} for tf in rev_tfs}
    end = min(i + 1 + max_bars, len(m15))
    for j in range(i + 1, end):
        b = m15.iloc[j]
        h, l, c = float(b['high']), float(b['low']), float(b['close'])
        dt = b.name

        # SL/TP 优先(与固定止盈分支同规则: SL 先判)
        if is_sell:
            if h >= sl:
                return "SL", round((entry - sl) * PIP_SCALE, 1), dt
            if l <= tp:
                return "TP", round((entry - tp) * PIP_SCALE, 1), dt
        else:
            if l <= sl:
                return "SL", round((sl - entry) * PIP_SCALE, 1), dt
            if h >= tp:
                return "TP", round((tp - entry) * PIP_SCALE, 1), dt

        # 各周期反转检测(仅在该周期出现新K线时重新检测)
        all_ok = True
        for tf in rev_tfs:
            st = state[tf]
            tfdf = tf_map[tf]
            pos = int(tfdf.index.searchsorted(dt, side="right")) - 1
            if pos != st["pos"]:
                st["pos"] = pos
                if pos < 200:
                    st["ok"] = False
                    st["count"] = 0
                else:
                    sma = float(tfdf.iloc[pos]['SMA200'])
                    c_tf = float(tfdf.iloc[pos]['close'])
                    broken = (c_tf > sma) if is_sell else (c_tf < sma)
                    pat = detect_reversal_any(tfdf.iloc[:pos + 1], direction) if broken else ""
                    if broken and pat:
                        st["count"] += 1
                        st["ok"] = st["count"] > confirm
                    else:
                        st["count"] = 0
                        st["ok"] = False
            if not st["ok"]:
                all_ok = False
        if not all_ok:
            continue

        # 浮盈判定
        float_pips = (entry - c) * PIP_SCALE if is_sell else (c - entry) * PIP_SCALE
        if float_pips < min_profit_pips:
            continue
        return "REV", round(float_pips, 1), dt

    # 窗口走完未出场 → 按最新收盘结算(OPEN)
    last_c = float(m15.iloc[end - 1]['close'])
    pips = (entry - last_c) * PIP_SCALE if is_sell else (last_c - entry) * PIP_SCALE
    return "OPEN", round(pips, 1), m15.iloc[end - 1].name


def sim_simple_trail(m15, i, direction, entry, sl, tp, trigger, lock_pips, tp_mult, max_bars):
    """简单锁利追踪:
      浮盈达止盈的 trigger 比例 → 止损移到赚 lock_pips 点, 止盈翻倍(tp_mult)
    返回 (oc, pip, exit_time): oc ∈ SL/TP/OPEN, exit_time=平仓bar时间(限单模拟用)
    """
    cur_sl, cur_tp = sl, tp
    trailed = False
    tp_move = abs(tp - entry)          # 价格单位(如 0.004 = 40pips)
    end = min(i + 1 + max_bars, len(m15))

    for j in range(i + 1, end):
        b = m15.iloc[j]
        h, l, c = float(b['high']), float(b['low']), float(b['close'])

        if direction == "BUY":
            if l <= cur_sl:
                return "SL", round((cur_sl - entry) * PIP_SCALE, 1), b.name
            if h >= cur_tp:
                return "TP", round((cur_tp - entry) * PIP_SCALE, 1), b.name
            if not trailed and c - entry >= tp_move * trigger:
                trailed = True
                cur_sl = entry + lock_pips / PIP_SCALE
                cur_tp = entry + tp_move * tp_mult
        else:
            if h >= cur_sl:
                return "SL", round((entry - cur_sl) * PIP_SCALE, 1), b.name
            if l <= cur_tp:
                return "TP", round((entry - cur_tp) * PIP_SCALE, 1), b.name
            if not trailed and entry - c >= tp_move * trigger:
                trailed = True
                cur_sl = entry - lock_pips / PIP_SCALE
                cur_tp = entry - tp_move * tp_mult

    # 窗口走完未出场 → 按最新收盘结算(记为 OPEN, 不计入胜负)
    last_c = float(m15.iloc[end - 1]['close'])
    pips = (last_c - entry) * PIP_SCALE if direction == "BUY" else (entry - last_c) * PIP_SCALE
    return "OPEN", round(pips, 1), m15.iloc[end - 1].name


def load_data(days, symbol=None):
    """加载回测数据: 本地 parquet 优先; 指定品种(如 AUD_USD)时从 OANDA 分页拉取

    OANDA count 上限 5000, M15 超 52 天需分页(from_time 向前翻页)。
    拉取走专用代理(未运行/未配置时自动退回默认通道)。
    """
    sym = symbol or o.symbol
    cutoff = pd.Timestamp.now(tz='UTC') - timedelta(days=days)

    def fetch_tf(tf, minutes, warmup_days=0):
        """warmup_days: 数据起点往前多拉, 保证回测起点处指标(SMA200/ATR14)已预热"""
        start = cutoff - timedelta(days=warmup_days)
        # 本地优先(该品种 parquet 已存在时直接读, 避免重复拉取)
        try:
            fpath = o.data_dir / tf / f"{sym}_{tf}.parquet"
            if fpath.exists():
                df = pd.read_parquet(fpath)
                if not df.empty:
                    return df[df.index >= start]
        except Exception:
            pass
        # OANDA 分页拉取
        client = OandaClient(use_proxy=True)
        candles = []
        cur_from = start.to_pydatetime()
        while True:
            batch = client.fetch_candles(tf, 5000, from_time=cur_from, symbol=sym)
            if not batch:
                break
            candles.extend(batch)
            if len(batch) < 5000:
                break
            cur_from = pd.Timestamp(batch[-1]["time"]).to_pydatetime() + timedelta(minutes=minutes)
        if not candles:
            raise RuntimeError(f"{sym} {tf} 无数据")
        df = pd.DataFrame(candles)
        df["time"] = pd.to_datetime(df["time"])
        df.set_index("time", inplace=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df[~df.index.duplicated(keep="last")].sort_index()

    # H1/H4 预热60天(SMA200/ATR14); M15 预热2天(EMA/RSI), 裁回 cutoff 保证回测起点对齐
    m15 = fetch_tf("M15", 15, warmup_days=2)
    m15 = m15[m15.index >= cutoff]
    h1 = fetch_tf("H1", 60, warmup_days=60)
    h4 = fetch_tf("H4", 240, warmup_days=60)
    print(f"[数据] {sym} M15 {len(m15)}根 / H1 {len(h1)}根 / H4 {len(h4)}根")
    return m15, h1, h4


def run(m15, h1, h4, session_filter=True):
    h4 = ind.add_sma(h4, 200); h4 = ind.add_atr(h4, 14)
    m15 = ind.add_ema(m15, 5); m15 = ind.add_ema(m15, 15); m15 = ind.add_rsi(m15, 14)
    # 反转出场/H1哨兵用: 各周期 200SMA 预计算(检测时只做时间对齐切片)
    tf_map = {}
    h1_s = ind.add_sma(h1, 200) if (EXIT_REV_TF or H1_GUARD) else None
    m15_s = None
    if EXIT_REV_TF:
        m15_s = ind.add_sma(m15, 200)
        tf_map = {"H4": h4, "H1": h1_s, "M15": m15_s}
        rev_tfs = [t.strip() for t in EXIT_REV_TF.split("+") if t.strip()]
    """测试H4 200SMA方向: 只做顺势"""
    trades = []

    # 可选过滤: 重复价位/横盘区间/大阴动能 (0=关闭)
    last_entry = {}  # {direction: (ts, price)} 上一个未被过滤的同向信号
    loss_streak = {"SELL": 0, "BUY": 0}
    paused_until = {"SELL": None, "BUY": None}
    active_until = {"SELL": [], "BUY": []}  # 各方向在持仓的平仓时间(限单模拟)

    for i in range(20, len(m15)):
        # 检测频率模拟: 每DETECT_EVERY根检测一次(实盘轮询间隔的离散化)
        if DETECT_EVERY > 1 and i % DETECT_EVERY != 0:
            continue
        bar = m15.iloc[i]; p = float(bar['close']); dt = bar.name

        h4b = h4[h4.index <= dt]
        if len(h4b) == 0: continue
        h4_sma = float(h4b.iloc[-1]['SMA200'])
        h4_atr = float(h4b.iloc[-1].get('ATR', 0.002))
        # 挡 0/nan/负值 — 指标预热不足时跳过(防 nan 止损导致永不结算的幽灵持仓)
        if not (h4_atr > 0): continue
        if not (h4_sma > 0): continue

        # ── 横盘判定(近12根H4区间 < FLAT_RANGE): 与重复价位过滤联动 ──
        # 横盘期禁止同价位扎堆开仓; 趋势期允许顺势加仓
        flat_now = False
        if FLAT_RANGE > 0 and len(h4b) >= 12:
            w12 = h4b.iloc[-12:]
            flat_now = (w12['high'].max() - w12['low'].min()) * PIP_SCALE < FLAT_RANGE
        # ── 大阴/大阳动能过滤: 近6根H4内出现 >N pips 大阴(做多)/大阳(做空) → 跳过 ──
        bear_body = 0.0
        bull_body = 0.0
        if BEAR_BAR > 0 and len(h4b) >= 6:
            w6 = h4b.iloc[-6:]
            bear_body = float((w6['open'] - w6['close']).clip(lower=0).max() * PIP_SCALE)
            bull_body = float((w6['close'] - w6['open']).clip(lower=0).max() * PIP_SCALE)

        e5 = float(bar['EMA5']); e15 = float(bar['EMA15'])
        rsi = float(bar['RSI14'])
        hour_cst = (dt.hour + 8) % 24

        for direction, is_sell in [("SELL", True), ("BUY", False)]:
            # 熔断: 同方向连续SL后暂停该方向
            if FUSE_MAX_LOSS_STREAK > 0:
                pu = paused_until.get(direction)
                if pu is not None:
                    if dt <= pu:
                        continue
                    paused_until[direction] = None
                    loss_streak[direction] = 0
            # 确认K线模式: 形态在 bar i-1 及以前、bar i 同向确认(多=阳/空=阴)才进场
            # 评估时点 = bar i 收盘后(与实盘检测器一致: 倒数第二根形态+最后根确认);
            # SMA/EMA/RSI 用形态bar(i-1); 入场价/SL/TP/时段用确认bar(i);
            # DETECT_EVERY=3 时 i 仅取3的倍数 = 15分钟轮询模拟
            skip_std = False
            if CONFIRM_BAR:
                if i < 1:
                    continue
                cbar = m15.iloc[i]          # 确认bar = 评估时点最后一根已收盘
                sig_bar = m15.iloc[i - 1]   # 形态bar = 倒数第二根
                c_open, c_close = float(cbar['open']), float(cbar['close'])
                if (not is_sell and c_close <= c_open) or (is_sell and c_close >= c_open):
                    continue  # 确认bar不是同向K线 → 信号作废
                # 形态检测(窗口 i-6..i-1, 末根=i-1)
                w2 = m15.iloc[max(0, i - 6):i]
                pat = False
                if is_sell:
                    pat = (ind.is_bearish_reversal(w2) or ind.is_evening_star(w2) or
                           ind.is_bearish_harami(w2) or ind.is_decisive_bearish(w2))
                    if not pat and len(w2) >= 2:
                        pv, ls = w2.iloc[-2], w2.iloc[-1]
                        pat = (float(ls['close']) < float(ls['open']) and
                               float(pv['close']) > float(pv['open']) and
                               float(ls['close']) < float(pv['open']))
                else:
                    pat = (ind.is_bullish_reversal(w2) or ind.is_morning_star(w2) or
                           ind.is_bullish_harami(w2) or ind.is_decisive_bullish(w2))
                    if not pat and len(w2) >= 2:
                        pv, ls = w2.iloc[-2], w2.iloc[-1]
                        pat = (float(ls['close']) > float(ls['open']) and
                               float(pv['close']) < float(pv['open']) and
                               float(ls['close']) > float(pv['open']))
                if not pat:
                    continue
                # 趋势/EMA/RSI(形态bar值, 与实盘一致)
                if is_sell and float(sig_bar['close']) >= h4_sma: continue
                if not is_sell and float(sig_bar['close']) <= h4_sma: continue
                e5 = float(sig_bar['EMA5']); e15 = float(sig_bar['EMA15'])
                if is_sell and e5 >= e15: continue
                if not is_sell and e5 <= e15: continue
                rsi = float(sig_bar.get('RSI14', 50))
                if rsi < RSI_LO or rsi > RSI_HI: continue
                # 时段(确认bar)
                hour_cst = (dt.hour + 8) % 24
                if session_filter and not _in_session(hour_cst, SESSION_START, SESSION_END):
                    continue
                # 更新入场价/ATR(确认bar)
                p = c_close
                h4b = h4[h4.index <= dt]
                h4_atr = float(h4b.iloc[-1].get('ATR', 0))
                if not (h4_atr > 0):
                    continue
                skip_std = True  # 五项标准检查已在确认块完成, 跳过下方同款检查

            # 趋势过滤
            if not skip_std and is_sell and p >= h4_sma: continue
            if not skip_std and not is_sell and p <= h4_sma: continue

            # 趋势质量: 结构确认 + H1哨兵 — 默认拦截; --degrade 模式下降级放行
            # (H4说多但H1已转空 / 均线向上但低点结构破坏 = 下跌初段典型特征)
            degraded = False
            if H1_GUARD and h1_s is not None:
                h1b = h1_s[h1_s.index <= dt]
                # H1 数据不足时哨兵不生效(放行) — 实盘 H1 始终有数据, 回测早期不足不拦
                if len(h1b) > 0:
                    h1c = float(h1b.iloc[-1]['close'])
                    h1s = float(h1b.iloc[-1]['SMA200'])
                    if (not is_sell and h1c <= h1s) or (is_sell and h1c >= h1s):
                        if not DEGRADE_MODE:
                            continue
                        degraded = True

            if STRUCT_GUARD and len(h4b) >= 24:
                rec = h4b.iloc[-12:]; prev = h4b.iloc[-24:-12]
                if (not is_sell and float(rec['low'].min()) < float(prev['low'].min())) or \
                   (is_sell and float(rec['high'].max()) > float(prev['high'].max())):
                    if not DEGRADE_MODE:
                        continue
                    degraded = True

            # 大阴/大阳动能过滤
            if BEAR_BAR > 0 and not is_sell and bear_body > BEAR_BAR: continue
            if BEAR_BAR > 0 and is_sell and bull_body > BEAR_BAR: continue

            # 重复价位过滤: 横盘期(或无FLAT时全局)距上一个未被过滤的同向信号
            # < DEDUP pips(48h内) → 跳过。趋势期不拦, 允许顺势加仓(限单机制兜底)
            if DEDUP_PIPS > 0 and (flat_now or FLAT_RANGE <= 0):
                prev = last_entry.get(direction)
                if prev and (dt - prev[0]) <= pd.Timedelta(hours=48) and \
                   abs(p - prev[1]) * PIP_SCALE < DEDUP_PIPS:
                    continue

            # EMA (确认模式已用形态bar判定, 跳过)
            if not skip_std and is_sell and e5 >= e15: continue
            if not skip_std and not is_sell and e5 <= e15: continue

            # K线形态 — 与实盘 entry.py _detect_any_* 完全一致(含宽松吞没分支)
            if not skip_std:
                w = m15.iloc[max(0,i-5):i+1]
                if is_sell:
                    pat = (ind.is_bearish_reversal(w) or ind.is_evening_star(w) or
                           ind.is_bearish_harami(w) or ind.is_decisive_bearish(w))
                    if not pat and len(w) >= 2:
                        # 宽松"阴吞阳": 阴线 + prev阳线 + 收盘破prev开盘(实盘 entry.py 同款)
                        pv, ls = w.iloc[-2], w.iloc[-1]
                        pat = (float(ls['close']) < float(ls['open']) and
                               float(pv['close']) > float(pv['open']) and
                               float(ls['close']) < float(pv['open']))
                    if not pat: continue
                else:
                    pat = (ind.is_bullish_reversal(w) or ind.is_morning_star(w) or
                           ind.is_bullish_harami(w) or ind.is_decisive_bullish(w))
                    if not pat and len(w) >= 2:
                        # 宽松"阳吞阴": 阳线 + prev阴线 + 收盘破prev开盘(实盘 entry.py 同款)
                        pv, ls = w.iloc[-2], w.iloc[-1]
                        pat = (float(ls['close']) > float(ls['open']) and
                               float(pv['close']) < float(pv['open']) and
                               float(ls['close']) > float(pv['open']))
                    if not pat: continue

            # RSI
            if not skip_std and (rsi < RSI_LO or rsi > RSI_HI): continue

            # 时段
            if not skip_std and session_filter and not _in_session(hour_cst, SESSION_START, SESSION_END): continue

            # SL/TP (降级单: tight_sl/both 模式收紧止损)
            sl_mult = SL_ATR_MULT
            if degraded and DEGRADE_MODE in ("tight_sl", "both"):
                sl_mult = SL_TIGHT_MULT
            if is_sell:
                sl = p + h4_atr * sl_mult
                tp = p - TP_PIPS / PIP_SCALE
            else:
                sl = p - h4_atr * sl_mult
                tp = p + TP_PIPS / PIP_SCALE

            sl_pips = round(h4_atr * sl_mult * PIP_SCALE, 1)
            if TP_PIPS / sl_pips < RR_MIN: continue

            # 同方向持仓上限(模拟实盘限单机制, 0=不限): 超限信号跳过
            if MAX_POS > 0:
                active_until[direction] = [t for t in active_until[direction] if t > dt]
                if len(active_until[direction]) >= MAX_POS:
                    continue

            # 通过全部入场条件 → 记录为"上一个未被过滤的同向信号"(48h基准)
            if DEDUP_PIPS > 0:
                last_entry[direction] = (dt, p)

            # 结果（反转出场 / 简单锁利追踪 / 固定止盈）— 均返回 (oc, pip, exit_time)
            # 确认K线模式下入场在 bar i 收盘 → 结算从 i+1 起(确认bar高低点发生在入场前)
            settle_i = i
            if EXIT_REV_TF:
                oc, pip, exit_t = sim_reversal_exit(
                    m15, tf_map, settle_i, direction, p, sl, tp,
                    rev_tfs, EXIT_REV_MIN_PROFIT, EXIT_REV_CONFIRM, max_bars=SETTLE_BARS)
                resolved = oc != "OPEN"
            elif TRAIL_TRIGGER > 0:
                oc, pip, exit_t = sim_simple_trail(
                    m15, settle_i, direction, p, sl, tp,
                    TRAIL_TRIGGER, LOCK_PIPS, TP_MULT, TRAIL_BARS)
                resolved = oc in ("SL", "TP")
            else:
                after = m15.iloc[settle_i+1:settle_i+1+SETTLE_BARS]
                if len(after) < 50: continue
                # 逐bar模拟: SL 先判(与实盘同规则), 记录平仓时间
                oc, exit_t = "OPEN", after.index[-1]
                for _, b in after.iterrows():
                    if is_sell:
                        if float(b['high']) >= sl:
                            oc, exit_t = "SL", b.name
                            break
                        if float(b['low']) <= tp:
                            oc, exit_t = "TP", b.name
                            break
                    else:
                        if float(b['low']) <= sl:
                            oc, exit_t = "SL", b.name
                            break
                        if float(b['high']) >= tp:
                            oc, exit_t = "TP", b.name
                            break
                if oc == "TP":
                    pip = TP_PIPS
                elif oc == "SL":
                    pip = -sl_pips
                else:
                    # 未结算: 按窗口末收盘价计浮动盈亏 — 必须计入统计,
                    # 否则横盘慢单被静默丢弃导致胜率虚高(幸存者偏差)
                    last_c = float(after.iloc[-1]['close'])
                    pip = (last_c - p) * PIP_SCALE if not is_sell else (p - last_c) * PIP_SCALE
                resolved = oc != "OPEN"

            # 记录持仓占用(限单模拟)
            if MAX_POS > 0:
                active_until[direction].append(exit_t)

            # 降级单(half_lot/both): 手数减半 → pip 收益减半
            if degraded and DEGRADE_MODE in ("half_lot", "both"):
                pip = pip * 0.5

            # 熔断状态更新
            if FUSE_MAX_LOSS_STREAK > 0:
                if oc == "SL":
                    loss_streak[direction] += 1
                    if loss_streak[direction] >= FUSE_MAX_LOSS_STREAK:
                        paused_until[direction] = dt + pd.Timedelta(hours=FUSE_HOURS)
                        loss_streak[direction] = 0
                else:
                    loss_streak[direction] = 0

            trades.append({
                'time': str(dt)[:16], 'dir': direction,
                'entry': p, 'sl': sl, 'sl_p': sl_pips, 'tp': tp, 'tp_p': TP_PIPS,
                'rr': round(TP_PIPS/sl_pips, 1), 'oc': oc, 'pip': round(pip, 1),
                'deg': degraded,
            })

    # 不去重: 实盘每个信号都可能开一单, 回测须如实反映信号频率
    # (过去的"同日同方向只算一次"会让回测低估扎堆开仓的伤害)
    return trades


if __name__ == "__main__":
    days = 30; output_json = "--json" in sys.argv
    for a in sys.argv[1:]:
        if a.startswith("--days="): days = int(a.split("=")[1])
        if a.startswith("--session="):
            s = a.split("=")[1].split(",")
            SESSION_START, SESSION_END = int(s[0]), int(s[1])
        if a.startswith("--rr="): RR_MIN = float(a.split("=")[1])
        if a.startswith("--tp="): TP_PIPS = float(a.split("=")[1])
        if a.startswith("--trail="): TRAIL_TRIGGER = float(a.split("=")[1])   # 如 --trail=0.6
        if a.startswith("--lock="): LOCK_PIPS = float(a.split("=")[1])  # 锁定pips(如4)
        if a.startswith("--lockratio="): LOCK_RATIO = float(a.split("=")[1])  # 锁在止盈比例(如0.8)
        if a.startswith("--rsi="):   # RSI 窗口, 如 --rsi=0,100 关闭过滤
            _r = a.split("=")[1].split(",")
            RSI_LO, RSI_HI = int(_r[0]), int(_r[1])
        if a.startswith("--dedup="): DEDUP_PIPS = float(a.split("=")[1])  # 同价位扎堆过滤
        if a.startswith("--flat="): FLAT_RANGE = float(a.split("=")[1])   # 横盘区间过滤
        if a.startswith("--bear="): BEAR_BAR = float(a.split("=")[1])     # 大阴/大阳动能过滤
        if a.startswith("--fuse="): FUSE_MAX_LOSS_STREAK = int(a.split("=")[1])  # 熔断
        if a.startswith("--fuse-hours="): FUSE_HOURS = int(a.split("=")[1])
        if a.startswith("--exit-rev="): EXIT_REV_TF = a.split("=")[1].upper()  # 反转出场: H4/H1/M15/组合
        if a.startswith("--exit-rev-min="): EXIT_REV_MIN_PROFIT = float(a.split("=")[1])
        if a.startswith("--exit-rev-confirm="): EXIT_REV_CONFIRM = int(a.split("=")[1])
        if a.startswith("--struct="): STRUCT_GUARD = int(a.split("=")[1])      # 结构确认过滤
        if a.startswith("--h1guard="): H1_GUARD = int(a.split("=")[1])         # H1 200SMA哨兵
        if a.startswith("--degrade="): DEGRADE_MODE = a.split("=")[1]          # 降级: half_lot/tight_sl/both
        if a.startswith("--maxpos="): MAX_POS = int(a.split("=")[1])           # 同方向持仓上限(0=不限)
        if a.startswith("--confirm-bar="): CONFIRM_BAR = int(a.split("=")[1])  # 确认K线
        if a.startswith("--detect-every="): DETECT_EVERY = int(a.split("=")[1])  # 检测频率
        if a.startswith("--symbol="): BACKTEST_SYMBOL = a.split("=")[1].upper()  # 回测品种
    if BACKTEST_SYMBOL and BACKTEST_SYMBOL.endswith("JPY"):
        PIP_SCALE = 100  # JPY 类品种 1 pip = 0.01

        if a.startswith("--maxbars="): SETTLE_BARS = int(a.split("=")[1])      # 结算窗口(M15根数)
        if a.startswith("--slmult="): SL_ATR_MULT = float(a.split("=")[1])     # ATR止损倍数

    # RSI 默认跟随 config.yaml(与实盘一致), --rsi= 可显式覆盖
    if not any(a.startswith("--rsi=") for a in sys.argv[1:]):
        _y = config.load().get("strategy", {}).get("entry", {}).get("rsi", {})
        if _y.get("enabled", False):
            RSI_LO, RSI_HI = int(_y.get("lo", RSI_LO)), int(_y.get("hi", RSI_HI))
        else:
            RSI_LO, RSI_HI = 0, 100  # 实盘默认关闭 → 回测默认同步关闭

    # 未显式指定时, 读取 config.yaml trail 段作为默认开关
    if TRAIL_TRIGGER == 0.0:
        _tc = config.load().get("trail", {})
        if _tc.get("enabled"):
            TRAIL_TRIGGER = float(_tc.get("trigger", 0.70))
            LOCK_RATIO = float(_tc.get("lock_ratio", 0.80))
            TP_MULT = float(_tc.get("tp_mult", 2.0))

    if LOCK_RATIO > 0:
        LOCK_PIPS = TP_PIPS * LOCK_RATIO   # 锁利 = 止盈 × 比例

    m15, h1, h4 = load_data(days, symbol=BACKTEST_SYMBOL)
    trades = run(m15, h1, h4)

    wins = sum(1 for t in trades if t['oc'] == 'TP')
    losses = sum(1 for t in trades if t['oc'] == 'SL')
    revs = sum(1 for t in trades if t['oc'] == 'REV')
    opens = sum(1 for t in trades if t['oc'] == 'OPEN')
    total_pip = sum(t['pip'] for t in trades)
    wr = wins / max(wins + losses, 1) * 100

    if EXIT_REV_TF:
        mode = f"反转出场({EXIT_REV_TF},确认{EXIT_REV_CONFIRM+1}根,浮盈>{EXIT_REV_MIN_PROFIT:.0f}p)"
    elif TRAIL_TRIGGER > 0:
        mode = f"锁利追踪(触发{TRAIL_TRIGGER*100:.0f}%→锁{LOCK_PIPS:.0f}p+TPx{TP_MULT:.0f})"
    else:
        mode = "固定止盈"
    flt = ""
    if DEDUP_PIPS: flt += f" | 重复价位>{DEDUP_PIPS:.0f}p"
    if FLAT_RANGE: flt += f" | 横盘<{FLAT_RANGE:.0f}p"
    if BEAR_BAR: flt += f" | 大阴/大阳>{BEAR_BAR:.0f}p"
    if STRUCT_GUARD: flt += " | 结构确认"
    if H1_GUARD: flt += " | H1哨兵"
    if DEGRADE_MODE: flt += f" | 降级模式({DEGRADE_MODE})"
    print(f"6条件叠加法回测 (近{days}天) 时段: {SESSION_START:02d}:00-{SESSION_END:02d}:00 北京时间 | R:R≥{RR_MIN} | RSI{RSI_LO}-{RSI_HI} | {mode}{flt}")
    print(f"{'时间':<18} {'方向':<5} {'入场':>8} {'SLp':>5} {'TPp':>4} {'R:R':>4} {'结果':>5} {'Pip':>7}")
    print("-" * 65)
    for t in trades:
        print(f"{t['time']:<18} {t['dir']:<5} {t['entry']:>8.5f} {t['sl_p']:>5.0f} {t['tp_p']:>4.0f} {t['rr']:>4.1f} {t['oc']:>5} {t['pip']:>+7.1f}")
    print("-" * 65)
    sn = sum(1 for t in trades if t['dir'] == 'SELL'); bn = len(trades) - sn
    print(f"总计: {len(trades)}信号({sn}S/{bn}B) {wins}胜{losses}负 {wr:.0f}% {total_pip:+.0f}pips")
    if DEGRADE_MODE:
        degs = [t for t in trades if t.get('deg')]
        d_w = sum(1 for t in degs if t['oc'] == 'TP')
        d_l = sum(1 for t in degs if t['oc'] == 'SL')
        d_pip = sum(t['pip'] for t in degs)
        print(f"      其中 {len(degs)} 单降级(趋势转弱) {d_w}胜{d_l}负 {d_pip:+.0f}pips")
    if revs:
        rev_pip = sum(t['pip'] for t in trades if t['oc'] == 'REV')
        print(f"      其中 {revs} 单反转出场(浮盈保本) 平均 {rev_pip/revs:+.0f}pips")
    if opens:
        open_pip = sum(t['pip'] for t in trades if t['oc'] == 'OPEN')
        print(f"      其中 {opens} 单未结算(48h窗口内未触TP/SL) 浮动 {open_pip:+.0f}pips")
    print(f"月均: {len(trades)/max(days,1)*30:.1f}信号 {total_pip/max(days,1)*30:+.0f}pips/月")
    # 账号回撤: 按时间顺序累计权益曲线, 峰值到谷底的最大回撤
    eq, peak, mdd = 0, 0, 0
    for t in sorted(trades, key=lambda x: x['time']):
        eq += t['pip']
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    print(f"最大回撤: {mdd:.0f} pips (0.01手=${mdd*0.1:.1f}, 0.1手=${mdd:.1f})")

    if output_json:
        print(json.dumps(trades, ensure_ascii=False, indent=2))
