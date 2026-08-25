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
class OpenAdLibraryError(Exception):
    pass


# 平台的 `geoCountry` 参数**不用了,国家一律在本地筛**。
#
# 实测(2026-08-20)它有两种坏法,而且会交替出现:
#   · 大多数时候直接 **503**(和 `minDaysRunning`、`lastSeenFrom` 一个毛病);
#   · 偶尔 **200 但一条都不返回** —— 这种更阴险,看不出是坏了。
# 只针对 503 做降级会被第二种骗过去(实测就骗到了:返回 0 条,而降级没触发)。
# 反正每条结果都带 `geos`,本地筛既准又不会被平台的毛病影响
# (实测 US 有结果、JP 为 0,筛得对)。这和「平台排不了的我们自己排」是同一条路子。


def _key() -> str:
    """取 API key —— **全公司一份,配在 .env 里**。

    这里**故意不做"每人一份"**:key 不过期、额度 5000 次/天,而且查的是公开的
    竞品广告库,没有"谁的数据"之分。给每人配一份只会多一套要维护的界面和状态,
    换不来任何隔离价值。(NewsBreak 那边是相反的:各人各绑,绝不回落 `.env` ——
    因为那关系到各自的广告账户和钱。)

    走 `_read_env_value` 现读文件,而不是只信进程启动时的环境变量:
    `.env` 里后来才填上的键,运行中的进程是读不到的(第八节「空值配置读不到」)。
    """
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
        # geos 要带出来:平台的 geoCountry 参数坏掉时,靠它在本地筛国家
        "geos": [str(g).upper() for g in (item.get("geos") or []) if g],
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

    want_geo = country.strip().upper()
    pool, seen, total = [], set(), None
    for page in range(1, pages + 1):
        # 注意不发 geoCountry,国家在本地筛(原因见上面那段注释)
        params = {"search": (keyword or "").strip(), "pageSize": per, "page": page}
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

    # 国家一律本地筛(条目自带 geos)。没有 geos 的条目保留 ——
    # 宁可多给一条待确认的,也不要因为平台没给字段就把它悄悄丢了。
    if want_geo:
        pool = [x for x in pool
                if not x.get("geos") or want_geo in [str(g).upper() for g in x["geos"]]]

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


# ── 品类词典(粗分桶,用来做"市场上什么在跑"的汇总) ──────────
# **这是一份粗分桶,不是精确分类。** 靠标题里的词命中,命中不了的一律进「其它」
# 并**如实报出占比** —— 假装分类完整比分错更糟,用户会以为看到了全貌。
# 词典只在这里存一份,加品类改这里就行。
VERTICALS = [
    ("健康养生", ["weight", "belly", "fat", "pound", "diabet", "blood sugar", "blood pressure",
                  "joint", "knee", "shoulder", "back pain", "spine", "stenosis", "nerve",
                  "neuropathy", "hearing", "dental", "teeth", "gum", "implant",
                  "skin", "wrinkle", "hair", "supplement", "vitamin", "collagen",
                  "sleep apnea", "snor", "prostate", "bladder", "vision", "eye ",
                  "throat", "mucus", "lung", "gut", "digest", "artery", "heart",
                  "cholesterol", "cognitive", "memory", "brain", "hydrat", "doctor",
                  "cardiologist", "surgeon", "clinic", "symptom", "remedy", "detox"]),
    ("医美塑形", ["breast", "lift", "botox", "filler", "liposuction", "tummy tuck",
                  "facelift", "before-and-after", "before and after"]),
    ("家装维修", ["roof", "gutter", "window", "siding", "hvac", "furnace", "plumb",
                  "bathroom", "shower", "kitchen", "remodel", "foundation", "driveway",
                  "garage", "insulation", "solar", "fence", "deck", "basement",
                  "walk-in tub", "granny pod", "adu"]),
    ("金融投资", ["insurance", "loan", "mortgage", "refinanc", "credit", "debt",
                  "medicare", "retire", "annuit", "invest", "stock", "crypto", "bitcoin",
                  "trading", "bank", "savings", "tax", "settlement", "payout",
                  "send money", "cash", "wealth", "richest", "millionaire"]),
    ("政府补贴福利", ["grant", "benefit", "stimulus", "eligible", "qualify",
                      "social security", "assistance", "subsid", "relief",
                      "may be entitled", "government"]),
    ("家居日用", ["mattress", "bedsheet", "sheets", "pillow", "sofa", "furniture",
                  "vacuum", "air purifier", "appliance", "cookware", "showerhead",
                  "patch", "costco", "amazon", "walmart"]),
    ("汽车", ["car ", "cars", "suv", "truck", "vehicle", "auto ", "ev ", "tire",
              "dealership", "lease", "drivers"]),
    ("宠物", ["dog", "cat ", "cats", "puppy", "pet ", "vet "]),
    ("旅游出行", ["cruise", "flight", "fly ", "business class", "hotel", "travel",
                  "vacation", "resort", "airline"]),
    ("科技数码", ["phone", "iphone", "laptop", "internet", "wifi", "streaming",
                  "app ", "ai ", "chatgpt", "robot"]),
    ("教育就业", ["job", "career", "degree", "course", "training", "hiring", "salary"]),
    ("猎奇内容", ["you won't believe", "take a look", "look inside", "photos",
                  "never went", "in the 1950s", "history", "celebrit", "actress",
                  "actor", "son is", "star ", "most handsome", "most beautiful"]),
]

