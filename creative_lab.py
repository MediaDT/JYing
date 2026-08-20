"""
创意实验室 —— 把竞品广告"拆开看懂",再照着套路写出我们自己的方案。

**这一期(P0)只出文字,不出图。** 拆解 + 归纳 + 出创意方案这段技术成熟、
成本极低、没有版权风险;而"自动出图直接投"有三个过不去的坎(素材是缩略图、
AI 渲染精确文字不可靠、全自动等于把版权风险批量化),留到后面按期做。

两个能力:
  · decompose()  看一张广告图 → 结构化的「素材模型」(版式/主体/文字层/配色/CTA/文案角度)
  · summarize()  多个素材模型汇总 → 共同套路 + 照着写的我们自己的文案方案

**合规是这个模块的硬约束,写在提示词的最前面而不是末尾**:
产出的方案里绝不许出现竞品的品牌名、logo 描述,也不许照抄竞品的具体承诺
(「$99 起」「48 小时上门」这类)—— 我们做不到就是虚假宣传,平台也会拒审。
"""

import io
import json
import re

# 喂给模型前把图缩到这个边长。实测竞品/图库原图能到 6000×4000(10MB),
# 直接发既慢又贵,而拆解广告结构根本不需要那么大。1024 足够看清版式和文字。
MAX_EDGE = 1024

# 视觉模型的接力顺序。和聊天那条链一个道理:flash 会 503(高负载),
# 不接力的话整个功能就断了 —— 实测第一次调用就撞上了。
VISION_MODELS = ("gemini-flash-latest", "gemini-flash-lite-latest")


def shrink(data: bytes, mime: str = "") -> tuple[bytes, str]:
    """把图缩到 MAX_EDGE 以内并转成 JPEG。失败就原样返回,不因为缩图失败而中断。"""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.thumbnail((MAX_EDGE, MAX_EDGE))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=82)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return data, mime or "image/jpeg"


def _json_from(text: str) -> dict:
    """模型爱把 JSON 包在 ```json 围栏里,也可能前后带客套话。都剥掉。"""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    else:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        return {"error": f"模型没有返回合法 JSON:{e}", "原文": (text or "")[:400]}


def _ask_vision(image: bytes, mime: str, prompt: str) -> dict:
    """把图 + 提示词发给视觉模型,要一个 JSON 回来。逐个模型接力。"""
    import agent_server as srv
    from google import genai
    from google.genai import types

    key = srv._read_env_value("GEMINI_API_KEY")
    if not key:
        return {"error": "没有配置 GEMINI_API_KEY,看不了图"}
    client = genai.Client(api_key=key)
    small, smime = shrink(image, mime)

    last = ""
    for model in VISION_MODELS:
        try:
            r = client.models.generate_content(
                model=model,
                contents=[types.Part.from_bytes(data=small, mime_type=smime), prompt],
            )
            return _json_from(r.text)
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:120]}"
            continue
    return {"error": f"视觉模型都没响应({last})"}


# ============ 拆解 ============

_DECOMPOSE_PROMPT = """你是广告创意分析师。看这张广告图,拆出它的「素材模型」。

**只输出 JSON,不要任何解释文字。** 格式:
{
  "版式": "如 上图下字 / 左右对比 / 全屏图叠字 / 三宫格",
  "画面主体": "画面里最主要的东西,一句话说清(如:破损的沥青瓦屋顶特写,白天晴天)",
  "有无真人": "有真人出镜 / 无真人",
  "文字层": [{"角色": "主标题|副标题|优惠|价格|CTA按钮|角标", "内容": "原样抄图上的字", "位置": "顶部|中部|底部|左上角..."}],
  "画面风格": "如 实拍 / 效果图 / 前后对比 / 产品特写;光线和氛围",
  "可否复刻": "用图库照片或自己拍,能不能做出类似的画面?难在哪?",
  "主色": ["#RRGGBB 或颜色名,2~4 个"],
  "信任背书": ["如 Licensed & Insured 徽章 / 五星评分 / BBB logo"],
  "文案角度": "痛点 / 优惠 / 紧迫感 / 社会证明 / 好奇心,可多个",
  "可复用的点": "这条广告值得学的是什么,一两句话",
  "竞品标识": ["图上出现的品牌名或 logo,原样列出;没有就空数组"]
}

三条硬规矩:
1. **只描述你真正看到的东西。** 图上没有的字段留空字符串或空数组,**绝对不要编**。
   这如果不是一张广告图(比如只是张风景照),就把 CTA、信任背书、文案角度都留空。
2. 「文字层」里的内容要**原样照抄图上的文字**,不要翻译、不要改写。
3. 「竞品标识」要老实列全 —— 后面要靠它把别人的品牌剔干净。"""


def decompose(image: bytes, mime: str = "", headline: str = "", body: str = "") -> dict:
    """看一张广告图,返回它的「素材模型」。

    **headline / body 要一起传进来**:原生广告(Taboola / Outbrain / Yahoo 那类)
    的文字**不在图上**,是平台单独渲染的字段 —— 只看图的话「文字层」永远是空的,
    「文案角度」也无从判断。把文案一并喂给模型,它才能连着画面一起分析。
    """
    prompt = _DECOMPOSE_PROMPT
    if headline or body:
        prompt += (f"\n\n【这条广告的文案(不在图上,是平台单独展示的)】\n"
                   f"标题:{headline or '(无)'}\n正文:{body or '(无)'}\n"
                   f"**「文案角度」要结合这段文字来判断**;"
                   f"「文字层」只填**图上真的印着的**字,图上没字就留空数组 —— "
                   f"别把这段文案当成图上的文字。")
    return _ask_vision(image, mime, prompt)


