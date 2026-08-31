"""脱机测试 — 生成示例 EUR/USD K 线数据用于验证系统

无需 OANDA API Key 即可测试大势判断模块。
"""
from pathlib import Path

import numpy as np
import pandas as pd


def generate_mock_ohlcv(
    days: int = 200,
    start_price: float = 1.0800,
    seed: int = 42,
) -> pd.DataFrame:
    """生成模拟 EUR/USD 日线数据

    Args:
        days: 天数
        start_price: 起始价格
        seed: 随机种子（固定可复现）

    Returns:
        DataFrame with open, high, low, close, volume
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range(
        end=pd.Timestamp.now(tz="UTC"),
        periods=days,
        freq="D",
    )
    # 随机游走 + 趋势
    returns = rng.normal(0, 0.004, days)  # EUR/USD 日波动约 40 pips = 0.004
    # 前半段震荡，后半段上涨趋势
    trend = np.concatenate([
        np.zeros(days // 2),                  # 横盘
        np.linspace(0, 0.03, days - days // 2)  # 缓慢上涨
    ])
    prices = start_price * (1 + np.cumsum(returns * 0.5 + trend * 0.02))

    ohlcv = []
    for i in range(days):
        close = prices[i]
        daily_range = abs(rng.normal(0.004, 0.001))  # 日波幅 ≈ 40 pips
        open_ = close + rng.normal(0, daily_range * 0.3)
        high = max(open_, close) + abs(rng.normal(0, daily_range * 0.3))
        low = min(open_, close) - abs(rng.normal(0, daily_range * 0.3))
        volume = int(rng.integers(5000, 50000))

        ohlcv.append({
            "time": dates[i],
            "open": round(open_, 5),
            "high": round(high, 5),
            "low": round(low, 5),
            "close": round(close, 5),
            "volume": volume,
        })

    df = pd.DataFrame(ohlcv)
    df.set_index("time", inplace=True)
    return df


def generate_mock_h4(d1_df: pd.DataFrame) -> pd.DataFrame:
    """从日线模拟生成 4 小时线"""
    rng = np.random.default_rng(42)
    rows = []
    for date, row in d1_df.iterrows():
        for h in range(6):  # 每天 6 根 H4
            t = date + pd.Timedelta(hours=h * 4)
            intra_range = (row["high"] - row["low"]) / 6
            offset = rng.normal(0, intra_range * 0.5)
            close = row["open"] + (row["close"] - row["open"]) * (h + 1) / 6 + offset
            open_ = close - intra_range * rng.uniform(-0.5, 0.5)
            high = max(open_, close) + intra_range * rng.uniform(0.1, 0.5)
            low = min(open_, close) - intra_range * rng.uniform(0.1, 0.5)
            rows.append({
                "time": t, "open": round(open_, 5),
                "high": round(high, 5), "low": round(low, 5),
                "close": round(close, 5),
                "volume": int(rng.integers(1000, 10000)),
            })

    df = pd.DataFrame(rows)
    df.set_index("time", inplace=True)
    return df


def setup_test_data(data_dir: str = None):
    """生成测试数据并保存到本地 Parquet"""
    if data_dir is None:
        data_dir = Path(__file__).resolve().parent.parent / "data" / "forex"

    print("🔄 生成测试 K 线数据...")
    d1 = generate_mock_ohlcv(days=200)
    h4 = generate_mock_h4(d1)

    # 保存
    for tf, df in [("D1", d1), ("H4", h4)]:
        path = Path(data_dir) / tf / f"EUR_USD_{tf}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)

    print("✅ 测试数据已就绪，可以运行 TrendAnalyzer!")
    return d1, h4


if __name__ == "__main__":
    setup_test_data()
    print("\n📊 最后 5 根 D1 K 线:")
    d1 = pd.read_parquet(
        Path(__file__).resolve().parent.parent
        / "data" / "forex" / "D1" / "EUR_USD_D1.parquet"
    )
    print(d1.tail())
