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
    # **必须带超时**:下面是逐个模型接力,没有超时的话一旦对方不回音,
    # 每个模型都要干等一次,拆一张图能卡好几分钟(见 agent_server 的 _gemini_client)
    client = srv._gemini_client(key)
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

# ── 生图关键词的九个角度 ───────────────────────────────────
# **这是整条链的关键一环**:拆解不是为了写份报告,是为了拿到能直接驱动生图的
# 专业关键词。所以拆解的角度必须和「一条合格的生图提示词由哪几部分组成」对齐 ——
# 业内通行的结构就是下面这九段,顺序也是有讲究的(主体在前、技术在后)。
#
# 为什么要英文:关键词最终要喂给生图模型,而这些模型是按英文摄影术语训练的。
# 中文散文("光线柔和一点")模型只能猜;`overcast diffused daylight` 是它认得的词。
# 中文那份「画面怎么拍」保留着 —— 那是给人看的,两者用途不同。
# **JSON 的键统一用英文**(中文只做显示标签)。血泪:一开始键用中文、值的开头写
# 英文名(`"主体": "subject —— 画面里最主要的…"`),模型直接把值里的英文词当成了键,
# 返回来是 `主体 / action / environment` 混着的一串,按中文名取全是空。
# 键名和值里的内容长得像,模型就会分不清哪个是键。
KEYWORD_DIMENSIONS = [
    ("subject", "主体", "画面正中最主要的人或物,带上关键特征(年纪/穿着/材质/新旧)"),
    ("action", "动作", "他/它正在做什么,要是**正在进行**的动作,不是摆拍姿势"),
    ("environment", "环境", "在什么地方,周围有什么(房子类型、草坪、车、工具)"),
    ("shot", "镜头", "景别和机位:wide establishing / medium / close-up / macro /"
                     " over-the-shoulder / low-angle / high-angle"),
    ("lens", "镜头参数", "焦段光圈和景深,如 35mm f/2.8 shallow depth of field"),
    ("light", "光线", "光的来源和质地,如 natural overcast daylight, soft shadows /"
                      " golden hour backlight / harsh midday sun"),
    ("color", "色调", "整体色彩倾向,如 muted earth tones, desaturated sky"),
    ("treatment", "质感", "摄影门类,如 candid documentary photography, unposed /"
                          " editorial / product studio / phone-shot UGC"),
    ("technical", "技术", "成像细节,如 sharp focus on subject, natural skin texture,"
                          " no HDR, no heavy retouching"),
]

# 英文键 → 中文标签,给人看的时候用
KEYWORD_LABELS = {en: zh for en, zh, _ in KEYWORD_DIMENSIONS}

_KW_SCHEMA = ",\n    ".join(f'"{en}": "{tip}"' for en, _zh, tip in KEYWORD_DIMENSIONS)