# 钩子套路(标题的写法)。比品类更能说明"原生平台吃哪一套"。
HOOKS = [
    ("资格/福利框架", ["eligible", "qualify", "if you live", "zip code", "born between",
                       "homeowners in", "residents", "may be entitled", "who ask"]),
    ("价格悬念", ["cost", "price", "how much", "what should", "cheap", "$", "for free"]),
    ("否定既有认知", ["thing of the past", "stop ", "never ", "forget ", "don't ",
                      "myth", "wrong", "changing the way"]),
    ("清单/名单", ["companies", "ways", "things", "reasons", "signs", "tips",
                   " list", "here are"]),
    ("身份对号入座", ["seniors", "homeowners", "drivers", "women over", "men over",
                     "if you're", "side sleepers", "-year-old"]),
    ("秘诀/内幕", ["secret", "few know", "doctors", "experts say", "here's what",
                   "here's why", "trick", "hack"]),
    ("时间紧迫", ["before ", "now ", "2026", "this year", "deadline", "ending"]),
]


def _bucket(text: str, table: list) -> str:
    """粗分桶。命中不了返回空字符串 —— **不猜**,交给上层归到「其它」。"""
    low = (text or "").lower()
    for name, words in table:
        if any(w in low for w in words):
            return name
    return ""


def market_scan(top_n: int = 200, active_only: bool = False) -> dict:
    """**不指定品类**,看整个原生广告市场上什么在跑得最好。

    用途:用户问「现在什么广告跑得好」「这个平台适合跑什么单子」这类**开放问题**时用 ——
    这时不该拿他账户已有的品类去框(那会把他困在现有生意里,看不到别的机会)。

    做法:按版位数取全库最靠前的一批,然后按**品类**和**钩子套路**两个维度汇总。
    版位数 = 这条广告铺了多少个位置,是平台唯一给的"跑得动"信号
    (展示量、点击率、转化率平台一概不给)。
    """
    top_n = max(20, min(int(top_n or 200), 500))
    per = 50
    rows, seen = [], set()
    for page in range(1, (top_n // per) + 2):
        params = {"pageSize": per, "page": page, "sort": "placements"}
        if active_only:
            params["status"] = "active"
        data = raw_search(**params)
        got = data.get("data") or []
        if not got:
            break
        for r in got:
            rid = r.get("id")
            if rid and rid not in seen:
                seen.add(rid)
                rows.append(r)
        if len(rows) >= top_n:
            break
    rows = rows[:top_n]

    def tally(table):
        agg: dict = {}
        for r in rows:
            text = f"{r.get('headline') or ''} {r.get('body') or ''}"
            name = _bucket(text, table) or "其它(词典没命中)"
            a = agg.setdefault(name, {"条数": 0, "版位数合计": 0, "例子": []})
            a["条数"] += 1
            a["版位数合计"] += int(r.get("placements") or 0)
            if len(a["例子"]) < 3:
                a["例子"].append({"标题": str(r.get("headline") or "")[:90],
                                  "版位数": r.get("placements"),
                                  "广告网络": r.get("adNetwork")})
        return sorted(agg.items(), key=lambda kv: -kv[1]["版位数合计"])

    nets: dict = {}
    for r in rows:
        n = str(r.get("adNetwork") or "(未标注)")
        nets[n] = nets.get(n, 0) + 1

    return {
        "看了多少条": len(rows),
        "口径": "按版位数取全库最靠前的一批。版位数=铺了多少个位置,"
                "是平台唯一给的「跑得动」信号;展示量/点击率/转化率平台不提供。",
        "品类排行": [{"品类": k, **v} for k, v in tally(VERTICALS)],
        "钩子排行": [{"钩子": k, **v} for k, v in tally(HOOKS)],
        "覆盖的原生平台": nets,
        "提醒": "品类是靠标题里的关键词粗分的,**命中不了的都归在「其它」** —— "
                "那一栏占比高就说明这份词典没覆盖到,别当成「这市场上没有别的品类」。",
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
