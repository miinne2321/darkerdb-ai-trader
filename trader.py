"""
DarkerDB AI Trader - 云端版（Server酱推送）
修复：精确变体 ID + /v2/market fallback 双参数尝试
Obsidian Ore 替换 Copper Ore
"""
import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
import re

DARKERDB_KEY = os.environ["DARKERDB_KEY"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
SERVERCHAN_SENDKEY = os.environ["SERVERCHAN_SENDKEY"]

# === 追踪的物品清单（10 个热门物品）===
WATCHLIST = [
    ("Troll's Blood", "epic"),
    ("Gold Ore", "epic"),
    ("Rubysilver Ore", "epic"),
    ("Obsidian Ore", "epic"),          # ← 替换 Copper Ore
    ("Bone", "common"),
    ("Potion of Healing", "uncommon"),
    ("Bandage", "rare"),
    ("Grave Essence", "uncommon"),
    ("Blue Sapphire (Perfect)", "epic"),
    ("Ruby (Perfect)", "epic"),
]

# === 信号阈值 ===
BUY_T = -15
SELL_T = 20
LISTING_WINDOW_HOURS = 12
SALE_WINDOW_HOURS = 48
MIN_SAMPLES = 1
HISTORY_FILE = "price_memory.json"
DEBUG = True

# === 工具函数 ===
def safe_get(url, params=None, retries=3):
    headers = {
        "X-API-Key": DARKERDB_KEY,
        "X-API-Version": "2026-08-03",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params or {}, timeout=30)
            if r.status_code == 200:
                return r
            elif r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 5))
                print(f"    ⚠️ 限流，等待 {retry_after}s")
                time.sleep(retry_after)
                continue
            return None
        except:
            if i < 2: time.sleep(2)
    return None

def norm(s):
    return (s or "").strip().lower().replace("’", "'").replace("'", "").replace("-", " ").replace("_", " ").replace("(", "").replace(")", "").replace(":", "")

def resolve_archetype_id(name):
    r = safe_get("https://api.darkerdb.com/v2/search", {"q": name, "limit": 5})
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
            if DEBUG: print(f"    [DEBUG] archetype_id for '{name}': {found}")
            return found
    for item in results:
        if isinstance(item, dict) and item.get("type") == "item":
            return item.get("id")
    return None

def get_price_from_market_fallback(item_id, rarity, archetype_id=None):
    """兜底：用 /v2/market，同时尝试 item_id 和 archetype 两种参数"""
    # 尝试1: item_id 参数
    params = {"rarity": rarity, "limit": 20}
    if item_id:
        params["item_id"] = item_id
    r = safe_get("https://api.darkerdb.com/v2/market", params)
    if r and r.status_code == 200:
        data = r.json()
        body = data.get("body")
        listings = body.get("listings", []) if body else []
        if DEBUG and item_id:
            print(f"    [DEBUG] /v2/market item_id={item_id}: {len(listings)} listings")
        if listings:
            prices = [float(l.get("price")) for l in listings if l.get("price") and l.get("price") > 0]
            if prices:
                return _build_fallback_result(prices)

    # 尝试2: archetype 参数（带 id.item. 前缀）
    if archetype_id:
        for arch_param in [archetype_id, archetype_id.replace("id.item.", "")]:
            params2 = {"archetype": arch_param, "rarity": rarity, "limit": 20}
            r2 = safe_get("https://api.darkerdb.com/v2/market", params2)
            if r2 and r2.status_code == 200:
                data2 = r2.json()
                body2 = data2.get("body")
                listings2 = body2.get("listings", []) if body2 else []
                if DEBUG:
                    print(f"    [DEBUG] /v2/market archetype={arch_param}: {len(listings2)} listings")
                if listings2:
                    prices = [float(l.get("price")) for l in listings2 if l.get("price") and l.get("price") > 0]
                    if prices:
                        return _build_fallback_result(prices)
    return None

