"""
落地页实验室 —— 找同行落地页、拆解转化结构、生成新的落地页草稿。

边界说清楚:
  · OpenAdLibrary 经常只给 landingDomain,不给完整 URL，但会提供落地页截图元数据。
  · 截图可用时结合视觉证据拆解；截图失效时只用域名当前公开页面文字做降级分析，
    并明确它不一定是广告当时的完整投放路径。
  · 生成的新页面学习的是结构、信息顺序和 offer 设计,不复制竞品文案、品牌和图片。
"""

from __future__ import annotations

import html
import html as html_mod          # 局部变量常叫 html,遮住模块名,配图那段要用转义
import ipaddress
import json
import hashlib
import re
import socket
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx

import creative_search as cs

TIMEOUT = 18.0
MAX_TEXT_CHARS = 12000
MAX_IMAGE_BYTES = 15 * 1024 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

GENERATED_DIR = Path(__file__).parent / "data" / "landing_pages"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
KEEP_GENERATED = 80        # 每个人各留这么多份,不是所有人共用这么多


def _owner_dir(owner: str = "") -> Path:
    """这个人的预览目录。

    **按人分目录。** 原来所有人的页面平铺在同一个目录里,后果有三层:
      ① 预览不存在时的 404 页面会把**别人的**预览文件列成可点链接;
      ② 取文件时零归属检查 —— 拿到文件名就能读别人的整页(里面有他的
         落地页文案、CTA 追踪地址);
      ③ `KEEP_GENERATED=80` 是**所有人共用**的,A 多生成几轮就把 B 的页面
         删掉了,而 B 的待办还指着那个文件 —— 确认发布时才报「文件找不到」。

    `owner` 为空 = 不在用户上下文(命令行 / 测试)→ 用平铺的老目录。
    **老文件(按人分目录之前生成的)仍然平铺在根目录下**,当作"没有归属"处理:
    谁都读得到。和老待办、老定时任务的兼容口径一致 —— 否则那些页面谁都发不了。
    """
    name = re.sub(r"[^A-Za-z0-9_-]+", "", str(owner or ""))[:64]
    if not name:
        return GENERATED_DIR
    d = GENERATED_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_preview(filename: str, owner: str = "") -> Path | None:
    """把文件名解析成盘上的真实路径:**先找他自己的,再找没有归属的老文件**。

    找不到返回 None。目录穿越(`../`、带斜杠、非 .html)一律拒绝 ——
    这个函数的返回值会被直接读出来发给用户。
    """
    name = Path(str(filename or "")).name
    if not name or "/" in str(filename or "") or "\\" in str(filename or "") \
            or not name.lower().endswith(".html"):
        return None
    for base in ([_owner_dir(owner), GENERATED_DIR] if owner else [GENERATED_DIR]):
        path = (base / name).resolve()
        if path.parent == base.resolve() and path.is_file():
            return path
    return None


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.skip = 0
        self.title = ""
        self.headings: list[str] = []
        self.buttons: list[str] = []
        self.links: list[str] = []
        self.form_fields: list[str] = []
        self._tag_stack: list[str] = []
        self._buf: list[str] = []
        self._href = ""
        self.text_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript", "svg"):
            self.skip += 1
            return
        attrs_d = {str(k).lower(): str(v or "") for k, v in attrs}
        if t in ("title", "h1", "h2", "h3", "button", "a", "label"):
            self._tag_stack.append(t)
            self._buf = []
            self._href = attrs_d.get("href", "")
        if t in ("input", "select", "textarea"):
            name = attrs_d.get("placeholder") or attrs_d.get("name") or attrs_d.get("type") or t
            if name and len(self.form_fields) < 20:
                self.form_fields.append(name[:80])

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1
            return
        if not self._tag_stack or self._tag_stack[-1] != t:
            return
        txt = _clean(" ".join(self._buf))
        self._tag_stack.pop()
        self._buf = []
        if not txt:
            return
        if t == "title":
            self.title = txt[:160]
        elif t in ("h1", "h2", "h3") and len(self.headings) < 25:
            self.headings.append(txt[:180])
        elif t == "button" and len(self.buttons) < 20:
            self.buttons.append(txt[:80])
        elif t == "a":
            if len(txt) <= 90 and len(self.links) < 30:
                self.links.append(txt[:90])
            self._href = ""
        elif t == "label" and len(self.form_fields) < 20:
            self.form_fields.append(txt[:80])

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        txt = _clean(data)
        if not txt:
            return
        if self._tag_stack:
            self._buf.append(txt)
        if len(txt) >= 2:
            self.text_chunks.append(txt)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(s or ""))).strip()


def _json_from(text: str) -> dict:
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
    except Exception:
        return {"error": "模型没有返回合法 JSON", "原文": (text or "")[:600]}


