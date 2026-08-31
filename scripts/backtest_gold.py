#!/usr/bin/env python3
"""黄金 XAU/USD 回测 — 和 gold_entry.py 完全一致 (EUR/USD六条件法 + RSI)

策略:
  - ① 趋势: H4 200SMA
  - ② 入场: M15 EMA5/15 排列
  - ③ K线形态 (含宽松吞没分支, 与 gold_entry.py 一致)
  - ④ RSI 30-60
  - ⑤ 时段: 仅欧美盘
  - ⑥ 止损: H4 ATR × 2.5 | 止盈: 固定 $100 | RR≥1.0

可选模块:
  --exit-rev=H4/H1/M15/组合   反转出场(浮盈≥阈值 → 按收盘平仓, 与 EURUSD 出场模块同款)
  --exit-rev-min=USD          反转出场最小浮盈(美元, 默认0)
  --exit-rev-confirm=N        连续确认根数(0=1根即触发)
  --fuse=N                    熔断: 同方向连续SL N次后暂停 FUSE_HOURS 小时(防趋势反转期连亏)
  --fuse-hours=H              熔断冷却小时(默认48)
  --rsi=lo,hi                 覆盖RSI窗口(如 --rsi=0,100 关闭)
  --dedup=N                   重复价位过滤(横盘联动见 --flat)
  --flat=N                    横盘区间阈值(近12根H4区间< N美元 → 横盘, 联动dedup)

用法:
    python scripts/backtest_gold.py --days 90
    python scripts/backtest_gold.py --days 90 --json
"""
import sys, json, pandas as pd
sys.path.insert(0, '.')
from datetime import timezone, timedelta
from src.analysis.indicators import Indicators
from src.data_collection.oanda import OandaClient

ind = Indicators()
o = OandaClient()
SYMBOL = "XAU_USD"

# ── 配置（和 gold_entry.py / config.yaml 一致）──
SL_ATR_MULT = 2.5             # H4 ATR 止损倍数
TP_USD = 100                  # 固定止盈美元
TREND_SMA = 200               # H4 趋势 SMA
RSI_LO, RSI_HI = 30, 60       # RSI 关键过滤器
SESSION_START, SESSION_END = 15, 23  # 北京时间欧美盘

# ── 可选模块 ──
EXIT_REV_TF = ""              # 反转出场检测周期: ""=关闭, "H4"/"H1"/"M15"/组合
EXIT_REV_MIN_USD = 0.0        # 反转出场最小浮盈(美元)
EXIT_REV_CONFIRM = 0          # 连续确认根数(0=1根即触发)
FUSE_MAX_LOSS_STREAK = 0      # 熔断: 同方向连续SL N次后暂停(0=关闭)
FUSE_HOURS = 48               # 熔断冷却小时
BE_TRIGGER_USD = 0.0           # 保本止损: 浮盈≥X美元后SL移到入场价(0=关闭)
BE_BUFFER_POINTS = 0           # 保本锁定容差(点, 0.1美元=1点): SL锁在盈利侧
MAX_BARS = 192                # 持仓窗口(M15根数, 192=48h)
DEDUP_PIPS = 0.0              # 重复价位过滤(美元单位, 黄金用美元不用pips)
FLAT_RANGE = 0.0              # 横盘区间阈值(美元)


def load_data(days):
    cutoff = pd.Timestamp.now(tz='UTC') - timedelta(days=days)
    m15 = o.load_parquet("M15", symbol=SYMBOL)
    m15 = m15[m15.index >= cutoff]
    h1 = o.load_parquet("H1", symbol=SYMBOL)
    h1_cut = h1[h1.index >= cutoff - timedelta(days=60)]
    h4 = o.load_parquet("H4", symbol=SYMBOL)
    h4_cut = h4[h4.index >= cutoff - timedelta(days=60)]
    return m15, h1_cut, h4_cut


def detect_reversal_any(df, direction):
    """反转形态检测 — 与 position_manager 出场模块同款(周期无关)"""
    w = df.iloc[-4:] if len(df) >= 4 else df
    if direction == "SELL":   # 持仓做空 → 查多头反转
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
    else:                     # 持仓做多 → 查空头反转
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


