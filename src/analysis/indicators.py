"""技术指标 — 50 SMA / 5 EMA / 15 EMA / ATR / 斜率检测 / 反转形态

纯 pandas 实现，无需 pandas-ta / ta-lib 依赖
"""

from typing import Optional

import numpy as np
import pandas as pd


class Indicators:
    """计算你的交易系统所需的全部指标（纯 pandas）"""

    # K 线形态置信度权重（基础分，需结合大势方向加权）
    CANDLE_WEIGHTS = {
        "锤子线": 2.0,
        "阳吞阴": 2.0,
        "启明星": 2.5,
        "看涨孕线": 1.5,
        "刺透形态": 1.5,
        "十字星+连跌": 0.5,
        "坚决大阳线": 2.0,
        "射击之星": 2.0,
        "阴吞阳": 2.0,
        "黄昏星": 2.5,
        "看跌孕线": 1.5,
        "乌云盖顶": 1.5,
        "十字星+连涨": 0.5,
        "坚决大阴线": 2.0,
    }

    MIN_PATTERN_SCORE = 1.5  # 默认值，实际从 config 读取（strategy.entry.min_pattern_score）

    @staticmethod
    def score_patterns(m15_pattern: str, _secondary_pattern: str = None, trend_factor: float = 1.0) -> float:
        """计算 M15 K线形态置信度分数

        Args:
            m15_pattern: M15 检测到的形态名称（唯一定价框架）
            _secondary_pattern: 保留参数（历史兼容，当前不使用）
            trend_factor: 趋势加权因子（顺势=1.0，强趋势=1.2）

        Returns:
            综合得分（≥ MIN_PATTERN_SCORE 可入场）
        """
        score = 0.0
        if m15_pattern:
            score += Indicators.CANDLE_WEIGHTS.get(m15_pattern, 0) * trend_factor
        # 历史兼容：如果传了第二个形态参数，也计入（权重减半）
        if _secondary_pattern:
            score += Indicators.CANDLE_WEIGHTS.get(_secondary_pattern, 0) * 0.5
        return score

    @staticmethod
    def add_sma(df: pd.DataFrame, period: int = 50) -> pd.DataFrame:
        """添加 SMA（简单移动平均）"""
        df = df.copy()
        df[f"SMA{period}"] = df["close"].rolling(window=period).mean()
        return df

    @staticmethod
    def add_ema(df: pd.DataFrame, period: int = 15) -> pd.DataFrame:
        """添加 EMA（指数移动平均）"""
        df = df.copy()
        df[f"EMA{period}"] = df["close"].ewm(span=period, adjust=False).mean()
        return df

    @staticmethod
    def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """添加 RSI（相对强弱指数）"""
        df = df.copy()
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(span=period, adjust=False).mean()
        avg_loss = loss.ewm(span=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        df[f"RSI{period}"] = 100.0 - (100.0 / (1.0 + rs))
        return df

    @staticmethod
    def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """添加 ATR（平均真实波幅）"""
        df = df.copy()
        high, low, close = df["high"], df["low"], df["close"]

        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        df["ATR"] = tr.rolling(window=period).mean()
        return df

    @staticmethod
    def calculate_all(df: pd.DataFrame) -> pd.DataFrame:
        """一键计算所有常用指标"""
        df = df.copy()
        df = Indicators.add_sma(df, 50)
        df = Indicators.add_sma(df, 5)
        df = Indicators.add_ema(df, 15)
        df = Indicators.add_atr(df, 14)
        return df

    @staticmethod
    def calculate_slope(series: pd.Series, n_bars: int = 3) -> float:
        """线性回归斜率: 用最近 N 根做最小二乘拟合，比简单差值抗噪声

        正=向上趋势，负=向下趋势，接近 0=横盘
        """
        if len(series) < n_bars + 1:
            return 0.0
        import numpy as np
        y = series.iloc[-(n_bars + 1):].values.astype(float)
        x = np.arange(len(y))
        # 最小二乘: slope = Σ((x-x̄)(y-ȳ)) / Σ((x-x̄)²)
        x_mean = x.mean()
        y_mean = y.mean()
        numerator = ((x - x_mean) * (y - y_mean)).sum()
        denominator = ((x - x_mean) ** 2).sum()
        if denominator == 0:
            return 0.0
        return float(numerator / denominator)

    @staticmethod
    def get_ma_direction(
        df: pd.DataFrame, ma_col: str, n_bars: int = 3
    ) -> str:
        """判定 MA 方向（线性回归斜率 + 价格位置）

        UP:      价格 > MA 且 MA 斜率显著向上
        DOWN:    价格 < MA 且 MA 斜率显著向下
        SIDEWAYS: 斜率不足或价格横跨 MA
        """
        if len(df) < n_bars + 1:
            return "SIDEWAYS"

        last = df.iloc[-1]
        price = float(last["close"])
        ma_val = float(last[ma_col])

        slope = Indicators.calculate_slope(df[ma_col], n_bars)
        # 斜率阈值：SMA 值 × 0.0001 作为方向性最低要求
        threshold = ma_val * 0.0001 if ma_val > 0 else 0.0001

        if price > ma_val and slope > threshold:
            return "UP"
        elif price < ma_val and slope < -threshold:
            return "DOWN"
        else:
            return "SIDEWAYS"

    @staticmethod
    def is_consecutive_bear(df: pd.DataFrame, n: int = 3) -> bool:
        """最近 N 根是否连续下跌（用于反转形态的上下文判断）"""
        if len(df) < n:
            return False
        for i in range(n):
            idx = -(i + 1)
            if df["close"].iloc[idx] >= df["open"].iloc[idx]:
                return False
            # 每根收盘都比前一根低才算连续下跌
            # close[-2] (older) > close[-1] (newer) → 价格下降
            if i > 0 and df["close"].iloc[idx] <= df["close"].iloc[idx + 1]:
                return False
        return True

    @staticmethod
    def is_consecutive_bull(df: pd.DataFrame, n: int = 3) -> bool:
        """最近 N 根是否连续上涨"""
        if len(df) < n:
            return False
        for i in range(n):
            idx = -(i + 1)
            if df["close"].iloc[idx] <= df["open"].iloc[idx]:
                return False
            # 每根收盘都比前一根高才算连续上涨
            # close[-2] (older) < close[-1] (newer) → 价格上升
            if i > 0 and df["close"].iloc[idx] >= df["close"].iloc[idx + 1]:
                return False
        return True

    @staticmethod
    def is_doji(df: pd.DataFrame) -> bool:
        """十字星: 开盘≈收盘，实体极小，趋势衰竭信号"""
        if len(df) < 1:
            return False
        last = df.iloc[-1]
        body = abs(float(last["close"]) - float(last["open"]))
        total_range = float(last["high"]) - float(last["low"])
        if total_range == 0:
            return False
        return body / total_range < 0.1

    @staticmethod
    def is_bullish_reversal(df: pd.DataFrame) -> bool:
        """检测多头反转 K 线形态（需在连续下跌后使用）"""
        if len(df) < 2:
            return False
        last = df.iloc[-1]
        prev = df.iloc[-2]

        cl, op = float(last["close"]), float(last["open"])
        body = abs(cl - op)
        lower_shadow = min(op, cl) - float(last["low"])
        upper_shadow = float(last["high"]) - max(op, cl)

        # 锤子线: 下影线 ≥ 实体 × 2，收盘 > 开盘
        if lower_shadow >= body * 2 and cl > op and upper_shadow <= body * 1.5:
            return True

        # 阳吞阴
        pcl, pop = float(prev["close"]), float(prev["open"])
        if (cl > op and pcl < pop and
            cl > pop and op < pcl):
            return True

        # 看涨刺透: 阴线后阳线，阳线收盘深入到前阴线 50% 以上
        if len(df) >= 2:
            if (pcl < pop and cl > op and
                cl > (pop + pcl) / 2 and   # 收盘在前阴线中点以上
                op < pcl):                 # 开盘在前阴线收盘以下
                return True

        return False

    @staticmethod
    def is_bearish_reversal(df: pd.DataFrame) -> bool:
        """检测空头反转 K 线形态（需在连续上涨后使用）"""
        if len(df) < 2:
            return False
        last = df.iloc[-1]
        prev = df.iloc[-2]

        cl, op = float(last["close"]), float(last["open"])
        body = abs(cl - op)
        upper_shadow = float(last["high"]) - max(op, cl)
        lower_shadow = min(op, cl) - float(last["low"])

        # 射击之星: 上影线 ≥ 实体 × 2，收盘 < 开盘，下影线短
        if upper_shadow >= body * 2 and cl < op and lower_shadow <= body * 1.5:
            return True

        # 阴吞阳
        pcl, pop = float(prev["close"]), float(prev["open"])
        if (cl < op and pcl > pop and
            cl < pop and op > pcl):
            return True

        # 乌云盖顶: 阳线后阴线，阴线收盘深入到前阳线 50% 以下
        if (pcl > pop and cl < op and
            cl < (pop + pcl) / 2 and    # 收盘在前阳线中点以下
            op > pcl):                  # 开盘在前阳线收盘以上
            return True

        return False

    @staticmethod
    def is_morning_star(df: pd.DataFrame) -> bool:
        """启明星: 3 根 K 线底部反转

        1. 长阴线
        2. 小实体（十字星/纺锤），跳空低开
        3. 长阳线，收盘 ≥ 第 1 根阴线的中点
        """
        if len(df) < 3:
            return False
        c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]

        o1, c1c = float(c1["open"]), float(c1["close"])
        o2, c2c = float(c2["open"]), float(c2["close"])
        o3, c3c = float(c3["open"]), float(c3["close"])

        body1 = abs(c1c - o1)
        body2 = abs(c2c - o2)
        body3 = abs(c3c - o3)
        range1 = float(c1["high"]) - float(c1["low"])

        if range1 == 0:
            return False

        # 第1根: 长阴线（实体 ≥ 范围的 50%）
        # 第2根: 小实体（实体 ≤ 范围的 30%），跳空低开
        # 第3根: 阳线，收盘 ≥ 第1根中点
        return (
            c1c < o1 and body1 / range1 >= 0.48 and    # 长阴
            body2 / max(float(c2["high"]) - float(c2["low"]), 0.0001) <= 0.3 and  # 小实体
            max(o2, c2c) < min(o1, c1c) and            # 跳空低开
            c3c > o3 and                                # 阳线
            c3c >= (o1 + c1c) / 2                       # 收在阴线中点以上
        )

    @staticmethod
    def is_evening_star(df: pd.DataFrame) -> bool:
        """黄昏星: 3 根 K 线顶部反转

        1. 长阳线
        2. 小实体（十字星/纺锤），跳空高开
        3. 长阴线，收盘 ≤ 第 1 根阳线的中点
        """
        if len(df) < 3:
            return False
        c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]

        o1, c1c = float(c1["open"]), float(c1["close"])
        o2, c2c = float(c2["open"]), float(c2["close"])
        o3, c3c = float(c3["open"]), float(c3["close"])

        body1 = abs(c1c - o1)
        body2 = abs(c2c - o2)
        body3 = abs(c3c - o3)
        range1 = float(c1["high"]) - float(c1["low"])

        if range1 == 0:
            return False

        return (
            c1c > o1 and body1 / range1 >= 0.48 and    # 长阳
            body2 / max(float(c2["high"]) - float(c2["low"]), 0.0001) <= 0.3 and  # 小实体
            min(o2, c2c) > max(o1, c1c) and            # 跳空高开
            c3c < o3 and                                # 阴线
            c3c <= (o1 + c1c) / 2                       # 收在阳线中点以下
        )

    @staticmethod
    def is_bullish_harami(df: pd.DataFrame) -> bool:
        """看涨孕线: 阴线后出现小阳线，阳线实体完全在前阴线实体内"""
        if len(df) < 2:
            return False
        prev, last = df.iloc[-2], df.iloc[-1]

        pop, pcl = float(prev["open"]), float(prev["close"])
        op, cl = float(last["open"]), float(last["close"])

        return (
            pcl < pop and          # 前一根阴线
            cl > op and             # 当前阳线
            op >= pcl and           # 阳线开盘 ≥ 前阴收盘
            cl <= pop               # 阳线收盘 ≤ 前阴开盘
        )

    @staticmethod
    def is_bearish_harami(df: pd.DataFrame) -> bool:
        """看跌孕线: 阳线后出现小阴线，阴线实体完全在前阳线实体内"""
        if len(df) < 2:
            return False
        prev, last = df.iloc[-2], df.iloc[-1]

        pop, pcl = float(prev["open"]), float(prev["close"])
        op, cl = float(last["open"]), float(last["close"])

        return (
            pcl > pop and          # 前一根阳线
            cl < op and             # 当前阴线
            op <= pcl and           # 阴线开盘 ≤ 前阳收盘
            cl >= pop               # 阴线收盘 ≥ 前阳开盘
        )

    # ── 坚决K线（大阳线/大阴线确认）──

    @staticmethod
    def is_decisive_bearish(df: pd.DataFrame) -> bool:
        """检测坚决大阴线 — 对比前一根K线，出现实体大、收在低点的阴线

        条件:
          1. 阴线 (close < open)
          2. 实体 ≥ 最近 5 根均值的 1.5 倍
          3. 下影线 < 实体的 30%（收盘坚定，不是吊颈）
          4. 收盘低于前一根的最低价（向下突破）
        """
        if len(df) < 6:
            return False
        last = df.iloc[-1]; prev = df.iloc[-2]
        cl, op = float(last["close"]), float(last["open"])
        hi, lo = float(last["high"]), float(last["low"])
        p_lo = float(prev["low"])

        if cl >= op:  # 不是阴线
            return False

        body = op - cl
        total_range = hi - lo
        lower_wick = cl - lo

        if total_range == 0:
            return False

        # 最近 5 根平均实体
        avg_body = sum(
            abs(float(df.iloc[i]["close"]) - float(df.iloc[i]["open"]))
            for i in range(-6, -1)
        ) / 5
        if avg_body == 0:
            return False

        # 实体够大（≥1.5倍均值）
        if body < avg_body * 1.5:
            return False

        # 收盘坚定（下影线 < 实体30%，即大部分是实体，不是探底回升）
        if lower_wick > body * 0.3:
            return False

        # 突破前低（收盘低于前一根最低价）
        if cl >= p_lo:
            return False

        return True

    @staticmethod
    def is_decisive_bullish(df: pd.DataFrame) -> bool:
        """检测坚决大阳线 — 对比前一根K线，出现实体大、收在高点的阳线

        条件:
          1. 阳线 (close > open)
          2. 实体 ≥ 最近 5 根均值的 1.5 倍
          3. 上影线 < 实体的 30%（收盘坚定）
          4. 收盘高于前一根的最高价（向上突破）
        """
        if len(df) < 6:
            return False
        last = df.iloc[-1]; prev = df.iloc[-2]
        cl, op = float(last["close"]), float(last["open"])
        hi, lo = float(last["high"]), float(last["low"])
        p_hi = float(prev["high"])

        if cl <= op:
            return False

        body = cl - op
        total_range = hi - lo
        upper_wick = hi - cl

        if total_range == 0:
            return False

        avg_body = sum(
            abs(float(df.iloc[i]["close"]) - float(df.iloc[i]["open"]))
            for i in range(-6, -1)
        ) / 5
        if avg_body == 0:
            return False

        if body < avg_body * 1.5:
            return False

        if upper_wick > body * 0.3:
            return False

        if cl <= p_hi:
            return False

        return True

    # ── 趋势波段高低点 ──

    @staticmethod
    def find_trend_swings(df: pd.DataFrame, min_pips: float = 40):
        """找趋势级别的波段高点和低点（中长线止损/止盈用）

        与 find_swing_points 的区别:
          - 找的是整个趋势结构中的显著极值，不是局部 5-bar 小摆动
          - 最小振幅过滤: 高低点之间至少差 min_pips，过滤横盘噪音
          - 适用于 H4/D1 级别的止损止盈参考

        逻辑:
          在整个 df 范围内找到 highest high 和 lowest low，
          然后按时间先后判断当前趋势结构。

        Returns:
          {
            "major_high": float,   # 整个区间最高点（趋势级阻力）
            "major_low": float,    # 整个区间最低点（趋势级支撑）
            "recent_high": float,  # 最近的显著波段高点（前一个上升趋势顶点）
            "recent_low": float,   # 最近的显著波段低点（前一个下降趋势低点）
          }
          或 None
        """
        if len(df) < 20:
            return None

        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values

        # ── 找显著 swing 点（用大窗口 10-15 bar）──
        window = max(5, len(df) // 30)  # 自适应: 数据多时窗口更大
        window = min(window, 15)

        swing_highs = []  # [(iloc, price), ...]
        swing_lows = []

        for i in range(window, len(df) - window):
            if highs[i] == max(highs[i - window : i + window + 1]):
                # 确认是显著高点: 比前后最低值高出 min_pips
                local_range = max(highs[i - window : i + window + 1]) - min(lows[i - window : i + window + 1])
                if local_range >= min_pips / 10000:
                    swing_highs.append((i, float(highs[i])))
            if lows[i] == min(lows[i - window : i + window + 1]):
                local_range = max(highs[i - window : i + window + 1]) - min(lows[i - window : i + window + 1])
                if local_range >= min_pips / 10000:
                    swing_lows.append((i, float(lows[i])))

        if len(swing_highs) < 1 or len(swing_lows) < 1:
            return None

        # 整个区间的极值
        major_high = max(sh[1] for sh in swing_highs)
        major_low = min(sl[1] for sl in swing_lows)

        # 最近的显著波段高点和低点（取最后两个，跳过太近的）
        recent_high = swing_highs[-1][1]
        if len(swing_highs) >= 2:
            # 如果最近的高点太接近区间末尾（<3 bar），取上一个
            if len(df) - swing_highs[-1][0] < 3:
                recent_high = swing_highs[-2][1]

        recent_low = swing_lows[-1][1]
        if len(swing_lows) >= 2:
            if len(df) - swing_lows[-1][0] < 3:
                recent_low = swing_lows[-2][1]

        return {
            "major_high": round(major_high, 5),
            "major_low": round(major_low, 5),
            "recent_high": round(recent_high, 5),
            "recent_low": round(recent_low, 5),
        }

    # ── Fibonacci 回调位 ──

    @staticmethod
    def find_swing_points(df: pd.DataFrame, window: int = 5):
        """找最近一个完整的波段高点和低点（H4 级别用）

        使用局部极值法：一根 bar 是 swing high 如果它的 high
        是前后各 window 根 bar 中最高的。swing low 同理。

        Returns:
            (swing_high_price, swing_low_price) 或 None（数据不足）
        """
        if len(df) < window * 2 + 1:
            return None

        highs = df["high"].values
        lows = df["low"].values

        swing_highs = []  # (index, price)
        swing_lows = []   # (index, price)

        for i in range(window, len(df) - window):
            # Swing high: 当前 high 是前后 window 范围内的最高
            if highs[i] == max(highs[i - window : i + window + 1]):
                swing_highs.append((i, highs[i]))
            # Swing low: 当前 low 是前后 window 范围内的最低
            if lows[i] == min(lows[i - window : i + window + 1]):
                swing_lows.append((i, lows[i]))

        if len(swing_highs) < 1 or len(swing_lows) < 1:
            return None

        # 取最近一个 swing high 和 swing low
        last_high = swing_highs[-1]
        last_low = swing_lows[-1]

        # 确保高点在低点之上，且发生在不同时间
        if abs(last_high[0] - last_low[0]) < window:
            # 太近了，往前找更早的
            if len(swing_highs) > 1 and len(swing_lows) > 1:
                last_high = swing_highs[-2]
                last_low = swing_lows[-2] if swing_lows[-2][0] < swing_highs[-1][0] - window else swing_lows[-1]
                if last_high[0] == last_low[0] and len(swing_lows) > 2:
                    last_low = swing_lows[-2]

        return (float(last_high[1]), float(last_low[1]))

    @staticmethod
    def calc_fib_retracement(swing_high: float, swing_low: float):
        """计算斐波那契回调位

        Returns:
            {"0.382": float, "0.500": float, "0.618": float}
        """
        diff = swing_high - swing_low
        return {
            "0.382": round(swing_high - diff * 0.382, 5),
            "0.500": round(swing_high - diff * 0.500, 5),
            "0.618": round(swing_high - diff * 0.618, 5),
        }

    @staticmethod
    def is_near_level(price: float, target: float, tolerance_atr: float) -> bool:
        """判断价格是否在目标位 ± tolerance 范围内"""
        return abs(price - target) <= tolerance_atr

    # ── 斐波那契延伸位（止盈参考）──

    @staticmethod
    def calc_fib_extension(swing_high: float, swing_low: float, direction: str = "SELL"):
        """计算斐波那契延伸位（用于止盈目标）

        做空: 从 swing_high → swing_low 的跌幅，延伸至 swing_low 下方
              127.2% = swing_low - diff × 0.272
              161.8% = swing_low - diff × 0.618
        做多: 从 swing_low → swing_high 的涨幅，延伸至 swing_high 上方
              127.2% = swing_high + diff × 0.272
              161.8% = swing_high + diff × 0.618

        Returns:
            {"1.272": float, "1.618": float}
        """
        diff = swing_high - swing_low
        if direction == "SELL":
            return {
                "1.272": round(swing_low - diff * 0.272, 5),
                "1.618": round(swing_low - diff * 0.618, 5),
            }
        else:  # BUY
            return {
                "1.272": round(swing_high + diff * 0.272, 5),
                "1.618": round(swing_high + diff * 0.618, 5),
            }

    # ── M15 波段点检测（入场用）──

    @staticmethod
    def find_recent_swing_points_m15(df: pd.DataFrame, window: int = 5, lookback: int = 36):
        """在 M15 上找最近的有效波段高点和低点

        用于用户的四步入场法:
          - 做空: 找最近的 swing_high（反弹最高点）→ 止损参考
          - 做多: 找最近的 swing_low（回调最低点）→ 止损参考

        Args:
            df: M15 K线数据（需已按时间排序）
            window: 极点检测窗口（前后各 N 根）
            lookback: 只在最近 N 根 K 线范围内搜索

        Returns:
            {"swing_high": (idx, price), "swing_low": (idx, price)}
            或 None（数据不足）
        """
        if len(df) < window * 2 + 1:
            return None

        # 只看最近 lookback 根
        work = df.iloc[-lookback:] if len(df) > lookback else df
        highs = work["high"].values
        lows = work["low"].values
        n = len(work)

        swing_highs = []  # (local_idx, price, original_idx)
        swing_lows = []

        for i in range(window, n - window):
            if highs[i] == max(highs[i - window : i + window + 1]):
                swing_highs.append((i, float(highs[i]), work.index[i]))
            if lows[i] == min(lows[i - window : i + window + 1]):
                swing_lows.append((i, float(lows[i]), work.index[i]))

        if not swing_highs or not swing_lows:
            return None

        # 取最近一个（列表末尾）
        sh = swing_highs[-1]
        sl = swing_lows[-1]

        return {
            "swing_high": (sh[2], sh[1]),  # (original_index, price)
            "swing_low": (sl[2], sl[1]),
        }

    # ── EMA 交叉检测 ──

    @staticmethod
    def find_ema_cross_after(df: pd.DataFrame, fast_col: str, slow_col: str,
                             direction: str, after_idx) -> Optional[int]:
        """在指定位置之后找第一个 EMA 交叉点

        Args:
            df: K线数据（含 fast_col 和 slow_col 列）
            fast_col: 快线列名（如 EMA5）
            slow_col: 慢线列名（如 EMA15）
            direction: "death" (快线下穿慢线) 或 "golden" (快线上穿慢线)
            after_idx: 从哪个 index 之后开始找

        Returns:
            交叉发生的 iloc 位置索引，或 None
        """
        # 将 after_idx 转为 iloc
        try:
            after_iloc = df.index.get_loc(after_idx)
        except (KeyError, TypeError):
            return None

        if after_iloc >= len(df) - 3:
            return None  # 不够空间形成交叉

        fast = df[fast_col].values
        slow = df[slow_col].values

        for i in range(after_iloc + 1, len(df) - 1):
            if direction == "death":
                # 快线从上方下穿慢线: 前一根 fast > slow，当前 fast < slow
                if fast[i-1] > slow[i-1] and fast[i] < slow[i]:
                    return i
            elif direction == "golden":
                # 快线从下方上穿慢线: 前一根 fast < slow，当前 fast > slow
                if fast[i-1] < slow[i-1] and fast[i] > slow[i]:
                    return i

        return None

    @staticmethod
    def find_highest_after(df: pd.DataFrame, after_idx, below_price: Optional[float] = None):
        """在指定位置之后找最高点（用于检测反弹是否超过前高）

        Args:
            df: K线数据
            after_idx: 从哪个 index 之后开始
            below_price: 如果指定，最高点必须低于此价格（做空反弹检测用）

        Returns:
            (iloc, high_price) 或 (None, None)
        """
        try:
            after_iloc = df.index.get_loc(after_idx)
        except (KeyError, TypeError):
            return None, None

        if after_iloc >= len(df) - 1:
            return None, None

        segment = df.iloc[after_iloc + 1:]
        if len(segment) == 0:
            return None, None

        max_iloc = segment["high"].idxmax()
        max_price = float(segment["high"].max())

        if below_price is not None and max_price >= below_price:
            return None, None  # 反弹超过了前高 → 无效

        # 转回原 df 的 iloc
        try:
            max_idx = df.index.get_loc(max_iloc)
        except KeyError:
            return None, None

        return max_idx, max_price
