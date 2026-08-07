"""部署自检:三个外部接口到底能不能用(不只是"能不能连上")。

用法(在项目目录下):
    ./venv/bin/python check_brain.py

它会真的各发一次最小请求,把平台返回的原话打出来。
"""
import re
import sys
from pathlib import Path

ENV = Path(__file__).with_name(".env")


def env(key: str) -> str:
    """从 .env 里现读一个值(不依赖进程环境变量)。"""
    if not ENV.exists():
        return ""
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip()
    return ""


def mask(s: str) -> str:
    return f"{s[:6]}…{s[-4:]}" if len(s) > 12 else "(空)"


def show(name: str, ok: bool, detail: str) -> None:
    print(f"  {'✅' if ok else '❌'} {name}:{detail}")


print("=" * 60)
print("部署自检 —— 真的发一次请求,不只是 ping")
print("=" * 60)

# ---------- 1. Gemini ----------
print("\n【1】Gemini(免费额度那条路)")
key = env("GEMINI_API_KEY")
if not key:
    show("Gemini", False, "`.env` 里没有 GEMINI_API_KEY")
else:
    print(f"     用的钥匙:{mask(key)}")
    try:
        from google import genai
        client = genai.Client(api_key=key)
        r = client.models.generate_content(model="gemini-flash-latest", contents="说'好'一个字")
        show("Gemini", True, f"能用,回了「{(r.text or '').strip()[:20]}」")
    except Exception as e:
        msg = str(e)
        if "location is not supported" in msg.lower() or "user_location" in msg.lower():
            show("Gemini", False, "**你所在地区不支持** —— 网络通但 Google 拒绝服务,必须改用 ofox")
        elif "429" in msg or "quota" in msg.lower():
            show("Gemini", True, "钥匙有效,只是今天免费额度用完了(明天会恢复)")
        elif "api key" in msg.lower() or "401" in msg or "403" in msg:
            show("Gemini", False, f"钥匙有问题:{msg[:120]}")
        else:
            show("Gemini", False, f"连不上或出错:{msg[:160]}")

# ---------- 2. ofox / OpenAI 通道 ----------
print("\n【2】ofox 中转(付费那条路,国内一般能通)")
key = env("OPENAI_API_KEY")
base = env("OPENAI_BASE_URL")
model = env("OPENAI_MODEL") or "gpt-4o-mini"
if not key:
    show("ofox", False, "`.env` 里没有 OPENAI_API_KEY")
else:
    print(f"     地址:{base or '(默认 OpenAI 官方)'}  模型:{model}")
    print(f"     用的钥匙:{mask(key)}")
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url=base or None, timeout=60)
        r = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "说'好'一个字"}], max_tokens=50)
        reply = (r.choices[0].message.content or "").strip()
        show("ofox", True, f"能用,回了「{reply[:20]}」" if reply
             else "连通正常(这次回复为空,常见于推理模型把额度用在思考上,不影响判断)")
    except Exception as e:
        msg = str(e)
        if "insufficient_quota" in msg or "billing" in msg.lower():
            show("ofox", False, "钥匙有效但**账户没余额**了")
        elif "401" in msg or "invalid" in msg.lower():
            show("ofox", False, f"钥匙不对:{msg[:120]}")
        else:
            show("ofox", False, f"连不上或出错:{msg[:160]}")

# ---------- 3. NewsBreak ----------
print("\n【3】NewsBreak 广告平台(这条必须通,否则项目没意义)")
key = env("NEWSBREAK_ACCESS_TOKEN")
if not key:
    show("NewsBreak", False, "`.env` 里没有 NEWSBREAK_ACCESS_TOKEN")
else:
    print(f"     用的钥匙:{mask(key)}")
    try:
        import newsbreak_client as nb
        orgs = nb.list_organizations()
        show("NewsBreak", True, f"能用,查到 {len(orgs)} 个组织")
    except Exception as e:
        show("NewsBreak", False, f"{str(e)[:160]}")

print("\n" + "=" * 60)
print("怎么读这份结果:")
print("  · Gemini ❌ / ofox ✅  → `.env` 里设 BRAIN=openai(别用 auto,会白等超时)")
print("  · Gemini ✅ / ofox ✅  → 设 BRAIN=auto(免费的优先用)")
print("  · NewsBreak ❌         → 必须先解决,不然查不了数据也投不了广告")
print("=" * 60)