def _assert_public_url(url: str) -> None:
    """阻止落地页抓取被用来访问本机、云元数据或其它内网服务。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL 必须是公开的 http(s) 地址")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("不能抓取本机或内网地址")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError("域名无法解析") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("不能抓取本机、内网或保留地址")


def _check_request(request: httpx.Request) -> None:
    # httpx 每次重定向都会触发 request hook，因此跳转到内网也会被拦下。
    _assert_public_url(str(request.url))


def fetch_page(url: str) -> dict:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"error": "URL 必须以 http 或 https 开头"}
    try:
        _assert_public_url(url)
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True, trust_env=False,
                          headers={"User-Agent": UA}, event_hooks={"request": [_check_request]}) as c:
            r = c.get(url)
        ctype = (r.headers.get("content-type") or "").lower()
        if r.status_code >= 400:
            return {"error": f"页面返回 HTTP {r.status_code}", "url": url}
        if "html" not in ctype and "<html" not in r.text[:1000].lower():
            return {"error": f"这不像 HTML 页面({ctype or 'unknown content-type'})", "url": url}
    except Exception as e:
        return {"error": f"抓取失败:{str(e)[:160]}", "url": url}

    p = _PageParser()
    try:
        p.feed(r.text[:800000])
    except Exception:
        pass
    text = _clean(" ".join(p.text_chunks))[:MAX_TEXT_CHARS]
    return {
        "url": str(r.url),
        "domain": urlparse(str(r.url)).netloc,
        "title": p.title,
        "headings": p.headings,
        "buttons": sorted(set(p.buttons))[:12],
        "links": sorted(set(p.links))[:20],
        "form_fields": sorted(set(p.form_fields))[:20],
        "text": text,
        "text_chars": len(text),
    }


def fetch_image(url: str) -> tuple[bytes, str] | tuple[None, str]:
    """安全下载公开截图；失败时第二项是可直接展示的原因。"""
    try:
        _assert_public_url(url)
        with httpx.Client(timeout=18, follow_redirects=True, trust_env=False,
                          headers={"User-Agent": UA}, event_hooks={"request": [_check_request]}) as c:
            r = c.get(url)
        ctype = (r.headers.get("content-type") or "").split(";", 1)[0].lower()
        if r.status_code >= 400:
            return None, f"截图返回 HTTP {r.status_code}"
        if not ctype.startswith("image/"):
            return None, f"截图地址返回的不是图片({ctype or 'unknown'})"
        if len(r.content) > MAX_IMAGE_BYTES:
            return None, "截图超过 15MB，暂不处理"
        return r.content, ctype
    except Exception as e:
        return None, f"截图下载失败:{str(e)[:120]}"


def _ask_json(prompt: str) -> dict:
    import agent_server as srv
    import openai

    key = srv._read_env_value("OPENAI_API_KEY")
    if not key:
        return {"error": "没有配置 OPENAI_API_KEY,暂时不能做落地页智能拆解/生成"}
    client = openai.OpenAI(
        api_key=key,
        base_url=srv._read_env_value("OPENAI_BASE_URL") or None,
        timeout=75,
        max_retries=1,
    )
    model = srv._read_env_value("OPENAI_MODEL") or "gpt-5-mini"
    r = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是直效广告落地页策略师。只输出合法 JSON。"},
            {"role": "user", "content": prompt},
        ],
    )
    return _json_from(r.choices[0].message.content or "")


def decompose(page: dict, ad_context: dict | None = None, lang: str = "zh") -> dict:
    ad_context = ad_context or {}
    prompt = f"""请拆解这个落地页为什么可能转化好。

硬规则:
1. 只能基于给你的页面文本和广告上下文分析,不要编造真实 CTR/CVR/收入。
2. 如果页面没有抓到完整内容,必须降低置信度并说明原因。
3. 不要复制竞品品牌名、联系方式、价格承诺或具体优惠数字。
4. 输出 JSON。

广告上下文:
{json.dumps(ad_context, ensure_ascii=False, indent=1)}

页面抽取:
{json.dumps({k: v for k, v in page.items() if k != "text"}, ensure_ascii=False, indent=1)}

页面正文节选:
{page.get("text", "")[:MAX_TEXT_CHARS]}

输出格式:
{{
  "页面主题": "",
  "目标人群": "",
  "offer": {{
    "核心承诺": "",
    "低门槛设计": "",
    "紧迫感或资格门槛": "",
    "风险逆转": ""
  }},
  "为什么可能表现好": [
    {{"角度": "排版/文案/offer/信任/表单/移动端节奏", "判断": "", "证据": ""}}
  ],
  "页面结构": [
    {{"模块": "首屏/痛点/方案/信任/表单/FAQ/CTA", "作用": "", "页面证据": ""}}
  ],
  "关键词和关键信息": {{
    "关键词": [],
    "痛点词": [],
    "利益点": [],
    "信任背书": [],
    "CTA": []
  }},
  "可迁移优点": [],
  "不能照搬的内容": [],
  "置信度": "高/中/低"
}}"""
    return _ask_json(prompt)


def decompose_screenshot(image: bytes, mime: str, page: dict,
                         ad_context: dict | None = None, lang: str = "zh") -> dict:
    """结合落地页截图、页面文字和投放信号做视觉拆解。"""
    import creative_lab

    ad_context = ad_context or {}
    prompt = f"""你是直效广告落地页策略师。请结合截图和证据拆解这个页面。

硬规则:
1. 截图是主要视觉证据；页面文字可能只是该域名当前首页，不一定是广告当时的完整路径，必须标注证据边界。
2. 投放天数、版位数、抓取次数只能作为持续投放/传播信号，不等于真实 CTR、CVR 或收入。
3. 不要照抄竞品品牌、联系方式、具体价格、保证或优惠数字。
4. 只输出合法 JSON。

广告与落地页证据:
{json.dumps(ad_context, ensure_ascii=False, indent=1)}

页面文字抽取:
{json.dumps({k: v for k, v in page.items() if k != 'text'}, ensure_ascii=False, indent=1)}

页面正文节选:
{str(page.get('text') or '')[:MAX_TEXT_CHARS]}

