"""
竞品广告查询(Insightrackr)—— 看**别人正在投什么广告**。

和另外两个素材来源的分工,别搞混:
  · recommend_creatives   → 你自己账户投过的(有真实数据,版权最干净)
  · search_stock_creatives→ 授权图库的图(可商用,但只是"好看",没有市场验证)
  · **这里**              → 竞品正在投的真实广告(**有市场验证**:投了半年还在投的
                            广告肯定跑得动),但素材是别人的

**这个平台不是标准 API,是拿登录态在请求**(实现参考 qx-ad-bot,只读没改它):
认证头是 `Authorization` + `Cookie` 两个,**都是从浏览器里拷出来的、会过期**。
过期时平台返回 `code == -3106`,这里会翻译成人话让用户去重新贴 —— 这一步很关键,
不然用户只会看到"没找到素材",根本不知道是凭据过期了。

平台参数(实测自 qx-ad-bot 的生产调用):
  sortField: **只有 "4" 能用**(按预估曝光),别的要么返回空要么排序无意义 ——
    详见 SORT_FIELD_IMPRESSIONS 上面的实测记录;sortRule: desc/asc
  startTime/endTime: "YYYY-MM-DD"
"""

import contextvars
import os
from datetime import date, datetime, timedelta

import httpx

# 当前请求这个人自己的凭据(在 AuthMiddleware 里设,必须设在 call_next 之前)。
#
# **和 newsbreak_client.CURRENT_CREDS 的取值规则故意不一样,别当成 bug 改掉**:
#   · NewsBreak 是"在用户上下文但没绑 → 直接报错,绝不回落 .env" ——
#     那关系到各人自己的广告账户和钱,回落就等于没隔离;
#   · Insightrackr 是**公司一份订阅的只读查询**,没有"谁的数据"之分,
#     所以这里是"自己贴过就用自己的,没贴过就回落 .env 那份公用的"。
#     各人贴各自的还有个额外好处:万一平台是单会话互踢,就不会互相顶掉。
CURRENT_CREDS: contextvars.ContextVar = contextvars.ContextVar(
    "insightrackr_creds", default=None)

TIMEOUT = 30.0
DEFAULT_URL = "https://data.insightrackr.com/cas/api/v2/imagevideo/search"

# 排序代码。**qx-ad-bot 前端标的是错的,别照抄**:它写 3=曝光/1=首投/2=最近投,
# 实测(2026-08-18,拿真凭据打的):
#   · 1 和 2 → **一条都返回不了**;
#   · 3 → 排出来全是曝光 1 的边角料,不是曝光排序;
#   · 5/6/7 → 返回结果完全相同,像是回落到了某个默认排序;
#   · **4 → 真的按曝光排**(desc 给 106万/77万/74万…,asc 给全 1),而且排在前面的
#     都是 findCnt 300 左右、投了近两年的广告 —— 正是"跑得动"的那批。
# 所以只留 4 这一个。想看新广告用 days 收窄时间窗,别指望这里的排序。
SORT_FIELD_IMPRESSIONS = "4"

# 关键词去匹配**哪些字段**。qx-ad-bot 用的是全字段 "0,1,2,3,4,6,8,9",
# 但实测那样会被正文匹配(类型3)的噪音淹掉 —— 搜 "roof repair" 返回的
# 全是小说 App GoodNovel(它正文里碰巧有 roof)。
# 实测各值:0=广告主/产品 ✅、2=广告标题 ✅ 最准、3=正文 ❌噪音、9=其它 ❌、
# 1/4/5/6/7/8 一条都搜不到。所以只留 0 和 2。
KEYWORD_FIELDS = "0,2"

