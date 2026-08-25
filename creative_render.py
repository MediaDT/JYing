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
# 少了任何一条就是版权风险,或者画出来的图根本不能用。
_BASE_RULES = (
    "ABSOLUTELY NO TEXT of any kind: no letters, no numbers, no words, no captions, "
    "no watermarks, no signage, no license plates, no readable labels. "
    "NO logos, NO brand marks, NO trademarks, NO company names. "
    "Do not imitate any existing advertisement or brand. "
    "Landscape composition, 3:2 aspect."
)

# 质量方向。**照着真正跑得动的竞品广告定的**,不是凭感觉:
# 实测版位数最高的几条(3245 / 2108 / 1373)全是**朴素的纪实实拍** ——
# 真人在真的干活、自然光、略带随手拍的粗糙感;没有一条是打光完美的棚拍摆拍。
# 家装这个品类卖的就是"可信",太精致的商业摄影反而像广告、像假的。
_QUALITY = (
    "Candid documentary photograph, shot on a full-frame DSLR with a 35mm or 50mm lens. "
    "Real working people in a real American residential setting, caught mid-action, "
    "natural expressions, not posed and not looking at the camera. "
    "Natural available daylight, realistic shadows, believable everyday imperfection "
    "(worn tools, ordinary houses, real weather). "
    "Sharp on the subject, honest colors, no HDR, no heavy retouching, "
    "no glossy commercial studio look, no stock-photo perfection."
)

# 三版之间靠**镜头语言**拉开差距,不是靠换文案。
# 血泪:第一版三张全用同一套模板 + 相近提示词,出来三张几乎一样的图,
# 拿去做 A/B 根本测不出东西 —— 变量只有文案,画面是同一张。
VARIETY = [
    "Medium-wide establishing shot showing the whole house and its surroundings; "
    "the worker is small in frame, context dominates.",
    "Tight close-up on hands and the material or tool being worked on; "
    "shallow depth of field, the person's face is out of frame or blurred.",
    "Low-angle shot looking up at the worker against the sky; "
    "strong diagonal lines from a ladder or roofline.",
    "Over-the-shoulder view from behind the worker, showing what they see; "
    "the house or gutter fills the far half of the frame.",
    "Wide shot at golden hour with long shadows across the lawn; "
    "a finished, well-kept exterior with the crew packing up in the background.",
]


