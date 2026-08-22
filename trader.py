"""
DarkerDB AI Trader - 云端版（Server酱推送）
强制绕过缓存，增加调试打印，使用最低价作为当前价
"""
import os
import json
import time
import requests
from datetime import datetime, timedelta
import re

DARKERDB_KEY = os.environ["DARKERDB_KEY"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]
SERVERCHAN_SENDKEY = os.environ["SERVERCHAN_SENDKEY"]

# === 追踪的物品清单（物品名|品质）===
WATCHLIST = [
    ("Spectral Fabric", "epic"),
    ("Arcane Essence", "unique"),
    ("Gold Coin Bag", "unique"),
    ("Troll's Blood", "epic"),
    ("Gold Ore", "epic"),
    ("Rubysilver Ore", "epic"),
    ("Obsidian Ore", "epic"),
    ("Copper Ore", "uncommon"),
    ("Bone", "common"),
    ("Phantom Flower", "rare"),
    ("Lifeleaf", "rare"),
    ("Tar", "common"),
    ("Wardweed", "common"),
    ("Bavin", "uncommon"),
    ("Ruby (Royal)", "legendary"),
    ("Blue Sapphire (Royal)", "legendary"),
    ("Potion of Healing", "uncommon"),
    ("Potion of Protection", "rare"),
    ("Magic Protection Potion", "epic"),
    ("Surgical Kit", "rare"),
    ("Bandage", "rare"),
    ("Lockpick", "common"),
    ("Great Potion of Luck", "rare"),
    ("Scraps", "epic"),
    ("Hard Crab Shell", "uncommon"),
    ("Cockatrice's Lucky Feather", "rare"),
    ("Dark Matter", "epic"),
    ("Banshee Sonnet", "epic"),
    ("Cyclops Precious Mirror", "epic"),
    ("Cave Trolls Precious Rock", "epic"),
    ("Thick Forefoot", "epic"),
    ("Ancient Scroll (Royal)", "legendary"),
    ("Giant Horn", "epic"),
    ("Cyclops Eye", "epic"),
    ("Gold Crown (Royal)", "legendary"),
    ("Gold Chalice (Royal)", "legendary"),
    ("Gold Waterpot (Royal)", "legendary"),
    ("Gold Goblet (Royal)", "legendary"),
    ("Broken Skull", "poor"),
    ("Moldy Bread", "poor"),
    ("Frosted Feather", "rare"),
    ("Antiquated Coin", "unique"),
    ("Cockatrice's Egg", "rare"),
    ("Gold Waterpot (Exquisite)", "rare"),
    ("Saltvine", "rare"),
    ("Sturdy Rope", "rare"),
    ("Sturdy Log", "rare"),
    ("Bone Powder", "epic"),
    ("Centaur Hoof", "epic"),
    ("Maggots", "common"),
    ("Cracked Tusk", "common"),
    ("Sturdy Cloth", "common"),
    ("Billet", "common"),
    ("Bowstring", "common"),
    ("Torn Sail", "common"),
    ("Bat Claw", "common"),
    ("Sharp Sea Urchin Spine", "uncommon"),
    ("Grave Essence", "uncommon"),
    ("Blue Sapphire (Perfect)", "epic"),
    ("Ruby (Perfect)", "epic"),
    ("Ruby (Exquisite)", "rare"),
    ("Emerald (Royal)", "legendary"),
    ("Emerald (Exquisite)", "rare"),
    ("Emerald (Ultimate)", "unique"),
    ("Blue Sapphire (Exquisite)", "rare"),
    ("Diamond (Royal)", "legendary"),
    ("Golden Teeth", "rare"),
    ("Silver Ingot", "epic"),
    ("Potion of Clarity", "epic"),
    ("Trap Disarming Kit", "uncommon"),
    ("Lyre", "legendary"),
    ("Lyre", "rare"),
    ("Flute", "unique"),
    ("Potion of Invisibility", "rare"),
    ("Throwing Knife", "rare"),
    ("Dynamite", "rare"),
    ("Soul Heart", "epic"),
    ("Lantern", "epic"),
    ("Lantern", "rare"),
]

# === 信号阈值 ===
BUY_T = -15
SELL_T = 20
PER_ITEM_LIMIT = 20
MIN_VALID_PRICE = 1
HISTORY_FILE = "price_memory.json"

# === 工具函数 ===
def safe_get(url, params=None, retries=3):
    headers = {
        "X-API-Key": DARKERDB_KEY,
        "X-API-Version": "2026-08-03",
        "Cache-Control": "no-cache",   # 强制绕过缓存
        "Pragma": "no-cache"           # 兼容老版本
    }
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params or {}, timeout=30)
            if r.status_code == 200:
                return r
            elif r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 5)))
                continue
            return None
        except:
            if i < 2: time.sleep(2)
    return None

def norm(s):
    return (s or "").strip().lower().replace("’", "'").replace("'", "").replace("-", " ").replace("_", " ").replace("(", "").replace(")", "").replace(":", "")

def derive_archetype(item_id):
    if not item_id:
        return None
    parts = item_id.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return item_id

def resolve_item_id(name):
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
            return item.get("id")
    for item in results:
        if isinstance(item, dict) and item.get("type") == "item":
            return item.get("id")
    return None