输出格式:
{{
  "页面主题": "",
  "目标人群": "",
  "offer": {{"核心承诺": "", "低门槛设计": "", "紧迫感或资格门槛": "", "风险逆转": ""}},
  "为什么可能表现好": [{{"角度": "排版/文案/offer/信任/表单/移动端节奏", "判断": "", "证据": "截图/文字/投放信号"}}],
  "页面结构": [{{"模块": "首屏/痛点/方案/信任/表单/FAQ/CTA", "作用": "", "页面证据": ""}}],
  "关键词和关键信息": {{"关键词": [], "痛点词": [], "利益点": [], "信任背书": [], "CTA": []}},
  "可迁移优点": [],
  "不能照搬的内容": [],
  "证据边界": [],
  "置信度": "高/中/低"
}}"""
    return creative_lab._ask_vision(image, mime, prompt)


def _today_str() -> str:
    import scheduler as sched
    return sched.now_beijing().strftime("%Y-%m-%d")


def summarize(models: list[dict], brand: str = "", offer: str = "",
              audience: str = "", lang: str = "zh", n_variants: int = 2) -> dict:
    """汇总竞品落地页并生成 n_variants 个新页面(1 或 2)。

    **要几版必须能由用户说了算。** 原来写死 2 版:用户说「只要一个」也照出两个,
    多的那一版是白花的模型钱,而且他还要在两个里挑 —— 等于没听他说话。
    """
    n_variants = 2 if int(n_variants or 2) >= 2 else 1
    many = n_variants > 1
    prompt = f"""下面是若干个竞品/同行落地页拆解结果。请汇总共性,再生成 {n_variants} 个全新的落地页。

硬规则(第 1 条最重要,不满足的页面**根本发布不出去**,等于白做):
1. **每一个真正跳往 Offer 的 CTA,必须写成 <a href="[[CLICKFLARE_CTA_URL]]">**,
   一个字都不许改、不许自己编跳转地址。整页至少要有 1 处,通常 2~3 处
   (首屏一个、中间一个、页尾一个)。
   · 表单式落地页也一样:提交按钮要么写成上面那种 <a>,要么表单
     <form action="[[CLICKFLARE_CTA_URL]]" method="get">。
   · **绝对不许**用 onclick="alert(...)"、href="#"、href="javascript:void(0)"
     这类假交互冒充 CTA —— 那样用户点了哪儿也去不了,广告费全打水漂。
2. 在 </body> 前原样放一个 <!--[[CLICKFLARE_LANDER_SCRIPT]]--> 注释占位符,
   发布时由后端安全替换。
3. 新页面必须是新的表达,不能照抄任何竞品文案、品牌、电话号码、价格、保证或样式细节。
4. 重点分析为什么表现好:排版、文字描述、offer、表单、信任背书、CTA 节奏。
5. {"生成的两个页面要方向不同,方便 A/B test。" if many else
   "**只要 1 个页面**,别多给 —— 用户明确只要一个,多出来的那版是白花钱。"}
6. **配图**:需要放图的地方写 `<img src="[[IMAGE_1]]" alt="...">`,
   编号从 1 开始、本页内不重复;并在这一版的 `"配图"` 里说明每个编号要找什么图。
   `搜索词`**必须是英文**(图库按英文搜),写得具体些:
   `metal roof installation crew` 比 `roof` 强得多。
   **绝不许自己写任何图片网址**(http、https、data: 都不行)——
   图片由后端从正规授权图库找好、下载下来、逐字替换进去;你写出来的网址一定是编的,
   用户点开是一张裂图,而买来的流量已经落在上面了。
   一页 2~3 张就够,**首屏那张最有用**。真的不需要图就别写 `<img>`,也别留空占位框。
   除了图片,**不要引用外部 JS/CSS/字体**;版式用 CSS 做。
7. **今年是 {this_year()} 年**(北京时间 {_today_str()})。页面里任何地方出现年份 ——
   页脚版权、标题里的「XXXX 年提醒」、文中的「XXXX 年新规」—— **一律用 {this_year()}**,
   绝不许写更早的年份。写成过期年份的话,用户一眼就看出这是张旧页面,信任感当场没了。
   拿不准就**干脆别写年份**。
8. 只输出 JSON。

我们自己的信息:
品牌: {brand or "(未提供)"}
offer: {offer or "(未提供,请用可替换占位表达,不要编具体价格)"}
目标人群: {audience or "(未提供)"}

拆解结果:
{json.dumps(models, ensure_ascii=False, indent=1)}

输出格式:
{{
  "共同规律": [
    {{"规律": "", "为什么有效": "", "证据": "", "如何迁移": ""}}
  ],
  "关键词池": {{
    "核心关键词": [],
    "痛点词": [],
    "利益点": [],
    "信任词": [],
    "CTA词": []
  }},
  "offer设计建议": [],
  "新落地页": [
    {{
      "命名": "Version A - ...",
      "定位": "",
      "学到的优点": [],
      "首屏主标题": "",
      "首屏副标题": "",
      "表单策略": "",
      "配图": [
        {{"编号": 1, "搜索词": "英文关键词", "说明": "放在哪儿、为什么要这张"}}
      ],
      "html": "<!doctype html>..."
    }},
    {{
      "命名": "Version B - ...",
      "定位": "",
      "学到的优点": [],
      "首屏主标题": "",
      "首屏副标题": "",
      "表单策略": "",
      "html": "<!doctype html>..."
    }}
  ],
  "合规提醒": []
}}"""
    return _ask_json(prompt)


CTA_MARK = "[[CLICKFLARE_CTA_URL]]"
SCRIPT_MARK = "<!--[[CLICKFLARE_LANDER_SCRIPT]]-->"
_FAKE_CTA = (r"""onclick\s*=\s*["'][^"']*alert\(""", r"""href\s*=\s*["']\s*#\s*["']""",
             r"""href\s*=\s*["']\s*javascript:""")


