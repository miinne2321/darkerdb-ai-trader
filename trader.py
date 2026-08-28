"""
DarkerDB AI Trader - 长期趋势版 (v5)
====================================
核心升级：
1. SQLite 长期存储（price_history.db），突破 7 天限制，默认 90 天窗口
2. 多维趋势信号系统（替代单点 7 日均价）：
   - 短期 MA(7) vs 长期 MA(21/30) 金叉/死叉
   - 均线斜率（线性回归，趋势强度）
   - 当前价相对均线偏离（回调买入）
   - 3 日动量确认
   - 波动率惩罚（风控）
3. 保留原有：双 Key 轮转、Server酱推送、AI 分析、挂牌费经济学
"""
import os
import json
import time
import sqlite3
import subprocess
import numpy as np
import requests
from datetime import datetime, timedelta, timezone
import re

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
HISTORY_DAYS = 90          # 从 SQLite 读取的历史窗口（天）
DATA_RETENTION_DAYS = 365  # SQLite 保留时长（天）
DB_FILE = "price_history.db"

# ===== 均线/信号参数 =====
SHORT_WINDOW = 7    # 短期均线（天）
LONG_WINDOW = 30    # 长期均线（天）
SLOPE_THRESHOLD = 0.5       # 斜率阈值（%/天）
VOLATILITY_PENALTY = 15     # 波动率惩罚阈值（%）
MOMENTUM_THRESHOLD = 1.0    # 动量阈值（%）
CONFIDENCE_THRESHOLD = 0.5  # 信号置信度阈值

# ===== 物品清单 =====
WATCHLIST = [
    ("Troll Pelt", "Epic", 0.12),
    ("Troll's Blood", "Epic", 0.12),
    ("Ruby (Royal)", "Legendary", 0.15),
    ("Blue Sapphire (Royal)", "Legendary", 0.15),
    ("Obsidian Ore", "Epic", 0.15),
    ("Rubysilver Ore", "Epic", 0.15),
    ("Gold Ore", "Epic", 0.20),
    ("Diamond (Royal)", "Epic", 0.25),
]

