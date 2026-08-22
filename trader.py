"""
DarkerDB AI Trader - 云端版（Server酱推送）
只使用 /v2/market + since 过滤，获取最近2小时内的新鲜样本
统计方式：去极值后的均价（去掉最低10%和最高10%）
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
FRESH_WINDOW_HOURS = 2   # 只取最近2小时内的样本
HISTORY_FILE = "price_memory.json"

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
                print(f"  ⚠️ 限流，等待 {retry_after}s")
                time.sleep(retry_after)
                continue
            return None
        except:
            if i < 2: time.sleep(2)
    return None

def norm(s):
    return (s or "").strip().lower().replace("’", "'").replace("'", "").replace("-", " ").replace("_", " ").replace("(", "").replace(")", "").replace(":", "")

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

def get_fresh_samples(archetype, rarity, max_age_hours=2):
    """
    只从 /v2/market 拿最近 N 小时内新上架的活跃挂牌。
    返回 dict: {
        "prices": [float],       # 新鲜样本单价列表
        "sample_count": int,     # 样本数
        "min_price": float,      # 最低价
        "trimmed_avg": float,    # 去极值后的均价（去掉最低10%和最高10%）
        "latest_listed_at": str, # 最新样本的上架时间
        "freshness": str,        # "fresh" / "empty"
    }
    """
    # 计算 N 小时前的时间戳（ISO 8601 with Z）
    since_dt = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    since_param = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    params = {
        "archetype": archetype,
        "rarity": rarity,
        "listing_state": "active",
        "since": since_param,       # 关键：只查最近N小时新上架的
        "sort": "created_at:desc",  # 最新的排前面
        "limit": 250,               # 最大化样本量（API上限250）
    }
    
    r = safe_get("https://api.darkerdb.com/v2/market", params)
    if not r:
        return None
    
    data = r.json()
    body = data.get("body", [])
    
    if not body:
        # 官方警告：空结果权威仅当家族扫描状态为 fresh
        # 这里我们无法得知扫描状态，故保守处理
        return {
            "prices": [],
            "sample_count": 0,
            "min_price": None,
            "trimmed_avg": None,
            "latest_listed_at": None,
            "freshness": "empty",
        }
    
    # 收集新鲜样本（双重校验 created_at）
    fresh_prices = []
    latest_listed_at = None
    
    for m in body:
        ppu = m.get("price_per_unit")
        if not ppu or ppu <= 0:
            continue
        
        # 本地二次校验 created_at
        created_at = m.get("created_at")
        if created_at:
            try:
                ct = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if ct < since_dt:
                    continue  # 超出时间窗，跳过
                if latest_listed_at is None or created_at > latest_listed_at:
                    latest_listed_at = created_at
            except:
                pass
        
        # 验证总价一致性，过滤异常数据
        qty = m.get("quantity", 1) or 1
        total = m.get("price", 0)
        if total and abs(ppu * qty - total) / total > 0.1:
            continue
        
        fresh_prices.append(float(ppu))
    
    if not fresh_prices:
        return {
            "prices": [],
            "sample_count": 0,
            "min_price": None,
            "trimmed_avg": None,
            "latest_listed_at": None,
            "freshness": "empty",
        }
    
    # 排序，准备去极值
    sorted_prices = sorted(fresh_prices)
    n = len(sorted_prices)
    min_price = sorted_prices[0]
    
    # 去极值：去掉最低10%和最高10%（至少保留3个样本）
    trim_count = max(0, int(n * 0.1))
    if n - 2 * trim_count >= 3:
        trimmed = sorted_prices[trim_count:n-trim_count]
    else:
        # 样本太少，不去极值，直接用全部
        trimmed = sorted_prices
    
    trimmed_avg = sum(trimmed) / len(trimmed)
    
    return {
        "prices": fresh_prices,
        "sample_count": n,
        "min_price": min_price,
        "trimmed_avg": round(trimmed_avg, 2),
        "latest_listed_at": latest_listed_at,
        "freshness": "fresh",
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


def format_report(analysis_json):
    try:
        data = json.loads(analysis_json)
    except:
        return f"⚠️ AI 分析解析失败:\n{analysis_json[:1500]}"
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
    print("🚀 DarkerDB AI Trader 启动（/v2/market 新鲜样本模式）...")
    print(f"⏰ 时间窗口：最近 {FRESH_WINDOW_HOURS} 小时")
    today = datetime.now().strftime("%Y-%m-%d")
    mem = load_memory()
    print("🔍 查询市场价格（仅新鲜样本）...")
    current_data = []
    skipped = []
    missing_ids = {}
    for name, rarity in WATCHLIST:
        cache_key = f"__id__{name}"
        item_id = mem.get(cache_key) if cache_key in mem else missing_ids.get(name)
        if not item_id:
            item_id = resolve_item_id(name)
            missing_ids[name] = item_id
        if not item_id:
            print(f"  ❌ {name}: 无法解析 item_id")
            continue
        
        # 从 item_id 推导 archetype（去掉末尾的 _数字 后缀）
        # 例如 id.item.troll_blood_5001 -> id.item.troll_blood
        parts = item_id.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            archetype = parts[0]
        else:
            archetype = item_id
        
        # 获取新鲜样本
        result = get_fresh_samples(archetype, rarity, max_age_hours=FRESH_WINDOW_HOURS)
        
        if not result or result["freshness"] != "fresh" or result["sample_count"] == 0:
            msg = f"{name}|{rarity}: {FRESH_WINDOW_HOURS}小时内无新鲜样本"
            print(f"  ⚠️ {msg}")
            skipped.append(msg)
            continue
        
        price = result["trimmed_avg"]
        print(f"  ✅ {name}|{rarity}: 新鲜均价={price} "
              f"(样本:{result['sample_count']} 最低:{result['min_price']} "
              f"最新上架:{result['latest_listed_at']})")
        
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
            hist_avg = price
            dev = 0
        
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
        mem[f"__id__{name}"] = item_id
        time.sleep(1)  # 限流保护：60次/60秒
    
    if not current_data:
        print("❌ 没有获取到任何新鲜价格数据")
        if skipped:
            print(f"⚠️ 跳过 {len(skipped)} 个物品（无新鲜样本）")
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
            lines.append(f"{emoji} {e['item']}: 新鲜均价={e['current_price']} (偏离{e['deviation_pct']}%) [样本:{e['sample_size']}]")
        if skipped:
            lines.append(f"\n⚠️ 跳过 {len(skipped)} 个物品（{FRESH_WINDOW_HOURS}小时内无新鲜样本）")
        report = "\n".join(lines)
    else:
        report = format_report(analysis_json)
        if skipped:
            report += f"\n\n⚠️ 另有 {len(skipped)} 个物品在{FRESH_WINDOW_HOURS}小时内无新鲜样本，未参与分析"
    
    save_memory(mem)
    print("📤 推送报告到微信...")
    title = f"📊 DarkerDB 市场分析 | {datetime.now().strftime('%m-%d %H:%M')}"
    push_to_serverchan(title, report)
    print("\n" + "=" * 46)
    print(report[:2000])
    print(f"\n✅ 完成！报告已发送到微信")
    print(f"📊 统计：{len(current_data)} 个物品有新鲜数据，{len(skipped)} 个被跳过")


if __name__ == "__main__":
    main()