# 凭据没配 / 过期时给用户的话。**必须说清楚去哪儿拿**,
# 因为这两个值不是 API key,是从浏览器开发者工具里拷的,不会有人自己猜到。
HOWTO = (
    "拿凭据的办法:①用 Chrome 登录 insightrackr.com;②按 F12 打开开发者工具 → Network;"
    "③在网站上随便搜一次素材;④在 Network 里点那条 search 请求 → Headers → "
    "把 `Authorization` 和 `Cookie` 两行的值整段复制出来;"
    "⑤分别填进 .env 的 INSIGHTRACKR_AUTHORIZATION 和 INSIGHTRACKR_COOKIE。"
    "**两行都要换,尤其是 Authorization —— 真正的登录票据是它**"
    "(实测只带 Cookie 会报「传入token为空」)。"
)


class InsightrackrError(Exception):
    pass


# .env 里的键名 → 用户凭据里的字段名
_FIELD = {"INSIGHTRACKR_AUTHORIZATION": "authorization", "INSIGHTRACKR_COOKIE": "cookie"}


def _conf(name: str, default: str = "") -> str:
    """取一项配置:**先看这个人自己贴的**,没有再回落 .env。"""
    creds = CURRENT_CREDS.get()
    if isinstance(creds, dict):
        v = str(creds.get(_FIELD.get(name, ""), "") or "").strip()
        if v:
            return v
    try:
        import agent_server
        v = agent_server._read_env_value(name)
        if v:
            return v
    except Exception:
        pass
    return os.getenv(name, "").strip() or default


def is_configured() -> bool:
    return bool(_conf("INSIGHTRACKR_AUTHORIZATION") and _conf("INSIGHTRACKR_COOKIE"))


def validate(authorization: str = "", cookie: str = "") -> tuple[bool, str]:
    """拿一对凭据打一次**最小**请求,确认它真的能用。

    两个用处:①存之前先验,验不过就不覆盖旧的(和 NewsBreak 存 token 一个规矩,
    免得填错一次把好凭据冲掉);②跑批量任务之前先验,别跑到一半才发现票据过期 ——
    前面花的钱和时间就白搭了。

    不传参数就验当前生效的那份。返回 (能不能用, 说明)。
    """
    auth = (authorization or "").strip() or _conf("INSIGHTRACKR_AUTHORIZATION")
    ck = (cookie or "").strip() or _conf("INSIGHTRACKR_COOKIE")
    if not auth:
        return False, "缺 Authorization —— 真正的登录票据是它。" + HOWTO
    # HTTP 头只认 latin-1。凭据里混进中文/全角字符(复制时带上了页面文字、
    # 或者输入法没切回来)的话,httpx 会抛 'ascii' codec can't encode —— 那是天书,
    # 用户根本不知道该改什么。这里提前挡下并说人话。
    # (项目里同类的坑:WWW-Authenticate 放中文会 500,见 CLAUDE.md 第八节)
    for label, val in (("Authorization", auth), ("Cookie", ck)):
        try:
            val.encode("latin-1")
        except UnicodeEncodeError:
            bad = next((c for c in val if ord(c) > 255), "?")
            return False, (f"{label} 里混进了非法字符「{bad}」—— 凭据只能是英文数字符号。"
                           "多半是复制时带上了网页文字,或者输入法没切回英文,重新复制一次。")
    end = date.today()
    body = _payload("test", (end - timedelta(days=7)).isoformat(), end.isoformat(),
                    5, SORT_FIELD_IMPRESSIONS, "desc")
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False) as c:
            resp = c.post(_conf("INSIGHTRACKR_SEARCH_URL", DEFAULT_URL),
                          json=body, headers=_headers(auth, ck))
    except Exception as e:
        return False, f"连不上平台:{str(e)[:120]}"
    if resp.status_code >= 500:
        # 504 是平台自己后端慢,不是凭据问题 —— 这两种绝不能混report,
        # 否则用户会跑去反复换凭据,而问题根本不在他那儿
        return False, f"平台暂时不可用(HTTP {resp.status_code}),这不是凭据问题,过几分钟再试"
    try:
        code = resp.json().get("code")
    except Exception:
        return False, f"平台返回的不是 JSON(HTTP {resp.status_code}),稍后再试"
    if code in (None, 0, 200):
        return True, "凭据有效"
    if code == -3106:
        return False, "登录已过期。**要换的是 `Authorization` 那一行**,光换 Cookie 没用"
    if code == -3108:
        return False, "没收到登录票据 —— Authorization 是空的或填错了"
    return False, f"平台拒绝了这对凭据(错误码 {code})"


