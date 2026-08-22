"""
DarkerDB AI Trader - 云端版（Server酱推送）
- 双账号自动轮转
- 按时间戳记录价格，支持昼夜规律分析
- 每2小时运行一次优化参数
- AI 安全过滤规避 + 稳定免费模型回退
- 增强降级逻辑
"""
import os
import json
import time
import requests
import subprocess
from datetime import datetime, timedelta, timezone
import re

# ===== 从环境变量读取配置 =====
_raw_keys = os.environ.get("DARKERDB_KEYS", "").strip()
if not _raw_keys:
    _single = os.environ.get("DARKERDB_KEY", "").strip()
    if _single:
        _raw_keys = _single
DARKERDB_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]

OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
SERVERCHAN_SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")

# ===== 追踪的物品清单（10 个热门物品）=====
WATCHLIST = [
    ("Troll's Blood", "epic"),
    ("Gold Ore", "epic"),
    ("Rubysilver Ore", "epic"),
    ("Obsidian Ore", "epic"),
    ("Bone", "common"),
    ("Potion of Healing", "uncommon"),
    ("Bandage", "rare"),
    ("Grave Essence", "uncommon"),
    ("Blue Sapphire (Perfect)", "epic"),
    ("Ruby (Perfect)", "epic"),
]

# ===== 信号阈值 =====
BUY_T = -15
SELL_T = 20
LISTING_WINDOW_HOURS = 6
SALE_WINDOW_HOURS = 24
MIN_SAMPLES = 1
HISTORY_FILE = "price_memory.json"
ACCOUNT_STATE_FILE = "account_state.json"
DATA_RETENTION_DAYS = 7
DEBUG = True

# ===== 工具函数 =====

def load_account_state():
    if os.path.exists(ACCOUNT_STATE_FILE):
        try:
            with open(ACCOUNT_STATE_FILE) as f:
                state = json.load(f)
                idx = state.get("current_key_index", 0)
                if 0 <= idx < len(DARKERDB_KEYS):
                    return idx
        except:
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
                    remaining = r.headers.get("X-RateLimit-Remaining")
                    if remaining is not None:
                        remaining = int(remaining)
                        limit = int(r.headers.get("X-RateLimit-Limit", 60))
                        if remaining < limit * 0.1:
                            print(f"    ⚠️ Key[{key_idx}] 额度快耗尽 (剩余{remaining}/{limit})，下次切换")
                            save_account_state((key_idx + 1) % len(DARKERDB_KEYS))
                    return r
                elif r.status_code == 429:
                    continue
                elif r.status_code == 403:
                    continue
                else:
                    print(f"    ⚠️ 请求失败: {r.status_code}")
                    return None
            except Exception as e:
                if DEBUG:
                    print(f"    ⚠️ Key[{key_idx}] 请求异常: {e}")
                continue
        wait = 5 * (attempt + 1)
        print(f"    ⚠️ 所有 key 均不可用，等待 {wait}s 后重试...")
        time.sleep(wait)
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
            if DEBUG:
                print(f"    [DEBUG] archetype_id for '{name}': {found}")
            return found
    for item in results:
        if isinstance(item, dict) and item.get("type") == "item":
            return item.get("id")
    return None

def get_price_from_market_fallback(archetype_id, rarity):
    params = {"archetype": archetype_id, "rarity": rarity, "limit": 20}
    r = safe_get("https://api.darkerdb.com/v2/market", params)
    if not r or r.status_code != 200:
        return None
    data = r.json()
    body = data.get("body")
    if not body:
        return None
    if isinstance(body, list):
        listings = body
    elif isinstance(body, dict):
        listings = body.get("listings", [])
    else:
        return None
    if not listings:
        return None
    prices = [float(l.get("price")) for l in listings if l.get("price") and l.get("price") > 0]
    if not prices:
        return None
    if DEBUG:
        print(f"    [DEBUG] /v2/market fallback: {len(prices)} listings, 价格范围 {min(prices)}-{max(prices)}")
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
        lower = q1 - 1.5 * iqr; upper = q3 + 1.5 * iqr
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

