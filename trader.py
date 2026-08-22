"""
DarkerDB AI Trader - 云端版（Server酱推送）
修复：先通过 /v2/items 解析精确变体 ID，再调用 /v2/price-checks
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

# === 追踪的物品清单（10 个最热门物品）===
WATCHLIST = [
    ("Troll's Blood", "epic"),
    ("Gold Ore", "epic"),
    ("Rubysilver Ore", "epic"),
    ("Copper Ore", "uncommon"),
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
    """Step 1: 通过 /v2/search 获取 archetype id"""
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
            found_id = item.get("id")
            if DEBUG:
                print(f"    [DEBUG] archetype_id for '{name}': {found_id}")
            return found_id
    # 降级：返回第一个 item 类型的 id
    for item in results:
        if isinstance(item, dict) and item.get("type") == "item":
            return item.get("id")
    return None

def resolve_exact_item_id(archetype_id, rarity):
    """
    Step 2: 通过 /v2/items/{archetype_id} 获取精确变体 ID
    文档: concrete variant id 是 rarity-suffixed
    """
    if not archetype_id:
        return None
    
    # 尝试直接构造可能的变体 ID 格式
    # 格式1: {archetype_id}_{rarity_suffix}
    rarity_map = {
        "common": "1001",
        "uncommon": "2001", 
        "rare": "3001",
        "epic": "5001",
        "legendary": "6001",
        "unique": "7001",
        "artifact": "8001",
        "poor": "0001"
    }
    suffix = rarity_map.get(rarity.lower())
    if suffix:
        candidate = f"{archetype_id}_{suffix}"
        # 验证这个 ID 是否有效
        r = safe_get(f"https://api.darkerdb.com/v2/items/{candidate}")
        if r and r.status_code == 200:
            if DEBUG:
                print(f"    [DEBUG] 精确变体 ID 命中: {candidate}")
            return candidate
    
    # 格式2: 如果上面失败，尝试用 /v2/items 列表查询
    # 但这需要知道 archetype 的 base 名称
    base_name = archetype_id.replace("id.item.", "")
    # 去掉可能的复数形式
    singular = base_name.rstrip('s')
    
    # 尝试几个常见后缀
    for test_suffix in ["1001", "2001", "3001", "5001", "6001"]:
        candidate = f"id.item.{singular}_{test_suffix}"
        r = safe_get(f"https://api.darkerdb.com/v2/items/{candidate}")
        if r and r.status_code == 200:
            data = r.json()
            item_data = data.get("body", {})
            if item_data.get("rarity", "").lower() == rarity.lower():
                if DEBUG:
                    print(f"    [DEBUG] 精确变体 ID 命中: {candidate}")
                return candidate
    
    # 如果都不行，返回原 archetype_id（Bone/Grave Essence 这种单变体物品可用）
    if DEBUG:
        print(f"    [DEBUG] 无法解析精确变体 ID，回退到 archetype_id: {archetype_id}")
    return archetype_id

def get_price_from_market_fallback(item_id, rarity, archetype_id=None):
    """兜底：使用 /v2/market 获取价格"""
    params = {"rarity": rarity, "limit": 20}
    if item_id:
        params["item_id"] = item_id
    elif archetype_id:
        params["archetype"] = archetype_id.replace("id.item.", "")
    
    r = safe_get("https://api.darkerdb.com/v2/market", params)
    if not r:
        return None
    data = r.json()
    body = data.get("body")
    if not body:
        return None
    
    listings = body.get("listings", [])
    if not listings:
        return None
    
    prices = [float(l.get("price")) for l in listings if l.get("price") and l.get("price") > 0]
    if not prices:
        return None
    
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

def get_fresh_price_checks(item_id, rarity, 
                           listing_window_hours=LISTING_WINDOW_HOURS, 
                           sale_window_hours=SALE_WINDOW_HOURS,
                           min_samples=MIN_SAMPLES):
    """
    使用 /v2/price-checks，优先取 similar_listings，
    不够时补充 similar_sales，再不够用 /v2/market 兜底。
    """
    params = {"item_id": item_id, "rarity": rarity}
    r = safe_get("https://api.darkerdb.com/v2/price-checks", params)
    if not r:
        return None
    
    if r.status_code == 404:
        # 404 意味着 item_id 不对，需要回退到 /v2/market
        if DEBUG:
            print(f"    [DEBUG] price-checks 404，回退到 /v2/market")
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

    # 收集新鲜 listings
    fresh_prices = []
    for listing in similar_listings:
        listed_at = listing.get("listed_at")
        if not listed_at:
            continue
        try:
            lt = datetime.fromisoformat(listed_at.replace("Z", "+00:00"))
            if lt < listing_cutoff:
                continue
        except:
            continue
        price = listing.get("price")
        if price and price > 0:
            fresh_prices.append(float(price))

    source = "listings"
    # 如果 listings 不够，补充 sales
    if len(fresh_prices) < min_samples:
        for sale in similar_sales:
            sold_at = sale.get("sold_at")
            if not sold_at:
                continue
            try:
                st = datetime.fromisoformat(sold_at.replace("Z", "+00:00"))
                if st < sale_cutoff:
                    continue
            except:
                continue
            price = sale.get("price")
            if price and price > 0:
                fresh_prices.append(float(price))
        if len(fresh_prices) >= min_samples:
            source = "mixed"

    if not fresh_prices:
        return None

    # IQR 去极值
    sorted_p = sorted(fresh_prices)
    n = len(sorted_p)
    min_price = sorted_p[0]

    if n >= 4:
        q1_idx = n // 4
        q3_idx = 3 * n // 4
        q1 = sorted_p[q1_idx]
        q3 = sorted_p[q3_idx]
        iqr = q3 - q1
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        trimmed = [p for p in fresh_prices if lower <= p <= upper]
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
        "freshness": "fresh" if source == "listings" else "low",
        "source": source,
    }


# === 长期记忆 ===
def load_memory():
    if os.path.exists(HISTORY_FILE):
        try:
            return json.load(open(HISTORY_FILE, encoding="utf-8"))
        except:
            pass
    return {}

def save_memory(mem):
    json.dump(mem, open(HISTORY_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def get_price_series(mem, key, days=30):
    if key not in mem:
        return []
    prices = mem[key].get("prices", {})
    cut = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    series = [(k, v) for k, v in prices.items() if k >= cut]
    return sorted(series)

def add_memory(mem, key, today, price):
    if key not in mem:
        mem[key] = {"prices": {}}
    mem[key]["prices"][today] = price
    prices = mem[key]["prices"]
    old_keys = [k for k in prices if k < (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")]
    for k in old_keys:
        del prices[k]


# === AI 分析 ===
def extract_json(text):
    try:
        return json.loads(text)
    except:
        pass
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except:
            pass
    return None

def analyze_with_ai(current_data, memory_context):
    prompt = f"""你是 Dark and Darker 游戏市场分析师 AI。基于提供的材料/消耗品价格数据，完成分析：