def _headers(auth: str = "", cookie: str = "") -> dict:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        # 平台按浏览器请求来对待,UA 照着 qx-ad-bot 生产环境的写
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Authorization": auth or _conf("INSIGHTRACKR_AUTHORIZATION"),
        "Cookie": cookie or _conf("INSIGHTRACKR_COOKIE"),
    }


def _payload(keyword: str, start: str, end: str, page_size: int,
             sort_field: str, sort_rule: str) -> dict:
    """平台要的请求体。字段极多且大部分必须存在(哪怕是空数组),
    照抄 qx-ad-bot 生产环境在用的那一份,别自己精简 —— 少一个字段就可能报错。"""
    return {
        "keyWord": keyword, "keyWordType": KEYWORD_FIELDS, "keyWordList": [],
        "keyWordListType": True, "isNew": False, "creativeList": [],
        "appealTypeList": [], "interactionList": [], "languages": [], "productIds": [],
        "productOption": {"productType": [], "selling": [], "monetization": [],
                          "payType": [], "companyLocation": [], "campaignList": []},
        "baseOption": {
            "permission": False, "putOverseaInland": None,
            "tradeLevel1": [], "tradeLevel2": [], "tradeLevel3": [], "subjectType": [],
            "countryLevel2": [], "adfactionIds": [], "mediaIds": [], "device": [],
            "topicType": [], "productModel": [], "dayMode": "DD",
            "startTime": start, "endTime": end,
            "compareEndDate": "", "compareStartDate": "",
            "pageIndex": 1, "pageSize": page_size,
            "sortField": sort_field, "sortRule": sort_rule,
            "gptSearch": False, "materialTopLimit": "", "szfxList": [],
        },
        "materialType": "", "classIds": [], "seelTargets": [], "webTools": [],
        "demoadFormats": [], "adMediaType": [], "materialTag": [], "creativeTeam": [],
        "szfxList": [], "materialRemovalRepeat": True,
    }


def _first(item: dict, keys: tuple) -> object:
    """平台同一个意思会用好几种字段名(驼峰/下划线/单复数),挨个试。"""
    for k in keys:
        v = item.get(k)
        if isinstance(v, list):
            v = next((x for x in v if x not in (None, "")), None)
        if v not in (None, ""):
            return v
    return None


def _media_url(item: dict) -> str:
    v = _first(item, ("videoUrl", "video_url", "imageUrl", "image_url", "materialUrl",
                      "material_url", "fileUrl", "file_url", "previewUrl", "preview_url", "url"))
    if v:
        return str(v)
    for k in ("material", "creative", "image", "video", "cover"):
        nested = item.get(k)
        if isinstance(nested, dict):
            got = _media_url(nested)
            if got:
                return got
    return ""


def _when(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 10_000_000_000 else v)
    try:
        return datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(str(v).strip(), fmt)
            except ValueError:
                continue
    return None


def _clean(v) -> str:
    """平台会把命中的关键词用 <font color='red'> 包起来做高亮,
    直接显示给用户就是一串 HTML。这里剥掉标签。"""
    import re as _re
    return _re.sub(r"<[^>]+>", "", str(v or "")).strip()


def _num(v) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _rows(payload: dict) -> list[dict]:
    """结果藏在哪一层不固定,挨层找。"""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for k in ("records", "items", "list", "rows", "data"):
        v = data.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            got = _rows({"data": v})
            if got:
                return got
    return []


