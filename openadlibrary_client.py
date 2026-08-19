"""
竞品广告查询(OpenAdLibrary)—— 看**别人正在投什么原生广告**。

**替代了原来的 Insightrackr**,换掉的理由很实在:
  · Insightrackr 是拿浏览器登录态在请求,**几小时就过期**,要人工重贴;
    这家是**正规 API key**(`oal_` 开头),不过期 —— "老过期"这个问题直接消失;
  · 关键词、排序、国家、投放天数全是**文档化的具名参数**,不用像上家那样
    靠实测去猜 `keyWordType=0,2` / `sortField=4` 这种魔法数字;
  · 有明确的配额和状态码(402 额度用尽 / 429 频率超限),报错分得清。

覆盖的是**原生广告网络**(Taboola / Outbrain / MGID 那类)—— 正好是家装线索
这类品类的主战场。

接口:`GET https://openadlibrary.com/api/v1/ads`
认证:`Authorization: Bearer <key>`(也支持 `x-api-key`,我们用前者)
配额:Pro 5000 次/天 + 120 次/分钟的瞬时上限
"""

import contextvars
import os
from datetime import date, timedelta

import httpx

TIMEOUT = 30.0
BASE_URL = "https://openadlibrary.com"
ADS_PATH = "/api/v1/ads"

# 每分钟 120 次是硬上限,批量任务要留间隔。我们是用户点一次查一次,
# 但以后要做批量拆解的话,别忘了这条。
BURST_PER_MINUTE = 120

HOWTO = ("拿 key 的办法:登录 openadlibrary.com → Settings → API keys → 新建一个,"
         "**它只在创建时显示一次**,复制下来。key 以 `oal_` 开头。"
         "然后在网页上点 🕵️ 按钮贴进去即可(不用改配置文件、不用重启)。")

# 当前请求这个人自己贴的 key(在 AuthMiddleware 里设,必须设在 call_next 之前)。
# 取值规则:**自己贴过就用自己的,没贴过就回落 .env 那份公用的** ——
# 这是公司一份订阅的只读查询,没有"谁的数据"之分,和 NewsBreak 那种
# "没绑就报错、绝不回落"的规矩故意不同(那边关系到各人的钱)。
CURRENT_CREDS: contextvars.ContextVar = contextvars.ContextVar(
    "openadlibrary_creds", default=None)


class OpenAdLibraryError(Exception):
    pass


def _key() -> str:
    creds = CURRENT_CREDS.get()
    if isinstance(creds, dict):
        v = str(creds.get("api_key", "") or "").strip()
        if v:
            return v
    try:
        import agent_server
        v = agent_server._read_env_value("OPENADLIBRARY_API_KEY")
        if v:
            return v
    except Exception:
        pass
    return os.getenv("OPENADLIBRARY_API_KEY", "").strip()


def is_configured() -> bool:
    return bool(_key())


def _headers(key: str = "") -> dict:
    return {"Authorization": f"Bearer {key or _key()}",
            "Accept": "application/json"}


def _check(resp: httpx.Response) -> dict:
    """把 HTTP 状态翻译成人话。**每种失败原因必须分得清**,
    否则用户只会瞎换 key(这条是上一个平台踩出来的教训)。"""
    c = resp.status_code
    if c == 401 or c == 403:
        return {"__error": "API key 无效或已被撤销。" + HOWTO}
    if c == 402:
        return {"__error": "当天的免费额度用完了(免费账号每天 2 次)。"
                           "升级 Pro 后是 5000 次/天:https://openadlibrary.com/app/billing"}
    if c == 429:
        return {"__error": "请求太频繁或当天配额用尽(Pro 是 5000 次/天、120 次/分钟),"
                           "过一会儿再试"}
    if c >= 500:
        return {"__error": f"OpenAdLibrary 暂时不可用(HTTP {c}),**这不是 key 的问题**,过几分钟再试"}
    if c >= 400:
        return {"__error": f"请求被拒绝(HTTP {c}):{resp.text[:200]}"}
    try:
        return resp.json()
    except Exception:
        return {"__error": f"平台返回的不是 JSON(HTTP {c}):{resp.text[:200]}"}