# ===== 长期记忆（支持时间戳）=====

def load_memory():
    if os.path.exists(HISTORY_FILE):
        try:
            data = json.load(open(HISTORY_FILE, encoding="utf-8"))
            for key in list(data.keys()):
                if key.startswith("__"):
                    continue
                prices = data[key].get("prices", {})
                new_prices = {}
                for k, v in prices.items():
                    if re.match(r'^\d{4}-\d{2}-\d{2}$', k):
                        ts = f"{k}T12:00:00Z"
                        new_prices[ts] = v
                    else:
                        new_prices[k] = v
                data[key]["prices"] = new_prices
            return data
        except:
            pass
    return {}

def save_memory(mem):
    json.dump(mem, open(HISTORY_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def get_price_series(mem, key, hours=168):
    if key not in mem:
        return []
    prices = mem[key].get("prices", {})
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    series = [(k, v) for k, v in prices.items() if k >= cutoff]
    return sorted(series)

def add_memory(mem, key, timestamp, price):
    if key not in mem:
        mem[key] = {"prices": {}}
    mem[key]["prices"][timestamp] = price
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DATA_RETENTION_DAYS)).isoformat()
    prices = mem[key]["prices"]
    old = [k for k in prices if k < cutoff]
    for k in old:
        del prices[k]

# ===== AI 分析（稳定免费模型 + 去敏感化 prompt）=====

def extract_json(text):
    if "User Safety" in text or (text.strip().lower().startswith("safe") and len(text) < 80):
        print("    ⚠️ AI 请求被安全过滤，尝试备用模型...")
        return None
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
    pattern = r'\{(?:[^{}]|(?:\{[^{}]*\}))*\}'
    matches = re.findall(pattern, text)
    for match in matches:
        try:
            return json.loads(match)
        except:
            continue
    return None

def analyze_with_ai(current_data, memory_context):
    # 去敏感化的 prompt：纯技术性数据分析，避免触发安全过滤
    prompt = f"""Task: Analyze the following JSON price data and output a JSON analysis report.

Current price data (item|rarity, current_avg, 7day_avg, deviation_percent):
{json.dumps(current_data, ensure_ascii=False, indent=2)}

Historical data (with timestamps):
{json.dumps(memory_context, ensure_ascii=False, indent=2)}

Requirements:
1. Output ONLY a valid JSON object, no other text.
2. Structure:
{{
  "analyses": [
    {{
      "item": "Item|Rarity",
      "signal": "BUY" or "SELL" or "HOLD",
      "current_price": number,
      "reason": "brief explanation, say 'uncertain' if unknown",
      "trend": "rising" or "falling" or "stable" or "insufficient_data",
      "trend_basis": "explanation of trend, mention 'insufficient_data' if data_points < 2",
      "advice": "specific action suggestion",
      "risk": "low" or "medium" or "high",
      "position_note": "note or null"
    }}
  ],
  "summary": "1-2 sentence overall summary"
}}
3. You MUST provide exactly one analysis entry for EACH item in the current price data. Do NOT skip any.
4. Even with only 1 data point, assign a signal based on the deviation_percent (negative = BUY, positive > 20 = SELL, otherwise HOLD)."""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json"
    }
    # 使用当前稳定可用的免费模型（带 :free 后缀）
    models = [
        "openrouter/free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "openai/gpt-oss-20b:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "qwen/qwen3-next-80b-a3b-instruct:free"
    ]

    for i, model in enumerate(models):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4000
        }
        try:
            r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                              headers=headers, json=payload, timeout=90)
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                if DEBUG:
                    print(f"    [DEBUG] AI response via {model}: {content[:300]}")
                # 检查是否被安全过滤
                if "User Safety" in content or (content.strip().lower().startswith("safe") and len(content) < 80):
                    print(f"    ⚠️ {model} blocked by safety filter, trying next...")
                    continue
                return content
            else:
                print(f"    ⚠️ {model} returned {r.status_code}")
                if r.status_code == 404:
                    print(f"        Model not available, skipping")
                    continue
        except Exception as e:
            print(f"    ⚠️ {model} error: {e}")
        time.sleep(0.5)

    print("❌ 所有模型均失败")
    return None

