//+------------------------------------------------------------------+
//|                                        TradeRelay.mq4              |
//|  MT4 EA v3 — 品种路由 + 成交价校准 + 券商报价上报                  |
//|  使用 WinHTTP (MQL4 内置) 进行 HTTP GET/POST                       |
//|                                                                    |
//|  v3 变更(修复双重抓单 + 数据校准):                                  |
//|   1. /pending 轮询带 symbol=<图表品种> — 每个 EA 只认领本品种订单    |
//|      (黄金图表不会再执行 EURUSD 订单; 双图表不会抢同一订单)         |
//|   2. 轮询捎带券商 bid/ask — relay 存档供 Mac 端数据校准             |
//|   3. 开仓指令支持 sl_pips/tp_pips(距离模式):                        |
//|      按当前报价换算初始 SL/TP 下单, 成交后按真实成交价修正,         |
//|      消除行情源(OANDA)与券商之间的点差/基差影响                     |
//|   4. 持仓上报只报本图表品种(clear 只清本品种), 多图表互不干扰       |
//|                                                                    |
//|  兼容: 无 sl_pips/tp_pips 字段的指令走绝对价格路径(黄金等)          |
//+------------------------------------------------------------------+
#property copyright "Forex Quant Trading"
#property version   "3.0"
#property strict

input string   RelayUrl = "http://你的中继服务器域名:8080";  // 中继服务器域名
input int      PollSeconds = 5;                     // 轮询间隔(秒)
input int      Slippage = 30;                       // 滑点(pips)
input string   TradeComment = "auto";               // 订单注释

datetime g_lastPoll = 0;
string g_lastOrderId = "";   // 上次处理过的订单ID，避免重复执行
int g_pollCount = 0;
bool g_shouldReport = true;  // 启动后立即上报

//+------------------------------------------------------------------+
int OnInit() {
    Print("TradeRelay EA v3 启动 (品种路由+成交价校准)");
    Print("  图表品种: ", Symbol());
    Print("  中继: ", RelayUrl);
    Print("  轮询间隔: ", PollSeconds, "秒");
    // 定时器驱动 — 不依赖行情 tick(周末休市也能上报心跳, relay 状态不失真)
    EventSetTimer(PollSeconds);
    return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) {
    EventKillTimer();
    Print("TradeRelay EA 停止");
}

//+------------------------------------------------------------------+
void OnTick() {
    // 已改用 OnTimer 驱动 — 行情 tick 不再触发任何操作
}

void OnTimer() {
    if (TimeCurrent() - g_lastPoll < PollSeconds) return;
    g_lastPoll = TimeCurrent();

    // 先上报持仓
    if (g_shouldReport) {
        ReportPositions();
        g_shouldReport = false;
    } else if (g_pollCount % 12 == 0) {  // 每60秒上报一次
        ReportPositions();
    }
    g_pollCount++;

    CheckAndExecute();
}

//+------------------------------------------------------------------+
// 1 pip 对应的价格单位 (5位报价=10 points, 4位报价=1 point)
//+------------------------------------------------------------------+
double PipSize(string sym) {
    int d = (int)MarketInfo(sym, MODE_DIGITS);
    double pt = MarketInfo(sym, MODE_POINT);
    if (d == 3 || d == 5) return pt * 10;
    return pt;
}

//+------------------------------------------------------------------+
// 持仓上报 — 只报本图表品种, 只清理本品种的服务器缓存
// (多图表各自上报互不干扰)
//+------------------------------------------------------------------+
void ReportPositions() {
    string mysym = Symbol();
    // 只清本品种旧缓存, 不影响其它图表 EA 上报的品种
    HttpGet(RelayUrl + "/rp?clear=1&sym=" + mysym);
    int count = 0;
    for(int i = 0; i < OrdersTotal(); i++) {
        if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
        if(OrderSymbol() != mysym) continue;  // 只上报本图表品种
        // 每单单独上报，避免 URL 太长
        string p = "t=" + IntegerToString(OrderTicket());
        p += "&type=" + IntegerToString(OrderType());
        p += "&lots=" + DoubleToString(OrderLots(), 2);
        p += "&open=" + DoubleToString(OrderOpenPrice(), 5);
        p += "&sl=" + DoubleToString(OrderStopLoss(), 5);
        p += "&tp=" + DoubleToString(OrderTakeProfit(), 5);
        p += "&profit=" + DoubleToString(OrderProfit(), 2);
        p += "&sym=" + OrderSymbol();
        HttpGet(RelayUrl + "/rp?" + p);
        count++;
    }
    // 上报总数
    HttpGet(RelayUrl + "/rp?count=" + IntegerToString(count) + "&sym=" + mysym);
}