# 版权声明里的年份:© / &copy; / (c) / Copyright 后面跟一个年份,或者 2019-2024 这种区间。
# 这几种形状是**确定**的,可以放心自动改;正文里的年份不行(见 fix_stale_years)。
_COPYRIGHT_YEAR = re.compile(
    r"(&copy;|©|\(c\)|copyright)(\s*)(?:(20\d\d)(\s*[-–—]\s*))?(20\d\d)", re.I)
_ANY_YEAR = re.compile(r"\b(20\d\d)\b")


def this_year() -> int:
    """今年是哪年 —— **按北京时间**,和命名规范用的是同一个口径。"""
    import scheduler as sched
    return sched.now_beijing().year


def fix_stale_years(html: str, year: int | None = None) -> tuple[str, list[str], list[str]]:
    """把过期的年份处理掉。返回 (新 html, 自动改了什么, 还得用户自己定的)。

    **为什么必须有**:生成落地页的提示词里原来一个字都没提今天是哪年,模型只能
    用训练时的默认年份 —— 实测 8 个生成页里出现了 **9 次 2024**,而当时是 2026 年。
    一个 2026 年的广告落地页写着「© 2024」「2024 Homeowner Alert」,用户一眼看出是旧的,
    信任感当场没了。提示词已经补上今天的日期,但**提示词不是防线**(这个项目反复踩过),
    所以再加一道确定性的。

    分两类处理,这是关键:
      · **版权声明**(© 2024 / Copyright 2019-2024)—— 形状固定、不涉及文案,**直接改对**;
      · **正文里的年份**(标题里的「2024 Homeowner Alert」)—— 那是**广告文案**,
        改了就等于替用户改了广告的说法。只**如实报出来**让他自己定。
    """
    year = int(year or this_year())
    fixed: list[str] = []

    def _repl(m):
        mark, gap, start, dash, end = m.groups()
        if int(end) >= year:
            return m.group(0)
        fixed.append("%s %s → %d" % (mark, (start + dash if start else "") + end, year))
        if start:
            return "%s%s%s%s%d" % (mark, gap, start, dash, year)
        return "%s%s%d" % (mark, gap, year)

    out = _COPYRIGHT_YEAR.sub(_repl, html)

    # **扫正文之前先把版权那几段遮掉**:`Copyright 2019-2026` 里的起始年 2019
    # 是合法的(版权区间本来就从过去某年算起),不遮的话会当成"过期年份"误报,
    # 而误报多了用户就不看这个提示了。
    masked = _COPYRIGHT_YEAR.sub(lambda m: " " * len(m.group(0)), out)
    stale: list[str] = []
    for m in _ANY_YEAR.finditer(masked):
        if int(m.group(1)) >= year:
            continue
        a, b = max(0, m.start() - 45), min(len(out), m.end() + 25)
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", out[a:b])).strip()
        # 把那个年份标出来 —— 上下文里可能还有别的(已经改好的)年份,不标的话看不出说的是哪个
        stale.append(snippet.replace(m.group(1), "【%s】" % m.group(1), 1))
    return out, fixed, stale


def _check_publishable(html: str) -> tuple[str, list[str]]:
    """存盘前就把「这页能不能发布」判出来,并把能确定性补的补上。

    **为什么必须在这里查**:提示词管不住模型 —— 实测它生成过整页零个占位符、
    CTA 是 `onclick="alert('Thank you!')"` 的假按钮。那种页面走到发布那一步会被
    护栏拒绝,但用户已经把整条链走完了才知道白做。所以在交出去之前就说清楚。

    脚本占位符可以**确定性**补(位置固定,就在 </body> 前);
    CTA 占位符**不能猜** —— 哪个按钮才是真正跳往 Offer 的,正则判断不了,
    猜错就是把追踪挂在错误的元素上。只能如实报"这版不能发布"。
    """
    problems = []
    if SCRIPT_MARK not in html and "[[CLICKFLARE_LANDER_SCRIPT]]" not in html:
        if re.search(r"</body\s*>", html, re.I):
            html = re.sub(r"</body\s*>", SCRIPT_MARK + "\n</body>", html, count=1, flags=re.I)
        else:
            html += "\n" + SCRIPT_MARK
    if html.count(CTA_MARK) < 1:
        problems.append(f"没有任何 CTA 占位符({CTA_MARK}),发布时会被拒绝")
    for pattern in _FAKE_CTA:
        if re.search(pattern, html, re.I):
            problems.append("页面里有 alert()/#/javascript: 这类假 CTA,用户点了哪儿也去不了")
            break
    return html, problems


_IMAGE_MARK = re.compile(r"\[\[IMAGE_(\d+)\]\]")
# 带着某个占位符的整个 <img> 标签 —— 找不到图时要把整个标签摘掉,不能留个破图
_IMG_TAG = r"<img\b[^>]*%s[^>]*>"
# 外部图片地址:模型偶尔还是会写一个出来(提示词管不住,见坑表)
_EXTERNAL_IMG = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"\']\s*(?:https?:|//|data:)[^>]*>", re.I)
IMG_SUBDIR = "img"
MAX_IMAGES_PER_PAGE = 4


def _img_dir(owner: str = "") -> Path:
    """这个人的落地页配图目录。和页面同级放在 `img/` 下面 ——
    页面里用**相对路径** `img/xxx.jpg` 引用,这样本地预览和发布到
    Cloudflare(`/<slug>/a/index.html` + `/<slug>/a/img/xxx.jpg`)**同一套写法都成立**,
    不用在两个地方各拼一次绝对地址。
    """
    d = _owner_dir(owner) / IMG_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


