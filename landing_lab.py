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
import ipaddress
import json
import re
import socket
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx

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
6. HTML 要完整可预览,但不要引用外部 JS/CSS/图片;可以用 CSS 做干净版式。
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
        name = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(p.get("命名") or f"landing-{i}")).strip("-").lower()
        filename = f"{uuid.uuid4().hex[:8]}-{name or 'landing'}.html"
        path = _owner_dir(owner) / filename
        path.write_text(raw, encoding="utf-8")
        out.append({k: v for k, v in p.items() if k != "html"} | {
            "file": filename,
            "preview_url": f"/landing-pages/{filename}",
            "可发布": not problems,
            "CTA占位符数量": raw.count(CTA_MARK),
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