# ===== 阈值 =====
BUY_T = -15
SELL_T = 20
LISTING_WINDOW_HOURS = 6
SALE_WINDOW_HOURS = 24
MIN_SAMPLES = 1
ACCOUNT_STATE_FILE = "account_state.json"
DEBUG = True

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
    """取出最近 days 天的原始价格序列（时间升序）"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT timestamp, price FROM price_history WHERE item=? AND rarity=? AND timestamp>=? ORDER BY timestamp ASC",
        (item, rarity, cutoff)
    ).fetchall()
    conn.close()
    result = []
    for ts, price in rows:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            result.append((dt, price))
        except Exception:
            continue
    return result

def clean_old_data():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DATA_RETENTION_DAYS)).isoformat()
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM price_history WHERE timestamp<?", (cutoff,))
    conn.commit()
    conn.close()

# ===== 账户轮转 =====
def load_account_state():
    if os.path.exists(ACCOUNT_STATE_FILE):
        try:
            with open(ACCOUNT_STATE_FILE) as f:
                state = json.load(f)
                idx = state.get("current_key_index", 0)
                if 0 <= idx < len(DARKERDB_KEYS):
                    return idx
        except Exception:
            pass
    return 0

def save_account_state(idx):
    with open(ACCOUNT_STATE_FILE, "w") as f:
        json.dump({"current_key_index": idx}, f)

# ===== API 请求 =====
def safe_get(url, params=None, retries=3):
    if not DARKERDB_KEYS:
        print("❌ 未配置 DARKERDB_KEYS")
        return None
    current_idx = load_account_state()
    headers_base = {
        "X-API-Version": "2026-08-03",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    for attempt in range(retries):
        for key_offset in range(len(DARKERDB_KEYS)):
            key_idx = (current_idx + key_offset) % len(DARKERDB_KEYS)
            api_key = DARKERDB_KEYS[key_idx]
            headers = {**headers_base, "X-API-Key": api_key}
            try:
                r = requests.get(url, headers=headers, params=params or {}, timeout=30)
                save_account_state(key_idx)
                if r.status_code == 200:
                    return r
                elif r.status_code == 429:
                    continue
                elif r.status_code in (403, 401):
                    continue
                else:
                    return None
            except Exception as e:
                if DEBUG:
                    print(f"    ⚠️ Key[{key_idx}] 请求异常: {e}")
                continue
        time.sleep(5 * (attempt + 1))
    return None

def norm(s):
    return (s or "").strip().lower().replace("’", "'").replace("'", "").replace("-", " ").replace("_", " ").replace("(", "").replace(")", "").replace(":", "")

def resolve_archetype_id(name):
    r = safe_get(f"{API_BASE}/search", {"q": name, "limit": 5})
    if not r:
        return None
    data = r.json()
    body = data.get("body", {})
    results = body.get("results", []) if isinstance(body, dict) else []
    name_n = norm(name)
    for item in results:
        if not isinstance(item, dict) or item.get("type") != "item":
            continue
        iname = norm(item.get("name", ""))
        if name_n == iname or name_n in iname or iname in name_n:
            found = item.get("id")
            if DEBUG:
                print(f"    [DEBUG] archetype_id for '{name}': {found}")
            return found
    for item in results:
        if isinstance(item, dict) and item.get("type") == "item":
            return item.get("id")
    return None

def get_price_from_market_fallback(archetype_id, rarity):
    params = {"archetype": archetype_id, "rarity": rarity, "limit": 20}
    r = safe_get(f"{API_BASE}/market", params)
    if not r or r.status_code != 200:
        return None
    data = r.json()
    body = data.get("body")
    if not body:
        return None
    listings = body.get("listings", []) if isinstance(body, dict) else (body if isinstance(body, list) else [])
    if not listings:
        return None
    prices = [float(l.get("price")) for l in listings if l.get("price") and l.get("price") > 0]
    if not prices:
        return None
    min_price = min(prices)
    avg_price = sum(prices) / len(prices)
    final = min(min_price * 1.1, avg_price)
    if DEBUG:
        print(f"    [DEBUG] /v2/market fallback: {len(prices)} listings, 区间 {min_price}-{max(prices)}")
    return {
        "prices": prices,
        "sample_count": len(prices),
        "trimmed_avg": round(final, 2),
        "min_price": min_price,
        "source": "market_fallback",
    }

def get_fresh_price_checks(item_id, rarity):
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
    similar_listings = body.get("similar_listings", [])
    similar_sales = body.get("similar_sales", [])
    fresh_prices = []
    for listing in similar_listings:
        listed_at = listing.get("listed_at")
        if not listed_at:
            continue
        try:
            lt = datetime.fromisoformat(listed_at.replace("Z", "+00:00"))
            if lt < listing_cutoff:
                continue
        except Exception:
            continue
        price = listing.get("price")
        if price and price > 0:
            fresh_prices.append(float(price))
    source = "listings"
    if len(fresh_prices) < MIN_SAMPLES:
        for sale in similar_sales:
            sold_at = sale.get("sold_at")
            if not sold_at:
                continue
            try:
                st = datetime.fromisoformat(sold_at.replace("Z", "+00:00"))
                if st < sale_cutoff:
                    continue
            except Exception:
                continue
            price = sale.get("price")
            if price and price > 0:
                fresh_prices.append(float(price))
        if len(fresh_prices) >= MIN_SAMPLES:
            source = "mixed"
    if not fresh_prices:
        return None
    sorted_p = sorted(fresh_prices)
    n = len(sorted_p)
    min_price = sorted_p[0]
    if n >= 4:
        q1 = sorted_p[n // 4]
        q3 = sorted_p[3 * n // 4]
        iqr = q3 - q1
        trimmed = [p for p in fresh_prices if (q1 - 1.5 * iqr) <= p <= (q3 + 1.5 * iqr)]
        if not trimmed:
            trimmed = fresh_prices
    else:
        trimmed = fresh_prices
    trimmed_avg = sum(trimmed) / len(trimmed)
    return {
        "prices": fresh_prices,
        "sample_count": n,
        "trimmed_avg": round(trimmed_avg, 2),
        "min_price": min_price,
        "source": source,
    }

# ===== 多维指标计算 =====
def calc_indicators(series, short_window=SHORT_WINDOW, long_window=LONG_WINDOW):
    """
    输入: series = [(datetime, price), ...] 升序
    输出: 各维度指标字典，样本不足返回 None
    """
    if len(series) < short_window:
        return None
    prices = [p for _, p in series]
    current = prices[-1]

    # 均线（数据不足长窗口时用短窗口近似）
    sma_short = sum(prices[-short_window:]) / short_window
    sma_long = sum(prices[-long_window:]) / long_window if len(prices) >= long_window else sma_short

    # 斜率（线性回归，%/天）
    y = prices[-short_window:]
    x = list(range(short_window))
    slope = np.polyfit(x, y, 1)[0]
    slope_pct = (slope / sma_short * 100) if sma_short > 0 else 0

    # 偏离度
    deviation = ((current - sma_short) / sma_short * 100) if sma_short > 0 else 0

    # 波动率（变异系数 %）
    volatility = (np.std(y) / sma_short * 100) if sma_short > 0 else 0

    # 3 日动量
    if len(prices) >= 7:
        recent = sum(prices[-3:]) / 3
        prev = sum(prices[-7:-3]) / 4 if len(prices) >= 7 else prices[-1]
        momentum = recent - prev
        momentum_pct = (momentum / sma_short * 100) if sma_short > 0 else 0
    else:
        momentum_pct = 0

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
    """
    多维信号合成
    返回: (action, confidence, reason)
    """
    if ind is None:
        return "HOLD", 0, "样本不足"

    score = 0
    signals = []

    # 1. 趋势（金叉/死叉 + 斜率）
    if ind["is_golden_cross"] and ind["slope_pct"] > SLOPE_THRESHOLD:
        signals.append("趋势向上↑")
        score += 0.3
    elif not ind["is_golden_cross"] and ind["slope_pct"] < -SLOPE_THRESHOLD:
        signals.append("趋势向下↓")
        score -= 0.3

    # 2. 回调买入（趋势向上 + 价格短期回调）
    if ind["is_golden_cross"] and ind["deviation"] < -2:
        signals.append("上升趋势中的回调")
        score += 0.25

    # 3. 动量确认
    if ind["momentum_pct"] > MOMENTUM_THRESHOLD:
        signals.append("动量为正")
        score += 0.15
    elif ind["momentum_pct"] < -MOMENTUM_THRESHOLD:
        signals.append("动量为负")
        score -= 0.15

    # 4. 波动率惩罚（风控）
    if ind["volatility"] > VOLATILITY_PENALTY:
        signals.append("⚠️ 高波动")
        score *= 0.6

    # 5. 长期均线位置
    if ind["current"] < ind["sma_long"] * 0.95:
        signals.append("低于长期均线")
        score += 0.1

    confidence = min(abs(score), 1.0)
    reason = " + ".join(signals) if signals else "信号不明确"

    if score >= CONFIDENCE_THRESHOLD:
        return "BUY", confidence, reason
    elif score <= -CONFIDENCE_THRESHOLD:
        return "SELL", confidence, reason
    else:
        return "HOLD", confidence, reason

# ===== AI 分析 =====
def _try_fix_json(text):
    if text.count('"') % 2 != 0:
        text = text + '"'
    ob = text.count("{")
    cb = text.count("}")
    if ob > cb:
        text = text + "}" * (ob - cb)
    return text

def extract_json(text):
    if "User Safety" in text or (text.strip().lower().startswith("safe") and len(text) < 80):
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
    for match in re.findall(r'\{(?:[^{}]|(?:\{[^{}]*\}))*\}', text):
        try:
            return json.loads(match)
        except Exception:
            continue
    return None

def analyze_with_ai(current_data, memory_context):
    prompt = f"""任务：分析以下价格数据与多维趋势指标，输出 JSON 分析报告。

