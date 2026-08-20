"""按创意方案生成可投放的广告图。

**分工是这个模块的全部要点**:AI 只画画面、**一个字都不许写**;
文字(标题/描述/CTA)全部由代码用固定模板压上去。

为什么不让 AI 把文字一起画进去 —— 两条实打实的理由:
1. **AI 画英文经常拼错**(Free Estimate 画成 Free Estimte),而广告图上一个错别字
   就废了整张,还可能被平台拒审;
2. **AI 每次的字号、位置、换行都不一样**,做 A/B 测试时就分不清"效果差是因为
   文案不行,还是因为这次的字正好压在房子上看不清"。

代码叠字是**确定性**的:同样的输入永远同样的输出,不会拼错,位置精确。

另一条经验(实测踩到的):**生图时就按目标比例生成,不要先生成方图再裁**。
1024×1024 裁成 1200×628 横条时,把画面下半部分的正主(屋顶样品)整个裁掉了,
只剩虚化的背景 —— 等于广告主体没了。
"""
from __future__ import annotations

import base64
import io
import os

import httpx
from PIL import Image, ImageDraw, ImageFont

# ── 尺寸与模型 ──────────────────────────────────────────────
AD_SIZE = (1200, 628)        # NewsBreak 建议的广告图尺寸
GEN_SIZE = "1536x1024"       # 生成时就用横版(3:2),裁到 1200×628 只切边不伤主体
IMAGE_MODEL = "openai/gpt-image-1.5"

# 一张 1536×1024 的成本,用来在确认前给用户预期、以及生成前判断余额够不够。
# **$0.20 是实测值**(2026-08-20 记余额 → 生成一张 → 再记余额,差值 $0.2028),
# 不是按单价估的 —— 一开始按 ofox 报的 output_image 单价 × 官方 token 数算出 $0.05,
# 实际是它的 4 倍。**这类"按公开单价算出来的成本"一律要拿账单实测校正。**
COST_PER_IMAGE_USD = 0.20

TIMEOUT = 240.0

_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# 底图的硬规矩。**写死在代码里,不放提示词模板里由 AI 拼** ——
# 少了任何一条,画出来的图就没法安全叠字或有版权风险。
_BASE_RULES = (
    "ABSOLUTELY NO TEXT of any kind: no letters, no numbers, no words, no captions, "
    "no watermarks, no signage, no license plates, no readable labels. "
    "NO logos, NO brand marks, NO trademarks, NO company names. "
    "Do not imitate any existing advertisement or brand. "
    "Keep the UPPER THIRD of the frame visually calm and uncluttered (open sky, "
    "plain wall or soft background) so that text can be overlaid there later; "
    "put the main subject in the lower half. "
    "Photorealistic advertising photography, bright natural daylight, "
    "sharp focus on the subject, wide landscape composition."
)


def check_balance(*, base_url: str = "", api_key: str = "") -> float | None:
    """查生图通道还剩多少钱。查不到返回 None(不因为查不到就拦住用户)。

    ofox 的这个接口不在 OpenAI 方言里(文档没写,是试出来的),所以要容错:
    换了中转商就可能没有,那时应当放行而不是报错。
    """
    base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "").rstrip("/")
    api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
    if not base_url or not api_key:
        return None
    try:
        r = httpx.get(f"{base_url}/user/balance", timeout=20, trust_env=False,
                      headers={"Authorization": f"Bearer {api_key}"})
        if r.status_code != 200:
            return None
        return float(r.json()["balance"])
    except Exception:
        return None


def build_prompt(scene: str) -> str:
    """把方案里的「画面怎么拍」变成生图提示词。"""
    return f"{(scene or '').strip()}\n\n{_BASE_RULES}"


def generate_base(scene: str, *, base_url: str = "", api_key: str = "",
                  model: str = "") -> bytes:
    """让模型画一张无文字底图,返回图片字节。失败时抛 RuntimeError(说人话)。"""
    base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "").rstrip("/")
    api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
    if not base_url or not api_key:
        raise RuntimeError("没有配置生图通道(需要 .env 里的 OPENAI_BASE_URL 和 OPENAI_API_KEY)")

    try:
        r = httpx.post(
            f"{base_url}/images/generations", timeout=TIMEOUT, trust_env=False,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model or IMAGE_MODEL, "prompt": build_prompt(scene),
                  "n": 1, "size": GEN_SIZE},
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"连不上生图服务:{e}") from e

    if r.status_code != 200:
        raise RuntimeError(_gen_error(r))

    try:
        d = (r.json().get("data") or [{}])[0]
    except Exception as e:
        raise RuntimeError(f"生图服务返回的内容看不懂:{str(e)[:80]}") from e

    if d.get("b64_json"):
        return base64.b64decode(d["b64_json"])
    if d.get("url"):
        img = httpx.get(d["url"], timeout=TIMEOUT, trust_env=False)
        if img.status_code != 200:
            raise RuntimeError(f"生成好了但取不回图片(HTTP {img.status_code})")
        return img.content
    raise RuntimeError("生图服务没返回图片")