# ===== Server酱 推送 =====

def push_to_serverchan(title, content):
    sendkey = SERVERCHAN_SENDKEY
    if not sendkey:
        print("⚠️ 未配置 SERVERCHAN_SENDKEY")
        return
    if sendkey.startswith("sctp"):
        m = re.match(r'^sctp(\d+)t', sendkey)
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
        print("⚠️ AI 返回空分析列表，降级到基础报告")
        return None
    lines = [f"📊 **DarkerDB 数据分析报告** | {datetime.now().strftime('%Y-%m-%d %H:%M')}", "=" * 72]
    sc = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for a in analyses:
        sig = a.get("signal", "HOLD")
        sc[sig] = sc.get(sig, 0) + 1
        em = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(sig, "⚪")
        lines += [
            f"\n{em} **{a.get('item', '?')}** — {sig}",
            f"   当前均价: {a.get('current_price', '?')}",
            f"   💡 原因: {a.get('reason', 'N/A')}",
            f"   📈 趋势: {a.get('trend', '?')}（{a.get('trend_basis', '')}）",
            f"   🎯 建议: {a.get('advice', 'N/A')}",
            f"   ⚠️ 风险: {a.get('risk', '?')}"
        ]
        if a.get("position_note"):
            lines.append(f"   📌 持仓: {a['position_note']}")
    if data.get("summary"):
        lines += ["\n" + "=" * 57, f"📋 **总结**: {data['summary']}"]
    lines += ["\n" + "=" * 44, f"📊 信号: 🟢BUY {sc['BUY']} | 🔴SELL {sc['SELL']} | ⚪HOLD {sc['HOLD']}"]
    return "\n".join(lines)

# ===== 主流程 =====