当前数据（物品|品质, 当前价, 短期均线MA{SHORT_WINDOW}, 长期均线MA{LONG_WINDOW}, 偏离%, 斜率%/天, 动量%, 波动率%, 多维信号, 置信度）：
{json.dumps(current_data, ensure_ascii=False, indent=2)}

历史序列（最近 {HISTORY_DAYS} 天）：
{json.dumps(memory_context, ensure_ascii=False, indent=2)}

要求：
1. 只输出一个合法 JSON 对象，不要其他文字。
2. 结构：
{{
  "analyses": [
    {{
      "item": "物品名|品质",
      "signal": "BUY" 或 "SELL" 或 "HOLD",
      "current_price": 数值,
      "reason": "中文原因，不确定就说'不确定'",
      "trend": "上涨" 或 "下跌" 或 "震荡" 或 "样本不足",
      "trend_basis": "基于斜率/金叉/动量的中文说明",
      "advice": "中文建议，BUY 给出建议买入价上限",
      "risk": "低" 或 "中" 或 "高",
      "position_note": "持仓备注或 null"
    }}
  ],
  "summary": "1-2句中文总结"
}}
3. 必须为 current_data 中每个物品输出一条分析，不能遗漏。
4. 信号判断严格遵守挂牌费经济学：
   - 挂牌费 = 售价的 5% 或 15金取高
   - 单价 ≤ 300金：15金最低费吃掉利润，不给 BUY
   - 单价 300-500金：要求偏离 ≤ -20% 且预估利润 ≥ 20% 才 BUY
   - 单价 > 500金：偏离 ≤ -15% 且预估利润 ≥ 15% 即可 BUY
   - 偏离 > 20% → SELL，否则 HOLD