def sim_basic(m15, i, direction, entry, sl, tp, max_bars=None):
    """基线模拟: 逐 bar 先判SL再判TP(精确时序), 未触发按窗口末收盘计浮动盈亏

    保本止损(--be=X): 浮盈≥X美元后 SL 移到入场价, 此后打平按 BE 结算(盈亏≈0)
    """
    max_bars = max_bars if max_bars is not None else MAX_BARS
    is_sell = direction == "SELL"
    end = min(i + 1 + max_bars, len(m15))
    be_active = False
    for j in range(i + 1, end):
        b = m15.iloc[j]
        h, l, c = float(b['high']), float(b['low']), float(b['close'])
        # 保本止损: 浮盈达到触发值 → SL 移到入场价±容差(盈利侧)
        if not be_active and BE_TRIGGER_USD > 0:
            profit = (entry - c) if is_sell else (c - entry)
            if profit >= BE_TRIGGER_USD:
                be_active = True
                buf = BE_BUFFER_POINTS * 0.1  # 黄金 0.1美元=1点
                sl = entry - buf if is_sell else entry + buf
        if is_sell:
            if h >= sl:
                return ("BE" if be_active else "SL"), round(entry - sl, 1)
            if l <= tp:
                return "TP", round(entry - tp, 1)
        else:
            if l <= sl:
                return ("BE" if be_active else "SL"), round(sl - entry, 1)
            if h >= tp:
                return "TP", round(tp - entry, 1)
    last_c = float(m15.iloc[end - 1]['close'])
    pnl = (entry - last_c) if is_sell else (last_c - entry)
    return "OPEN", round(pnl, 1)


def sim_reversal_exit(m15, tf_map, i, direction, entry, sl, tp,
                      rev_tfs, min_profit_usd, confirm, max_bars=None):
    max_bars = max_bars if max_bars is not None else MAX_BARS
    """反转出场模拟(黄金, 浮盈按美元) — SL/TP 优先, 反转+浮盈≥阈值 → 按收盘平"""
    is_sell = direction == "SELL"
    state = {tf: {"pos": -1, "count": 0, "ok": False} for tf in rev_tfs}
    end = min(i + 1 + max_bars, len(m15))
    for j in range(i + 1, end):
        b = m15.iloc[j]
        h, l, c = float(b['high']), float(b['low']), float(b['close'])
        dt = b.name

        if is_sell:
            if h >= sl:
                return "SL", round((entry - sl), 1)
            if l <= tp:
                return "TP", round((entry - tp), 1)
        else:
            if l <= sl:
                return "SL", round((sl - entry), 1)
            if h >= tp:
                return "TP", round((tp - entry), 1)

        all_ok = True
        for tf in rev_tfs:
            st = state[tf]
            tfdf = tf_map[tf]
            pos = int(tfdf.index.searchsorted(dt, side="right")) - 1
            if pos != st["pos"]:
                st["pos"] = pos
                if pos < TREND_SMA:
                    st["ok"] = False
                    st["count"] = 0
                else:
                    sma = float(tfdf.iloc[pos][f'SMA{TREND_SMA}'])
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

        profit = (entry - c) if is_sell else (c - entry)
        if profit < min_profit_usd:
            continue
        return "REV", round(profit, 1)

    last_c = float(m15.iloc[end - 1]['close'])
    pnl = (entry - last_c) if is_sell else (last_c - entry)
    return "OPEN", round(pnl, 1)