def query_market(archetype, rarity):
    params = {
        "archetype": archetype,
        "limit": PER_ITEM_LIMIT,
        "listing_state": "active",
        "sort": "price_per_unit:asc",
    }
    if rarity:
        params["rarity"] = rarity.lower()
    r = safe_get("https://api.darkerdb.com/v2/market", params)
    if not r:
        return []
    body = r.json().get("body", [])
    
    # 调试：打印第一条挂牌的信息（id、price_per_unit、当前时间）
    if body:
        first = body[0]
        print(f"  [DEBUG] {first.get('name')} id={first.get('id')} ppu={first.get('price_per_unit')} at {datetime.now().strftime('%H:%M:%S')}")
    
    prices = []
    for m in body:
        ppu = m.get("price_per_unit")
        if not ppu or ppu <= MIN_VALID_PRICE:
            continue
        if m.get("listing_state") != "active":
            continue
        qty = m.get("quantity", 1) or 1
        total = m.get("price", 0)
        if total and abs(ppu * qty - total) / total > 0.1:
            continue
        prices.append(float(ppu))
    return prices

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
def analyze_with_ai(current_data, memory_context):
    prompt = f"""你是 Dark and Darker 游戏市场分析师 AI。基于提供的材料/消耗品价格数据，完成分析：

【当前价格数据】（物品|品质, 当前最低价, 7日均价, 偏离%）：
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
        "max_tokens": 4096
    }
    try:
        r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                         headers=headers, json=data, timeout=60)
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

def format_report(analysis_json):
    try:
        data = json.loads(analysis_json)
    except:
        return f"⚠️ AI 分析解析失败:\n{analysis_json[:1500]}"
    lines = [f"📊 **DarkerDB AI 市场分析报告** | {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    lines.append("=" * 40)
    analyses = data.get("analyses", [])
    signal_count = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for a in analyses:
        signal = a.get("signal", "HOLD")
        signal_count[signal] = signal_count.get(signal, 0) + 1
        emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(signal, "⚪")
        lines.append(f"\n{emoji} **{a.get('item', '?')}** — {signal}")
        lines.append(f"   当前价: {a.get('current_price', '?')}")
        lines.append(f"   💡 原因: {a.get('reason', 'N/A')}")
        lines.append(f"   📈 趋势: {a.get('trend', '?')}（{a.get('trend_basis', '')}）")
        lines.append(f"   🎯 建议: {a.get('advice', 'N/A')}")
        lines.append(f"   ⚠️ 风险: {a.get('risk', '?')}")
        if a.get("position_note"):
            lines.append(f"   📌 持仓: {a['position_note']}")
    overview = data.get("market_overview", "")
    if overview:
        lines.append(f"\n{'='*40}")
        lines.append(f"📋 **市场总览**: {overview}")
    lines.append(f"\n{'='*40}")
    lines.append(f"📊 信号统计: 🟢BUY {signal_count['BUY']} | 🔴SELL {signal_count['SELL']} | ⚪HOLD {signal_count['HOLD']}")
    return "\n".join(lines)

# === 主流程 ===
def main():
    print("🚀 DarkerDB AI Trader 启动...")
    today = datetime.now().strftime("%Y-%m-%d")
    mem = load_memory()
    print("🔍 查询市场价格...")
    current_data = []
    missing_ids = {}
    for name, rarity in WATCHLIST:
        cache_key = f"__id__{name}"
        item_id = mem.get(cache_key) if cache_key in mem else missing_ids.get(name)
        if not item_id:
            item_id = resolve_item_id(name)
            missing_ids[name] = item_id
        if not item_id:
            print(f"  ❌ {name}: 无法解析")
            continue
        archetype = derive_archetype(item_id)
        prices = query_market(archetype, rarity)
        if not prices:
            print(f"  ⚠️ {name}|{rarity}: 无有效挂牌")
            continue
        current = min(prices)  # 使用最低价（不加过滤）
        avg_list = sum(prices) / len(prices)
        series = get_price_series(mem, f"{name}|{rarity}")
        if len(series) >= 2:
            past_prices = [p for _, p in series[:-1]]
            if past_prices:
                hist_avg = sum(past_prices) / len(past_prices)
                dev = ((current - hist_avg) / hist_avg) * 100
            else:
                hist_avg = current
                dev = 0
        else:
            hist_avg = current
            dev = 0
        if dev < BUY_T:
            signal = "BUY"
        elif dev > SELL_T:
            signal = "SELL"
        else:
            signal = "HOLD"
        current_data.append({
            "item": f"{name}|{rarity}",
            "current_price": current,
            "avg_7d": round(hist_avg, 1),
            "deviation_pct": round(dev, 1),
            "signal": signal,
            "sample_size": len(prices)
        })
        add_memory(mem, f"{name}|{rarity}", today, current)
        mem[f"__id__{name}"] = item_id
        print(f"  ✅ {name}|{rarity}: {current} ({signal})")
        time.sleep(0.5)
    if not current_data:
        print("❌ 没有获取到任何价格数据")
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
    analysis_json = analyze_with_ai(current_data, memory_context)
    if not analysis_json:
        print("⚠️ AI 分析失败，使用基础报告")
        lines = [f"📊 价格报告 | {today}"]
        for e in current_data:
            emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(e["signal"], "⚪")
            lines.append(f"{emoji} {e['item']}: {e['current_price']} (偏离{e['deviation_pct']}%)")
        report = "\n".join(lines)
    else:
        report = format_report(analysis_json)
    save_memory(mem)
    print("📤 推送报告到微信...")
    title = f"📊 DarkerDB 市场分析 | {datetime.now().strftime('%m-%d %H:%M')}"
    push_to_serverchan(title, report)
    print("\n" + "=" * 40)
    print(report[:2000])
    print(f"\n✅ 完成！报告已发送到微信")

if __name__ == "__main__":
    main()
