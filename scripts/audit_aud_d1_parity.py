#!/usr/bin/env python3
"""澳元 D1双K线参考 — 实盘 vs 回测 双重审计(动态逻辑对照)

审计范围: 入场检测器(含 D1 过滤)在实盘 aud_entry.py 与回测 backtest_four_step.py
中的行为一致性。引擎级过滤(熔断/横盘/重复价位/限单)不在此范围(两处本就独立)。

方法(时间旅行回放, 无未来数据):
  1. 用回测的 load_data 取同一份数据(保证输入完全一致)
  2. 回测侧: 纯检测器口径(关掉熔断/去重/横盘/限单)跑两遍 — D1开/关
  3. 实盘侧: monkeypatch load_parquet 截断到历史时点, 实盘检测器逐bar回放:
     a. 全窗口逐bar: 实盘 _d1_net_body 与回测 _d1_bias 的符号/放行决策 100% 一致
     b. 回测信号bar: 实盘 detect()(D1关) 必须出同方向同入场价信号
     c. 回测信号bar: 实盘 detect()(D1开) 信号存在性必须与回测D1口径一致
     d. 反向抽样(每日1根): 实盘出信号 ⇒ 回测基线同bar必有信号(防实盘多出信号)
"""
import importlib.util
import sys

sys.path.insert(0, ".")
import pandas as pd  # noqa: E402

from src.strategy.aud_entry import AudEntryDetector  # noqa: E402

DAYS = 170

# ── 载入回测模块(不执行 main) ──
spec = importlib.util.spec_from_file_location("bt", "scripts/backtest_four_step.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

# 回测参数 → 纯检测器口径(澳元方案A, 引擎级过滤全关)
bt.SL_ATR_MULT = 2.0
bt.TP_PIPS = 35.0
bt.SESSION_START, bt.SESSION_END = 8, 16
bt.RSI_LO, bt.RSI_HI = 0, 100          # 澳元无RSI
bt.DEDUP_PIPS = 0.0
bt.FLAT_RANGE = 0.0
bt.BEAR_BAR = 0.0
bt.FUSE_MAX_LOSS_STREAK = 0
bt.MAX_POS = 0
bt.EXIT_REV_TF = ""                    # 结算方式不影响入场检测
bt.SETTLE_BARS = 192
bt.DETECT_EVERY = 1
bt.CONFIRM_BAR = 0
bt.H1_PATTERN = 0
bt.TREND_TF = "H4"
bt.SMA_PERIOD = 200
bt.D1_BIAS = 0

m15, h1, h4, _ = bt.load_data(DAYS, symbol="AUD_USD")
base = bt.run(m15, h1, h4)             # 基线: D1关

bt.D1_BIAS = 1
_, _, _, d1 = bt.load_data(DAYS, symbol="AUD_USD")
d1run = bt.run(m15, h1, h4, d1=d1)     # D1开
bt.D1_BIAS = 0

base_map = {(t["time"], t["dir"]): t for t in base}
d1_map = {(t["time"], t["dir"]): t for t in d1run}
print(f"[回测] 基线(检测器纯口径): {len(base)}信号 / D1开: {len(d1run)}信号")

# 子集校验: D1口径 ⊆ 基线口径
missing = [k for k in d1_map if k not in base_map]
print(f"[回测] D1口径⊆基线: {'PASS' if not missing else 'FAIL ' + str(missing[:5])}")

# ── 实盘侧: 时间旅行 ──
frames = {"M15": m15, "H1": h1, "H4": h4, "D1": d1}
cutoff = m15.index.min()


class TimeTravel:
    """把 detector 的数据读取截断到历史时点 dt(不接触未来数据)"""

    def __init__(self, frames):
        self.frames = frames
        self.dt = None

    def load_parquet(self, tf, symbol=None):
        df = self.frames[tf]
        return df[df.index <= self.dt]


_orig_prop = AudEntryDetector.d1_bias   # 保存原始 property, 结尾还原

# 类属性patch后所有实例共享 → 改成按实例开关(per-instance flag)
AudEntryDetector.d1_bias = property(lambda self: self._audit_d1_bias)

det_off = AudEntryDetector()
tt_off = TimeTravel(frames)
det_off.oanda.load_parquet = tt_off.load_parquet
det_off._audit_d1_bias = False

det_on = AudEntryDetector()
tt_on = TimeTravel(frames)
det_on.oanda.load_parquet = tt_on.load_parquet
det_on._audit_d1_bias = True


def live_sign(det, tt, dt):
    tt.dt = dt
    net = det._d1_net_body(dt)
    if net is None:
        return 0
    return 1 if net > 0 else (-1 if net < 0 else 0)


# a. 全窗口逐bar: 实盘 D1 符号 == 回测 D1 符号(170天 × 96根/天)
gate_mismatch = []
bars = m15.index
for dt in bars:
    ls = live_sign(det_on, tt_on, dt)
    bs = bt._d1_bias(d1, dt)
    if ls != bs:
        gate_mismatch.append((dt, ls, bs))
        if len(gate_mismatch) > 5:
            break
print(f"[审计a] 全窗口逐bar D1符号一致({len(bars)}根): "
      f"{'PASS' if not gate_mismatch else 'FAIL ' + str(gate_mismatch[:3])}")

# b/c. 回测信号bar: 实盘 detect() 对照
b_fail, c_fail = [], []
for key, t in base_map.items():
    dt = pd.Timestamp(t["time"] + ":00", tz="UTC")
    # b. D1关: 实盘必须出同方向信号, 入场价一致
    tt_off.dt = dt
    sig = det_off.detect()
    if sig is None or sig.direction != t["dir"] or abs(sig.entry_price - t["entry"]) > 1e-9:
        b_fail.append((key, None if sig is None else (sig.direction, sig.entry_price),
                       t["dir"], t["entry"]))
    # c. D1开: 信号存在性 == 回测D1口径
    tt_on.dt = dt
    sig_on = det_on.detect()
    expect = key in d1_map
    got = sig_on is not None
    if got != expect:
        c_fail.append((key, expect, None if sig_on is None else sig_on.direction))
print(f"[审计b] 回测信号bar × 实盘(D1关) 同方向同价: "
      f"{'PASS' if not b_fail else 'FAIL ' + str(b_fail[:3])} ({len(base_map)}bar)")
print(f"[审计c] 回测信号bar × 实盘(D1开) 存在性一致: "
      f"{'PASS' if not c_fail else 'FAIL ' + str(c_fail[:3])} ({len(base_map)}bar)")

# d. 反向抽样: 每日一根, 实盘(D1关)出信号 ⇒ 回测基线同bar必有
d_fail = []
for i in range(0, len(bars), 96):
    dt = bars[i]
    tt_off.dt = dt
    sig = det_off.detect()
    if sig is not None:
        key = (str(dt)[:16], sig.direction)
        if key not in base_map:
            d_fail.append((key, sig.entry_price))
print(f"[审计d] 反向抽样({len(range(0, len(bars), 96))}根) 实盘信号⇒回测必有: "
      f"{'PASS' if not d_fail else 'FAIL ' + str(d_fail[:3])}")

AudEntryDetector.d1_bias = _orig_prop   # 还原 property

ok = not gate_mismatch and not missing and not b_fail and not c_fail and not d_fail
print("\n═══ 双重审计总结: " + ("✅ 全部通过" if ok else "❌ 存在不一致, 见上方 FAIL") + " ═══")