# ============ 归纳 + 出方案 ============

def _summary_prompt(models: list, brand: str, landing: str, n_variants: int) -> str:
    """拼归纳提示词。合规要求放在**最前面**,和 SYSTEM_PROMPT_EN 那条教训一样 ——
    放末尾的约束容易被前面一大段内容带跑。"""
    return f"""【绝对要求,优先于下面所有内容】
1. 你产出的文案里**绝不许出现任何竞品的品牌名、公司名或 logo 描述**。
   下面素材模型里的「竞品标识」字段列出的名字,一个都不许出现在你的产出里。
2. **绝不许照抄竞品的具体承诺** —— 例如「$99 起」「48 小时上门」「终身质保」
   这类具体价格、时效、保证。我们做不到就是虚假宣传,平台也会拒审。
   要学的是**表达角度**(比如"用低门槛报价降低决策成本"),不是具体数字。
3. **主标题和描述必须是你自己重新写的**,不许把「文字层」里的句子原样搬过来。
   学的是**角度和句式结构**,用词要换成我们自己的。
   反例:竞品写 "Call an Expert Contractor Now",你就不能也写这句;
   可以学它"直接下命令 + 强调专业"的路子,写成别的话。
4. 只能基于下面给你的素材模型来归纳。**不许编造你没看到的广告**。

---

我给你 {len(models)} 条同行正在投的高效广告的「素材模型」(都是投放时间长、曝光高的)。
我们自己的信息:品牌「{brand or '(未提供)'}」,落地页「{landing or '(未提供)'}」。

素材模型:
{json.dumps(models, ensure_ascii=False, indent=1)}

---

请输出 JSON,不要解释文字:
{{
  "共同点": [
    {{"维度": "版式|画面|文案角度|信任背书|配色", "规律": "这批广告的共同做法", "出现次数": 0, "为什么有效": "一句话"}}
  ],
  "值得学的": ["3~5 条可直接照做的建议"],
  "方案": [
    {{
      "命名": "给这版方案起个短名字",
      "主标题": "≤90 字符,英文",
      "描述": "3~90 字符,英文",
      "画面怎么拍": "具体到能照着去找图或拍图:主体、角度、光线、有没有人",
      "版式建议": "文字放哪、什么颜色",
      "学的是哪一条": "对应上面「共同点」里的哪一条"
    }}
  ],
  "还缺什么": "要做出这批方案,还需要用户提供什么(比如真实照片、品牌色)"
}}

「方案」要出 {n_variants} 版,**角度彼此不同**(别三版都是打折促销)。
主标题和描述的字符数限制是平台硬性要求,超了建不了广告。
「出现次数」要如实数,数不出来就填 0,不要编。"""


def summarize(models: list, brand: str = "", landing: str = "",
              n_variants: int = 3, lang: str = "zh") -> dict:
    """把多个素材模型归纳成共同套路 + 我们自己的文案方案。走聊天那套大脑接力。"""
    if not models:
        return {"error": "没有可归纳的素材模型,请先拆解几条竞品广告"}
    import agent_server as srv
    prompt = _summary_prompt(models, brand, landing, max(1, min(int(n_variants or 3), 5)))
    try:
        # 复用聊天那条三级火箭,但不给工具 —— 这一步是纯文本推理,不该去调接口
        text = srv._plain_completion(prompt, lang=lang)
    except Exception as e:
        return {"error": f"归纳失败:{str(e)[:200]}"}
    out = _json_from(text)
    if not out.get("error"):
        _flag_copied(out, models)
    return out


def _words(t: str) -> set:
    return {w for w in re.findall(r"[a-z0-9']+", (t or "").lower()) if len(w) > 2}


def _flag_copied(out: dict, models: list) -> None:
    """查产出的文案有没有把竞品原句搬过来,搬了就点名标出来。

    **为什么要在代码里查**:提示词里已经写了"不许照抄",但实测模型照样会把
    竞品的主标题原样吐回来(第一版实测里两条都中招)。文案抄袭不像品牌名那样
    一眼能看出来,用户很可能就直接拿去投了 —— 这种事不能只靠模型自觉。

    判据:和竞品某句的实词重合度 ≥70%,就算抄。取 70% 是因为学句式必然会有
    共同词(roof / free / now),但整句重合到这个程度就不是"学"了。
    """
    src = []
    for m in models:
        for layer in (m.get("文字层") or []):
            if isinstance(layer, dict) and layer.get("内容"):
                src.append(str(layer["内容"]))
    for v in (out.get("方案") or []):
        if not isinstance(v, dict):
            continue
        for field in ("主标题", "描述"):
            mine = _words(v.get(field, ""))
            if len(mine) < 3:
                continue
            for one in src:
                theirs = _words(one)
                if theirs and len(mine & theirs) / len(mine) >= 0.7:
                    v.setdefault("⚠️查重", []).append(
                        f"{field}和竞品原句高度重合:「{one[:60]}」—— 请换个说法")
                    out["有照抄嫌疑"] = True
                    break
