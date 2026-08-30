"""
DarkerDB AI Trader - 长期趋势版（最终定稿版 v3）
============================================
- 14 个原核心目标 + 6 个高价值粉末 = 20 个监控物品
- 所有物品先经 resolve_archetype_id() 拿到真实 item_id
- /v2/market 兜底 + 多维信号 + SQLite 长期存储
- OpenRouter AI 分析 + Server酱推送 + Git 提交
"""
import os
import json
import re
import time
import sqlite3
import requests
import subprocess
import numpy as np
from datetime import datetime, timedelta, timezone

# ===== 配置 =====
_raw_keys = os.environ.get("DARKERDB_KEYS", "").strip()
if not _raw_keys:
    _single = os.environ.get("DARKERDB_KEY", "").strip()
    if _single:
        _raw_keys = _single
DARKERDB_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]

OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
SERVERCHAN_SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")

API_BASE = "https://api.darkerdb.com/v2"

# ===== 长期历史配置 =====
HISTORY_DAYS = 90
DATA_RETENTION_DAYS = 365
DB_FILE = "price_history.db"

# ===== 信号/采样配置 =====
MIN_SAMPLES = 3
LISTING_WINDOW_HOURS = 6
SALE_WINDOW_HOURS = 24
DEBUG = True

SHORT_WINDOW = 7
LONG_WINDOW = 30
SLOPE_THRESHOLD = 0.5
VOLATILITY_PENALTY = 15
CONFIDENCE_THRESHOLD = 0.5

# ===== WATCHLIST（硬编码，一劳永逸）=====
WATCHLIST = [
    # ---- 原 8 个核心目标 ----
    ("Troll Pelt", "epic", 0.12),
    ("Troll's Blood", "epic", 0.12),
    ("Ruby", "legendary", 0.15),
    ("Sapphire", "legendary", 0.15),
    ("Obsidian Ore", "epic", 0.15),
    ("Rubysilver Ore", "epic", 0.15),
    ("Gold Ore", "epic", 0.20),
    ("Diamond", "legendary", 0.20),

    # ---- 第二梯队：高价值非装备（≥600）----
    ("Arcane Essence", "legendary", 0.15),
    ("Arcane Essence", "unique", 0.15),
    ("Gold Coin Bag", "unique", 0.15),
    ("Gold Coin Pouch", "unique", 0.15),
    ("Gold Coin Chest", "unique", 0.15),
    ("Spectral Coinbag", "unique", 0.15),

    # ---- 第三梯队：高价值粉末（epic，市场价≥600）----
    ("Obsidian Powder", "epic", 0.15),
    ("Rubysilver Powder", "epic", 0.15),
    ("Froststone Powder", "epic", 0.15),
    ("Tidestone Powder", "epic", 0.15),
    ("Brimstone Powder", "epic", 0.15),
    ("Gold Powder", "epic", 0.15),
]

ACCOUNT_STATE_FILE = "account_state.json"

# ===== SQLite 初始化 =====
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item TEXT NOT NULL,
            rarity TEXT NOT NULL,
            price REAL NOT NULL,
            timestamp TEXT NOT NULL,
            UNIQUE(item, rarity, timestamp)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_item_ts ON price_history(item, rarity, timestamp)")
    conn.commit()
    conn.close()

def save_price_to_db(item, rarity, price, timestamp):
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO price_history (item, rarity, price, timestamp) VALUES (?, ?, ?, ?)",
            (item, rarity, price, timestamp)
        )
        conn.commit()
    finally:
        conn.close()

def get_price_series(item, rarity, days=HISTORY_DAYS):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT timestamp, price FROM price_history WHERE item=? AND rarity=? AND timestamp>=? ORDER BY timestamp ASC",
        (item, rarity, cutoff)
    ).fetchall()
    conn.close()
    out = []
    for ts, price in rows:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            out.append((dt, price))
        except Exception:
            continue
    return out

def clean_old_data():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DATA_RETENTION_DAYS)).isoformat()
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM price_history WHERE timestamp<?", (cutoff,))
    conn.commit()
    conn.close()

