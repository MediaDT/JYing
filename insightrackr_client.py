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
  sortField: "3"=预估曝光 / "1"=首次投放 / "2"=最近投放;sortRule: desc/asc
  startTime/endTime: "YYYY-MM-DD"
"""

import os
from datetime import date, datetime, timedelta

import httpx

TIMEOUT = 30.0
DEFAULT_URL = "https://data.insightrackr.com/cas/api/v2/imagevideo/search"

# 排序方式:说人话的名字 → 平台的数字代码
SORT_FIELDS = {"impressions": "3", "first_seen": "1", "last_seen": "2"}

# 凭据没配 / 过期时给用户的话。**必须说清楚去哪儿拿**,
# 因为这两个值不是 API key,是从浏览器开发者工具里拷的,不会有人自己猜到。
HOWTO = (
    "拿凭据的办法:①用 Chrome 登录 insightrackr.com;②按 F12 打开开发者工具 → Network;"
    "③在网站上随便搜一次素材;④在 Network 里点那条 search 请求 → Headers → "
    "把 `Authorization` 和 `Cookie` 两行的值整段复制出来;"
    "⑤分别填进 .env 的 INSIGHTRACKR_AUTHORIZATION 和 INSIGHTRACKR_COOKIE。"
)


class InsightrackrError(Exception):
    pass


def _conf(name: str, default: str = "") -> str:
    """现读 .env(和项目其它地方一个路数:留空后来才填的键,运行中的进程也能读到)。"""
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


def _payload(keyword: str, start: str, end: str, page_size: int,
             sort_field: str, sort_rule: str) -> dict:
    """平台要的请求体。字段极多且大部分必须存在(哪怕是空数组),
    照抄 qx-ad-bot 生产环境在用的那一份,别自己精简 —— 少一个字段就可能报错。"""
    return {
        "keyWord": keyword, "keyWordType": "0,1,2,3,4,6,8,9", "keyWordList": [],
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
    kind = str(_first(item, ("materialType", "mediaType", "type")) or "").lower()
    is_video = "video" in kind or url.lower().endswith(".mp4")
    w, h = _num(_first(item, ("width",))) or 0, _num(_first(item, ("height",))) or 0
    return {
        "image_url": url,
        "thumbnail": str(_first(item, ("thumbnailUrl", "thumbnail_url", "coverUrl", "cover_url",
                                       "coverImage", "cover_image", "imageUrl", "image_url")) or url),
        "title": str(_first(item, ("title", "name", "materialName", "creativeName",
                                   "productName")) or "(无标题)")[:80],
        "author": str(_first(item, ("brandName", "appName", "advertiser")) or "(未知广告主)")[:60],
        "media_type": "VIDEO" if is_video else "IMAGE",
        "width": w, "height": h,
        "first_seen": first.strftime("%Y-%m-%d") if first else None,
        "last_seen": last.strftime("%Y-%m-%d") if last else None,
        "投放天数": run_days,
        "预估曝光": _num(_first(item, ("impressions", "impression", "estimatedImpressions",
                                       "estimateExposure", "estimatedExposure", "exposure",
                                       "showCount", "viewCount"))),
        "source": "Insightrackr(竞品广告)",
        "source_page": str(_first(item, ("landingPage", "landing_page", "previewUrl")) or ""),
        "license": "⚠️ 这是其它广告主正在投的广告素材,不是授权图库的图",
        "quality": cs._quality(w, h),
    }


def search(keyword: str, days: int = 180, count: int = 8,
           sort: str = "impressions") -> dict:
    """按关键词查竞品正在投的广告。days=往前看多少天;sort 见 SORT_FIELDS。"""
    if not is_configured():
        raise InsightrackrError("还没配置 Insightrackr 凭据。" + HOWTO)

    end = date.today()
    start = end - timedelta(days=max(1, min(int(days or 180), 365)))
    body = _payload((keyword or "").strip(), start.isoformat(), end.isoformat(),
                    max(1, min(int(count or 8), 40)),
                    SORT_FIELDS.get(sort, "3"), "desc")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        # 平台按浏览器请求来对待,UA 照着 qx-ad-bot 生产环境的写
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Authorization": _conf("INSIGHTRACKR_AUTHORIZATION"),
        "Cookie": _conf("INSIGHTRACKR_COOKIE"),
    }
    with httpx.Client(timeout=TIMEOUT, trust_env=False) as c:
        resp = c.post(_conf("INSIGHTRACKR_SEARCH_URL", DEFAULT_URL), json=body, headers=headers)
        resp.raise_for_status()
    data = resp.json()

    code = data.get("code")
    if code not in (None, 0, 200):
        # -3106 = 登录态失效。**这条必须单独翻译**:凭据是从浏览器拷的、会过期,
        # 不说清楚的话用户只会以为"搜不到素材",查半天查不到原因。
        if code == -3106:
            raise InsightrackrError("Insightrackr 登录已过期(凭据是从浏览器拷的,会失效)。" + HOWTO)
        raise InsightrackrError(str(data.get("msg") or data.get("message") or f"平台返回错误码 {code}"))

    items = [_normalize(x) for x in _rows(data)]
    items = [x for x in items if x["image_url"]]
    return {"results": items[:count], "query": keyword,
            "period": f"{start} ~ {end}", "sort": sort}