def _app_name(item: dict) -> str:
    """高曝光那批记录的广告主名字藏在 appList[0].name 里,不在 brandName。"""
    apps = item.get("appList") or item.get("sourceAppList") or []
    if isinstance(apps, list) and apps and isinstance(apps[0], dict):
        return _clean(apps[0].get("name"))
    return ""


def _landing(item: dict) -> str:
    """落地页同理:常规字段没有时,去 nonLocalDemoad 这个数组里拿第一个。"""
    v = _first(item, ("landingPage", "landing_page", "webSite", "demoadWebSite"))
    if v and str(v).startswith("http"):
        return _clean(v)
    for k in ("nonLocalDemoad", "originalUrl"):
        u = item.get(k)
        if isinstance(u, list):
            u = next((x for x in u if x), None)
        if u and str(u).startswith("http"):
            return str(u)
    return ""


def _domain(url: str) -> str:
    if not url:
        return ""
    return url.split("//")[-1].split("/")[0].split("?")[0][:40]


def _normalize(item: dict) -> dict:
    import creative_search as cs
    first, last = _when(_first(item, ("firstSeenTime", "first_seen_at", "firstTime",
                                      "globalFirstTime", "startTime", "startDate", "firstPutTime"))), \
                  _when(_first(item, ("lastSeenTime", "last_seen_at", "lastTime",
                                      "globalLastTime", "endTime", "endDate", "lastPutTime")))
    # **投放了多少天是这个功能最有价值的一个数**:一条广告能连续投几个月,
    # 说明它真的跑得动 —— 这是图库素材给不了的市场验证。
    run_days = (last - first).days if (first and last) else None
    url = _media_url(item)
    # materialType 是**数字**:1=图片,2=视频。原来按 "video" 字样判断,
    # 结果所有视频都被当成图片,建广告时 creative type 传错平台会拒收。
    kind = str(_first(item, ("materialType", "mediaType", "type")) or "").strip()
    is_video = kind == "2" or "video" in kind.lower() or url.lower().endswith(".mp4")
    w, h = _num(_first(item, ("width",))) or 0, _num(_first(item, ("height",))) or 0
    return {
        "image_url": url,
        # 视频的封面在 converUrl / thumbnailConverUrl(平台把 cover 拼成了 conver)。
        # 漏了这两个的话,视频那条的缩略图会退化成 mp4 地址,聊天里就是一张裂图。
        "thumbnail": str(_first(item, ("thumbnailImageUrl", "thumbnailConverUrl", "converUrl",
                                       "thumbnailUrl", "thumbnail_url", "coverUrl", "cover_url",
                                       "coverImage", "cover_image", "imageUrl", "image_url")) or url),
        "title": (_clean(_first(item, ("title", "name", "materialName", "creativeName",
                                       "productName")))
                  or _app_name(item) or _domain(_landing(item)) or "(无标题)")[:80],
        "author": (_clean(_first(item, ("brandName", "appName", "advertiser", "opCompanyName")))
                   or _app_name(item) or "(未知广告主)")[:60],
        "文案": _clean(_first(item, ("describe", "description", "copywriting")))[:200] or None,
        "视频时长": _num(_first(item, ("videoTimeSpan",))) or None,
        "media_type": "VIDEO" if is_video else "IMAGE",
        "width": w, "height": h,
        "first_seen": first.strftime("%Y-%m-%d") if first else None,
        "last_seen": last.strftime("%Y-%m-%d") if last else None,
        "投放天数": run_days,
        "预估曝光": _num(_first(item, ("impressions", "impression", "estimatedImpressions",
                                       "estimateExposure", "estimatedExposure", "exposure",
                                       "showCount", "viewCount"))),
        "source": "Insightrackr(竞品广告)",
        "source_page": _landing(item),
        # **域名要单独列出来给用户看**:这个平台是全球的,搜 roof repair 排第一的
        # 可能是马来西亚的广告(hsroofrepair.com.my)。NewsBreak 是美国平台,
        # 别人在别的市场跑得好不代表在美国跑得好 —— 让用户自己一眼看出来。
        "落地页域名": _domain(_landing(item)),
        "license": "⚠️ 这是其它广告主正在投的广告素材,不是授权图库的图",
        "quality": cs._quality(w, h, "VIDEO" if is_video else "IMAGE"),
    }