def raw_search(**params) -> dict:
    """按给定参数直接查,返回**平台的原始 JSON**。

    留这个口子是给探索和排障用的:响应字段结构官方文档里没写死
    (只标了 `200 Default Response`),所以字段映射是实测出来的 ——
    平台哪天改了结构,用它打一次就能看出来。
    """
    if not is_configured():
        raise OpenAdLibraryError("还没配置 OpenAdLibrary 的 API key。" + HOWTO)
    with httpx.Client(timeout=TIMEOUT, trust_env=False) as c:
        resp = c.get(BASE_URL + ADS_PATH, params=params, headers=_headers())
    data = _check(resp)
    if isinstance(data, dict) and data.get("__error"):
        raise OpenAdLibraryError(data["__error"])
    return data


def validate(api_key: str = "") -> tuple[bool, str]:
    """拿一把 key 打一次**最小**请求,确认它真能用。

    两个用处:①存之前先验,验不过就不覆盖旧的;
    ②跑批量任务之前先验,别跑到一半才发现不能用。
    """
    key = (api_key or "").strip() or _key()
    if not key:
        return False, "还没填 API key。" + HOWTO
    if not key.startswith("oal_"):
        return False, f"这不像 OpenAdLibrary 的 key(应该以 `oal_` 开头,你填的以 `{key[:4]}` 开头)。" + HOWTO
    try:
        key.encode("latin-1")   # HTTP 头只认 latin-1,混进中文会抛天书般的报错
    except UnicodeEncodeError:
        return False, "key 里混进了非法字符 —— 只能是英文数字符号,重新复制一次。"
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False) as c:
            resp = c.get(BASE_URL + ADS_PATH, params={"pageSize": 1},
                         headers=_headers(key))
    except Exception as e:
        return False, f"连不上平台:{str(e)[:120]}"
    data = _check(resp)
    if isinstance(data, dict) and data.get("__error"):
        return False, data["__error"]
    return True, "API key 有效"


# ============ 实测出来的参数真相(文档没写全,别照文档猜)============
#
# 2026-08-19 拿真 key 逐个打出来的:
#   · `sort=placements` ✅ 按**版位数**降序(实测 41/24/17/17)。版位数 =
#     这条广告在多少个位置被观测到,铺得越广说明广告主越愿意为它花钱 ——
#     这是本平台最接近"跑得动"的信号(它不提供曝光量)。
#   · `sort=oldest`     ✅ 有效(返回另一批)。
#   · **`sortBy` 整个参数无效**,`sort` 的其它值(days / daysRunning /
#     firstSeen / lastSeen / newest / recent)也**全是静默忽略** ——
#     不报错,只是假装排了。比直接报错更阴险,所以这里只暴露验证过的两个值。
#   · **`sortDir` 无效**:配 sort=placements 时 asc/desc 结果一模一样(恒降序)。
#   · `status=active`   ✅ 有效,实测把 12892 条收到 1921 条(只看还在投的)。
#   · **`minDaysRunning` 稳定 503**,隔几秒重试三次都一样 —— 是平台服务端的问题,
#     不是我们传错了。所以**投放天数只能拿回来自己算**(见 _days_running)。
#   · `mediaType=image` 无效(结果和不传一样)。
SORTS = {"placements": "版位数从多到少(铺得最广的)", "oldest": "最早投放的在前"}


def _days_running(first: str, last: str) -> int | None:
    from datetime import datetime
    try:
        f = datetime.fromisoformat((first or "").replace("Z", "+00:00"))
        l = datetime.fromisoformat((last or "").replace("Z", "+00:00"))
        return (l - f).days
    except (ValueError, AttributeError):
        return None