5. 全部中文，简洁避免截断。"""

    headers = {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
    models = [
        "openrouter/free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "openai/gpt-oss-20b:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "qwen/qwen3-next-80b-a3b-instruct:free",
    ]
    for model in models:
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
    print("❌ 所有模型均失败")
    return None

# ===== Server酱推送 =====
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
    lines = [f"📊 **DarkerDB 长期趋势分析** | {datetime.now().strftime('%Y-%m-%d %H:%M')} (窗口{HISTORY_DAYS}天)", "=" * 90]
    sc = {"BUY": 0, "SELL": 0, "HOLD": 0}
    analyses.sort(key=lambda x: (x.get("signal") != "BUY", x.get("signal") != "SELL"))
    for a in analyses:
        sig = a.get("signal", "HOLD")
        sc[sig] = sc.get(sig, 0) + 1
        em = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(sig, "⚪")
        lines += [
            f"\n{em} **{a.get('item', '?')}** — {sig} (置信度{a.get('confidence', '?')})",
            f"   当前均价: {a.get('current_price', '?')}",
            f"   💡 原因: {a.get('reason', 'N/A')}",
            f"   📈 趋势: {a.get('trend', '?')} ({a.get('trend_basis', '')})",
            f"   🎯 建议: {a.get('advice', 'N/A')}",
            f"   ⚠️ 风险: {a.get('risk', '?')}",
        ]
    if data.get("summary"):
        lines += ["\n" + "=" * 70, f"📋 **总结**: {data['summary']}"]
    lines += ["\n" + "=" * 54, f"📊 信号: 🟢BUY {sc['BUY']} | 🔴SELL {sc['SELL']} | ⚪HOLD {sc['HOLD']}"]
    return "\n".join(lines)

def build_basic_report(current_data, fallback_used, skipped):
    lines = [f"📊 长期趋势报告 | {datetime.now().strftime('%Y-%m-%d %H:%M')} (窗口{HISTORY_DAYS}天)"]
    order = {"BUY": 0, "SELL": 1, "HOLD": 2}
    current_data.sort(key=lambda x: order.get(x.get("signal"), 3))
    for e in current_data:
        em = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(e.get("signal"), "⚪")
        lines.append(
            f"{em} {e['item']}: 当前{e['current_price']} MA{SHORT_WINDOW}={e.get('sma_short',0):.0f} "
            f"MA{LONG_WINDOW}={e.get('sma_long',0):.0f} 偏离{e.get('deviation',0):+.1f}% "
            f"斜率{e.get('slope_pct',0):+.2f}%/天 动量{e.get('momentum_pct',0):+.1f}% "
            f"波动{e.get('volatility',0):.1f}% → {e['signal']}({e.get('confidence',0):.0%})"
        )
    if fallback_used:
        lines.append(f"\n🔄 兜底: {', '.join(fallback_used)}")
    if skipped:
        lines.append(f"\n⚠️ 跳过 {len(skipped)} 个")
    return "\n".join(lines)

# ===== 主流程 =====
def main():
    print(f"🚀 DarkerDB AI Trader 启动（长期趋势版，窗口={HISTORY_DAYS}天）...")
    print(f"📋 已配置 {len(DARKERDB_KEYS)} 个 DarkerDB 账号")
    print(f"🔧 均线: MA{SHORT_WINDOW}/MA{LONG_WINDOW} | 置信度阈值: {CONFIDENCE_THRESHOLD}")

    init_db()
    clean_old_data()

    now_utc = datetime.now(timezone.utc)
    timestamp_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"⏰ 当前时间戳: {timestamp_str}")

    print("🔍 查询市场价格并计算多维趋势...")
    current_data, skipped, fallback_used = [], [], []

    for name, rarity, min_margin in WATCHLIST:
        print(f"\n--- 处理 {name}|{rarity} ---")
        arch_id = None
        exact_id = None

        # 从 price_memory.json 读取缓存的 ID
        mem = {}
        if os.path.exists("price_memory.json"):
            try:
                mem = json.load(open("price_memory.json"))
            except Exception:
                mem = {}
        ck_arch = f"__arch_id__{name}"
        ck_id = f"__exact_id__{name}|{rarity}"
        arch_id = mem.get(ck_arch)
        exact_id = mem.get(ck_id)

        if not arch_id:
            arch_id = resolve_archetype_id(name)
            if arch_id:
                mem[ck_arch] = arch_id
        if not exact_id and arch_id:
            exact_id = arch_id
            mem[ck_id] = exact_id
        if arch_id or exact_id:
            json.dump(mem, open("price_memory.json", "w"), ensure_ascii=False, indent=2)

        if not exact_id:
            print(f"  ❌ {name}: 无法解析 ID")
            skipped.append(f"{name}|{rarity}: 无法解析 ID")
            continue
        print(f"  item_id: {exact_id}")

        result = get_fresh_price_checks(exact_id, rarity)
        if not result:
            if DEBUG:
                print(f"    [DEBUG] price-checks 无数据，尝试 /v2/market 兜底")
            result = get_price_from_market_fallback(arch_id, rarity)
            if result:
                fallback_used.append(f"{name}|{rarity}")
        if not result or result["sample_count"] == 0:
            print(f"  ⚠️ {name}|{rarity}: 无有效样本")
            skipped.append(f"{name}|{rarity}: 无有效样本")
            continue

        price = result["trimmed_avg"]
        src = {"listings": "挂牌", "mixed": "挂牌+成交", "market_fallback": "兜底(/v2/market)"}.get(result["source"], "?")
        print(f"  ✅ 当前均价={price} (样本:{result['sample_count']} 最低:{result['min_price']} 来源:{src})")

        # 保存当前价格到 SQLite（长期历史）
        save_price_to_db(name, rarity, price, timestamp_str)

        # ===== 多维趋势分析 =====
        series = get_price_series(name, rarity, days=HISTORY_DAYS)
        ind = calc_indicators(series)
        if ind:
            action, confidence, reason = generate_signal(ind)
            print(f"  📈 趋势: MA{SHORT_WINDOW}={ind['sma_short']:.0f} MA{LONG_WINDOW}={ind['sma_long']:.0f} "
                  f"斜率{ind['slope_pct']:+.2f}%/天 偏离{ind['deviation']:+.1f}% "
                  f"动量{ind['momentum_pct']:+.1f}% 波动{ind['volatility']:.1f}%")
            print(f"  🎯 多维信号: {action} (置信度{confidence:.0%}) - {reason}")
        else:
            # 样本不足，回退到简单的偏离判断
            past = [(dt, p) for dt, p in series if dt < now_utc.replace(hour=0, minute=0, second=0, microsecond=0)]
            if len(past) >= 2:
                hist_avg = sum(p for _, p in past) / len(past)
            else:
                hist_avg = result["min_price"]
            deviation = ((price - hist_avg) / hist_avg * 100) if hist_avg > 0 else 0
            listing_fee = max(price * 0.05, 15)
            profit_margin = ((hist_avg - price - listing_fee) / price) if price > 0 else 0
            if deviation < BUY_T and profit_margin >= min_margin:
                action, confidence, reason = "BUY", 0.4, "样本不足，基于简单偏离判断"
            elif deviation > SELL_T:
                action, confidence, reason = "SELL", 0.4, "样本不足，基于简单偏离判断"
            else:
                action, confidence, reason = "HOLD", 0.3, "样本不足，暂无明确信号"
            ind = {
                "sma_short": hist_avg, "sma_long": hist_avg,
                "slope_pct": 0, "deviation": deviation,
                "volatility": 0, "momentum_pct": 0,
                "current": price, "is_golden_cross": False, "sample_size": len(series),
            }
            print(f"  📈 样本不足({len(series)})，回退简单判断: {action} 偏离{deviation:+.1f}%")

        current_data.append({
            "item": f"{name}|{rarity}",
            "current_price": price,
            "avg_7d": round(ind["sma_short"], 1),
            "deviation_pct": round(ind["deviation"], 1),
            "slope_pct": round(ind["slope_pct"], 2),
            "momentum_pct": round(ind["momentum_pct"], 1),
            "volatility": round(ind["volatility"], 1),
            "signal": action,
            "confidence": confidence,
            "reason": reason,
            "sample_size": ind["sample_size"],
            "sma_short": ind["sma_short"],
            "sma_long": ind["sma_long"],
        })
        time.sleep(1)

    if not current_data:
        print("❌ 无数据")
        if skipped:
            print(f"⚠️ 跳过 {len(skipped)} 个: {skipped}")
        return

    # AI 分析上下文（传历史序列）
    mc = {}
    for e in current_data:
        iname = e["item"].split("|")[0]
        irarity = e["item"].split("|")[1]
        s = get_price_series(iname, irarity, days=HISTORY_DAYS)
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

    title = f"📊 DarkerDB 长期趋势 | {datetime.now().strftime('%m-%d %H:%M')}"
    push_to_serverchan(title, report)

    print("\n" + "=" * 92)
    print(report[:2500])
    print(f"\n✅ 完成！有数据:{len(current_data)} 跳过:{len(skipped)} 兜底:{len(fallback_used)}")

    # Git 提交（保留长期数据）
    try:
        subprocess.run(["git", "config", "--global", "user.email", "action@github.com"], capture_output=True)
        subprocess.run(["git", "config", "--global", "user.name", "GitHub Action"], capture_output=True)
        subprocess.run(["git", "add", DB_FILE, ACCOUNT_STATE_FILE, "price_memory.json"], capture_output=True)
        subprocess.run(["git", "commit", "-m", f"Update long-term data at {timestamp_str}"], capture_output=True)
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