def run(m15, h1, h4, session_filter=True):
    h4 = ind.add_sma(h4, TREND_SMA); h4 = ind.add_atr(h4, 14)
    m15 = ind.add_ema(m15, 5); m15 = ind.add_ema(m15, 15); m15 = ind.add_rsi(m15, 14)
    tf_map = {}
    rev_tfs = []
    if EXIT_REV_TF:
        tf_map = {
            "H4": h4,
            "H1": ind.add_sma(h1, TREND_SMA),
            "M15": ind.add_sma(m15, TREND_SMA),
        }
        rev_tfs = [t.strip() for t in EXIT_REV_TF.split("+") if t.strip()]

    trades = []
    last_entry = {}            # 重复价位过滤基准
    loss_streak = {"SELL": 0, "BUY": 0}
    paused_until = {"SELL": None, "BUY": None}

    for i in range(20, len(m15)):
        bar = m15.iloc[i]
        p = float(bar['close'])
        dt = bar.name

        h4b = h4[h4.index <= dt]
        if len(h4b) == 0: continue
        h4_sma = float(h4b.iloc[-1][f'SMA{TREND_SMA}'])
        h4_atr = float(h4b.iloc[-1].get('ATR', 0))
        if h4_atr == 0: continue

        e5 = float(bar['EMA5']); e15 = float(bar['EMA15'])
        rsi = float(bar['RSI14'])
        hour_cst = (dt.hour + 8) % 24

        # 横盘判定(近12根H4区间 < FLAT_RANGE 美元) — 与 dedup 联动
        flat_now = False
        if FLAT_RANGE > 0 and len(h4b) >= 12:
            w12 = h4b.iloc[-12:]
            flat_now = (w12['high'].max() - w12['low'].min()) < FLAT_RANGE

        for direction, is_sell in [("SELL", True), ("BUY", False)]:
            # 熔断: 连续SL后暂停该方向
            if FUSE_MAX_LOSS_STREAK > 0:
                pu = paused_until.get(direction)
                if pu is not None:
                    if dt <= pu:
                        continue
                    paused_until[direction] = None
                    loss_streak[direction] = 0

            # ① 趋势 (H4 200SMA)
            if is_sell and p >= h4_sma: continue
            if not is_sell and p <= h4_sma: continue

            # 重复价位过滤: 横盘期(或无FLAT时全局)距48h内同向信号 < DEDUP 美元 → 跳过
            if DEDUP_PIPS > 0 and (flat_now or FLAT_RANGE <= 0):
                prev = last_entry.get(direction)
                if prev and (dt - prev[0]) <= pd.Timedelta(hours=48) and \
                   abs(p - prev[1]) < DEDUP_PIPS:
                    continue

            # ② EMA 排列
            if is_sell and e5 >= e15: continue
            if not is_sell and e5 <= e15: continue

            # ③ K线形态 (含宽松吞没分支, 与 gold_entry.py 一致)
            w = m15.iloc[max(0, i-5):i+1]
            pat = ""
            if is_sell:
                if ind.is_bearish_reversal(w): pat = "射击之星"
                elif ind.is_evening_star(w): pat = "黄昏星"
                elif ind.is_bearish_harami(w): pat = "看跌孕线"
                elif ind.is_decisive_bearish(w): pat = "坚决大阴"
                elif len(w) >= 2:
                    prev, last = w.iloc[-2], w.iloc[-1]
                    if (float(last['close']) < float(last['open']) and
                        float(prev['close']) > float(prev['open']) and
                        float(last['close']) < float(prev['open'])):
                        pat = "阴吞阳"
                if not pat: continue
            else:
                if ind.is_bullish_reversal(w): pat = "锤子线"
                elif ind.is_morning_star(w): pat = "启明星"
                elif ind.is_bullish_harami(w): pat = "看涨孕线"
                elif ind.is_decisive_bullish(w): pat = "坚决大阳"
                elif len(w) >= 2:
                    prev, last = w.iloc[-2], w.iloc[-1]
                    if (float(last['close']) > float(last['open']) and
                        float(prev['close']) < float(prev['open']) and
                        float(last['close']) > float(prev['open'])):
                        pat = "阳吞阴"
                if not pat: continue

            # ④ RSI 30-60
            if rsi < RSI_LO or rsi > RSI_HI: continue

            # ⑤ 时段
            if session_filter and (hour_cst < SESSION_START or hour_cst > SESSION_END): continue

            # ⑥ SL/TP: ATR止损 + 固定TP
            sl_usd = round(h4_atr * SL_ATR_MULT, 2)
            if TP_USD / sl_usd < 1.0: continue  # RR≥1.0

            if is_sell:
                sl = p + sl_usd; tp = p - TP_USD
            else:
                sl = p - sl_usd; tp = p + TP_USD

            if DEDUP_PIPS > 0:
                last_entry[direction] = (dt, p)

            # 结果跟踪（48h = 192根 M15, 统一逐 bar 时序模拟）
            if EXIT_REV_TF:
                oc, pnl = sim_reversal_exit(
                    m15, tf_map, i, direction, p, sl, tp,
                    rev_tfs, EXIT_REV_MIN_USD, EXIT_REV_CONFIRM, MAX_BARS)
            else:
                oc, pnl = sim_basic(m15, i, direction, p, sl, tp, MAX_BARS)

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
                'entry': round(p, 2), 'sl': round(sl, 2), 'tp': round(tp, 2),
                'sl_usd': sl_usd, 'tp_usd': TP_USD, 'rr': round(TP_USD/sl_usd, 2),
                'rsi': round(rsi, 0), 'oc': oc, 'pnl': round(pnl, 1), 'pattern': pat,
            })

    return trades