def build_basic_report(current_data, fallback_used, skipped):
    """AI 失败时的基础报告"""
    lines = [f"📊 价格报告 | {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    for e in current_data:
        em = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(e["signal"], "⚪")
        lines.append(f"{em} {e['item']}: 均价={e['current_price']} (偏离{e['deviation_pct']}%) [样本:{e['sample_size']}]")
    if fallback_used:
        lines.append(f"\n🔄 兜底: {', '.join(fallback_used)}")
    if skipped:
        lines.append(f"\n⚠️ 跳过 {len(skipped)} 个")
    return "\n".join(lines)

def main():
    print("🚀 DarkerDB AI Trader 启动（每2小时版，双账号轮转）...")
    print(f"📋 已配置 {len(DARKERDB_KEYS)} 个 DarkerDB 账号")
    current_idx = load_account_state()
    print(f"🔑 本次优先使用账号 #{current_idx + 1}")

    now_utc = datetime.now(timezone.utc)
    timestamp_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"⏰ 当前时间戳: {timestamp_str}")

    mem = load_memory()
    print("🔍 查询市场价格...")
    current_data, skipped, fallback_used = [], [], []

    for name, rarity in WATCHLIST:
        print(f"\n--- 处理 {name}|{rarity} ---")
        ck_id = f"__exact_id__{name}|{rarity}"
        ck_arch = f"__arch_id__{name}"
        arch_id = mem.get(ck_arch)
        exact_id = mem.get(ck_id)

        if not arch_id:
            arch_id = resolve_archetype_id(name)
            if arch_id:
                mem[ck_arch] = arch_id
        if not exact_id and arch_id:
            exact_id = arch_id
            mem[ck_id] = exact_id
        if not exact_id:
            print(f"  ❌ {name}: 无法解析 ID")
            skipped.append(f"{name}|{rarity}: 无法解析 ID")
            continue
        print(f"  item_id: {exact_id}")

        result = get_fresh_price_checks(exact_id, rarity)
        if not result:
            if DEBUG:
                print(f"    [DEBUG] price-checks 无数据，尝试 /v2/market 兜底 (archetype={arch_id})")
            result = get_price_from_market_fallback(arch_id, rarity)
            if result:
                fallback_used.append(f"{name}|{rarity}")
        if not result or result["sample_count"] == 0:
            print(f"  ⚠️ {name}|{rarity}: 无有效样本")
            skipped.append(f"{name}|{rarity}: 无有效样本")
            continue

        price = result["trimmed_avg"]
        src = {"listings": "挂牌", "mixed": "挂牌+成交", "sales": "成交", "market_fallback": "兜底(/v2/market)"}.get(result["source"], "?")
        print(f"  ✅ {name}|{rarity}: 均价={price} (样本:{result['sample_count']} 最低:{result['min_price']} 来源:{src})")

        series = get_price_series(mem, f"{name}|{rarity}", hours=168)
        today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        past_series = [(k, v) for k, v in series if k < today_start]
        if len(past_series) >= 2:
            past_prices = [v for _, v in past_series]
            hist_avg = sum(past_prices) / len(past_prices)
            dev = ((price - hist_avg) / hist_avg) * 100
        else:
            dmin = result["min_price"]
            hist_avg = dmin if dmin else price
            dev = ((price - dmin) / dmin) * 100 if dmin else 0

        signal = "BUY" if dev < BUY_T else ("SELL" if dev > SELL_T else "HOLD")
        current_data.append({
            "item": f"{name}|{rarity}",
            "current_price": price,
            "avg_7d": round(hist_avg, 1),
            "deviation_pct": round(dev, 1),
            "signal": signal,
            "sample_size": result["sample_count"]
        })
        add_memory(mem, f"{name}|{rarity}", timestamp_str, price)
        time.sleep(1)

    if not current_data:
        print("❌ 无数据")
        if skipped:
            print(f"⚠️ 跳过 {len(skipped)} 个: {skipped}")
        return

    mc = {}
    for e in current_data:
        s = get_price_series(mem, e["item"], hours=168)
        if s:
            mc[e["item"]] = {
                "price_history": [{"time": k, "price": v} for k, v in s[-20:]],
                "data_points": len(s)
            }

    print("\n🤖 AI 分析中...")
    at = analyze_with_ai(current_data, mc)
    if at:
        report = format_report(at)
        if report is None:
            print("⚠️ AI 返回内容无效，使用基础报告")
            report = build_basic_report(current_data, fallback_used, skipped)
    else:
        print("⚠️ AI 失败，使用基础报告")
        report = build_basic_report(current_data, fallback_used, skipped)

    # 追加兜底和跳过信息
    if report and (fallback_used or skipped):
        extra = ""
        if fallback_used:
            extra += f"\n\n🔄 兜底: {', '.join(fallback_used)}"
        if skipped:
            extra += f"\n\n⚠️ 跳过 {len(skipped)} 个: {', '.join(skipped)}"
        report += extra

    save_memory(mem)
    print("📤 推送...")
    push_to_serverchan(f"📊 DarkerDB 市场分析 | {datetime.now().strftime('%m-%d %H:%M')}", report)
    print("\n" + "=" * 76)
    print(report[:2000])
    print(f"\n✅ 完成！有数据:{len(current_data)} 跳过:{len(skipped)} 兜底:{len(fallback_used)}")

    # Git 操作
    try:
        subprocess.run(["git", "config", "--global", "user.email", "action@github.com"], capture_output=True)
        subprocess.run(["git", "config", "--global", "user.name", "GitHub Action"], capture_output=True)
        subprocess.run(["git", "add", HISTORY_FILE, ACCOUNT_STATE_FILE], capture_output=True)
        subprocess.run(["git", "commit", "-m", f"Update price memory at {timestamp_str}"], capture_output=True)
        pull = subprocess.run(["git", "pull", "--rebase", "origin", "main"], capture_output=True, text=True)
        if pull.returncode != 0:
            print(f"⚠️ Git pull 失败，尝试 force push")
            subprocess.run(["git", "push", "--force", "origin", "main"], capture_output=True)
        else:
            push = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push.returncode != 0:
                subprocess.run(["git", "push", "--force", "origin", "main"], capture_output=True)
    except Exception as e:
        print(f"⚠️ Git 操作异常: {e}")

if __name__ == "__main__":
    main()
