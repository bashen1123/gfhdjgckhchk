import ast
import json
import math
import os
import re
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("BOT_DB", "bot.db")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "").strip()
TONAPI_KEY = os.getenv("TONAPI_KEY", "").strip()
PROXY_API_URL = os.getenv("PROXY_API_URL", "").strip()
PROXY_REFRESH_SECONDS = int(os.getenv("PROXY_REFRESH_SECONDS", "300"))
PROXY_URL = os.getenv("PROXY_URL", "").strip()

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HTTP_OPENER = None
HTTP_OPENER_BUILT_AT = 0.0
HTTP_OPENER_LOCK = threading.Lock()


HELP = """可用命令

基础:
/calc 100*7.2+88
/payout 1000 7.23 3
/price BTC ETH TRX TON
/rate USD CNY 100

TRON 监控与记账:
/tron_watch 地址 标签
/tron_list
/tron_unwatch 地址
/tron_sync 地址
/ledger
/ledger_add in|out 资产 数量 备注

TON / Telegram NFT:
/ton_nfts TON地址
/tg_asset @username 或 +888号码
/whale_today

OTC 承兑台账:
/otc_new 客户 buy|sell 币种 数量 汇率 备注
/otc_pay 订单ID 金额 备注
/otc_release 订单ID 数量 备注
/otc_fee 订单ID 金额 备注
/otc_close 订单ID
/otc_open
/otc_show 订单ID
/otc_summary
"""