//+------------------------------------------------------------------+
void CheckAndExecute() {
    // 1. 查询待处理订单 — 只认领本图表品种 + 捎带券商实时报价
    string q = "/pending?symbol=" + Symbol()
             + "&bid=" + DoubleToString(MarketInfo(Symbol(), MODE_BID), 5)
             + "&ask=" + DoubleToString(MarketInfo(Symbol(), MODE_ASK), 5);
    string orderResp = HttpGet(RelayUrl + q);
    if (orderResp == "" || orderResp == "{}") return;

    // 2. 从返回中提取 order id
    int pos = StringFind(orderResp, "\"id\":\"");
    if (pos < 0) return;
    pos += 6;
    int endPos = StringFind(orderResp, "\"", pos);
    if (endPos <= pos) return;
    string orderId = StringSubstr(orderResp, pos, endPos - pos);

    // 3. 跳过已处理的（防本 EA 重复执行同一订单）
    if (orderId == g_lastOrderId) return;
    g_lastOrderId = orderId;

    // 4. 解析参数 (v3: orderResp 已包含全部字段)
    string action    = JsonValue(orderResp, "action");
    if (action == "") action = JsonValue(orderResp, "direction");  // 兼容旧版
    if (action == "BUY" || action == "SELL") action = "";  // direction字段=开仓
    string direction = JsonValue(orderResp, "direction");
    string symbol    = JsonValue(orderResp, "symbol");
    double volume    = StringToDouble(JsonValue(orderResp, "volume"));
    double sl        = StringToDouble(JsonValue(orderResp, "sl"));
    double tp        = StringToDouble(JsonValue(orderResp, "tp"));
    int    ticket_no = (int)StringToInteger(JsonValue(orderResp, "ticket"));
    // 距离模式字段(可选): sl_pips/tp_pips 存在时按成交价换算 SL/TP
    double sl_pips   = StringToDouble(JsonValue(orderResp, "sl_pips"));
    double tp_pips   = StringToDouble(JsonValue(orderResp, "tp_pips"));

    // ── 修改订单指令 ──
    if (action == "modify") {
        double new_sl = StringToDouble(JsonValue(orderResp, "sl"));
        double new_tp = StringToDouble(JsonValue(orderResp, "tp"));
        int result = ModifySLTP((int)ticket_no, new_sl, new_tp);
        string resp = RelayUrl + "/confirm?order_id=" + orderId + "&status=" + (result ? "modified" : "modify_failed");
        HttpGet(resp);
        return;
    }

    // ── 平仓指令 ──
    if (action == "close") {
        if (direction != "BUY" && direction != "SELL") direction = "";  // 空=平全部
        int closed = ClosePositions(symbol, direction, ticket_no);
        Print("📤 平仓: 已关闭 ", closed, " 笔订单");
        string resp = RelayUrl + "/confirm?order_id=" + orderId + "&status=closed&closed=" + IntegerToString(closed);
        HttpGet(resp);
        return;
    }

    // ── 开仓指令 ──
    if (direction != "BUY" && direction != "SELL") return;
    if (symbol == "") symbol = "EURUSD";
    if (volume <= 0) volume = 0.01;

    Print("📩 收到指令: ", direction, " ", volume, "手 SL=", sl, " TP=", tp,
          (sl_pips > 0 ? " [距离模式]" : " [绝对价格]"));

    int cmd = (direction == "BUY") ? OP_BUY : OP_SELL;
    double price = (direction == "BUY") ? MarketInfo(symbol, MODE_ASK) : MarketInfo(symbol, MODE_BID);
    if (price <= 0) { Print("❌ 无法获取报价"); return; }

    int digit = (int)MarketInfo(symbol, MODE_DIGITS);

    // 距离模式: 先按当前报价换算初始 SL/TP (下单时必须有 SL/TP 附单)
    double sl0 = sl, tp0 = tp;
    if (sl_pips > 0) {
        double pip = PipSize(symbol);
        if (direction == "BUY") { sl0 = price - sl_pips * pip; tp0 = price + tp_pips * pip; }
        else                    { sl0 = price + sl_pips * pip; tp0 = price - tp_pips * pip; }
    }
    if (sl0 > 0) sl0 = NormalizeDouble(sl0, digit);
    if (tp0 > 0) tp0 = NormalizeDouble(tp0, digit);
    volume = NormalizeDouble(volume, 2);

    int ticket = OrderSend(symbol, cmd, volume, price, Slippage, sl0, tp0, TradeComment, 0, 0, clrNONE);

    if (ticket > 0) {
        double fill = price;
        if (OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES)) fill = OrderOpenPrice();

        // 距离模式: 成交后按真实成交价修正 SL/TP — 消除行情源与券商间的点差
        if (sl_pips > 0) {
            double pip = PipSize(symbol);
            double sl1 = (direction == "BUY") ? fill - sl_pips * pip : fill + sl_pips * pip;
            double tp1 = (direction == "BUY") ? fill + tp_pips * pip : fill - tp_pips * pip;
            sl1 = NormalizeDouble(sl1, digit);
            tp1 = NormalizeDouble(tp1, digit);
            if (sl1 != sl0 || tp1 != tp0) {
                ModifySLTP(ticket, sl1, tp1);  // 校准后的精确 SL/TP
            }
        }

        Print("✅ 下单成功: Ticket=", ticket, " @ ", fill);
        // 通知服务器(回报真实成交价)
        string confirm = RelayUrl + "/confirm?order_id=" + orderId +
                        "&ticket=" + IntegerToString(ticket) +
                        "&status=filled&price=" + DoubleToString(fill, digit);
        HttpGet(confirm);
    } else {
        Print("❌ 下单失败: ", GetLastError());
        string err = RelayUrl + "/confirm?order_id=" + orderId +
                    "&status=error&error=" + IntegerToString(GetLastError());
        HttpGet(err);
    }
}