def _normalize(item: dict) -> dict:
    """把平台返回的一条广告整理成项目里通用的素材结构。

    字段名是 2026-08-19 实测确认的。几个要点:
      · `imageUrl` 是**相对路径**(/api/public/assets/xxx.webp),必须拼上域名;
        实测**下载不需要带 key**,公开可取。
      · `advertiserName` / `advertiserDomain` / `landingDomain` **只有约四成有值**
        —— 平台是"leak-safe"设计,不给落地页真实地址。没有就如实留空,别编。
      · 平台**不提供曝光量**,`placements`(版位数)是最接近的替代指标。
      · 没有宽高字段,尺寸要下载后才知道(实测这批是 1200×800,是投放级原图)。
    """
    first, last = item.get("firstSeenAt"), item.get("lastSeenAt")
    img = str(item.get("imageUrl") or "")
    if img.startswith("/"):
        img = BASE_URL + img
    return {
        "image_url": img,
        "thumbnail": img,
        "title": str(item.get("headline") or "(无标题)")[:120],
        "文案": str(item.get("body") or "")[:300] or None,
        "author": str(item.get("advertiserName") or item.get("advertiserDomain")
                      or item.get("trafficSource") or "(未知广告主)")[:60],
        "落地页域名": str(item.get("landingDomain") or ""),
        "media_type": "IMAGE",          # 平台目前只回 NATIVE 图片广告
        "width": 0, "height": 0,        # 平台不给尺寸,下载时才知道
        "first_seen": str(first or "")[:10] or None,
        "last_seen": str(last or "")[:10] or None,
        "投放天数": _days_running(first, last),
        "版位数": item.get("placements"),
        "还在投": bool(item.get("isActive")),
        "广告网络": str(item.get("adNetwork") or ""),
        "投放媒体": str(item.get("trafficSource") or ""),
        "source": "OpenAdLibrary(竞品广告)",
        "source_page": "",
        # 风险标记写死在这里,不靠提示词 —— 这是别人正在投的广告素材
        "license": "⚠️ 这是其它广告主正在投的广告素材,不是授权图库的图",
        "quality": {"verdict": "未知", "score": 0,
                    "reasons": ["平台不返回尺寸,选中转存时才能知道;实测这批多为 1200×800"]},
    }


# 本地排序的可选口径。**平台的 sort 基本不能用(见上面的实测记录),
# 所以这些是我们自己排的** —— 翻几页把候选拉回来,在本地按字段排。
# 实测翻页是有效的(page=1/page=2 零重叠),所以这条路走得通。
LOCAL_SORTS = {
    "placements": ("版位数从多到少(铺得最广)", lambda x: -(x["版位数"] or 0)),
    "days":       ("投放天数从长到短", lambda x: -(x["投放天数"] or 0)),
    "recent":     ("最近才开始投的在前", lambda x: (x["first_seen"] or "", ), ),
}

# 一次最多翻几页去凑候选池。别调太大:每页都是一次 API 调用,
# 平台每分钟只让打 120 次。
#
# ⚠️ **翻页是拿相关性换排序,不是白拿的**。实测平台的默认顺序就是按相关性排的:
# 搜 "roof repair" 时 page=1 有 97% 的标题真含 roof,**page=2 只剩 17%**。
# 所以翻页凑来的候选池必须先过 _relevant() 筛一道,否则按"投放天数"排出来的
# 会是一堆跑得久但跟屋顶无关的广告(实测就撞上了:养老金、社保那类)。
MAX_PAGES = 5


def _relevant(item: dict, keyword: str) -> bool:
    """这条广告和搜索词到底沾不沾边。

    平台的搜索是松匹配的,翻到后面几页会混进大量无关广告(见 MAX_PAGES 上面那段)。
    判据宽松但有效:关键词里的**任意一个实词**出现在标题或正文里就算数
    (搜 "roof repair",标题里有 roof 或 repair 都行)。
    """
    words = [w for w in (keyword or "").lower().split() if len(w) > 2]
    if not words:
        return True
    hay = (str(item.get("title") or "") + " " + str(item.get("文案") or "")).lower()
    return any(w in hay for w in words)