def _build_fallback_result(prices):
    min_price = min(prices)
    avg_price = sum(prices) / len(prices)
    conservative = min_price * 1.1
    final = min(conservative, avg_price)
    return {
        "prices": prices,
        "sample_count": len(prices),
        "trimmed_avg": round(final, 2),
        "min_price": min_price,
        "latest_listed_at": None,
        "freshness": "fallback",
        "source": "market_fallback",
    }

def get_fresh_price_checks(item_id, rarity, listing_window_hours=LISTING_WINDOW_HOURS,
                           sale_window_hours=SALE_WINDOW_HOURS, min_samples=MIN_SAMPLES):
    params = {"item_id": item_id, "rarity": rarity}
    r = safe_get("https://api.darkerdb.com/v2/price-checks", params)
    if not r or r.status_code != 200:
        if DEBUG:
            status = r.status_code if r else "None"
            print(f"    [DEBUG] price-checks status={status}")
        return None
    data = r.json()
    body = data.get("body")
    if not body:
        return None

    now = datetime.now(timezone.utc)
    listing_cutoff = now - timedelta(hours=listing_window_hours)
    sale_cutoff = now - timedelta(hours=sale_window_hours)

    similar_listings = body.get("similar_listings", [])
    similar_sales = body.get("similar_sales", [])
    if DEBUG:
        print(f"    [DEBUG] similar_listings={len(similar_listings)}, similar_sales={len(similar_sales)}")

    fresh_prices = []
    for listing in similar_listings:
        listed_at = listing.get("listed_at")
        if not listed_at: continue
        try:
            lt = datetime.fromisoformat(listed_at.replace("Z", "+00:00"))
            if lt < listing_cutoff: continue
        except: continue
        price = listing.get("price")
        if price and price > 0:
            fresh_prices.append(float(price))

    source = "listings"
    if len(fresh_prices) < min_samples:
        for sale in similar_sales:
            sold_at = sale.get("sold_at")
            if not sold_at: continue
            try:
                st = datetime.fromisoformat(sold_at.replace("Z", "+00:00"))
                if st < sale_cutoff: continue
            except: continue
            price = sale.get("price")
            if price and price > 0:
                fresh_prices.append(float(price))
        if len(fresh_prices) >= min_samples:
            source = "mixed"

    if not fresh_prices:
        return None

    sorted_p = sorted(fresh_prices)
    n = len(sorted_p)
    min_price = sorted_p[0]
    if n >= 4:
        q1 = sorted_p[n // 4]; q3 = sorted_p[3 * n // 4]
        iqr = q3 - q1
        lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        trimmed = [p for p in fresh_prices if lower <= p <= upper]
        if not trimmed: trimmed = fresh_prices
    else:
        trimmed = fresh_prices
    trimmed_avg = sum(trimmed) / len(trimmed)
    return {
        "prices": fresh_prices, "sample_count": n,
        "trimmed_avg": round(trimmed_avg, 2), "min_price": min_price,
        "freshness": "fresh" if source == "listings" else "low", "source": source,
    }

# === 长期记忆 ===
def load_memory():
    if os.path.exists(HISTORY_FILE):
        try: return json.load(open(HISTORY_FILE, encoding="utf-8"))
        except: pass
    return {}

def save_memory(mem):
    json.dump(mem, open(HISTORY_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def get_price_series(mem, key, days=30):
    if key not in mem: return []
    prices = mem[key].get("prices", {})
    cut = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return sorted([(k, v) for k, v in prices.items() if k >= cut])

def add_memory(mem, key, today, price):
    if key not in mem: mem[key] = {"prices": {}}
    mem[key]["prices"][today] = price
    prices = mem[key]["prices"]
    old = [k for k in prices if k < (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")]
    for k in old: del prices[k]

# === AI 分析 ===
def extract_json(text):
    try: return json.loads(text)
    except: pass
    s, e = text.find('{'), text.rfind('}')
    if s != -1 and e != -1 and e > s:
        try: return json.loads(text[s:e+1])
        except: pass
    return None

def analyze_with_ai(current_data, memory_context):
    prompt = f"""你是 Dark and Darker 游戏市场分析师 AI。基于提供的材料/消耗品价格数据，完成分析：

【当前价格数据】（物品|品质, 当前新鲜均价, 7日均价, 偏离%）：
{json.dumps(current_data, ensure_ascii=False, indent=2)}

【历史记忆】（price_history 为该物品历史价格记录，data_points 为有效数据点数）：
{json.dumps(memory_context, ensure_ascii=False, indent=2)}

请对每个物品输出 JSON：
{{"analyses":[{{"item":"物品名|品质","signal":"BUY/SELL/HOLD","current_price":数值,
"reason":"波动原因推测，不确定说'原因不明'，禁止编造版本号",
"trend":"上涨/下跌/震荡/样本不足","trend_basis":"趋势依据，data_points<2 时说明样本不足",
"advice":"具体建议","risk":"低/中/高","position_note":"持仓备注或null"}}],
"market_overview":"整体市场1-2句总结"}}

只输出 JSON，不要其他文字。"""

    headers = {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
    data = {"model": "openrouter/free", "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}, "max_tokens": 8192}
    try:
        r = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=120)
        if r.status_code == 200: return r.json()["choices"][0]["message"]["content"]
        print(f"⚠️ AI {r.status_code}: {r.text[:200]}"); return None
    except Exception as e:
        print(f"⚠️ AI 异常: {e}"); return None

# === Server酱 推送 ===
def push_to_serverchan(title, content):
    sendkey = SERVERCHAN_SENDKEY
    if not sendkey: print("⚠️ 未配置 SERVERCHAN_SENDKEY"); return
    if sendkey.startswith("sctp"):
        m = re.match(r'^sctp(\d+)t', sendkey)
        if m: url = f"https://{m.group(1)}.push.ft07.com/send/{sendkey}.send"
        else: print("⚠️ SendKey 格式错误"); return
    else: url = f"https://sctapi.ftqq.com/{sendkey}.send"
    if len(content) > 32000: content = content[:32000] + "\n...(截断)"
    try:
        res = requests.post(url, data={"title": title, "desp": content}, timeout=10).json()
        print("✅ 已推送到微信" if res.get("code") == 0 else f"⚠️ 推送失败: {res}")
    except Exception as e:
        print(f"⚠️ 推送异常: {e}")

def format_report(analysis_text):
    data = extract_json(analysis_text)
    if not data: return f"⚠️ AI 分析解析失败:\n{analysis_text[:1500]}"
    lines = [f"📊 **DarkerDB AI 市场分析报告** | {datetime.now().strftime('%Y-%m-%d %H:%M')}", "=" * 46]
    sc = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for a in data.get("analyses", []):
        sig = a.get("signal", "HOLD"); sc[sig] = sc.get(sig, 0) + 1
        em = {"BUY":"🟢","SELL":"🔴","HOLD":"⚪"}.get(sig, "⚪")
        lines += [f"\n{em} **{a.get('item','?')}** — {sig}",
                  f"   当前新鲜均价: {a.get('current_price','?')}",
                  f"   💡 原因: {a.get('reason','N/A')}",
                  f"   📈 趋势: {a.get('trend','?')}（{a.get('trend_basis','')}）",
                  f"   🎯 建议: {a.get('advice','N/A')}", f"   ⚠️ 风险: {a.get('risk','?')}"]
        if a.get("position_note"): lines.append(f"   📌 持仓: {a['position_note']}")
    if data.get("market_overview"): lines += ["\n" + "=" * 46, f"📋 **市场总览**: {data['market_overview']}"]
    lines += ["\n" + "=" * 46, f"📊 信号: 🟢BUY {sc['BUY']} | 🔴SELL {sc['SELL']} | ⚪HOLD {sc['HOLD']}"]
    return "\n".join(lines)

# === 主流程 ===
def main():
    print("🚀 DarkerDB AI Trader 启动（Obsidian Ore 替换 Copper Ore）...")
    today = datetime.now().strftime("%Y-%m-%d")
    mem = load_memory()
    print("🔍 查询市场价格...")
    current_data, skipped, fallback_used = [], [], []
    for name, rarity in WATCHLIST:
        print(f"\n--- 处理 {name}|{rarity} ---")
        ck_id, ck_arch = f"__exact_id__{name}|{rarity}", f"__arch_id__{name}"
        arch_id = mem.get(ck_arch); exact_id = mem.get(ck_id)
        if not arch_id:
            arch_id = resolve_archetype_id(name)
            if arch_id: mem[ck_arch] = arch_id
        if not exact_id and arch_id:
            exact_id = arch_id; mem[ck_id] = exact_id
        if not exact_id:
            print(f"  ❌ {name}: 无法解析 ID"); skipped.append(f"{name}|{rarity}: 无法解析 ID"); continue
        print(f"  item_id: {exact_id}")

        result = get_fresh_price_checks(exact_id, rarity)
        if not result:
            if DEBUG: print(f"    [DEBUG] price-checks 无数据，尝试 /v2/market 兜底")
            result = get_price_from_market_fallback(exact_id, rarity, arch_id)
            if result: fallback_used.append(f"{name}|{rarity}")
        if not result or result["sample_count"] == 0:
            print(f"  ⚠️ {name}|{rarity}: 无有效样本"); skipped.append(f"{name}|{rarity}: 无有效样本"); continue

        price = result["trimmed_avg"]
        src = {"listings":"挂牌","mixed":"挂牌+成交","sales":"成交","fallback":"兜底(/v2/market)"}.get(result["source"], "?")
        print(f"  ✅ {name}|{rarity}: 均价={price} (样本:{result['sample_count']} 最低:{result['min_price']} 来源:{src})")

        series = get_price_series(mem, f"{name}|{rarity}")
        if len(series) >= 2:
            past = [p for _, p in series[:-1]]
            hist = sum(past)/len(past) if past else price
            dev = ((price-hist)/hist)*100 if hist else 0
        else:
            dmin = result["min_price"]
            hist = dmin if dmin else price
            dev = ((price-dmin)/dmin)*100 if dmin else 0
        signal = "BUY" if dev < BUY_T else ("SELL" if dev > SELL_T else "HOLD")
        current_data.append({"item":f"{name}|{rarity}","current_price":price,"avg_7d":round(hist,1),
                             "deviation_pct":round(dev,1),"signal":signal,"sample_size":result["sample_count"]})
        add_memory(mem, f"{name}|{rarity}", today, price)
        time.sleep(1)

    if not current_data:
        print("❌ 无数据"); 
        if skipped: print(f"⚠️ 跳过 {len(skipped)} 个: {skipped}")
        return

    mc = {}
    for e in current_data:
        s = get_price_series(mem, e["item"], 30)
        if s: mc[e["item"]] = {"price_history":[{"date":d,"price":p} for d,p in s[-10:]], "data_points":len(s)}

    print("\n🤖 AI 分析中...")
    at = analyze_with_ai(current_data, mc)
    if not at:
        print("⚠️ AI 失败，基础报告")
        lines = [f"📊 价格报告 | {today}"]
        for e in current_data:
            em = {"BUY":"🟢","SELL":"🔴","HOLD":"⚪"}.get(e["signal"],"⚪")
            lines.append(f"{em} {e['item']}: 均价={e['current_price']} (偏离{e['deviation_pct']}%) [样本:{e['sample_size']}]")
        if fallback_used: lines.append(f"\n🔄 兜底: {', '.join(fallback_used)}")
        if skipped: lines.append(f"\n⚠️ 跳过 {len(skipped)} 个")
        report = "\n".join(lines)
    else:
        report = format_report(at)
        extra = ""
        if fallback_used: extra += f"\n\n🔄 兜底: {', '.join(fallback_used)}"
        if skipped: extra += f"\n\n⚠️ 跳过 {len(skipped)} 个: {', '.join(skipped)}"
        report += extra

    save_memory(mem)
    print("📤 推送...")
    push_to_serverchan(f"📊 DarkerDB 市场分析 | {datetime.now().strftime('%m-%d %H:%M')}", report)
    print("\n" + "=" * 46); print(report[:2000])
    print(f"\n✅ 完成！有数据:{len(current_data)} 跳过:{len(skipped)} 兜底:{len(fallback_used)}")

if __name__ == "__main__":
    main()