//+------------------------------------------------------------------+
// HTTP POST 请求
//+------------------------------------------------------------------+
void HttpPost(string url, string body) {
    char postData[];
    char resultData[];
    string resultHeaders;
    StringToCharArray(body, postData, 0, StringLen(body));
    int res = WebRequest("POST", url, "", 5000, postData, resultData, resultHeaders);
    if(res == -1) {
        Print("POST 失败: ", GetLastError());
    }
}

//+------------------------------------------------------------------+
// HTTP GET 请求 (MQL4 WebRequest)
//+------------------------------------------------------------------+
string HttpGet(string url) {
    string cookie = "";
    int timeout = 5000;
    char postData[1];  // 空数据
    char resultData[];
    string resultHeaders;
    ArrayResize(postData, 0);
    int res = WebRequest("GET", url, cookie, timeout, postData, resultData, resultHeaders);
    if (res == -1) {
        Print("WebRequest 失败: ", GetLastError());
        return "";
    }
    return CharArrayToString(resultData);
}

//+------------------------------------------------------------------+
// 修改止损止盈
//+------------------------------------------------------------------+
bool ModifySLTP(int ticket, double sl, double tp) {
    if(!OrderSelect(ticket, SELECT_BY_TICKET, MODE_TRADES)) {
        Print("❌ 未找到订单: ", ticket);
        return false;
    }
    int digit = (int)MarketInfo(OrderSymbol(), MODE_DIGITS);
    if(sl > 0) sl = NormalizeDouble(sl, digit);
    if(tp > 0) tp = NormalizeDouble(tp, digit);

    if(OrderModify(ticket, OrderOpenPrice(), sl, tp, 0, clrNONE)) {
        Print("✅ 修改成功: Ticket=", ticket, " SL=", sl, " TP=", tp);
        return true;
    } else {
        Print("❌ 修改失败: Ticket=", ticket, " Error=", GetLastError(), " SL=", sl, " TP=", tp);
        return false;
    }
}

//+------------------------------------------------------------------+
// 平仓
//+------------------------------------------------------------------+
int ClosePositions(string symbol, string direction, int ticket) {
    int closed = 0;
    for(int i = OrdersTotal() - 1; i >= 0; i--) {
        if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
        if(symbol != "" && OrderSymbol() != symbol) continue;
        if(ticket > 0 && OrderTicket() != ticket) continue;
        if(direction == "BUY" && OrderType() != OP_BUY) continue;
        if(direction == "SELL" && OrderType() != OP_SELL) continue;

        double price = 0;
        if(OrderType() == OP_BUY) price = MarketInfo(OrderSymbol(), MODE_BID);
        else if(OrderType() == OP_SELL) price = MarketInfo(OrderSymbol(), MODE_ASK);

        if(OrderClose(OrderTicket(), OrderLots(), price, 30, clrNONE)) {
            closed++;
            Print("✅ 平仓: Ticket=", OrderTicket());
        } else {
            Print("❌ 平仓失败: Ticket=", OrderTicket(), " Error=", GetLastError());
        }
    }
    return closed;
}

//+------------------------------------------------------------------+
// 简单 JSON 值提取 (支持字符串和数字)
//+------------------------------------------------------------------+
string JsonValue(string json, string key) {
    string s = "\"" + key + "\":";
    int p = StringFind(json, s);
    if (p < 0) return "";
    p += StringLen(s);
    while (p < StringLen(json) && StringGetCharacter(json, p) == ' ') p++;

    // 字符串值: "xxx"
    if (StringGetCharacter(json, p) == '"') {
        p++;
        int e = StringFind(json, "\"", p);
        if (e > p) return StringSubstr(json, p, e - p);
    }
    // 数字值: 123 或 1.234
    int e = p;
    while (e < StringLen(json) &&
           (StringGetCharacter(json, e) == '-' ||
            StringGetCharacter(json, e) == '.' ||
            (StringGetCharacter(json, e) >= '0' && StringGetCharacter(json, e) <= '9'))) {
        e++;
    }
    if (e > p) return StringSubstr(json, p, e - p);
    return "";
}
//+------------------------------------------------------------------+