def search(keyword: str, count: int = 8, country: str = "US",
           active_only: bool = True, sort_by: str = "placements",
           min_days: int = 0, pages: int = 2) -> dict:
    """按关键词查竞品正在投的原生广告。

    keyword:英文关键词,如 "roof repair";count:最后要几条(1~50);
    country:两位大写国家码,默认 US(NewsBreak 是美国平台);留空则不限;
    active_only:只看**还在投**的(默认开);
    sort_by:见 LOCAL_SORTS —— **是我们自己排的,不是平台排的**(平台的 sort 基本没用);
    min_days:只要投放天数 ≥ 这个数的(平台的 minDaysRunning 稳定 503,所以自己筛);
    pages:翻几页凑候选池(1~5)。排序和筛选都在候选池里做,池子越大越准、也越费配额。

    **返回里会如实说明"从多少条候选里排出来的"** —— 不能让用户以为是全库最优。
    """
    pages = max(1, min(int(pages or 2), MAX_PAGES))
    per = max(1, min(int(count or 8) * 6, 50))     # 候选池开大一点才排得准

    pool, seen, total = [], set(), None
    for page in range(1, pages + 1):
        params = {"search": (keyword or "").strip(), "pageSize": per, "page": page}
        if country.strip():
            params["geoCountry"] = country.strip().upper()
        if active_only:
            params["status"] = "active"
        data = raw_search(**params)
        if total is None:
            total = data.get("total")
        rows = data.get("data") or []
        if not rows:
            break
        for r in rows:
            one = _normalize(r)
            if not one["image_url"] or one["image_url"] in seen:
                continue
            seen.add(one["image_url"])
            pool.append(one)
        if len(rows) < per:      # 已经到底了,别白白多打一次
            break

    # 先剔掉松匹配捞进来的无关广告,再排序 —— 顺序不能反
    on_topic = [x for x in pool if _relevant(x, keyword)]
    dropped = len(pool) - len(on_topic)
    kept = [x for x in on_topic if not min_days or (x["投放天数"] or 0) >= min_days]
    label, keyfn = LOCAL_SORTS.get(sort_by, LOCAL_SORTS["placements"])
    kept.sort(key=keyfn)

    return {
        "results": kept[:count],
        "query": keyword,
        "总匹配数": total,
        "候选池": (f"翻了 {pages} 页、{len(pool)} 条候选"
                   + (f",剔掉 {dropped} 条不相关的" if dropped else "")
                   + (f",其中投放≥{min_days}天的 {len(kept)} 条" if min_days else "")),
        "筛选": ("只看在投中" if active_only else "含已停投")
                + (f" / {country.upper()}" if country.strip() else " / 不限国家"),
        "sort": label + "(本地排序)",
    }


def param_check(keyword: str = "roof repair") -> dict:
    """**参数体检**:逐个试探哪些查询参数是真的生效的。

    为什么要有这个:这个平台有一批参数传了**不报错、也不生效**
    (`sortBy`、`sort` 的多数值、`mediaType`),返回和不传一模一样 ——
    比"传错就报错"难发现得多。而且平台会改,今天不能用的明天可能修好。
    与其把结论写死在注释里慢慢过期,不如留一个能随时重跑的体检。

    **判据是 `total` 变没变,不是看头几条** —— 踩过这个坑:`dateFrom` 会改变
    total(说明生效了),但前 5 条恰好没被筛掉,只看头几条会误判成"被忽略"。
    """
    from datetime import date, timedelta
    t = date.today()
    base = raw_search(search=keyword, geoCountry="US", pageSize=5)
    base_total = base.get("total")
    base_ids = tuple(x.get("id") for x in base.get("data") or [])

    cases = {
        "sort=placements": {"sort": "placements"},
        "sort=oldest": {"sort": "oldest"},
        "sortBy=placements": {"sortBy": "placements"},
        "sortDir=asc": {"sort": "placements", "sortDir": "asc"},
        "status=active": {"status": "active"},
        "mediaType=image": {"mediaType": "image"},
        "dateTo=-30d": {"dateTo": (t - timedelta(days=30)).isoformat()},
        "minDaysRunning=30": {"minDaysRunning": 30},
        "lastSeenFrom=-30d": {"lastSeenFrom": (t - timedelta(days=30)).isoformat()},
    }
    out = {}
    for label, kw in cases.items():
        try:
            d = raw_search(search=keyword, geoCountry="US", pageSize=5, **kw)
        except OpenAdLibraryError as e:
            out[label] = f"❌ 报错:{str(e)[:60]}"
            continue
        ids = tuple(x.get("id") for x in d.get("data") or [])
        if d.get("total") != base_total:
            out[label] = f"✅ 生效(total {base_total} → {d.get('total')})"
        elif ids != base_ids:
            out[label] = "✅ 生效(改变了排序)"
        else:
            out[label] = "⚠️ 静默忽略(和不传完全一样)"
    return {"基准total": base_total, "结果": out,
            "note": "⚠️ 标记的参数传了没用但也不报错,别在代码里依赖它们。"}