def check_balance(*, base_url: str = "", api_key: str = "") -> float | None:
    """查生图通道还剩多少钱。查不到返回 None(不因为查不到就拦住用户)。

    ofox 的这个接口不在 OpenAI 方言里(文档没写,是试出来的),所以要容错:
    换了中转商就可能没有,那时应当放行而不是报错。

    **注意计费有延迟**:实测什么都不做、隔 8 秒再读,余额也会往下掉
    (上一笔请求还在结算)。所以这里读到的数偏高一点是正常的,
    只能当"够不够"的粗略判断,别拿它做精确对账。
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


# 关键词按这个顺序拼。**顺序有讲究**:生图模型对提示词开头的词更敏感,
# 所以主体和动作排前面、成像技术细节排最后。这也是业内提示词的通行结构。
_KW_ORDER = ["subject", "action", "environment", "shot", "lens",
             "light", "color", "treatment", "technical"]


def build_prompt(scene: str, variant: int = 0, keywords: dict | None = None) -> str:
    """拼生图提示词。

    **优先用 `keywords`** —— 那是从竞品素材里真拆出来、再归纳出来的专业关键词
    (九个角度见 `creative_lab.KEYWORD_DIMENSIONS`),已经是英文摄影术语,
    模型认得,比中文散文精确得多。

    `scene`(中文的「画面怎么拍」)是给人看的,同时当**兜底** ——
    没有关键词时照样能出图,只是精度差些。

    variant:第几版。**只有关键词里没交代镜头时**才从 VARIETY 补一种,
    免得同一批几张长得一样;关键词里已经指定了镜头就别再塞,否则两句话打架。
    """
    kw = keywords if isinstance(keywords, dict) else {}
    body = ", ".join(x for x in (str(kw.get(k) or "").strip() for k in _KW_ORDER) if x)

    parts = []
    if body:
        parts.append(body)
        if not (kw.get("shot") or kw.get("lens")):
            parts.append(VARIETY[variant % len(VARIETY)])
        # 质感/技术是"看着可信不可信"的兜底,关键词里没写就补上
        if not (kw.get("treatment") or kw.get("technical")):
            parts.append(_QUALITY)
    else:
        parts += [(scene or "").strip(), VARIETY[variant % len(VARIETY)], _QUALITY]

    parts.append(_BASE_RULES)
    return "\n\n".join(x for x in parts if x)


def generate_base(scene: str, variant: int = 0, keywords: dict | None = None, *,
                  base_url: str = "", api_key: str = "", model: str = "") -> bytes:
    """让模型画一张无文字底图,返回图片字节。失败时抛 RuntimeError(说人话)。"""
    base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "").rstrip("/")
    api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
    if not base_url or not api_key:
        raise RuntimeError("没有配置生图通道(需要 .env 里的 OPENAI_BASE_URL 和 OPENAI_API_KEY)")

    try:
        r = httpx.post(
            f"{base_url}/images/generations", timeout=TIMEOUT, trust_env=False,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model or IMAGE_MODEL, "prompt": build_prompt(scene, variant, keywords),
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
    # 叠字时从顶部裁:文字压在上方,那块留白不能被切掉
    im = _fit(base_image, size, top_anchored=True)

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


def _fit(image: bytes, size: tuple[int, int], top_anchored: bool) -> Image.Image:
    """等比放大后裁到目标尺寸(不拉伸变形)。

    **top_anchored 决定从哪儿裁,这个区别很要紧**:
    · 叠字时从**顶部**裁 —— 文字在上方,得保住那块留白;
    · 不叠字时**居中**裁 —— 3:2 的原图裁成 1200×628 要去掉 22% 的高度,
      一律从底部切的话,主体在下半部分的照片(蹲着干活的人、地上的材料)
      正好被切掉。实测这批图的主体基本都在中下部。
    """
    W, H = size
    im = Image.open(io.BytesIO(image)).convert("RGB")
    s = max(W / im.width, H / im.height)
    im = im.resize((max(W, round(im.width * s)), max(H, round(im.height * s))), Image.LANCZOS)
    x = (im.width - W) // 2
    y = 0 if top_anchored else (im.height - H) // 2
    return im.crop((x, y, x + W, y + H))


def _to_jpeg(image: bytes, size: tuple[int, int] = AD_SIZE) -> bytes:
    """只裁到广告位尺寸,不加任何东西。"""
    buf = io.BytesIO()
    _fit(image, size, top_anchored=False).save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def render(scene: str, headline: str = "", description: str = "", cta: str = "",
           size: tuple[int, int] = AD_SIZE, variant: int = 0,
           overlay: bool = False, keywords: dict | None = None, **kw) -> bytes:
    """生成一张广告图。

    **overlay 默认是关的,也就是出一张干净的实拍图,图上没有任何文字。**

    为什么默认不叠字 —— 三条都是实打实的:
    ① **NewsBreak 会自己渲染文字**。creative 里 `headline` / `description` /
       `callToAction` 是和 `assetUrl` **并列的独立字段**,平台负责把它们画在图旁边。
       再把标题烧进图里,同一句话就出现两遍。
    ② **画一个假按钮是最糟的**。平台会渲染真的行动按钮(实测某账户是 `Get Quote`),
       图上再画一个 `Learn More`,就成了一真一假两个按钮并排。
    ③ **真正跑得动的竞品广告都是干净实拍**。实测版位数最高的几条
       (3245 / 2108 / 1373)图上一个字都没有。

    什么时候才打开 overlay:图要用在**平台不渲染文字**的位置(比如 Push 通知的
    缩略图),或者用户明确要一张"自带标题"的图。那时也**不画按钮**(cta 留空即可)。
    """
    img = generate_base(scene, variant, keywords, **kw)
    if not overlay:
        return _to_jpeg(img, size)
    return compose(img, headline, description, cta, size)