# 这一轮里有哪几家图库已经确认**没反应**了 —— 同一次生成里不再重试。
# **别写成「连不上」** —— 2026-09-08 查实:DNS 18ms / TCP 20ms / TLS 123ms 全正常,
# 是 Openverse 自己的服务器在磨(同一分钟内 0.5~32 秒乱跳)。一页最多 4 张图,
# 每张都白等一次就是 80 秒 —— 用户那头看着像卡死,还可能撞上前端的空闲上限。
# 只在一次 attach_images 里生效,不做进程级缓存:钥匙/网络随时可能变好。
_DEAD_SOURCES: set = set()


def _pick_image(query: str) -> dict | None:
    """按关键词去授权图库找一张最好的。找不到返回 None。

    **只走 `creative_search`**:那里已经把「只要可商用」写死在请求参数里
    (Openverse 强制 `license=cc0,pdm`),换个地方找图就绕过了那道闸门。
    """
    ready = [x["id"] for x in cs.available_sources() if x.get("ready")]
    if ready and all(x in _DEAD_SOURCES for x in ready):
        return None            # 这一轮所有图库都确认连不上了,后面几张不再白等
    try:
        got = cs.search(query, count=6)
    except Exception:
        return None
    # 这次哪几家超时/报错了,记下来,后面几张图不再等它
    for line in (got.get("errors") or []):
        _DEAD_SOURCES.add(str(line).split(":")[0].strip())
    for one in got.get("results") or []:
        if one.get("quality", {}).get("可用") is False:
            continue                     # 分辨率不够 / 竖图,一票否决(见第六之十一节)
        return one
    return (got.get("results") or [None])[0]


def attach_images(html: str, specs: list, owner: str = "") -> tuple[str, list, list]:
    """把 `[[IMAGE_n]]` 换成真实图片。返回 (新 html, 用了哪些图, 问题清单)。

    **模型永远不碰图片网址** —— 它只写编号和「我要什么图」,
    找图、下载、落盘、替换全是 Python 做的确定性动作。
    和 CTA 追踪链接同一条规矩:凡是绝不许编的东西,连举例都不许它写。

    找不到图时**把整个 `<img>` 标签摘掉**,不留破图 ——
    页面少一张图只是丑一点,留一张裂图是「买来的流量落在坏页面上」。
    """
    used: list = []
    problems: list = []
    _DEAD_SOURCES.clear()          # 每次生成重新试一遍,别把上一轮的坏运气带过来
    by_no = {}
    for one in (specs or []):
        if isinstance(one, dict):
            try:
                by_no[int(one.get("编号"))] = str(one.get("搜索词") or "").strip()
            except (TypeError, ValueError):
                continue

    # 模型偶尔还是会直接写一个外链地址出来。**代码层摘掉并如实报告**,
    # 别指望提示词能管住(和「提示词管不住文案照抄」是同一条)。
    outside = _EXTERNAL_IMG.findall(html)
    if _EXTERNAL_IMG.search(html):
        html = _EXTERNAL_IMG.sub("", html)
        problems.append("页面里有 %d 处直接写死的外部图片地址,已经摘掉 —— "
                        "那种地址多半是模型编的,而且外链哪天失效就是一张裂图"
                        % len(outside))

    slots = []
    for m in _IMAGE_MARK.finditer(html):
        n = int(m.group(1))
        if n not in slots:
            slots.append(n)
    if len(slots) > MAX_IMAGES_PER_PAGE:
        problems.append("这一版要 %d 张图,只配前 %d 张(多了页面慢、也没必要)"
                        % (len(slots), MAX_IMAGES_PER_PAGE))

    for n in slots[:MAX_IMAGES_PER_PAGE]:
        query = by_no.get(n) or ""
        pick = _pick_image(query) if query else None
        if pick:
            try:
                data, fname, _mime = cs.download(pick["image_url"])
            except Exception as e:
                pick, problems = None, problems + [
                    "第 %d 张图(%s)下载失败:%s" % (n, query, str(e)[:80])]
            else:
                ext = (Path(fname).suffix or ".jpg").lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    ext = ".jpg"
                name = hashlib.sha256(data).hexdigest()[:16] + ext
                (_img_dir(owner) / name).write_bytes(data)
                # `data-slot` 留着,以后只换某一张时靠它定位(见 swap_image)。
                # **`data-want` 也要留着** —— 没有图库钥匙时,「换掉这张图」只剩
                # 「用 AI 重做一次」这一条路,而重做要的正是这句画面描述。
                # 这里不记,配上图的那一刻描述就丢了,以后再也找不回来。
                html = re.sub(_IMG_TAG % re.escape("[[IMAGE_%d]]" % n),
                              lambda mm: mm.group(0)
                              .replace("[[IMAGE_%d]]" % n, "%s/%s" % (IMG_SUBDIR, name))
                              .replace("<img", '<img data-slot="%d" data-want="%s"'
                                       % (n, html_mod.escape(query, quote=True)), 1),
                              html)
                used.append({"编号": n, "搜索词": query, "文件": name,
                             "来自": pick.get("source"), "许可证": pick.get("license"),
                             "原图页": pick.get("source_page"),
                             "尺寸": "%dx%d" % (pick.get("width") or 0, pick.get("height") or 0)})
                continue
        # 没找到 / 下载失败 → **绝不留破图**。但也别把位置整个丢掉:
        # 换成一个 `hidden` 的空 <img>,把「这儿要什么图」记在标签上。
        #   · 页面上什么都不显示(不是灰方块、不是裂图),现在发布出去也干净;
        #   · `_copy_images` 只认 `src="img/…"`,没有 src 的它不会去复制;
        #   · 以后用户说「用 AI 把图补上」时,`pending_image_slots()` 靠它找回位置和需求。
        # **为什么不当场用 AI 生图**:生图 $0.20/张,而生成落地页是免费步骤 ——
        # 一页 3 张就是 $0.6 没经用户同意。花钱的事必须走保险箱(第六之十五节)。
        if query:
            alt = ""
            m2 = re.search(_IMG_TAG % re.escape("[[IMAGE_%d]]" % n), html)
            if m2:
                a2 = re.search(r'\balt\s*=\s*"([^"]*)"', m2.group(0))
                alt = a2.group(1) if a2 else ""
            holder = ('<img data-slot="%d" data-want="%s" alt="%s" hidden>'
                      % (n, html_mod.escape(query, quote=True), html_mod.escape(alt, quote=True)))
            html = re.sub(_IMG_TAG % re.escape("[[IMAGE_%d]]" % n), lambda _m: holder, html)
            if not any(p.startswith("第 %d 张图" % n) for p in problems):
                problems.append("第 %d 张图没配上(搜的是「%s」),位置**留着但不显示** —— "
                                "用户说一声就能用 AI 把它生出来" % (n, query))
        else:
            html = re.sub(_IMG_TAG % re.escape("[[IMAGE_%d]]" % n), "", html)
            problems.append("第 %d 张图没说要找什么,这个位置去掉了" % n)

    # **「没找到合适的图」和「图库根本连不上」要分开报。**
    # 混着报的话用户会去换关键词 —— 而真正该做的是配一把钥匙,换多少词都没用。
    # 和坑表「5xx 里可能写着确定性的原因,别一律说成稍后再试」是同一条。
    if slots and not used:
        ready = [x["id"] for x in cs.available_sources() if x.get("ready")]
        if not ready:
            problems.insert(0, "⚠️ **一个图库都没启用,所以这页一张图都配不上。**"
                               "**这不是关键词的问题,换多少个词都一样。** "
                               "位置都留着(不显示,现在发布出去也干净)——"
                               "告诉用户:说一句「**用 AI 把图补上**」就能把它们生出来,"
                               "会先报价、他点头才花钱。")
        elif all(x in _DEAD_SOURCES for x in ready):
            problems.insert(0, "⚠️ **图库这会儿没反应**(%s 请求超时),所以这页一张图都配不上。"
                               "**不是关键词的问题。** 实测 Openverse 的响应时间在 0.5~32 秒之间乱跳,"
                               "而且家装类(roof / gutter 这些)本来就没多少可商用的图。"
                               "位置都留着(不显示)——告诉用户:说一句「**用 AI 把图补上**」"
                               "就能把它们生出来,会先报价、他点头才花钱。"
                               % "、".join(sorted(_DEAD_SOURCES)))

    # 剩下的占位符(没包在 <img> 里的)一律清掉,不能让 [[IMAGE_n]] 露在页面上
    html = _IMAGE_MARK.sub("", html)
    return html, used, problems


