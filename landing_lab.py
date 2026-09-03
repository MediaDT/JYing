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
KEEP_GENERATED = 80


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


def summarize(models: list[dict], brand: str = "", offer: str = "",
              audience: str = "", lang: str = "zh") -> dict:
    prompt = f"""下面是若干个竞品/同行落地页拆解结果。请汇总共性,再生成 2 个全新的落地页。

硬规则:
1. 新页面必须是新的表达,不能照抄任何竞品文案、品牌、电话号码、价格、保证或样式细节。
2. 重点分析为什么表现好:排版、文字描述、offer、表单、信任背书、CTA 节奏。
3. 生成的两个页面要方向不同,方便 A/B test。
4. HTML 要完整可预览,但不要引用外部 JS/CSS/图片;可以用 CSS 做干净版式。
5. 每一个真正跳往 Offer 的 CTA 必须写成 href="[[CLICKFLARE_CTA_URL]]"；不要自己编跳转地址。
6. 在 </body> 前原样放一个 <!--[[CLICKFLARE_LANDER_SCRIPT]]--> 注释占位符，发布时由后端安全替换。
7. 只输出 JSON。

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


def save_pages(pages: list[dict]) -> list[dict]:
    out = []
    for i, p in enumerate(pages[:2], start=1):
        raw = str(p.get("html") or "").strip()
        if not raw:
            continue
        if "<html" not in raw.lower():
            raw = "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>" + raw + "</body></html>"
        name = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(p.get("命名") or f"landing-{i}")).strip("-").lower()
        filename = f"{uuid.uuid4().hex[:8]}-{name or 'landing'}.html"
        path = GENERATED_DIR / filename
        path.write_text(raw, encoding="utf-8")
        out.append({k: v for k, v in p.items() if k != "html"} | {
            "file": filename,
            "preview_url": f"/landing-pages/{filename}",
        })
    _prune()
    return out


def _prune() -> None:
    try:
        files = sorted(GENERATED_DIR.glob("*.html"), key=lambda f: f.stat().st_mtime)
        for f in files[:-KEEP_GENERATED]:
            f.unlink(missing_ok=True)
    except Exception:
        pass