# ===== 账号轮转 =====
def load_account_state():
    if os.path.exists(ACCOUNT_STATE_FILE):
        try:
            with open(ACCOUNT_STATE_FILE) as f:
                idx = json.load(f).get("current_key_index", 0)
                if 0 <= idx < len(DARKERDB_KEYS):
                    return idx
        except Exception:
            pass
    return 0

def save_account_state(idx):
    with open(ACCOUNT_STATE_FILE, "w") as f:
        json.dump({"current_key_index": idx}, f)

def safe_get(url, params=None, retries=3):
    if not DARKERDB_KEYS:
        print("❌ 未配置 DARKERDB_KEYS")
        return None
    current_idx = load_account_state()
    headers_base = {
        "X-API-Version": "2026-08-03",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    for _ in range(retries):
        for key_offset in range(len(DARKERDB_KEYS)):
            key_idx = (current_idx + key_offset) % len(DARKERDB_KEYS)
            headers = {**headers_base, "X-API-Key": DARKERDB_KEYS[key_idx]}
            try:
                r = requests.get(url, headers=headers, params=params or {}, timeout=30)
                save_account_state(key_idx)
                if r.status_code == 200:
                    remaining = r.headers.get("X-RateLimit-Remaining")
                    if remaining is not None:
                        remaining = int(remaining)
                        limit = int(r.headers.get("X-RateLimit-Limit", 60))
                        if remaining < limit * 0.1:
                            print(f"    ⚠️ Key[{key_idx}] 额度快耗尽 (剩余{remaining}/{limit})，下次切换")
                            save_account_state((key_idx + 1) % len(DARKERDB_KEYS))
                    return r
                elif r.status_code == 429:
                    retry_after = int(r.headers.get("Retry-After", 5))
                    print(f"    ⚠️ 429 限流，等待 {retry_after}s")
                    time.sleep(retry_after)
                    continue
                elif r.status_code in (403, 401):
                    if DEBUG:
                        print(f"    ⚠️ {r.status_code} {url}: {r.text[:150]}")
                    return r
                else:
                    if DEBUG:
                        print(f"    ⚠️ HTTP {r.status_code}: {url} {r.text[:150]}")
                    return None
            except Exception as e:
                if DEBUG:
                    print(f"    ⚠️ 请求异常: {e}")
                continue
        time.sleep(3)
    return None

def norm(s):
    return (s or "").strip().lower().replace("’", "'").replace("'", "").replace("-", " ").replace("_", " ").replace("(", "").replace(")", "").replace(":", "")

# ===== 核心：真实 item_id 解析 =====
def resolve_archetype_id(name):
    r = safe_get(f"{API_BASE}/search", {"q": name, "limit": 5})
    if not r or r.status_code != 200:
        return None
    data = r.json()
    body = data.get("body", {})
    results = body.get("results", []) if isinstance(body, dict) else []
    name_n = norm(name)
    for item in results:
        if not isinstance(item, dict) or item.get("type") != "item":
            continue
        if name_n == norm(item.get("name", "")):
            found = item.get("id")
            if DEBUG:
                print(f"    [DEBUG] archetype_id for '{name}': {found} (精确匹配)")
            return found
    for item in results:
        if not isinstance(item, dict) or item.get("type") != "item":
            continue
        iname = norm(item.get("name", ""))
        if name_n in iname or iname in name_n:
            found = item.get("id")
            if DEBUG:
                print(f"    [DEBUG] archetype_id for '{name}': {found} (模糊匹配 '{item.get('name')}')")
            return found
    if DEBUG:
        print(f"    [DEBUG] archetype_id for '{name}': 未找到")
    return None

# ===== 价格获取 =====
def get_fresh_price_checks(item_id, rarity):
    if not item_id:
        return None
    params = {"item_id": item_id, "rarity": rarity}
    r = safe_get(f"{API_BASE}/price-checks", params)
    if not r or r.status_code != 200:
        return None
    data = r.json()
    body = data.get("body")
    if not body:
        return None
    now = datetime.now(timezone.utc)
    listing_cutoff = now - timedelta(hours=LISTING_WINDOW_HOURS)
    sale_cutoff = now - timedelta(hours=SALE_WINDOW_HOURS)
    fresh_prices = []
    for listing in body.get("similar_listings", []):
        listed_at = listing.get("listed_at")
        if not listed_at:
            continue
        try:
            lt = datetime.fromisoformat(listed_at.replace("Z", "+00:00"))
            if lt >= listing_cutoff:
                p = listing.get("price")
                if p and p > 0:
                    fresh_prices.append(float(p))
        except Exception:
            continue
    source = "listings"
    if len(fresh_prices) < MIN_SAMPLES:
        for sale in body.get("similar_sales", []):
            sold_at = sale.get("sold_at")
            if not sold_at:
                continue
            try:
                st = datetime.fromisoformat(sold_at.replace("Z", "+00:00"))
                if st >= sale_cutoff:
                    p = sale.get("price")
                    if p and p > 0:
                        fresh_prices.append(float(p))
            except Exception:
                continue
        if len(fresh_prices) >= MIN_SAMPLES:
            source = "mixed"
    if len(fresh_prices) < MIN_SAMPLES:
        return None
    sorted_p = sorted(fresh_prices)
    median_price = sorted_p[len(sorted_p) // 2]
    min_price = sorted_p[0]
    return {
        "prices": fresh_prices,
        "sample_count": len(fresh_prices),
        "trimmed_avg": round(median_price, 2),
        "min_price": min_price,
        "source": source,
    }

def get_price_from_market_fallback(archetype_id, rarity):
    if not archetype_id:
        if DEBUG:
            print("    [DEBUG] market 兜底跳过：archetype_id 为空")
        return None
    params = {"archetype": archetype_id, "rarity": rarity, "limit": 20, "listing_state": "active"}
    r = safe_get(f"{API_BASE}/market", params)
    if not r or r.status_code != 200:
        if DEBUG:
            print(f"    [DEBUG] /v2/market 请求失败: {r.status_code if r else 'None'}")
        return None
    try:
        data = r.json()
    except Exception as e:
        if DEBUG:
            print(f"    [DEBUG] /v2/market JSON 解析失败: {e}")
        return None
    body = data.get("body")
    if not body:
        if DEBUG:
            print("    [DEBUG] /v2/market body 为空")
        return None
    if isinstance(body, dict):
        listings = body.get("listings", [])
    elif isinstance(body, list):
        listings = body
    else:
        listings = []
    prices = []
    for l in listings:
        try:
            p = float(l.get("price"))
            if p and p > 0:
                prices.append(p)
        except Exception:
            continue
    if len(prices) < MIN_SAMPLES:
        if DEBUG:
            print(f"    [DEBUG] /v2/market 样本不足: {len(prices)} < {MIN_SAMPLES}")
        return None
    prices = sorted(prices)
    median_price = prices[len(prices) // 2]
    min_price = prices[0]
    avg_price = sum(prices) / len(prices)
    final = min(median_price, avg_price)
    if DEBUG:
        print(f"    [DEBUG] /v2/market 兜底成功: 样本{len(prices)} 中位数{median_price} 均价{avg_price:.1f}")
    return {
        "prices": prices,
        "sample_count": len(prices),
        "trimmed_avg": round(final, 2),
        "min_price": min_price,
        "source": "market_fallback",
    }

def fetch_price(name, rarity):
    archetype_id = resolve_archetype_id(name)
    if not archetype_id:
        return None, None
    result = get_fresh_price_checks(archetype_id, rarity)
    used_fallback = False
    if not result:
        if DEBUG:
            print("    [DEBUG] price-checks 无数据，尝试 /v2/market 兜底")
        result = get_price_from_market_fallback(archetype_id, rarity)
        used_fallback = True
    return result, archetype_id

# ===== 多维指标 =====
def calc_indicators(series, short_window=SHORT_WINDOW, long_window=LONG_WINDOW):
    if len(series) < short_window:
        return None
    prices = [p for _, p in series]
    if len(prices) < long_window:
        sma_short = sum(prices[-short_window:]) / short_window
        sma_long = sma_short
    else:
        sma_short = sum(prices[-short_window:]) / short_window
        sma_long = sum(prices[-long_window:]) / long_window

    y_short = prices[-short_window:]
    x_short = list(range(short_window))
    try:
        slope = np.polyfit(x_short, y_short, 1)[0]
    except Exception:
        slope = 0
    sma_ref = sma_short if sma_short > 0 else 1
    slope_pct = slope / sma_ref * 100

    current = prices[-1]
    deviation = (current - sma_short) / sma_ref * 100

    std_val = float(np.std(y_short)) if len(y_short) > 1 else 0
    volatility = std_val / sma_ref * 100

    if len(prices) >= 7:
        recent = sum(prices[-3:]) / 3
        prev = sum(prices[-7:-3]) / 4 if len(prices) >= 7 else prices[-4]
        momentum = recent - prev
        momentum_pct = momentum / sma_ref * 100
    else:
        momentum_pct = 0.0

    return {
        "sma_short": sma_short,
        "sma_long": sma_long,
        "slope_pct": slope_pct,
        "deviation": deviation,
        "volatility": volatility,
        "momentum_pct": momentum_pct,
        "current": current,
        "is_golden_cross": sma_short > sma_long,
        "sample_size": len(prices),
    }

def generate_signal(ind):
    if ind is None:
        return "HOLD", 0.0, "样本不足"
    score = 0.0
    signals = []

    if ind["is_golden_cross"] and ind["slope_pct"] > SLOPE_THRESHOLD:
        signals.append("趋势向上↑")
        score += 0.30
    elif not ind["is_golden_cross"] and ind["slope_pct"] < -SLOPE_THRESHOLD:
        signals.append("趋势向下↓")
        score -= 0.30

    if ind["is_golden_cross"] and ind["deviation"] < -2:
        signals.append("上升趋势中的回调")
        score += 0.25
    elif not ind["is_golden_cross"] and ind["deviation"] > 2:
        signals.append("下降趋势中的反弹")
        score -= 0.15

    if ind["momentum_pct"] > 1:
        signals.append("动量为正")
        score += 0.15
    elif ind["momentum_pct"] < -1:
        signals.append("动量为负")
        score -= 0.15

    if ind["current"] < ind["sma_long"] * 0.95:
        signals.append("价格低于长期均线")
        score += 0.10

    if ind["volatility"] > VOLATILITY_PENALTY:
        signals.append("⚠️ 高波动")
        score *= 0.6

    score = max(-1.0, min(1.0, score))
    if score >= CONFIDENCE_THRESHOLD:
        return "BUY", score, " + ".join(signals) if signals else "综合偏多"
    elif score <= -CONFIDENCE_THRESHOLD:
        return "SELL", abs(score), " + ".join(signals) if signals else "综合偏空"
    else:
        return "HOLD", abs(score), " + ".join(signals) if signals else "信号不明确"

def simple_signal(price, hist_avg, min_margin):
    listing_fee = max(price * 0.05, 15)
    if hist_avg > price + listing_fee:
        profit_margin = (hist_avg - price - listing_fee) / price
    else:
        profit_margin = 0.0
    dev = ((price - hist_avg) / hist_avg * 100) if hist_avg > 0 else 0
    if dev < -15 and profit_margin >= min_margin:
        return "BUY", profit_margin, f"偏离{dev:+.1f}% 预估利润{profit_margin*100:.1f}%"
    elif dev > 20:
        return "SELL", 0.0, f"偏离{dev:+.1f}%"
    else:
        return "HOLD", profit_margin, f"偏离{dev:+.1f}%"

# ===== AI 分析 =====
def _try_fix_json(text):
    if text.count('"') % 2 != 0:
        text += '"'
    open_braces = text.count("{")
    close_braces = text.count("}")
    if open_braces > close_braces:
        text += "}" * (open_braces - close_braces)
    return text

def extract_json(text):
    if not text or "User Safety" in text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            try:
                return json.loads(_try_fix_json(candidate))
            except Exception:
                pass
    return None

def analyze_with_ai(current_data, memory_context):
    prompt = f"""任务：分析以下价格数据，输出 JSON 格式的分析报告。

当前价格数据（物品|品质, 当前均价, 历史均价, 偏离%, 多维信号）:
{json.dumps(current_data, ensure_ascii=False, indent=2)}

历史数据（最近{HISTORY_DAYS}天）:
{json.dumps(memory_context, ensure_ascii=False, indent=2)}

要求：
1. 只输出一个合法 JSON 对象，不要其他文字。
2. 结构：
{{"analyses":[{{"item":"物品名|品质","signal":"BUY/SELL/HOLD","current_price":数值,
"reason":"中文原因","trend":"上涨/下跌/震荡/样本不足","trend_basis":"依据",
"advice":"建议","risk":"低/中/高","position_note":null}}],"summary":"1-2句总结"}}
3. 必须为 current_data 中每个物品输出一条分析。
4. signal 规则（挂牌费=售价5%或15金取高）：
   - 单价≤300金，15金最低费吃掉利润，不给 BUY
   - 单价300-500金，要求偏离≤-20%且利润≥20%才 BUY
   - 单价>500金，偏离≤-15%且利润≥15%即可 BUY
   - 偏离>20% -> SELL，其余 HOLD
5. 全部中文，简洁。"""
    headers = {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
    models = [
        "openrouter/free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "openai/gpt-oss-20b:free",
        "qwen/qwen3-next-80b-a3b-instruct:free",
    ]
    for model in models:
        if not OPENROUTER_KEY:
            break
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 8000}
        try:
            r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                              headers=headers, json=payload, timeout=120)
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                if DEBUG:
                    print(f"    [DEBUG] AI via {model}: {content[:300]}")
                if "User Safety" in content or (content.strip().lower().startswith("safe") and len(content) < 80):
                    continue
                parsed = extract_json(content)
                if parsed and parsed.get("analyses"):
                    return content
            elif r.status_code == 404:
                continue
        except Exception as e:
            print(f"    ⚠️ {model} error: {e}")
        time.sleep(0.5)
    return None

# ===== Server酱 推送 =====
def push_to_serverchan(title, content):
    sendkey = SERVERCHAN_SENDKEY
    if not sendkey:
        print("⚠️ 未配置 SERVERCHAN_SENDKEY")
        return
    if sendkey.startswith("sctp"):
        m = re.search(r'^sctp(\d+)t', sendkey)
        if m:
            url = f"https://{m.group(1)}.push.ft07.com/send/{sendkey}.send"
        else:
            print("⚠️ SendKey 格式错误")
            return
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
    if len(content) > 32000:
        content = content[:32000] + "\n...(截断)"
    try:
        res = requests.post(url, data={"title": title, "desp": content}, timeout=10).json()
        print("✅ 已推送到微信" if res.get("code") == 0 else f"⚠️ 推送失败: {res}")
    except Exception as e:
        print(f"⚠️ 推送异常: {e}")

def format_report(analysis_text):
    data = extract_json(analysis_text)
    if not data:
        return None
    analyses = data.get("analyses", [])
    if not analyses:
        return None
    lines = [f"📊 **DarkerDB 长期分析报告** | {datetime.now().strftime('%Y-%m-%d %H:%M')} (窗口:{HISTORY_DAYS}天)", "=" * 78]
    sc = {"BUY": 0, "SELL": 0, "HOLD": 0}
    analyses.sort(key=lambda x: (x.get("signal") != "BUY", x.get("signal") != "SELL"))
    for a in analyses:
        sig = a.get("signal", "HOLD")
        sc[sig] = sc.get(sig, 0) + 1
        em = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(sig, "⚪")
        lines += [
            f"\n{em} **{a.get('item','?')}** — {sig}",
            f"   当前均价: {a.get('current_price','?')}",
            f"   💡 原因: {a.get('reason','N/A')}",
            f"   📈 趋势: {a.get('trend','?')}",
            f"   🎯 建议: {a.get('advice','N/A')}",
            f"   ⚠️ 风险: {a.get('risk','?')}",
        ]
    if data.get("summary"):
        lines += ["\n" + "=" * 60, f"📋 **总结**: {data['summary']}"]
    lines += ["\n" + "=" * 44, f"📊 信号: 🟢BUY {sc['BUY']} | 🔴SELL {sc['SELL']} | ⚪HOLD {sc['HOLD']}"]
    return "\n".join(lines)

def build_basic_report(current_data, fallback_used, skipped):
    lines = [f"📊 长期价格报告 | {datetime.now().strftime('%Y-%m-%d %H:%M')} (窗口:{HISTORY_DAYS}天)"]
    order = {"BUY": 0, "SELL": 1, "HOLD": 2}
    current_data.sort(key=lambda x: order.get(x["signal"], 3))
    for e in current_data:
        em = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(e["signal"], "⚪")
        lines.append(f"{em} {e['item']}: 均价={e['current_price']} 历史均价={e['avg']} 偏离{e['deviation_pct']:+.1f}% 信号={e['signal']}")
    if fallback_used:
        lines.append(f"\n🔄 兜底: {', '.join(fallback_used)}")
    if skipped:
        lines.append(f"\n⚠️ 跳过 {len(skipped)} 个: {', '.join(skipped)}")
    return "\n".join(lines)

# ===== memory 辅助 =====
def load_memory():
    if os.path.exists("price_memory.json"):
        try:
            return json.load(open("price_memory.json", encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_memory(mem):
    json.dump(mem, open("price_memory.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# ===== 主流程 =====
def main():
    print(f"🚀 DarkerDB AI Trader 启动（长期趋势版，窗口={HISTORY_DAYS}天）...")
    if not DARKERDB_KEYS:
        print("❌ 未配置 DARKERDB_KEYS，退出")
        return
    print(f"📋 已配置 {len(DARKERDB_KEYS)} 个 DarkerDB 账号")
    print(f"📋 监控目标: {len(WATCHLIST)} 个")

    init_db()
    clean_old_data()

    now_utc = datetime.now(timezone.utc)
    timestamp_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"⏰ 当前时间戳: {timestamp_str}")

    mem = load_memory()
    current_data, skipped, fallback_used = [], [], []

    for name, rarity, min_margin in WATCHLIST:
        print(f"\n--- 处理 {name}|{rarity} ---")
        ck_id = f"__exact_id__{name}|{rarity}"
        ck_arch = f"__arch_id__{name}"
        archetype_id = mem.get(ck_arch)
        item_id = mem.get(ck_id)

        if not archetype_id:
            archetype_id = resolve_archetype_id(name)
            if archetype_id:
                mem[ck_arch] = archetype_id
        if not item_id and archetype_id:
            item_id = archetype_id
            mem[ck_id] = item_id
        save_memory(mem)

        if not item_id:
            print(f"  ❌ {name}: 无法解析 ID")
            skipped.append(f"{name}|{rarity}: 无法解析 ID")
            continue
        print(f"  item_id: {item_id}")

        result = get_fresh_price_checks(item_id, rarity)
        used_fallback = False
        if not result:
            if DEBUG:
                print("    [DEBUG] price-checks 无数据，尝试 /v2/market 兜底")
            result = get_price_from_market_fallback(archetype_id or item_id, rarity)
            used_fallback = True
        if not result or result["sample_count"] == 0:
            print(f"  ⚠️ {name}|{rarity}: 无有效样本")
            skipped.append(f"{name}|{rarity}: 无有效样本")
            continue

        price = result["trimmed_avg"]
        src = {"listings": "挂牌", "mixed": "挂牌+成交", "market_fallback": "兜底(/v2/market)"}.get(result["source"], "?")
        print(f"  ✅ {name}|{rarity}: 均价={price} (样本:{result['sample_count']} 最低:{result['min_price']} 来源:{src})")

        save_price_to_db(name, rarity, price, timestamp_str)

        series = get_price_series(name, rarity, days=HISTORY_DAYS)
        ind = calc_indicators(series)
        if ind:
            signal, confidence, reason = generate_signal(ind)
            hist_avg = ind["sma_short"]
            dev = ind["deviation"]
            trend_str = (
                f"MA7={ind['sma_short']:.0f} MA30={ind['sma_long']:.0f} "
                f"斜率{ind['slope_pct']:+.2f}%/天 动量{ind['momentum_pct']:+.2f}% 波动{ind['volatility']:.1f}%"
            )
            print(f"  📈 多维信号: {signal} (置信度{confidence:.0%}) - {reason}")
            print(f"     {trend_str}")
        else:
            past = [p for _, p in series]
            hist_avg = sum(past) / len(past) if past else result["min_price"]
            signal, confidence, reason = simple_signal(price, hist_avg, min_margin)
            dev = ((price - hist_avg) / hist_avg * 100) if hist_avg > 0 else 0
            print(f"  📈 样本不足({len(series)})，回退简单判断: {signal} {reason}")

        current_data.append({
            "item": f"{name}|{rarity}",
            "current_price": price,
            "avg": round(hist_avg, 1),
            "deviation_pct": round(dev, 1),
            "signal": signal,
            "sample_size": result["sample_count"],
            "min_margin": min_margin,
        })
        if used_fallback:
            fallback_used.append(f"{name}|{rarity}")
        time.sleep(1)

    if not current_data:
        print("❌ 无数据")
        if skipped:
            print(f"⚠️ 跳过 {len(skipped)} 个: {skipped}")
        return

    mc = {}
    for e in current_data:
        item_name = e["item"].split("|")[0]
        rarity_val = e["item"].split("|")[1]
        s = get_price_series(item_name, rarity_val, days=HISTORY_DAYS)
        if s:
            mc[e["item"]] = {
                "price_history": [{"time": dt.isoformat(), "price": p} for dt, p in s[-50:]],
                "data_points": len(s),
            }

    print("\n🤖 AI 分析中...")
    at = analyze_with_ai(current_data, mc)
    if at:
        report = format_report(at)
        if report is None:
            report = build_basic_report(current_data, fallback_used, skipped)
    else:
        report = build_basic_report(current_data, fallback_used, skipped)

    if report and (fallback_used or skipped):
        extra = ""
        if fallback_used:
            extra += f"\n\n🔄 兜底: {', '.join(fallback_used)}"
        if skipped:
            extra += f"\n\n⚠️ 跳过 {len(skipped)} 个: {', '.join(skipped)}"
        report += extra

    print("📤 推送...")
    push_to_serverchan(f"📊 DarkerDB 长期分析 | {datetime.now().strftime('%m-%d %H:%M')}", report)
    print("\n" + "=" * 84)
    print(report[:2000])
    print(f"\n✅ 完成！有数据:{len(current_data)} 跳过:{len(skipped)} 兜底:{len(fallback_used)}")

    # Git 提交
    try:
        subprocess.run(["git", "config", "--global", "user.email", "action@github.com"], capture_output=True)
        subprocess.run(["git", "config", "--global", "user.name", "GitHub Action"], capture_output=True)
        subprocess.run(["git", "add", DB_FILE, ACCOUNT_STATE_FILE, "price_memory.json"], capture_output=True)
        subprocess.run(["git", "commit", "-m", f"Update long-term price data at {timestamp_str}"], capture_output=True)
        pull = subprocess.run(["git", "pull", "--rebase", "origin", "main"], capture_output=True, text=True)
        if pull.returncode != 0:
            subprocess.run(["git", "push", "--force", "origin", "main"], capture_output=True)
        else:
            push = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push.returncode != 0:
                subprocess.run(["git", "push", "--force", "origin", "main"], capture_output=True)
    except Exception as e:
        print(f"⚠️ Git 操作异常: {e}")

if __name__ == "__main__":
    main()