_HOLDER = r'<img\b[^>]*\bdata-slot="%d"[^>]*>'

# src 可能是双引号、单引号、不带引号,也可能**压根没有**(隐藏占位就是没有)
_SRC_ATTR = re.compile(r'''\s+src\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)''', re.I)


def _src_landed(tag_html: str, url: str) -> bool:
    """这个标签最后是不是真的挂着 `url`、而且不再是隐藏的。

    **别拿「页面变没变」当判据** —— 换到一张**内容一模一样**的图时,
    文件名(内容 sha256)也一样,页面本来就不该变,那不是失败。
    """
    return (('src="%s"' % url) in tag_html
            and not re.search(r"\bhidden\b", tag_html, re.I))


def _set_src(tag_html: str, url: str) -> str:
    """把一个 `<img>` 标签的 src 换成 `url`,顺带把 `hidden` 摘掉。

    **不能假设 src 是双引号的。** 那个标签是**模型**写的,单引号、不带引号都可能;
    隐藏占位更是连 src 都没有。原来 `fill_image_slot` 和 `swap_image` 各写了一个
    只认双引号的正则,后果两条都很阴:
      · 换图对单引号的图和隐藏占位**一个字都没改**,却照样返回「换好了」——
        用户刷新预览看不出变化,只会以为是浏览器缓存;
      · AI 重做时旧的单引号 src 剥不掉,变成**两个 src**,而浏览器用第一个 ——
        钱花了,页面上还是那张旧图。
    自闭合写法(`<img … />`)也要照顾到,直接砍最后一个字符会留下一个孤零零的 `/`。
    """
    one = re.sub(r"\s+hidden\b", "", tag_html, flags=re.I)      # 露出来
    attr = ' src="%s"' % url
    if _SRC_ATTR.search(one):
        # **原地换掉,别挪位置。** 换图要是纯粹的「src 值替换」,别的一个字都不动 ——
        # 把 src 重排到末尾虽然照样能显示,但整个标签的 diff 就脏了,
        # 「只动那一张、别的不改」这条不变量也就没法验了。
        return _SRC_ATTR.sub(lambda _m: attr, one, count=1)
    one = one.rstrip()                                          # 隐藏占位:本来就没有 src
    close = "/>" if one.endswith("/>") else ">"
    return one[:-len(close)].rstrip() + attr + close