if __name__ == "__main__":
    days = 90
    output_json = "--json" in sys.argv
    for a in sys.argv[1:]:
        if a.startswith("--days="): days = int(a.split("=")[1])
        if a.startswith("--rsi="):
            _r = a.split("=")[1].split(",")
            RSI_LO, RSI_HI = int(_r[0]), int(_r[1])
        if a.startswith("--exit-rev="): EXIT_REV_TF = a.split("=")[1].upper()
        if a.startswith("--exit-rev-min="): EXIT_REV_MIN_USD = float(a.split("=")[1])
        if a.startswith("--exit-rev-confirm="): EXIT_REV_CONFIRM = int(a.split("=")[1])
        if a.startswith("--fuse="): FUSE_MAX_LOSS_STREAK = int(a.split("=")[1])
        if a.startswith("--be="): BE_TRIGGER_USD = float(a.split("=")[1])  # 保本止损触发
        if a.startswith("--be-buffer="): BE_BUFFER_POINTS = int(a.split("=")[1])  # 保本锁定容差(点)
        if a.startswith("--max-bars="): MAX_BARS = int(a.split("=")[1])  # 持仓窗口(384=96h)
        if a.startswith("--fuse-hours="): FUSE_HOURS = int(a.split("=")[1])
        if a.startswith("--dedup="): DEDUP_PIPS = float(a.split("=")[1])
        if a.startswith("--flat="): FLAT_RANGE = float(a.split("=")[1])

    m15, h1, h4 = load_data(days)
    trades = run(m15, h1, h4)

    wins = sum(1 for t in trades if t['oc'] == 'TP')
    losses = sum(1 for t in trades if t['oc'] == 'SL')
    bes = sum(1 for t in trades if t['oc'] == 'BE')
    revs = sum(1 for t in trades if t['oc'] == 'REV')
    opens = sum(1 for t in trades if t['oc'] == 'OPEN')
    total_pnl = sum(t['pnl'] for t in trades)
    wr = wins / max(wins + losses, 1) * 100
    sn = sum(1 for t in trades if t['dir'] == 'SELL'); bn = len(trades) - sn

    mode = "固定TP/SL"
    if EXIT_REV_TF:
        mode = f"反转出场({EXIT_REV_TF},确认{EXIT_REV_CONFIRM+1}根,浮盈>${EXIT_REV_MIN_USD:.0f})"
    extra = ""
    if FUSE_MAX_LOSS_STREAK: extra += f" | 熔断{FUSE_MAX_LOSS_STREAK}连SL/{FUSE_HOURS}h"
    if DEDUP_PIPS: extra += f" | 重复价位>${DEDUP_PIPS:.0f}"
    if FLAT_RANGE: extra += f" | 横盘<${FLAT_RANGE:.0f}"

    print(f"\n🥇 黄金 XAU/USD 回测 (近{days}天)")
    print(f"   策略: H4 {TREND_SMA}SMA + M15 EMA5/15 + 形态 + RSI{RSI_LO}-{RSI_HI} + 欧美盘")
    print(f"   止损: H4 ATR×{SL_ATR_MULT} | 止盈: ${TP_USD} | RR≥1.0 | {mode}{extra}")
    print()
    print(f"{'时间':<18} {'方向':<5} {'入场':>9} {'SL':>8} {'TP':>9} {'RSI':>4} {'R:R':>5} {'结果':>5} {'PnL':>8} {'形态':<8}")
    print("-" * 100)
    for t in trades:
        print(f"{t['time']:<18} {t['dir']:<5} {t['entry']:>9.2f} {t['sl']:>8.2f} {t['tp']:>9.2f} {t['rsi']:>4.0f} {t['rr']:>5.1f} {t['oc']:>5} {t['pnl']:>+8.1f} {t['pattern']:<8}")
    print("-" * 100)
    print(f"总计: {len(trades)}信号 ({sn}S/{bn}B) | {wins}胜{losses}负 | 胜率 {wr:.0f}% | 净盈亏 ${total_pnl:+.0f}")
    if bes:
        be_pnl = sum(t['pnl'] for t in trades if t['oc'] == 'BE')
        print(f"      其中 {bes} 单保本止损出场(浮盈≥${BE_TRIGGER_USD:.0f}后SL移入场价) 平均 ${be_pnl/bes:+.0f}")
    if revs:
        rev_pnl = sum(t['pnl'] for t in trades if t['oc'] == 'REV')
        print(f"      其中 {revs} 单反转出场(浮盈保本) 平均 ${rev_pnl/revs:+.0f}")
    if opens:
        open_pnl = sum(t['pnl'] for t in trades if t['oc'] == 'OPEN')
        print(f"      其中 {opens} 单未结算(48h窗口内未触TP/SL) 浮动 ${open_pnl:+.0f}")
    print(f"月均: {len(trades)/max(days,1)*30:.1f}信号 | ${total_pnl/max(days,1)*30:+.0f}/月")

    if output_json:
        print("\n" + json.dumps(trades, ensure_ascii=False, indent=2))