_DECOMPOSE_PROMPT = """你是广告投放分析师。看这条**正在真金白银投放**的广告,
回答一个问题:**它为什么跑得动?**

不是"它长什么样",而是**它凭什么拿到展示、凭什么被点、凭什么有转化**;
它最值得偷师的那一个点是什么;我们要怎么把那个点用到自己的广告上。

**只输出 JSON,不要任何解释文字。** 格式:
{
  "为什么有展示": "铺得广、投得久,靠的是什么 —— 题材够普适?人群够宽?"
                   "还是钩子不挑人?一两句说清",
  "为什么被点": {
    "钩子类型": "好奇缺口 / 否定既有认知 / 资格门槛 / 价格悬念 / 本地化 / "
                "时间紧迫 / 身份对号入座 / 恐惧规避,可多选",
    "钩子原话": "标题里真正起作用的那几个词,原样抄",
    "为什么有效": "这个钩子戳中了读者的什么心理,一两句话"
  },
  "为什么有转化": "点进去之后靠什么让人留资 —— 承诺了什么?门槛低在哪?"
                   "信任感从哪来?看不出来就写「从素材看不出」",
  "亮点": "这条广告最值得学的**那一个**点(只写一个,最要紧的那个)",
  "亮点为什么成立": "它为什么能起作用,讲清机制,别只说「吸引人」",
  "如何发挥这个亮点": "换成我们自己的广告,具体怎么做才能把这个点用足",
  "画面起什么作用": "图在这条广告里承担什么 —— 是提供可信度?制造疑问?"
                     "展示结果?还是只是配图不承担说服",
  "可迁移的公式": "抽成一句可以套用的模板,如「否定一个大家习以为常的做法 + 暗示有更好的」",
  "竞品标识": ["图上出现的品牌名或 logo,原样列出;没有就空数组"],
  "生图关键词": {
    __KW_SCHEMA__
  }
}

【硬规矩,违反了整条分析就没用】
1. **绝对不许编造数据。** 你**看不到**真实的点击率、转化率、曝光量 ——
   平台不给这些。你能看到的只有**版位数**(铺了多少个位置)和**投放天数**。
   所以一切结论都是**从"广告主愿意持续为它花钱"倒推出来的**,
   写的时候就要这么写("铺了 N 个版位说明…"),
   **绝不许写出"点击率 3.2%"这类具体数字** —— 那是编的。
2. **只描述你真正看到的东西。** 「竞品标识」要老实列全,后面要靠它把别人的品牌剔干净;
   「为什么有转化」看不出来就照实说看不出来,不要编一个落地页出来。
3. **「亮点」只写一个。** 什么都算亮点等于没有亮点。挑那个"拿掉它这条广告就垮了"的。
4. **「生图关键词」全部用英文**,用摄影和广告行业的专业说法,每项是短语不是句子。
   这一段是**直接拿去生成新素材**的,要写得让另一个人照着就能拍出同样的画面:
   · 好:`middle-aged roofer in worn hi-vis vest`、`overcast diffused daylight, soft shadows`
   · 不好:`a man`、`good lighting`、`高质量摄影`
5. **关键词九项一个都不许留空。** 第 2 条"看不到就照实说"针对的是文字、背书、转化路径
   那类**可能真的观察不到**的东西;而镜头、光线、色调、质感是**任何一张照片都必然有的**
   —— 你看到的就是某种景别、某种光线。拿不准就给最接近的专业说法,不要留空:
   留空的那几项,生成新素材时只能靠模板猜,拆解等于白做。
6. **关键词只描述画面,不含任何文字、logo、品牌名**(平台会单独渲染文字,
   烧进图里会重复;带竞品品牌就是侵权)。"""

# 提示词里全是 JSON 大括号,不能用 f-string(会被当成占位符),所以事后替换
_DECOMPOSE_PROMPT = _DECOMPOSE_PROMPT.replace("__KW_SCHEMA__", _KW_SCHEMA)


def decompose(image: bytes, mime: str = "", headline: str = "", body: str = "",
              perf: dict | None = None) -> dict:
    """看一张广告图,返回它的「素材模型」。

    **headline / body 要一起传进来**:原生广告(Taboola / Outbrain / Yahoo 那类)
    的文字**不在图上**,是平台单独渲染的字段 —— 只看图的话「文字层」永远是空的,
    「文案角度」也无从判断。把文案一并喂给模型,它才能连着画面一起分析。
    """
    prompt = _DECOMPOSE_PROMPT
    if perf:
        # **必须把投放实绩喂进去**,否则模型答不了"为什么跑得动" —— 它只看得到一张图。
        # 只给平台真给的字段(版位数/投放天数/还在不在投/在哪些网络),不给它编的余地。
        lines = [f"{k}:{v}" for k, v in perf.items() if v not in (None, "", [])]
        prompt += ("\n\n【这条广告的投放实绩(平台真给的数,别的都没有)】\n"
                   + "\n".join(lines)
                   + "\n**这就是你能拿到的全部数据。** 展示量、点击率、转化率平台一概不给,"
                     "所以只能从「版位数」和「投放天数」倒推 —— "
                     "铺得越广、投得越久,说明广告主越愿意持续为它花钱。"
                     "**不许把这些数换算成点击率之类的指标。**")
    if headline or body:
        prompt += (f"\n\n【这条广告的文案(不在图上,是平台单独展示的)】\n"
                   f"标题:{headline or '(无)'}\n正文:{body or '(无)'}\n"
                   f"**「文案角度」要结合这段文字来判断**;"
                   f"「文字层」只填**图上真的印着的**字,图上没字就留空数组 —— "
                   f"别把这段文案当成图上的文字。")
    return _ask_vision(image, mime, prompt)