def pending_image_slots(filename: str, owner: str = "",
                        include_filled: bool = False) -> list[dict]:
    """这一版落地页里还有哪几个位置没配上图(隐藏占位)。

    返回 `[{"slot": 1, "want": "metal roof installation crew", "filled": False}, ...]`,
    `want` 就是当初模型写的英文搜索词 —— **AI 生图直接拿它当画面描述**,
    不让模型在确认那一刻另写一句(那就成了"报价的是 A、做出来的是 B")。

    **`include_filled=True` 时,已经有图的位置也一起列出来**(`filled: True`)。
    没有图库钥匙时 `swap_image` 永远成功不了,「换掉这张图」只剩「用 AI 重做」
    这一条路 —— 而重做的前提是先找得到那个位置和它该画什么。
    """
    path = resolve_preview(filename, owner)
    if path is None:
        return []
    out = []
    pat = (r'<img\b[^>]*\bdata-slot="\d+"[^>]*>' if include_filled
           else r'<img\b[^>]*\bhidden\b[^>]*>')
    for m in re.finditer(pat, path.read_text(encoding="utf-8")):
        tag = m.group(0)
        slot = re.search(r'data-slot="(\d+)"', tag)
        if not slot:
            continue
        # **没有 `data-want` 也要把这个位置列出来。** 加这个属性之前配上的图只有
        # `data-slot`,当时那句画面描述没记下来。整条跳过的话,用户点名重做第 1 张
        # 会被告知「这一版没有第 1 张图」—— 那是**假话**,图明明在页面上。
        # 回落到 `alt`(模型写的,是页面自己的东西,不是我们编的);都没有就留空,
        # 由上层明说「不知道该画什么」并给出路。
        want = re.search(r'data-want="([^"]*)"', tag)
        alt = re.search(r'\balt\s*=\s*"([^"]*)"', tag)
        out.append({"slot": int(slot.group(1)),
                    "want": html_mod.unescape((want or alt).group(1)) if (want or alt) else "",
                    "filled": not re.search(r"\bhidden\b", tag, re.I)})
    return sorted(out, key=lambda x: x["slot"])


def fill_image_slot(filename: str, slot: int, data: bytes, ext: str = ".jpg",
                    owner: str = "") -> str:
    """把生成好的图片**先落盘**,再填进那个隐藏占位。返回文件名。

    顺序要紧:图是花过钱的,**落盘要排在改页面之前** —— 改页面失败还能重来,
    图丢了钱就白花了(和生图那条「付过钱的产物先落盘」同一条)。
    """
    path = resolve_preview(filename, owner)
    if path is None:
        raise RuntimeError("找不到这个落地页文件:%s" % (Path(str(filename or "")).name or "(空)"))
    if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    name = hashlib.sha256(data).hexdigest()[:16] + ext
    (_img_dir(owner) / name).write_bytes(data)          # ← 先落盘

    tag = re.compile(_HOLDER % int(slot), re.I)
    html = path.read_text(encoding="utf-8")
    if not tag.search(html):
        raise RuntimeError("这一版里没有第 %s 张图的位置了" % slot)

    rel = "%s/%s" % (IMG_SUBDIR, name)
    out = tag.sub(lambda m: _set_src(m.group(0), rel), html, count=1)
    landed = tag.search(out)
    if landed is None or not _src_landed(landed.group(0), rel):
        # 钱已经花过了,图也落盘了 —— 报错也要把文件名交出去,别让它白花
        raise RuntimeError("第 %s 张的标签改不动(写法不认识),图已经存下来了:%s" % (slot, name))
    path.write_text(out, encoding="utf-8")
    return name


def swap_image(filename: str, slot: int, query: str, owner: str = "") -> dict:
    """把已经生成好的页面里第 `slot` 张图换成按 `query` 重新找的一张。

    **为什么值得单独做一个**:不满意一张图就整页重生成的话,
    要等模型再出一整页(实测 61 秒)、还要再花一次钱,而且**其它内容也会跟着变**,
    用户刚看顺眼的文案就没了。
    """
    path = resolve_preview(filename, owner)
    if path is None:
        return {"error": "找不到这个落地页文件:%s" % (Path(str(filename or "")).name or "(空)")}
    html = path.read_text(encoding="utf-8")
    tag = re.compile(r'<img\b[^>]*\bdata-slot="%d"[^>]*>' % int(slot), re.I)
    if not tag.search(html):
        have = re.findall(r'data-slot="(\d+)"', html)
        return {"error": "这一版里没有第 %s 张图。现有的是:%s"
                         % (slot, "、".join(have) or "(一张都没有)")}
    # **先把「这一轮哪几家挂了」清掉。** 那份记录是给「一次生成里连配 4 张图」用的
    # (避免一张一张白等 20 秒),而换图是用户新发起的单张请求,网络可能早恢复了。
    # 不清的话会拿着上一次的坏运气直接返回「没找到合适的图」——
    # 而真实原因是连不上,用户会去换关键词,换多少个都没用(和第六之二十三节那条同一个坑)。
    _DEAD_SOURCES.clear()
    pick = _pick_image(query)
    if not pick:
        ready = [x["id"] for x in cs.available_sources() if x.get("ready")]
        if not ready:
            return {"error": "**一个图库都没启用**,所以从图库换不了图。页面一个字都没动。"
                             "**这不是关键词的问题,换多少个词都一样。**",
                    "改用这条路": "用 AI 重做这一张:propose_landing_images(landing_file, slots=\"%d\")。"
                                  "会先报价、用户点头才花钱。" % int(slot)}
        if all(x in _DEAD_SOURCES for x in ready):
            return {"error": "**图库这会儿没反应**(%s 请求超时),不是关键词的问题。"
                             "页面一个字都没动。" % "、".join(sorted(_DEAD_SOURCES)),
                    "改用这条路": "用 AI 重做这一张:propose_landing_images(landing_file, slots=\"%d\")。"
                                  "会先报价、用户点头才花钱。" % int(slot)}
        return {"error": "按「%s」没找到可商用的图,页面一个字都没动。换个关键词再试" % query}
    try:
        data, fname, _mime = cs.download(pick["image_url"])
    except Exception as e:
        return {"error": "图找到了但下载失败:%s。页面一个字都没动" % str(e)[:100]}
    ext = (Path(fname).suffix or ".jpg").lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".jpg"
    name = hashlib.sha256(data).hexdigest()[:16] + ext
    (_img_dir(owner) / name).write_bytes(data)
    rel = "%s/%s" % (IMG_SUBDIR, name)
    same = ('src="%s"' % rel) in (tag.search(html).group(0) if tag.search(html) else "")
    out = tag.sub(lambda m: _set_src(m.group(0), rel), html, count=1)
    # **改不动就别说「换好了」。** 原来这里只认双引号的 src,碰上单引号的图或者
    # 隐藏占位就一个字都没换,却照样返回成功 —— 用户刷新看不出变化,以为是缓存。
    landed = tag.search(out)
    if landed is None or not _src_landed(landed.group(0), rel):
        return {"error": "第 %s 张的标签是这样写的,代码改不动它:%s"
                         % (slot, (tag.search(html).group(0) if tag.search(html) else "")[:120]),
                "note": "**页面一个字都没动,别说成换好了。** 请把这一版重新生成一遍。"}
    html = out
    path.write_text(html, encoding="utf-8")
    return {"换好了": True, "文件": filename, "第几张": slot,
            "新图": {"搜索词": query, "文件": name, "来自": pick.get("source"),
                    "许可证": pick.get("license"), "原图页": pick.get("source_page")},
            **({"⚠️": "找回来的是**同一张图**(内容一模一样),所以画面不会有变化。"
                       "想换别的就换个关键词再来一次。"} if same else {}),
            "note": "**页面其它内容一个字都没动。** 让用户刷新预览看看。"}


