# ============================================================
# 新闻 RSS 采集 — 独立部署指南
# 用你的服务器（有 Hermes + Cloudflare）跑新闻采集
# ============================================================

## 方法一：Hermes Cron（推荐）

在服务器上的 Hermes 中创建 cron 任务：

```bash
hermes cron create \
  --name "forex-news-collect" \
  --schedule "0 */2 * * *" \       # 每 2 小时一次
  --workdir "/path/to/外汇量化交易" \
  --prompt "
运行外汇量化交易系统的新闻采集模块。
项目路径 /path/to/外汇量化交易，虚拟环境在 .venv/。

执行步骤:
1. cd /path/to/外汇量化交易 && source .venv/bin/activate
2. 执行: python3 -c \"
from src.data_collection.news_collector import NewsCollector
nc = NewsCollector()
arts = nc.fetch_feeds()
nc.save(arts)
print(f'采集完成: {len(arts)} 条')
for a in arts[:3]:
    print(f'  {a[\"title\"]}')
\"
3. 同时更新日历: python3 -c \"
from src.data_collection.calendar import EconomicCalendar
EconomicCalendar().collect_and_save()
\"

要求: 服务器网络能访问 investing.com 和 marketwatch.com
      通过 Cloudflare 代理出去的出站流量
" \
  --deliver "telegram"  # 可选：采集完通知你
```

## 方法二：服务器 crontab（无 Hermes 也行）

```bash
# 在服务器上编辑 crontab
crontab -e

# 每 2 小时采集一次新闻
0 */2 * * * cd /path/to/外汇量化交易 && .venv/bin/python3 -c "
from src.data_collection.news_collector import NewsCollector
nc = NewsCollector()
arts = nc.fetch_feeds()
path = nc.save(arts)
print(f'新闻: {len(arts)} 条 -> {path}')
"

# 每天 06:00 采集经济日历
0 6 * * * cd /path/to/外汇量化交易 && .venv/bin/python3 -c "
from src.data_collection.calendar import EconomicCalendar
path = EconomicCalendar().collect_and_save()
print(f'日历已更新: {path}')
"
```

## 方法三：手动触发

```bash
# 在你的服务器上
cd /path/to/外汇量化交易
source .venv/bin/activate

# 采集新闻
python3 -c "from src.data_collection.news_collector import NewsCollector; nc=NewsCollector(); arts=nc.fetch_feeds(); nc.save(arts); print(f'{len(arts)} 条')"

# 采集日历
python3 -c "from src.data_collection.calendar import EconomicCalendar; EconomicCalendar().collect_and_save()"
```

## 依赖安装

首次部署时执行：

```bash
cd /path/to/外汇量化交易
python3 -m venv .venv
source .venv/bin/activate
pip install feedparser beautifulsoup4 lxml pandas numpy httpx pyarrow
```

## 注意事项

1. 服务器不需要本项目全套代码，只需要 `src/data_collection/`、`src/utils/`、`config/` 和 `data/` 目录
2. 采集的数据文件（`data/news/*.csv`、`data/calendar/*.json`）可以通过 rsync/scp 同步回本地 Mac
3. Cloudflare 代理配置：确保你的服务器出站流量通过 Cloudflare Warp 或你的代理出口
