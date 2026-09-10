#!/usr/bin/env python3
"""EURUSD / XAUUSD — 实盘入场检测器 vs 回测 双重审计(动态逻辑对照)

方法与 audit_aud_d1_parity.py 相同: 时间旅行回放(monkeypatch load_parquet
截断到历史时点, 无未来数据), 同一份数据喂实盘检测器与回测引擎, 逐信号bar对照。

EURUSD 口径1: 当前实盘配置(confirm_bar=True + h1_pattern=True, RSI关)
EURUSD 口径2: 全关基线(confirm_bar=False + h1_pattern=False) — 验证开关行为
XAUUSD 口径: 当前实盘配置(RSI 30-60, 无确认K线/H1形态模块)
均采用纯检测器口径(引擎级过滤: 熔断/横盘/重复价位/限单 全关)。
"""
import importlib.util
import sys

sys.path.insert(0, ".")
import pandas as pd  # noqa: E402

from src.strategy.entry import EntryDetector        # noqa: E402
from src.strategy.gold_entry import GoldEntryDetector  # noqa: E402


class TimeTravel:
    """把 detector 的数据读取截断到历史时点 dt(不接触未来数据)"""

    def __init__(self, frames):
        self.frames = frames
        self.dt = None

    def load_parquet(self, tf, symbol=None):
        df = self.frames[tf]
        return df[df.index <= self.dt]


def audit_eurusd():
    print("\n" + "═" * 60)
    print("【EURUSD】实盘 entry.py vs backtest_four_step.py")
    print("═" * 60)
    spec = importlib.util.spec_from_file_location("bt_eu", "scripts/backtest_four_step.py")
    bt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bt)
    # 纯检测器口径 + 当前实盘配置
    bt.SL_ATR_MULT = 2.5
    bt.TP_PIPS = 40.0
    bt.SESSION_START, bt.SESSION_END = 15, 23
    bt.RSI_LO, bt.RSI_HI = 0, 100
    bt.DEDUP_PIPS = 0.0
    bt.FLAT_RANGE = 0.0
    bt.BEAR_BAR = 0.0
    bt.FUSE_MAX_LOSS_STREAK = 0
    bt.MAX_POS = 0
    bt.EXIT_REV_TF = ""
    bt.DETECT_EVERY = 1
    bt.H1_GUARD = 0
    bt.STRUCT_GUARD = 0
    bt.DEGRADE_MODE = ""
    bt.TRAIL_TRIGGER = 0.0
    bt.D1_BIAS = 0
    bt.TREND_TF = "H4"
    bt.SMA_PERIOD = 200

    m15, h1, h4, _ = bt.load_data(170, symbol=None)   # 默认 EUR_USD
    bt.CONFIRM_BAR, bt.H1_PATTERN = 1, 1
    base_cfg = bt.run(m15, h1, h4)                     # 口径1: confirm+h1
    bt.CONFIRM_BAR, bt.H1_PATTERN = 0, 0
    base_raw = bt.run(m15, h1, h4)                     # 口径2: 全关
    bt.CONFIRM_BAR, bt.H1_PATTERN = 1, 1

    frames = {"M15": m15, "H1": h1, "H4": h4}
    det = EntryDetector()
    tt = TimeTravel(frames)
    det.oanda.load_parquet = tt.load_parquet

    results = []
    for label, bt_trades, cbar, h1p in [
        ("口径1 confirm+h1_pattern(实盘当前配置)", base_cfg, True, True),
        ("口径2 全关基线(开关行为验证)", base_raw, False, False),
    ]:
        EntryDetector.confirm_bar = cbar
        EntryDetector.h1_pattern = h1p
        EntryDetector._rsi = {"enabled": False, "lo": 30, "hi": 60}
        bmap = {(t["time"], t["dir"]): t for t in bt_trades}
        fails = []
        for key, t in bmap.items():
            tt.dt = pd.Timestamp(t["time"] + ":00", tz="UTC")
            sig = det.detect()
            if sig is None or sig.direction != t["dir"] or abs(sig.entry_price - t["entry"]) > 1e-9:
                fails.append((key, None if sig is None else (sig.direction, round(sig.entry_price, 5)),
                              t["dir"], t["entry"]))
        # 反向抽样: 每日一根, 实盘出信号 ⇒ 回测必有
        rev_fails = []
        for i in range(0, len(m15.index), 96):
            dt = m15.index[i]
            tt.dt = dt
            sig = det.detect()
            if sig is not None:
                key = (str(dt)[:16], sig.direction)
                if key not in bmap:
                    rev_fails.append((key, round(sig.entry_price, 5)))
        ok = not fails and not rev_fails
        print(f"[{label}] 信号{len(bt_trades)}个")
        print(f"  a. 信号bar × 实盘 同方向同价: {'PASS' if not fails else 'FAIL ' + str(fails[:3])}")
        print(f"  b. 反向抽样{len(range(0, len(m15.index), 96))}根 实盘⇒回测必有: "
              f"{'PASS' if not rev_fails else 'FAIL ' + str(rev_fails[:3])}")
        results.append(ok)
    return all(results)