PENDING_INPUTS = {}
PENDING_TTL_SECONDS = 10 * 60


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            create table if not exists tron_watch (
                chat_id integer not null,
                address text not null,
                label text default '',
                last_seen_ms integer default 0,
                created_at text not null,
                primary key (chat_id, address)
            );

            create table if not exists ledger (
                id integer primary key autoincrement,
                chat_id integer not null,
                direction text not null,
                asset text not null,
                amount real not null,
                address text default '',
                txid text default '',
                note text default '',
                created_at text not null,
                unique(chat_id, txid, asset, amount, direction) on conflict ignore
            );

            create table if not exists otc_deals (
                id integer primary key autoincrement,
                chat_id integer not null,
                client text not null,
                side text not null,
                currency text not null,
                amount real not null,
                rate real not null,
                status text not null default 'open',
                note text default '',
                created_at text not null,
                closed_at text default ''
            );

            create table if not exists otc_entries (
                id integer primary key autoincrement,
                deal_id integer not null,
                kind text not null,
                amount real not null,
                note text default '',
                created_at text not null
            );
            """
        )


def _read_url(url, headers=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "telegram-web2-bot/1.0"})
    with DIRECT_OPENER.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _extract_proxy_url(text):
    raw = (text or "").strip()
    if not raw:
        return None
    match = re.search(r"<pre[^>]*>(.*?)</pre>", raw, flags=re.I | re.S)
    if match:
        raw = match.group(1).strip()
    raw = re.sub(r"<[^>]+>", "", raw).strip()
    if not raw:
        return None
    if "://" in raw:
        return raw
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) == 4:
        host, port, username, password = parts
        username = urllib.parse.quote(username, safe="")
        password = urllib.parse.quote(password, safe="")
        return f"http://{username}:{password}@{host}:{port}"
    if len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    return raw


def _env_proxy_map():
    proxies = {}
    all_proxy = os.getenv("ALL_PROXY", "").strip()
    http_proxy = os.getenv("HTTP_PROXY", "").strip()
    https_proxy = os.getenv("HTTPS_PROXY", "").strip()
    if all_proxy:
        proxies["http"] = all_proxy
        proxies["https"] = all_proxy
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    return proxies


def _resolve_proxy_url():
    if PROXY_URL:
        return PROXY_URL
    if not PROXY_API_URL:
        return None
    raw = _read_url(PROXY_API_URL, timeout=HTTP_TIMEOUT)
    return _extract_proxy_url(raw)


def _proxy_display(proxy_url):
    parsed = urllib.parse.urlsplit(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}{port}"


def _build_http_opener():
    proxies = _env_proxy_map()
    proxy_url = None
    try:
        proxy_url = _resolve_proxy_url()
    except Exception as exc:
        print(f"Proxy load failed: {exc}")
    if proxy_url:
        proxies["http"] = proxy_url
        proxies["https"] = proxy_url
        print(f"Using proxy: {_proxy_display(proxy_url)}")
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))


def http_opener(force_refresh=False):
    global HTTP_OPENER, HTTP_OPENER_BUILT_AT
    now = time.monotonic()
    should_refresh = (
        force_refresh
        or HTTP_OPENER is None
        or (PROXY_API_URL and PROXY_REFRESH_SECONDS > 0 and (now - HTTP_OPENER_BUILT_AT) >= PROXY_REFRESH_SECONDS)
    )
    if should_refresh:
        with HTTP_OPENER_LOCK:
            now = time.monotonic()
            if (
                force_refresh
                or HTTP_OPENER is None
                or (PROXY_API_URL and PROXY_REFRESH_SECONDS > 0 and (now - HTTP_OPENER_BUILT_AT) >= PROXY_REFRESH_SECONDS)
            ):
                HTTP_OPENER = _build_http_opener()
                HTTP_OPENER_BUILT_AT = now
    return HTTP_OPENER


def http_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "telegram-web2-bot/1.0"})
    with http_opener().open(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body)


def tg(method, payload):
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(f"{TG_API}/{method}", data=data)
    timeout = max(HTTP_TIMEOUT, int(payload.get("timeout", 0)) + 5)
    with http_opener().open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send(chat_id, text, reply_markup=None):
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for chunk in chunks:
        payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        tg("sendMessage", payload)


def answer_callback(callback_id, text=""):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    tg("answerCallbackQuery", payload)


def parse_args(text):
    return text.strip().split(maxsplit=1)[1:] or [""]


class SafeCalc(ast.NodeVisitor):
    allowed = {
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
        ast.USub, ast.UAdd, ast.Load, ast.Call, ast.Name
    }
    funcs = {"sqrt": math.sqrt, "abs": abs, "round": round, "pow": pow}

    def visit(self, node):
        if type(node) not in self.allowed:
            raise ValueError("表达式包含不允许的内容")
        return super().visit(node)

    def eval(self, expr):
        tree = ast.parse(expr, mode="eval")
        self.visit(tree)
        return eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, self.funcs)


def cmd_calc(chat_id, arg):
    if not arg:
        send(chat_id, "用法: /calc 100*7.2+88")
        return
    try:
        result = SafeCalc().eval(arg)
        send(chat_id, f"{arg} = {result}")
    except Exception as exc:
        send(chat_id, f"计算失败: {exc}")


def cmd_payout(chat_id, arg):
    parts = arg.split()
    if len(parts) < 2:
        send(chat_id, "用法: /payout 数量 汇率 [费用]\n例: /payout 1000 7.23 3")
        return
    try:
        amount = float(parts[0])
        rate = float(parts[1])
        fee = float(parts[2]) if len(parts) >= 3 else 0.0
        gross = amount * rate
        net = gross - fee
        send(chat_id, "\n".join([
            "下发金额换算:",
            f"数量: {amount:g}",
            f"汇率: {rate:g}",
            f"费用: {fee:g}",
            f"应下发: {net:.2f}",
        ]))
    except ValueError:
        send(chat_id, "数量、汇率、费用必须是数字。\n例: /payout 1000 7.23 3")


def cmd_price(chat_id, arg):
    symbols = [x.upper() for x in arg.split()] or ["BTC", "ETH", "TRX", "TON"]
    ids = {
        "BTC": "bitcoin", "ETH": "ethereum", "TRX": "tron", "TON": "the-open-network",
        "USDT": "tether", "BNB": "binancecoin", "SOL": "solana", "DOGE": "dogecoin"
    }
    coin_ids = [ids.get(s, s.lower()) for s in symbols]
    url = "https://api.coingecko.com/api/v3/simple/price?" + urllib.parse.urlencode({
        "ids": ",".join(coin_ids),
        "vs_currencies": "usd,cny",
        "include_24hr_change": "true"
    })
    try:
        data = http_json(url)
        lines = ["实时价格:"]
        for sym, cid in zip(symbols, coin_ids):
            item = data.get(cid)
            if not item:
                lines.append(f"{sym}: 未找到")
                continue
            lines.append(f"{sym}: ${item.get('usd')} / ¥{item.get('cny')}  24h {item.get('usd_24h_change', 0):.2f}%")
        send(chat_id, "\n".join(lines))
    except Exception as exc:
        send(chat_id, f"价格查询失败: {exc}")


def cmd_rate(chat_id, arg):
    parts = arg.split()
    if len(parts) < 2:
        send(chat_id, "用法: /rate USD CNY 100")
        return
    base, target = parts[0].upper(), parts[1].upper()
    amount = float(parts[2]) if len(parts) >= 3 else 1.0
    url = f"https://open.er-api.com/v6/latest/{urllib.parse.quote(base)}"
    try:
        data = http_json(url)
        rate = data.get("rates", {}).get(target)
        if not rate:
            send(chat_id, f"未找到汇率 {base}->{target}")
            return
        send(chat_id, f"{amount:g} {base} = {amount * rate:.4f} {target}\n汇率: {rate:.6f}")
    except Exception as exc:
        send(chat_id, f"汇率查询失败: {exc}")


def tron_headers():
    headers = {"User-Agent": "telegram-web2-bot/1.0"}
    if TRONGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return headers


def fetch_tron_trc20(address, limit=20):
    params = urllib.parse.urlencode({"limit": limit, "only_confirmed": "true", "order_by": "block_timestamp,desc"})
    url = f"https://api.trongrid.io/v1/accounts/{urllib.parse.quote(address)}/transactions/trc20?{params}"
    data = http_json(url, tron_headers())
    return data.get("data", [])


def parse_tron_tx(address, tx):
    token = tx.get("token_info") or {}
    decimals = int(token.get("decimals") or 6)
    raw_value = float(tx.get("value") or 0)
    amount = raw_value / (10 ** decimals)
    asset = token.get("symbol") or token.get("name") or "TRC20"
    from_addr = tx.get("from", "")
    to_addr = tx.get("to", "")
    direction = "in" if to_addr.lower() == address.lower() else "out"
    peer = from_addr if direction == "in" else to_addr
    return {
        "txid": tx.get("transaction_id", ""),
        "ts": int(tx.get("block_timestamp") or 0),
        "direction": direction,
        "asset": asset,
        "amount": amount,
        "peer": peer,
    }


def insert_ledger(chat_id, direction, asset, amount, address="", txid="", note=""):
    with db() as conn:
        cur = conn.execute(
            "insert or ignore into ledger(chat_id,direction,asset,amount,address,txid,note,created_at) values(?,?,?,?,?,?,?,?)",
            (chat_id, direction, asset, amount, address, txid, note, now_iso())
        )
        return cur.rowcount


def cmd_tron_watch(chat_id, arg):
    parts = arg.split(maxsplit=1)
    if not parts:
        send(chat_id, "用法: /tron_watch T地址 标签")
        return
    address = parts[0].strip()
    label = parts[1].strip() if len(parts) > 1 else ""
    try:
        txs = fetch_tron_trc20(address, 1)
        last_seen = int(txs[0].get("block_timestamp") or 0) if txs else 0
        with db() as conn:
            conn.execute(
                "insert or replace into tron_watch(chat_id,address,label,last_seen_ms,created_at) values(?,?,?,?,?)",
                (chat_id, address, label, last_seen, now_iso())
            )
        send(chat_id, f"已监控 TRON 地址:\n{address}\n标签: {label or '-'}\n后续新 TRC20 收支会自动记账。")
    except Exception as exc:
        send(chat_id, f"添加监控失败: {exc}")


def cmd_tron_list(chat_id):
    with db() as conn:
        rows = conn.execute("select address,label,last_seen_ms from tron_watch where chat_id=? order by created_at desc", (chat_id,)).fetchall()
    if not rows:
        send(chat_id, "当前没有监控地址")
        return
    send(chat_id, "\n".join([f"{r['address']}  {r['label'] or '-'}  last={r['last_seen_ms']}" for r in rows]))


def cmd_tron_unwatch(chat_id, arg):
    address = arg.strip()
    if not address:
        send(chat_id, "用法: /tron_unwatch T地址")
        return
    with db() as conn:
        cur = conn.execute("delete from tron_watch where chat_id=? and address=?", (chat_id, address))
    send(chat_id, "已取消监控" if cur.rowcount else "没有找到这个监控地址")


def cmd_tron_sync(chat_id, arg):
    address = arg.strip()
    if not address:
        send(chat_id, "用法: /tron_sync T地址")
        return
    try:
        count = 0
        for tx in fetch_tron_trc20(address, 20):
            item = parse_tron_tx(address, tx)
            count += insert_ledger(chat_id, item["direction"], item["asset"], item["amount"], item["peer"], item["txid"], f"TRON sync {address}")
        send(chat_id, f"同步完成，新增 {count} 条台账。")
    except Exception as exc:
        send(chat_id, f"同步失败: {exc}")


def cmd_ledger(chat_id, arg):
    limit = 20
    if arg.strip().isdigit():
        limit = min(100, int(arg.strip()))
    with db() as conn:
        rows = conn.execute("select * from ledger where chat_id=? order by id desc limit ?", (chat_id, limit)).fetchall()
    if not rows:
        send(chat_id, "暂无台账")
        return
    lines = ["最近台账:"]
    for r in rows:
        sign = "+" if r["direction"] == "in" else "-"
        lines.append(f"#{r['id']} {r['created_at']} {sign}{r['amount']:g} {r['asset']} {r['note']}")
    send(chat_id, "\n".join(lines))


def cmd_ledger_add(chat_id, arg):
    parts = arg.split(maxsplit=3)
    if len(parts) < 3:
        send(chat_id, "用法: /ledger_add in|out USDT 100 备注")
        return
    direction, asset, amount = parts[0], parts[1].upper(), float(parts[2])
    note = parts[3] if len(parts) > 3 else "manual"
    if direction not in ("in", "out"):
        send(chat_id, "方向必须是 in 或 out")
        return
    insert_ledger(chat_id, direction, asset, amount, note=note)
    send(chat_id, f"已记账: {direction} {amount:g} {asset} {note}")


def cmd_ton_nfts(chat_id, arg):
    address = arg.strip()
    if not address:
        send(chat_id, "用法: /ton_nfts TON地址")
        return
    headers = {"User-Agent": "telegram-web2-bot/1.0"}
    if TONAPI_KEY:
        headers["Authorization"] = f"Bearer {TONAPI_KEY}"
    url = f"https://tonapi.io/v2/accounts/{urllib.parse.quote(address)}/nfts?limit=20&offset=0"
    try:
        data = http_json(url, headers)
        items = data.get("nft_items", [])
        if not items:
            send(chat_id, "没有查询到 NFT 或接口无数据")
            return
        lines = [f"TON NFT 资产 Top {len(items)}:"]
        for item in items:
            meta = item.get("metadata") or {}
            collection = (item.get("collection") or {}).get("name", "-")
            lines.append(f"- {meta.get('name') or item.get('address')}\n  collection: {collection}\n  address: {item.get('address')}")
        send(chat_id, "\n".join(lines))
    except Exception as exc:
        send(chat_id, f"TON NFT 查询失败: {exc}")


def cmd_tg_asset(chat_id, arg):
    asset = arg.strip()
    if not asset:
        send(chat_id, "用法: /tg_asset @username 或 +888号码")
        return
    normalized = asset.lstrip("@").replace(" ", "")
    if normalized.startswith("+"):
        normalized = normalized[1:]
    fragment_url = f"https://fragment.com/username/{urllib.parse.quote(normalized)}"
    if normalized.startswith("888"):
        fragment_url = f"https://fragment.com/number/{urllib.parse.quote(normalized)}"
    send(chat_id, f"Telegram 收藏资产查询入口:\n{asset}\nFragment: {fragment_url}\n\n如需机器人内返回成交价，需要配置可用的 TON NFT 市场数据源/API。")


def cmd_whale_today(chat_id):
    send(chat_id, "今日 Telegram NFT 巨鲸播报框架已启用。\n当前未配置市场成交数据 API，因此只返回提示。可接入 TonAPI/Fragment/Getgems 成交流后按成交额排序播报。")


def cmd_otc_new(chat_id, arg):
    parts = arg.split(maxsplit=5)
    if len(parts) < 5:
        send(chat_id, "用法: /otc_new 客户 buy|sell USDT 1000 7.23 备注")
        return
    client, side, currency = parts[0], parts[1].lower(), parts[2].upper()
    amount, rate = float(parts[3]), float(parts[4])
    note = parts[5] if len(parts) > 5 else ""
    if side not in ("buy", "sell"):
        send(chat_id, "side 必须是 buy 或 sell")
        return
    with db() as conn:
        cur = conn.execute(
            "insert into otc_deals(chat_id,client,side,currency,amount,rate,status,note,created_at) values(?,?,?,?,?,?,?,?,?)",
            (chat_id, client, side, currency, amount, rate, "open", note, now_iso())
        )
        deal_id = cur.lastrowid
    send(chat_id, f"OTC 订单已创建 #{deal_id}\n客户: {client}\n方向: {side}\n数量: {amount:g} {currency}\n汇率: {rate:g}\n应收/应付: {amount * rate:.2f}")


def otc_entry(chat_id, arg, kind):
    parts = arg.split(maxsplit=2)
    if len(parts) < 2:
        send(chat_id, f"用法: /otc_{kind} 订单ID 金额 备注")
        return
    deal_id, amount = int(parts[0]), float(parts[1])
    note = parts[2] if len(parts) > 2 else ""
    with db() as conn:
        deal = conn.execute("select id from otc_deals where id=? and chat_id=?", (deal_id, chat_id)).fetchone()
        if not deal:
            send(chat_id, "订单不存在")
            return
        conn.execute("insert into otc_entries(deal_id,kind,amount,note,created_at) values(?,?,?,?,?)", (deal_id, kind, amount, note, now_iso()))
    send(chat_id, f"已记录 #{deal_id}: {kind} {amount:g} {note}")


def cmd_otc_close(chat_id, arg):
    if not arg.strip().isdigit():
        send(chat_id, "用法: /otc_close 订单ID")
        return
    deal_id = int(arg.strip())
    with db() as conn:
        cur = conn.execute("update otc_deals set status='closed', closed_at=? where id=? and chat_id=?", (now_iso(), deal_id, chat_id))
    send(chat_id, "订单已结清" if cur.rowcount else "订单不存在")


def cmd_otc_open(chat_id):
    with db() as conn:
        rows = conn.execute("select * from otc_deals where chat_id=? and status='open' order by id desc limit 30", (chat_id,)).fetchall()
    if not rows:
        send(chat_id, "没有未结 OTC 订单")
        return
    lines = ["未结 OTC 订单:"]
    for r in rows:
        lines.append(f"#{r['id']} {r['client']} {r['side']} {r['amount']:g} {r['currency']} @ {r['rate']:g} = {r['amount'] * r['rate']:.2f}")
    send(chat_id, "\n".join(lines))


def cmd_otc_show(chat_id, arg):
    if not arg.strip().isdigit():
        send(chat_id, "用法: /otc_show 订单ID")
        return
    deal_id = int(arg.strip())
    with db() as conn:
        deal = conn.execute("select * from otc_deals where id=? and chat_id=?", (deal_id, chat_id)).fetchone()
        entries = conn.execute("select * from otc_entries where deal_id=? order by id", (deal_id,)).fetchall()
    if not deal:
        send(chat_id, "订单不存在")
        return
    lines = [
        f"OTC #{deal['id']} {deal['status']}",
        f"客户: {deal['client']}",
        f"方向: {deal['side']} {deal['amount']:g} {deal['currency']} @ {deal['rate']:g}",
        f"法币金额: {deal['amount'] * deal['rate']:.2f}",
        f"备注: {deal['note'] or '-'}",
        "流水:"
    ]
    totals = {}
    for e in entries:
        totals[e["kind"]] = totals.get(e["kind"], 0.0) + e["amount"]
        lines.append(f"- {e['kind']} {e['amount']:g} {e['note']}")
    lines.append("汇总: " + ", ".join(f"{k}={v:g}" for k, v in totals.items()) if totals else "汇总: 无流水")
    send(chat_id, "\n".join(lines))


def cmd_otc_summary(chat_id):
    with db() as conn:
        rows = conn.execute(
            "select currency, side, status, count(*) c, sum(amount) amount, sum(amount*rate) fiat from otc_deals where chat_id=? group by currency,side,status",
            (chat_id,)
        ).fetchall()
    if not rows:
        send(chat_id, "暂无 OTC 数据")
        return
    lines = ["OTC 汇总:"]
    for r in rows:
        lines.append(f"{r['status']} {r['side']} {r['currency']}: {r['c']}单, {r['amount'] or 0:g}, 法币 {r['fiat'] or 0:.2f}")
    send(chat_id, "\n".join(lines))


BUTTON_ROWS = [
    [
        {"text": "价格查询", "callback_data": "ask:price"},
        {"text": "汇率换算", "callback_data": "ask:rate"},
    ],
    [
        {"text": "下发金额", "callback_data": "ask:payout"},
        {"text": "计算器", "callback_data": "ask:calc"},
    ],
    [
        {"text": "记账加减", "callback_data": "ask:ledger_add"},
        {"text": "查看台账", "callback_data": "run:ledger"},
    ],
    [
        {"text": "TRON 同步", "callback_data": "ask:tron_sync"},
        {"text": "监控地址", "callback_data": "ask:tron_watch"},
    ],
    [
        {"text": "新建 OTC", "callback_data": "ask:otc_new"},
        {"text": "OTC 付款", "callback_data": "ask:otc_pay"},
    ],
    [
        {"text": "OTC 下发/放行", "callback_data": "ask:otc_release"},
        {"text": "OTC 费用", "callback_data": "ask:otc_fee"},
    ],
    [
        {"text": "未结订单", "callback_data": "run:otc_open"},
        {"text": "OTC 汇总", "callback_data": "run:otc_summary"},
    ],
    [
        {"text": "查订单", "callback_data": "ask:otc_show"},
        {"text": "取消输入", "callback_data": "run:cancel"},
    ],
]

INPUT_ACTIONS = {
    "calc": ("计算器", "请输入表达式，例如: 100*7.2+88", cmd_calc),
    "payout": ("下发金额", "请输入: 数量 汇率 [费用]\n例如: 1000 7.23 3", cmd_payout),
    "price": ("价格查询", "请输入币种，例如: BTC ETH TRX TON", cmd_price),
    "rate": ("汇率换算", "请输入: 源币种 目标币种 金额\n例如: USD CNY 100", cmd_rate),
    "ledger_add": ("记账加减", "请输入: in|out 资产 数量 备注\n例如: in USDT 100 客户入款", cmd_ledger_add),
    "tron_watch": ("监控地址", "请输入: TRON地址 标签\n例如: Txxx 客户A", cmd_tron_watch),
    "tron_sync": ("TRON 同步", "请输入 TRON 地址，例如: Txxx", cmd_tron_sync),
    "otc_new": ("新建 OTC", "请输入: 客户 buy|sell 币种 数量 汇率 备注\n例如: 张三 buy USDT 1000 7.23 备注", cmd_otc_new),
    "otc_pay": ("OTC 付款", "请输入: 订单ID 金额 备注\n例如: 1 7230 客户付款", lambda chat_id, arg: otc_entry(chat_id, arg, "pay")),
    "otc_release": ("OTC 下发/放行", "请输入: 订单ID 数量 备注\n例如: 1 1000 已下发", lambda chat_id, arg: otc_entry(chat_id, arg, "release")),
    "otc_fee": ("OTC 费用", "请输入: 订单ID 金额 备注\n例如: 1 3 手续费", lambda chat_id, arg: otc_entry(chat_id, arg, "fee")),
    "otc_show": ("查订单", "请输入订单ID，例如: 1", cmd_otc_show),
}

RUN_ACTIONS = {
    "ledger": lambda chat_id: cmd_ledger(chat_id, ""),
    "otc_open": cmd_otc_open,
    "otc_summary": cmd_otc_summary,
}


def send_menu(chat_id):
    send(
        chat_id,
        "请选择功能按钮，然后按提示输入信息即可查询或记账。",
        {"inline_keyboard": BUTTON_ROWS}
    )


def set_pending(chat_id, action):
    PENDING_INPUTS[chat_id] = {"action": action, "created_at": time.time()}


def pop_pending(chat_id):
    state = PENDING_INPUTS.pop(chat_id, None)
    if not state:
        return None
    if time.time() - state["created_at"] > PENDING_TTL_SECONDS:
        return None
    return state


def handle_pending_input(chat_id, text):
    state = pop_pending(chat_id)
    if not state:
        return False
    action = state["action"]
    config = INPUT_ACTIONS.get(action)
    if not config:
        send(chat_id, "输入状态已失效，请重新点按钮。")
        return True
    _, _, handler = config
    handler(chat_id, text.strip())
    return True


def handle_callback(query):
    callback_id = query.get("id")
    data = query.get("data") or ""
    message = query.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    if callback_id:
        answer_callback(callback_id)
    if not chat_id:
        return
    if data == "run:cancel":
        PENDING_INPUTS.pop(chat_id, None)
        send(chat_id, "已取消当前输入。")
        return
    if data.startswith("ask:"):
        action = data.split(":", 1)[1]
        config = INPUT_ACTIONS.get(action)
        if not config:
            send(chat_id, "这个按钮暂不可用。")
            return
        title, prompt, _ = config
        set_pending(chat_id, action)
        send(chat_id, f"{title}\n{prompt}")
        return
    if data.startswith("run:"):
        action = data.split(":", 1)[1]
        handler = RUN_ACTIONS.get(action)
        if not handler:
            send(chat_id, "这个按钮暂不可用。")
            return
        handler(chat_id)


def dispatch(message):
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text") or ""
    if not chat_id or not text:
        return
    if not text.startswith("/") and handle_pending_input(chat_id, text):
        return
    if not text.startswith("/"):
        send_menu(chat_id)
        return
    cmd = text.split()[0].split("@")[0].lower()
    arg = parse_args(text)[0]
    try:
        if cmd in ("/start", "/help"):
            send(chat_id, HELP)
            send_menu(chat_id)
        elif cmd == "/calc":
            cmd_calc(chat_id, arg)
        elif cmd == "/payout":
            cmd_payout(chat_id, arg)
        elif cmd == "/price":
            cmd_price(chat_id, arg)
        elif cmd == "/rate":
            cmd_rate(chat_id, arg)
        elif cmd == "/menu":
            send_menu(chat_id)
        elif cmd == "/tron_watch":
            cmd_tron_watch(chat_id, arg)
        elif cmd == "/tron_list":
            cmd_tron_list(chat_id)
        elif cmd == "/tron_unwatch":
            cmd_tron_unwatch(chat_id, arg)
        elif cmd == "/tron_sync":
            cmd_tron_sync(chat_id, arg)
        elif cmd == "/ledger":
            cmd_ledger(chat_id, arg)
        elif cmd == "/ledger_add":
            cmd_ledger_add(chat_id, arg)
        elif cmd == "/ton_nfts":
            cmd_ton_nfts(chat_id, arg)
        elif cmd == "/tg_asset":
            cmd_tg_asset(chat_id, arg)
        elif cmd == "/whale_today":
            cmd_whale_today(chat_id)
        elif cmd == "/otc_new":
            cmd_otc_new(chat_id, arg)
        elif cmd == "/otc_pay":
            otc_entry(chat_id, arg, "pay")
        elif cmd == "/otc_release":
            otc_entry(chat_id, arg, "release")
        elif cmd == "/otc_fee":
            otc_entry(chat_id, arg, "fee")
        elif cmd == "/otc_close":
            cmd_otc_close(chat_id, arg)
        elif cmd == "/otc_open":
            cmd_otc_open(chat_id)
        elif cmd == "/otc_show":
            cmd_otc_show(chat_id, arg)
        elif cmd == "/otc_summary":
            cmd_otc_summary(chat_id)
        else:
            send(chat_id, "未知命令，发送 /help 查看功能。")
    except Exception as exc:
        send(chat_id, f"命令执行失败: {exc}")
        traceback.print_exc()


def tron_monitor_loop():
    while True:
        try:
            with db() as conn:
                rows = conn.execute("select * from tron_watch").fetchall()
            for watch in rows:
                chat_id = watch["chat_id"]
                address = watch["address"]
                last_seen = int(watch["last_seen_ms"] or 0)
                newest = last_seen
                new_items = []
                for tx in reversed(fetch_tron_trc20(address, 20)):
                    item = parse_tron_tx(address, tx)
                    newest = max(newest, item["ts"])
                    if item["ts"] <= last_seen:
                        continue
                    added = insert_ledger(chat_id, item["direction"], item["asset"], item["amount"], item["peer"], item["txid"], f"TRON watch {watch['label'] or address}")
                    if added:
                        new_items.append(item)
                if newest > last_seen:
                    with db() as conn:
                        conn.execute("update tron_watch set last_seen_ms=? where chat_id=? and address=?", (newest, chat_id, address))
                for item in new_items:
                    sign = "+" if item["direction"] == "in" else "-"
                    send(chat_id, f"TRON 收支提醒 {watch['label'] or ''}\n{address}\n{sign}{item['amount']:g} {item['asset']}\n对手方: {item['peer']}\nTX: {item['txid']}")
        except Exception:
            traceback.print_exc()
        time.sleep(POLL_INTERVAL)


def polling_loop():
    offset = 0
    while True:
        try:
            data = tg("getUpdates", {"timeout": 30, "offset": offset, "allowed_updates": json.dumps(["message", "callback_query"])})
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    dispatch(update["message"])
                elif "callback_query" in update:
                    try:
                        handle_callback(update["callback_query"])
                    except Exception:
                        traceback.print_exc()
        except urllib.error.HTTPError as exc:
            print("Telegram HTTP error:", exc.read().decode("utf-8", errors="replace"))
            time.sleep(5)
        except Exception:
            traceback.print_exc()
            time.sleep(5)


def main():
    if not BOT_TOKEN:
        raise SystemExit("请先设置 BOT_TOKEN。复制 .env.example 为 .env，然后填入 Telegram Bot Token。")
    init_db()
    threading.Thread(target=tron_monitor_loop, daemon=True).start()
    print("Telegram Web2 bot started.")
    polling_loop()


if __name__ == "__main__":
    main()