def save_pages(pages: list[dict], limit: int = 2, owner: str = "") -> list[dict]:
    """把生成的页面落盘。`limit` 是**这次要几版** —— 别再写死 2。

    `owner` 是当前用户 id:页面写进**他自己的**目录(见 `_owner_dir`)。
    """
    limit = max(1, min(int(limit or 2), 2))
    out = []
    for i, p in enumerate(pages[:limit], start=1):
        raw = str(p.get("html") or "").strip()
        if not raw:
            continue
        if "<html" not in raw.lower():
            raw = "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>" + raw + "</body></html>"
        raw, problems = _check_publishable(raw)
        raw, year_fixed, year_stale = fix_stale_years(raw)
        # **配图排在可发布性检查之后**:那两步是免费的,先做完再去联网找图下载
        # (和「花钱/可能失败的步骤排最后」同理)。
        raw, imgs, img_problems = attach_images(raw, p.get("配图") or [], owner)
        name = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(p.get("命名") or f"landing-{i}")).strip("-").lower()
        filename = f"{uuid.uuid4().hex[:8]}-{name or 'landing'}.html"
        path = _owner_dir(owner) / filename
        path.write_text(raw, encoding="utf-8")
        out.append({k: v for k, v in p.items() if k != "html"} | {
            "file": filename,
            "preview_url": f"/landing-pages/{filename}",
            "可发布": not problems,
            "CTA占位符数量": raw.count(CTA_MARK),
            **({"配图": imgs} if imgs else {}),
            **({"⚠️配图问题": img_problems} if img_problems else {}),
            **({"⚠️问题": problems} if problems else {}),
            # 版权年是代码改的,如实告诉用户一声(别偷偷改了不说)
            **({"已自动更新版权年": year_fixed} if year_fixed else {}),
            # 正文里的过期年份**不许代码擅自改** —— 那是广告文案,得用户定
            **({"⚠️文案里有过期年份": year_stale,
                "⚠️怎么办": "这几处是**广告文案**,代码不替你改。"
                          "%d 年的广告写着旧年份,用户一眼看出是旧的。"
                          "请问用户:改成今年、还是去掉年份?" % this_year()}
               if year_stale else {}),
        })
    _prune(owner)
    return out


def preview_exists(filename: str, owner: str = "") -> bool:
    """这个预览文件真的在盘上吗?

    **模型会照着 `<8位十六进制>-version-a---<英文名>.html` 这个形状编一个出来**
    (线上实测:整轮压根没调生成工具,直接给了个链接,用户点开是 404)。
    所以"给出去的链接"必须能被代码回查,不能只靠提示词让它别编。
    """
    return resolve_preview(filename, owner) is not None


def recent_previews(limit: int = 8, owner: str = "") -> list[dict]:
    """盘上真实存在的预览,新的排前面。给"编了个链接"时的兜底清单用。

    **只列他自己的 + 没有归属的老文件**,绝不列别人的 —— 这个清单会被直接
    渲染成可点链接甩到用户面前(404 页面和 ⚠️ 拆穿章)。
    """
    try:
        pool = list(_owner_dir(owner).glob("*.html")) if owner else []
        pool += list(GENERATED_DIR.glob("*.html"))          # 没有归属的老文件
        files = sorted({f.resolve(): f for f in pool}.values(),
                       key=lambda f: f.stat().st_mtime, reverse=True)
    except Exception:
        return []
    return [{"file": f.name, "preview_url": "/landing-pages/" + f.name,
             "mtime": f.stat().st_mtime} for f in files[:max(1, int(limit or 8))]]


def _prune(owner: str = "") -> None:
    """只淘汰**这个人自己**目录里的旧页面。

    原来是全局扫一遍砍到 80 份 —— A 多生成几轮就把 B 的页面删了,
    而 B 的发布待办还指着那个文件,他确认时才拿到「文件找不到」。
    """
    try:
        base = _owner_dir(owner)
        files = sorted(base.glob("*.html"), key=lambda f: f.stat().st_mtime)
        for f in files[:-KEEP_GENERATED]:
            f.unlink(missing_ok=True)
    except Exception:
        pass