def _bt_gold_bias(d1, dt):
    """黄金回测 D1 门控的同语义符号: +1偏多 / -1偏空 / 0无倾向(数据不足同0)"""
    if d1 is None:
        return 0
    db = d1[d1.index <= dt]
    if len(db) < 2:
        return 0
    net = float((db.iloc[-2:]['close'] - db.iloc[-2:]['open']).sum())
    if net > 0: return 1
    if net < 0: return -1
    return 0


def audit_gold():
    print("\n" + "═" * 60)
    print("【XAUUSD】实盘 gold_entry.py vs backtest_gold.py")
    print("═" * 60)
    spec = importlib.util.spec_from_file_location("bt_au", "scripts/backtest_gold.py")
    bt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bt)
    # 纯检测器口径 + 当前实盘配置(RSI 30-60 / SL2.5 / TP$100 / 欧美盘)
    bt.FUSE_MAX_LOSS_STREAK = 0
    bt.DEDUP_PIPS = 0.0
    bt.FLAT_RANGE = 0.0
    bt.MAX_POS = 0
    bt.EXIT_REV_TF = ""
    bt.CONFIRM_BAR = 0
    bt.H1_PATTERN = 0
    bt.DOUBLE_PATTERN = 0
    bt.DETECT_EVERY = 1
    bt.BE_TRIGGER_USD = 0.0

    # 口径1: D1关(基线) / 口径2: D1开(当前实盘配置)
    bt.D1_BIAS = 0
    m15, h1, h4, _ = bt.load_data(90)
    trades_off = bt.run(m15, h1, h4)
    bt.D1_BIAS = 1
    _, _, _, d1 = bt.load_data(90)
    trades_on = bt.run(m15, h1, h4, d1=d1)
    bt.D1_BIAS = 0
    frames = {"M15": m15, "H1": h1, "H4": h4, "D1": d1}

    _orig_prop = GoldEntryDetector.d1_bias
    GoldEntryDetector.d1_bias = property(lambda self: self._audit_d1_bias)
    det_off = GoldEntryDetector()
    det_on = GoldEntryDetector()
    det_off._audit_d1_bias = False
    det_on._audit_d1_bias = True

    # a. 全窗口逐bar: 实盘 _d1_net_body 符号 == 回测符号
    tt_g = TimeTravel(frames)
    det_on.oanda.load_parquet = tt_g.load_parquet
    gate_fails = []
    for dt in m15.index:
        tt_g.dt = dt
        net = det_on._d1_net_body(dt)
        ls = 0 if net is None else (1 if net > 0 else (-1 if net < 0 else 0))
        bs = _bt_gold_bias(d1, dt)
        if ls != bs:
            gate_fails.append((dt, ls, bs))
            if len(gate_fails) > 5:
                break
    print(f"  a. 全窗口逐bar D1符号一致({len(m15.index)}根): "
          f"{'PASS' if not gate_fails else 'FAIL ' + str(gate_fails[:3])}")

    results = []
    for label, bt_trades, det in [("口径1 D1关(基线)", trades_off, det_off),
                                   ("口径2 D1开(当前实盘配置)", trades_on, det_on)]:
        tt = TimeTravel(frames)
        det.oanda.load_parquet = tt.load_parquet
        bmap = {(t["time"], t["dir"]): t for t in bt_trades}
        fails = []
        for key, t in bmap.items():
            tt.dt = pd.Timestamp(t["time"] + ":00", tz="UTC")
            sig = det.detect()
            if sig is None or sig.direction != t["dir"] or abs(round(sig.entry_price, 2) - t["entry"]) > 1e-9:
                fails.append((key, None if sig is None else (sig.direction, round(sig.entry_price, 2)),
                              t["dir"], t["entry"]))
        rev_fails = []
        for i in range(0, len(m15.index), 96):
            dt = m15.index[i]
            tt.dt = dt
            sig = det.detect()
            if sig is not None:
                key = (str(dt)[:16], sig.direction)
                if key not in bmap:
                    rev_fails.append((key, round(sig.entry_price, 2)))
        ok = not fails and not rev_fails
        print(f"[{label}] 信号{len(bt_trades)}个")
        print(f"  b. 信号bar × 实盘 同方向同价(2位小数): {'PASS' if not fails else 'FAIL ' + str(fails[:3])}")
        print(f"  c. 反向抽样{len(range(0, len(m15.index), 96))}根 实盘⇒回测必有: "
              f"{'PASS' if not rev_fails else 'FAIL ' + str(rev_fails[:3])}")
        results.append(ok)
    GoldEntryDetector.d1_bias = _orig_prop
    return all(results) and not gate_fails


if __name__ == "__main__":
    ok1 = audit_eurusd()
    ok2 = audit_gold()
    print("\n" + "═" * 60)
    print("═══ 双重审计总结(EURUSD+XAUUSD): " +
          ("✅ 全部通过" if ok1 and ok2 else "❌ 存在不一致, 见上方 FAIL") + " ═══")