def _gen_error(r: httpx.Response) -> str:
    """把生图接口的报错翻译成用户能照着做的话。

    **402 余额不足和 401 钥匙无效必须分开报** —— 混着说会让人跑去反复检查钥匙,
    而真正要做的是充值。这和第八节「400 和 401/403 不能混报」是同一条教训。
    """
    try:
        msg = str((r.json().get("error") or {}).get("message") or "")[:200]
    except Exception:
        msg = r.text[:200]
    if r.status_code == 402:
        return f"生图通道余额不足,需要充值后才能继续。平台原话:{msg}"
    if r.status_code in (401, 403):
        return f"生图通道的钥匙无效或没有权限(不是余额问题)。平台原话:{msg}"
    if r.status_code == 429:
        return f"生图请求太频繁,过一会儿再试。平台原话:{msg}"
    if r.status_code >= 500:
        return f"生图服务自己暂时不可用(不是我们的问题),稍后重试。平台原话:{msg}"
    return f"生图失败(HTTP {r.status_code}):{msg}"


# ── 叠字 ────────────────────────────────────────────────────
def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> list[str]:
    """按像素宽度折行(英文按词折,不切断单词)。"""
    lines, line = [], ""
    for word in (text or "").split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def compose(base_image: bytes, headline: str, description: str, cta: str,
            size: tuple[int, int] = AD_SIZE) -> bytes:
    """把文案确定性地压到底图上,返回成品 JPEG 字节。"""
    W, H = size
    im = Image.open(io.BytesIO(base_image)).convert("RGB")

    # 等比放大后居中裁切 —— 直接 resize 会把画面拉变形
    s = max(W / im.width, H / im.height)
    im = im.resize((max(W, round(im.width * s)), max(H, round(im.height * s))), Image.LANCZOS)
    im = im.crop(((im.width - W) // 2, 0, (im.width - W) // 2 + W, H))

    # 顶部压一层由深到透的遮罩。**这层不能省**:白字压在亮天空上会糊成一片,
    # 而底图长什么样我们事先并不知道,只能靠遮罩兜住对比度。
    grad = Image.new("L", (1, H))
    for y in range(H):
        grad.putpixel((0, y), int(220 * max(0.0, 1 - y / (H * 0.58)) ** 1.4))
    im = Image.composite(Image.new("RGB", (W, H), (8, 12, 20)), im, grad.resize((W, H)))

    d = ImageDraw.Draw(im)
    pad = round(W * 0.055)
    f_h = ImageFont.truetype(_FONT_BOLD, round(H * 0.115))
    f_d = ImageFont.truetype(_FONT_REG, round(H * 0.062))
    f_c = ImageFont.truetype(_FONT_BOLD, round(H * 0.055))

    y = pad
    for ln in _wrap(d, headline, f_h, W - pad * 2):
        d.text((pad, y), ln, font=f_h, fill=(255, 255, 255))
        y += f_h.size * 1.16
    y += round(H * 0.02)
    for ln in _wrap(d, description, f_d, W - pad * 2):
        d.text((pad, y), ln, font=f_d, fill=(226, 232, 240))
        y += f_d.size * 1.3

    if cta:
        tw = d.textlength(cta, font=f_c)
        bw, bh = tw + pad * 1.5, f_c.size * 2.2
        bx, by = pad, H - pad - bh
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh / 2, fill=(255, 90, 40))
        d.text((bx + (bw - tw) / 2, by + (bh - f_c.size * 1.32) / 2), cta,
               font=f_c, fill=(255, 255, 255))

    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def render(scene: str, headline: str, description: str, cta: str,
           size: tuple[int, int] = AD_SIZE, **kw) -> bytes:
    """一步到位:生成底图 + 叠字,返回成品图字节。"""
    return compose(generate_base(scene, **kw), headline, description, cta, size)