def search(keyword: str, days: int = 365, count: int = 8, country: str = "") -> dict:
    """按关键词查竞品正在投的广告,按预估曝光从高到低排。

    days = 往前看多少天。**想看新广告就把 days 收窄**(平台的"按时间排序"是坏的,
    见 SORT_FIELD_IMPRESSIONS 上面那段实测记录)。
    country = 两位大写国家码(如 "US"),留空则不筛。

    **默认不筛国家**:实测筛了 US 之后,这几个家装品类只剩个位数结果、曝光掉到 1
    —— 平台在美国这类目的覆盖本来就薄。不筛能看到更多真正跑量的广告,
    再靠返回里的「落地页域名」自己判断是不是美国的,比硬筛完没东西看强。
    """
    if not is_configured():
        raise InsightrackrError("还没配置 Insightrackr 凭据。" + HOWTO)

    end = date.today()
    start = end - timedelta(days=max(1, min(int(days or 180), 365)))
    body = _payload((keyword or "").strip(), start.isoformat(), end.isoformat(),
                    max(1, min(int(count or 8), 40)),
                    SORT_FIELD_IMPRESSIONS, "desc")
    if country.strip():
        # 实测格式:两位大写字母(["US"] 有效;"USA"/"840"/"us" 一条都返回不了)
        body["baseOption"]["countryLevel2"] = [country.strip().upper()]
    with httpx.Client(timeout=TIMEOUT, trust_env=False) as c:
        resp = c.post(_conf("INSIGHTRACKR_SEARCH_URL", DEFAULT_URL),
                      json=body, headers=_headers())
        if resp.status_code >= 500:
            # 平台后端慢(实测遇到过 504),和凭据无关,要说清楚免得用户瞎换凭据
            raise InsightrackrError(
                f"Insightrackr 平台暂时不可用(HTTP {resp.status_code}),这不是凭据问题,过几分钟再试")
        resp.raise_for_status()
    data = resp.json()

    code = data.get("code")
    if code not in (None, 0, 200):
        # -3106 = 登录态失效。**这条必须单独翻译**:凭据是从浏览器拷的、会过期,
        # 不说清楚的话用户只会以为"搜不到素材",查半天查不到原因。
        # -3106 登录态失效,-3108 压根没收到票据。**两个要分开报**:
        # 实测过一次"用户以为换了凭据、其实只换了 Cookie"—— 因为鉴权只认
        # Authorization 头,Cookie 几乎不参与(只带 Cookie 会得到 -3108)。
        # 笼统说一句"过期了"的话,用户会反复换错那一半。
        if code == -3106:
            raise InsightrackrError(
                "Insightrackr 登录已过期。**真正的登录票据是 `Authorization` 那一行,"
                "光换 Cookie 没用**(实测鉴权只认 Authorization)。" + HOWTO)
        if code == -3108:
            raise InsightrackrError(
                "Insightrackr 没收到登录票据 —— `INSIGHTRACKR_AUTHORIZATION` 是空的或填错了。" + HOWTO)
        raise InsightrackrError(str(data.get("msg") or data.get("message") or f"平台返回错误码 {code}"))

    # 同一条素材会被不同广告/不同投放重复返回(平台的 materialRemovalRepeat
    # 并不能完全去掉),按媒体地址去重,别让用户看到 4 条一模一样的
    items, seen = [], set()
    for raw in _rows(data):
        one = _normalize(raw)
        if not one["image_url"] or one["image_url"] in seen:
            continue
        seen.add(one["image_url"])
        items.append(one)
    return {"results": items[:count], "query": keyword,
            "period": f"{start} ~ {end}", "sort": "预估曝光从高到低"}
