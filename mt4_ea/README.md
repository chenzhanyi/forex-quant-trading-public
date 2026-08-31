# MT4 远程交易部署指南

## 架构

```
┌──────────────┐   HTTP POST   ┌──────────────┐   TCP Socket   ┌──────────────┐
│  Mac 交易系统 │ ────────────→ │  VPS 中继     │ ────────────→ │ Windows MT4  │
│              │ ←──────────── │  (公网IP)     │ ←──────────── │  EA 执行     │
└──────────────┘   JSON响应    └──────────────┘   实时推送     └──────────────┘
```

## 部署步骤

### 1. VPS 中继服务器

```bash
# 把 server/ 文件夹上传到 VPS
scp -r server/ user@你的VPS:/home/user/forex-relay/

# SSH 到 VPS
ssh user@你的VPS

# 安装依赖
pip install flask

# 启动中继服务器
cd /home/user/forex-relay
nohup python3 server/relay_server.py --port 8080 --tcp-port 9090 &

# 确认运行
curl http://localhost:8080/health
# → {"ea_connected": false, "status": "ok"}
```

⚠️ VPS 防火墙需开放 8080 (HTTP) 和 9090 (TCP) 端口

### 2. Windows MT4 端

```bash
# 1. 把 mt4_ea/TradeRelay.mq4 复制到 MT4 的 MQL4/Experts/ 目录
# 2. 修改 EA 参数:
#    - RelayHost: 改为你的 VPS 公网 IP
#    - RelayPort: 9090
# 3. 打开 MT4 → 工具 → 选项 → EA交易 → 允许自动交易 ✅
# 4. 在 EURUSD 图表上拖入 TradeRelay EA
# 5. 右上角应有笑脸图标
```

### 3. Mac 交易系统端

```yaml
# config/config.local.yaml
mt4_relay:
  url: "http://你的VPS公网IP:8080"
  enabled: true  # 改为 true 启用自动下单
```

### 4. 测试

```python
from src.execution.mt4_remote import MT4Remote
mt4 = MT4Remote()

# 检查连接
print(mt4.health())

# 测试下单 (0.01手)
result = mt4.sell(symbol="EURUSD", volume=0.01, sl=1.14850, tp=1.14100)
print(result)
```

## 集成到交易信号

在 `src/daemon/engine.py` 的 `evaluate_signal()` 中，
当 `sig.can_trade == True` 时自动调用:

```python
from src.execution.mt4_remote import MT4Remote
mt4 = MT4Remote()
if sig.can_trade:
    direction_func = mt4.sell if "做空" in sig.direction else mt4.buy
    result = direction_func(
        symbol="EURUSD",
        volume=sig.position_lots,
        sl=sig.stop_loss,
        tp=sig.take_profit_1,
    )
```