【当前价格数据】（物品|品质, 当前新鲜均价, 7日均价, 偏离%）：
{json.dumps(current_data, ensure_ascii=False, indent=2)}

【历史记忆】：
{json.dumps(memory_context, ensure_ascii=False, indent=2)}

请对每个物品进行分析，输出 JSON 格式：
{{
  "analyses": [
    {{
      "item": "物品名|品质",
      "signal": "BUY/SELL/HOLD",
      "current_price": 数值,
      "reason": "波动可能原因（结合游戏版本/副本/供需推测，不确定时说'原因不明'，不要编造）",
      "trend": "上涨/下跌/震荡",
      "trend_basis": "趋势判断依据（基于价格序列形态）",
      "advice": "具体交易建议",
      "risk": "低/中/高",
      "position_note": "持仓相关备注或null"
    }}
  ],
  "market_overview": "整体市场1-2句总结"
}}

要求：
1. reason 必须基于已知事实推测，禁止虚构具体版本号或事件
2. trend_basis 要参考价格序列的高低点、均线方向
3. advice 要具体可执行
4. 只输出 JSON，不要其他文字"""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "openrouter/free",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "max_tokens": 8192
    }
    try:
        r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                         headers=headers, json=data, timeout=120)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        else:
            print(f"⚠️ AI API 返回 {r.status_code}: {r.text[:200]}")
            return None
    except Exception as e:
        print(f"⚠️ AI 调用异常: {e}")
        return None


# === Server酱 推送 ===
def push_to_serverchan(title, content):
    sendkey = SERVERCHAN_SENDKEY
    if not sendkey:
        print("⚠️ 未配置 SERVERCHAN_SENDKEY")
        return
    if sendkey.startswith("sctp"):
        match = re.match(r'^sctp(\d+)t', sendkey)
        if match:
            uid = match.group(1)
            url = f"https://{uid}.push.ft07.com/send/{sendkey}.send"
        else:
            print("⚠️ SendKey 格式错误")
            return
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
    if len(content) > 32000:
        content = content[:32000] + "\n...(截断)"
    data = {"title": title, "desp": content}
    try:
        r = requests.post(url, data=data, timeout=10)
        result = r.json()
        if result.get("code") == 0:
            print("✅ 已推送到微信")
        else:
            print(f"⚠️ 推送失败: {result}")
    except Exception as e:
        print(f"⚠️ 推送异常: {e}")


def format_report(analysis_text):
    data = extract_json(analysis_text)
    if not data:
        return f"⚠️ AI 分析解析失败:\n{analysis_text[:1500]}"
    lines = [f"📊 **DarkerDB AI 市场分析报告** | {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    lines.append("=" * 46)
    analyses = data.get("analyses", [])
    signal_count = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for a in analyses:
        signal = a.get("signal", "HOLD")
        signal_count[signal] = signal_count.get(signal, 0) + 1
        emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(signal, "⚪")
        lines.append(f"\n{emoji} **{a.get('item', '?')}** — {signal}")
        lines.append(f"   当前新鲜均价: {a.get('current_price', '?')}")
        lines.append(f"   💡 原因: {a.get('reason', 'N/A')}")
        lines.append(f"   📈 趋势: {a.get('trend', '?')}（{a.get('trend_basis', '')}）")
        lines.append(f"   🎯 建议: {a.get('advice', 'N/A')}")
        lines.append(f"   ⚠️ 风险: {a.get('risk', '?')}")
        if a.get("position_note"):
            lines.append(f"   📌 持仓: {a['position_note']}")
    overview = data.get("market_overview", "")
    if overview:
        lines.append(f"\n{'='*46}")
        lines.append(f"📋 **市场总览**: {overview}")
    lines.append(f"\n{'='*46}")
    lines.append(f"📊 信号统计: 🟢BUY {signal_count['BUY']} | 🔴SELL {signal_count['SELL']} | ⚪HOLD {signal_count['HOLD']}")
    return "\n".join(lines)


# === 主流程 ===
def main():
    print("🚀 DarkerDB AI Trader 启动（精确变体 ID 修复版）...")
    today = datetime.now().strftime("%Y-%m-%d")
    mem = load_memory()
    print("🔍 查询市场价格...")
    current_data = []
    skipped = []
    fallback_used = []
    for name, rarity in WATCHLIST:
        print(f"\n--- 处理 {name}|{rarity} ---")
        
        # 缓存键
        cache_key_id = f"__exact_id__{name}|{rarity}"
        cache_key_arch = f"__arch_id__{name}"
        
        archetype_id = mem.get(cache_key_arch)
        exact_item_id = mem.get(cache_key_id)
        
        if not archetype_id:
            archetype_id = resolve_archetype_id(name)
            if archetype_id:
                mem[cache_key_arch] = archetype_id
        
        if not exact_item_id and archetype_id:
            exact_item_id = resolve_exact_item_id(archetype_id, rarity)
            if exact_item_id:
                mem[cache_key_id] = exact_item_id
        
        if not exact_item_id:
            print(f"  ❌ {name}: 无法解析 item_id")
            skipped.append(f"{name}|{rarity}: 无法解析 ID")
            continue
        
        print(f"  exact_item_id: {exact_item_id}")
        
        # 尝试 price-checks
        result = get_fresh_price_checks(exact_item_id, rarity)
        
        # 如果 price-checks 失败，用 /v2/market 兜底
        if not result:
            if DEBUG:
                print(f"    [DEBUG] price-checks 无数据，尝试 /v2/market 兜底")
            result = get_price_from_market_fallback(exact_item_id, rarity, archetype_id)
            if result:
                fallback_used.append(f"{name}|{rarity}")
        
        if not result or result["sample_count"] == 0:
            msg = f"{name}|{rarity}: 无有效样本"
            print(f"  ⚠️ {msg}")
            skipped.append(msg)
            continue

        price = result["trimmed_avg"]
        source_label = {
            "listings": "挂牌", 
            "mixed": "挂牌+成交", 
            "sales": "成交",
            "fallback": "兜底(/v2/market)"
        }.get(result["source"], "?")
        
        print(f"  ✅ {name}|{rarity}: 均价={price} "
              f"(样本:{result['sample_count']} 最低:{result['min_price']} "
              f"来源:{source_label})")

        # 计算偏离度
        series = get_price_series(mem, f"{name}|{rarity}")
        if len(series) >= 2:
            past_prices = [p for _, p in series[:-1]]
            if past_prices:
                hist_avg = sum(past_prices) / len(past_prices)
                dev = ((price - hist_avg) / hist_avg) * 100
            else:
                hist_avg = price
                dev = 0
        else:
            day_min = result["min_price"]
            if day_min and day_min > 0:
                dev = ((price - day_min) / day_min) * 100
            else:
                dev = 0
            hist_avg = day_min if day_min else price

        if dev < BUY_T:
            signal = "BUY"
        elif dev > SELL_T:
            signal = "SELL"
        else:
            signal = "HOLD"

        current_data.append({
            "item": f"{name}|{rarity}",
            "current_price": price,
            "avg_7d": round(hist_avg, 1),
            "deviation_pct": round(dev, 1),
            "signal": signal,
            "sample_size": result["sample_count"]
        })

        add_memory(mem, f"{name}|{rarity}", today, price)
        time.sleep(1)

    if not current_data:
        print("❌ 没有获取到任何价格数据")
        if skipped:
            print(f"⚠️ 跳过 {len(skipped)} 个物品: {skipped}")
        return

    memory_context = {}
    for entry in current_data:
        key = entry["item"]
        series = get_price_series(mem, key, days=30)
        if series:
            memory_context[key] = {
                "price_history": [{"date": d, "price": p} for d, p in series[-10:]],
                "data_points": len(series)
            }

    print("\n🤖 AI 分析中...")
    analysis_text = analyze_with_ai(current_data, memory_context)
    if not analysis_text:
        print("⚠️ AI 分析失败，使用基础报告")
        lines = [f"📊 价格报告 | {today}"]
        for e in current_data:
            emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(e["signal"], "⚪")
            lines.append(f"{emoji} {e['item']}: 均价={e['current_price']} (偏离{e['deviation_pct']}%) [样本:{e['sample_size']}]")
        if fallback_used:
            lines.append(f"\n🔄 使用兜底数据的物品: {', '.join(fallback_used)}")
        if skipped:
            lines.append(f"\n⚠️ 跳过 {len(skipped)} 个物品")
        report = "\n".join(lines)
    else:
        report = format_report(analysis_text)
        extra = ""
        if fallback_used:
            extra += f"\n\n🔄 使用兜底数据的物品: {', '.join(fallback_used)}"
        if skipped:
            extra += f"\n\n⚠️ 跳过 {len(skipped)} 个物品: {', '.join(skipped)}"
        report += extra

    save_memory(mem)
    print("📤 推送报告到微信...")
    title = f"📊 DarkerDB 市场分析 | {datetime.now().strftime('%m-%d %H:%M')}"
    push_to_serverchan(title, report)
    print("\n" + "=" * 46)
    print(report[:2000])
    print(f"\n✅ 完成！报告已发送到微信")
    print(f"📊 统计：{len(current_data)} 个物品有数据，{len(skipped)} 个被跳过，{len(fallback_used)} 个用兜底")


if __name__ == "__main__":
    main()