# ============ 归纳 + 出方案 ============

_KW_KEYS = ",\n        ".join(
    f'"{en}": "{tip}"' for en, _zh, tip in KEYWORD_DIMENSIONS)


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
  "为什么这批能跑起来": [
    {{"规律": "这批跑得动的广告共同做对了什么", "出现次数": 0,
      "机制": "它为什么起作用,讲清楚 —— 别只说「吸引人」",
      "证据": "哪几条用了它、版位数多少"}}
  ],
  "最该学的亮点": {{
    "是什么": "这批广告里最值得偷师的**那一个**点",
    "凭什么": "为什么是它 —— 用版位数和投放天数说话",
    "我们怎么用": "换成我们自己的广告,具体怎么把这个点用足"
  }},
  "可迁移的公式": ["2~4 条可以直接套用的模板,如「否定一个大家习以为常的做法 + 暗示有更好的」"],
  "方案": [
    {{
      "命名": "给这版方案起个短名字",
      "主标题": "≤90 字符,英文",
      "描述": "3~90 字符,英文",
      "亮点用在哪": "这一版把上面哪个亮点用起来了、怎么用的",
      "画面怎么拍": "中文,给人看的:具体到能照着去找图或拍图",
      "生图关键词": {{
        {_KW_KEYS}
      }},
      "学的是哪一条": "对应上面「共同点」里的哪一条"
    }}
  ],
  "还缺什么": "要做出这批方案,还需要用户提供什么(比如真实照片、品牌色)"
}}

【最要紧的一条】归纳的目的**不是描述这些广告长什么样,而是搞清楚它们为什么跑得动**,
然后把那个道理用到我们自己的广告上。所以:
· 「为什么这批能跑起来」要讲**机制**(戳中了什么心理、降低了什么门槛),不是罗列外观;
· 「最该学的亮点」**只挑一个** —— 什么都算亮点等于没有亮点,挑那个"拿掉它就垮了"的;
· 每一版方案都要说清**它把哪个亮点用起来了**。

【绝对不许编数据】你能看到的只有**版位数**和**投放天数**,平台不给展示量、
点击率、转化率。所有结论都是从"广告主愿意持续为它花钱"倒推的,写的时候就要这么写。
**不许出现"点击率 3.2%"这类具体数字。**

「方案」要出 {n_variants} 版,**角度彼此不同**(别三版都是打折促销)。
主标题和描述的字符数限制是平台硬性要求,超了建不了广告。
「出现次数」要如实数,数不出来就填 0,不要编。

【关于「生图关键词」—— 这一段最重要,它是要直接拿去生成新素材的】
· **全部英文,用摄影和广告行业的专业说法**,每项是短语不是句子;
· **要从上面那批素材模型里真的归纳出来**,不是套模板 ——
  素材模型里每条都带 `生图关键词`,尤其要看**版位数高的那几条**用的是什么镜头、
  什么光线、什么质感,那是被市场验证过跑得动的画面语言;
· **{n_variants} 版的「镜头」和「镜头参数」必须彼此不同**(远景/特写/仰拍/过肩…) ——
  三版画面一样的话,A/B 测试里画面这个变量等于没变;
· 只描述画面,**不含任何文字、logo、品牌名**;
· 好:`middle-aged roofer in worn hi-vis vest`、`overcast diffused daylight, soft shadows`;
  不好:`a man`、`good lighting`、`高质量摄影`。"""


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
