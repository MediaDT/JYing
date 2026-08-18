"""
素材查找 —— 从**正规授权图库**里找可以合法投广告的图片。

为什么单独一个模块:它和 NewsBreak 没关系,是纯外部图库的封装。
以后想加图库(或者换掉),只改这里,不碰 agent_server。

**这个模块的存在前提是"版权干净"**,所以有两条铁律,都守在代码里而不是提示词里:
  1. **只返回明确允许商用的素材**。投广告是商业用途,拿 NC(禁止商用)的图去投
     就是侵权。Openverse 默认搜出来的第一条就是 by-nc-sa —— 实测过,所以这个
     过滤不能只靠"记得加参数",必须写死在请求里(见 _openverse)。
  2. **每条结果都带上许可证和出处**,让人能查证。不许出现"来源不明"的素材。

图库有两类:
  · Pexels / Pixabay —— 要一把**免费**钥匙(注册 1 分钟)。图是广告级的商业摄影,
    许可证本身就允许商用且不要求署名,最适合投放。**有钥匙就优先用它们。**
  · Openverse —— **不用钥匙**,现在就能跑。但它聚合的是维基百科/Flickr 那类
    纪实照片,家装维修这类广告品类的 CC0 存量很薄(实测 "gutter cleaning" 只有 1 张)。
    当兜底可以,当主力不够。
"""

import os
import httpx

TIMEOUT = 20.0

# 下载素材时的 User-Agent。**格式不能随便写**:Openverse 的图大量托管在
# Wikimedia,而它的机器人政策要求 UA 里带**联系方式**(括号里那段),否则 403。
# 实测过:浏览器 UA、curl 的 UA、以及不带括号联系方式的自定义 UA,**全部 403**;
# 只有下面这种「名字/版本 (联系地址) 库/版本」的格式能拿到 200。
UA = ("my-agent-adbot/1.0 (https://github.com/MediaDT/JYing; NewsBreak ad assistant) "
      "httpx/0.27")

# 竞品平台的 CDN 常按浏览器请求来对待,给下载留一个备用 UA
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# NewsBreak 信息流广告的图片规格。1200×628 是平台建议值(≈1.91:1),
# 比这个小的图在手机上会糊。这些数字只在这里存一份,改规格改这里。
REC_W, REC_H = 1200, 628          # 建议尺寸
MIN_W, MIN_H = 600, 314           # 硬底线,再小就别用了
BEST_RATIO = REC_W / REC_H        # ≈1.91,信息流最常见的横图比例

# 下载素材时的大小上限。防止一张 50MB 的原图把内存和上传都拖垮
# (线上 nginx 也只放行 100MB,见 CLAUDE.md 第六之六节)。
MAX_BYTES = 20 * 1024 * 1024


def _key(name: str) -> str:
    """现读 .env(和项目其它地方一个路数:留空后来才填的键,进程也能读到)。"""
    try:
        import agent_server
        v = agent_server._read_env_value(name)
        if v:
            return v
    except Exception:
        pass
    return os.getenv(name, "").strip()


def available_sources() -> list[dict]:
    """哪些图库现在能用。给用户看的,所以要说清楚"要不要钥匙、去哪领"。"""
    return [
        {"id": "pexels", "name": "Pexels", "ready": bool(_key("PEXELS_API_KEY")),
         "quality": "广告级商业摄影,最适合投放",
         "license": "Pexels License:可免费商用,无需署名,可修改",
         "how": "免费注册 https://www.pexels.com/api/ 拿钥匙,填进 .env 的 PEXELS_API_KEY"},
        {"id": "pixabay", "name": "Pixabay", "ready": bool(_key("PIXABAY_API_KEY")),
         "quality": "商业图库,质量不错",
         "license": "Pixabay Content License:可免费商用,无需署名",
         "how": "免费注册 https://pixabay.com/api/docs/ 拿钥匙,填进 .env 的 PIXABAY_API_KEY"},
        {"id": "openverse", "name": "Openverse", "ready": True,
         "quality": "纪实照片为主,家装维修类存量少",
         "license": "只取 CC0 / 公有领域:可商用、无需署名",
         "how": "不用钥匙,开箱即用"},
    ]


def _quality(w: int, h: int, media_type: str = "IMAGE") -> dict:
    """按 NewsBreak 的素材规格打分,并说人话解释为什么。

    用户说了"没有好素材时也要能看素材质量",所以这里不只给个分,
    还要讲清差在哪、能不能凑合用。

    **视频要单独判**:下面那套宽高比规矩是给信息流横图定的,
    竖版视频在视频广告里完全正常,照图片标准判会把好素材全毙掉。
    """
    w, h = int(w or 0), int(h or 0)
    if w <= 0 or h <= 0:
        return {"verdict": "未知", "score": 0, "reasons": ["没给出尺寸,建议下载后自己看一眼"]}

    if str(media_type).upper() == "VIDEO":
        if w < MIN_W and h < MIN_H:
            return {"verdict": "不建议", "score": 0,
                    "reasons": [f"视频分辨率 {w}×{h} 太小,手机上会糊"]}
        shape = "竖版" if h > w * 1.2 else ("横版" if w > h * 1.2 else "方形")
        return {"verdict": "可用", "score": 3,
                "reasons": [f"{shape}视频 {w}×{h};视频广告对宽高比没有图片那么挑,"
                            f"但要确认前 3 秒就能抓住人"]}

    reasons, score, ratio = [], 0, w / h

    # 这两条是**硬伤,一票否决**,不能被另一项的高分抵消。
    # (原来用纯加分制,结果 400×210 因为"宽高比正好"被判成"可用" —— 分辨率
    #  根本不够用还推荐给用户,是错的。冒烟测试抓出来的。)
    too_small = w < MIN_W or h < MIN_H
    portrait = ratio < 0.9

    # ① 分辨率
    if w >= REC_W and h >= REC_H:
        score += 2
        reasons.append(f"分辨率 {w}×{h},达到建议的 {REC_W}×{REC_H},手机上清晰")
    elif not too_small:
        score += 1
        reasons.append(f"分辨率 {w}×{h},能用但低于建议的 {REC_W}×{REC_H},放大可能会糊")
    else:
        reasons.append(f"分辨率 {w}×{h} 太小(底线 {MIN_W}×{MIN_H}),放到信息流里会糊")

    # ② 宽高比。信息流是横图的天下,竖图会被裁掉大半
    if portrait:
        reasons.append(f"这是竖图({ratio:.2f}:1),信息流里会被裁掉大半,主体容易没了")
    elif abs(ratio - BEST_RATIO) / BEST_RATIO <= 0.20:
        score += 2
        reasons.append(f"宽高比 {ratio:.2f}:1,接近信息流最合适的 {BEST_RATIO:.2f}:1")
    elif ratio <= 1.1:
        score += 1
        reasons.append(f"接近正方形({ratio:.2f}:1),可用,但横图通常点击率更好")
    else:
        score += 1
        reasons.append(f"偏宽({ratio:.2f}:1),上下可能被裁,注意主体别贴边")

    if too_small or portrait:
        verdict = "不建议"
    else:
        verdict = "推荐" if score >= 4 else "可用"
    return {"verdict": verdict, "score": score, "reasons": reasons}


def _norm(*, url, thumb, w, h, title, author, page, license_name, license_url, source):
    """把各家图库格式不一的返回,统一成一个样子。"""
    return {
        "image_url": url, "thumbnail": thumb or url,
        "width": int(w or 0), "height": int(h or 0),
        "title": (title or "").strip()[:80] or "(无标题)",
        "author": (author or "").strip()[:60] or "(未署名)",
        "source_page": page, "source": source,
        "license": license_name, "license_url": license_url,
        "quality": _quality(w, h),
    }


# ============ 各家图库 ============

def _pexels(query: str, count: int) -> list[dict]:
    key = _key("PEXELS_API_KEY")
    if not key:
        return []
    r = httpx.get("https://api.pexels.com/v1/search", timeout=TIMEOUT, trust_env=False,
                  headers={"Authorization": key},
                  params={"query": query, "per_page": count, "orientation": "landscape"})
    r.raise_for_status()
    out = []
    for p in r.json().get("photos", []):
        src = p.get("src") or {}
        out.append(_norm(
            url=src.get("large2x") or src.get("large") or src.get("original"),
            thumb=src.get("medium"), w=p.get("width"), h=p.get("height"),
            title=p.get("alt"), author=p.get("photographer"), page=p.get("url"),
            license_name="Pexels License(可免费商用,无需署名)",
            license_url="https://www.pexels.com/license/", source="Pexels"))
    return out


def _pixabay(query: str, count: int) -> list[dict]:
    key = _key("PIXABAY_API_KEY")
    if not key:
        return []
    r = httpx.get("https://pixabay.com/api/", timeout=TIMEOUT, trust_env=False,
                  params={"key": key, "q": query, "per_page": max(3, count),
                          "image_type": "photo", "orientation": "horizontal",
                          "safesearch": "true"})
    r.raise_for_status()
    out = []
    for p in r.json().get("hits", [])[:count]:
        out.append(_norm(
            url=p.get("largeImageURL"), thumb=p.get("webformatURL"),
            w=p.get("imageWidth"), h=p.get("imageHeight"),
            title=p.get("tags"), author=p.get("user"), page=p.get("pageURL"),
            license_name="Pixabay Content License(可免费商用,无需署名)",
            license_url="https://pixabay.com/service/license-summary/", source="Pixabay"))
    return out


def _openverse(query: str, count: int) -> list[dict]:
    """Openverse 不用钥匙。

    ⚠️ `license=cc0,pdm` 这个参数**绝对不能省**:不加的话默认会搜出 by-nc-sa
    (NC = 禁止商用)这类素材,拿去投广告就是侵权。实测第一条就是 by-nc-sa。
    只取 CC0 和公有领域,是因为这两种既可商用、又不要求在广告里署名 ——
    CC-BY 虽然也能商用,但要求署名,广告图上没地方放。
    """
    r = httpx.get("https://api.openverse.org/v1/images/", timeout=TIMEOUT, trust_env=False,
                  params={"q": query, "license": "cc0,pdm", "size": "large",
                          "page_size": count, "mature": "false"})
    r.raise_for_status()
    out = []
    for p in r.json().get("results", []):
        lic = (p.get("license") or "").upper()
        out.append(_norm(
            url=p.get("url"), thumb=p.get("thumbnail"),
            w=p.get("width"), h=p.get("height"),
            title=p.get("title"), author=p.get("creator"),
            page=p.get("foreign_landing_url"),
            license_name=f"{lic}(公有领域/可商用,无需署名)",
            license_url=p.get("license_url") or "", source=f"Openverse·{p.get('provider')}"))
    return out


_PROVIDERS = {"pexels": _pexels, "pixabay": _pixabay, "openverse": _openverse}


def search(query: str, count: int = 6, source: str = "auto") -> dict:
    """按关键词找可商用素材。source="auto" 时:有钥匙的商业图库优先,兜底 Openverse。"""
    query = (query or "").strip()
    if not query:
        return {"error": "要给一个搜索关键词,比如 roof repair"}
    count = max(1, min(int(count or 6), 12))

    order = ([source] if source in _PROVIDERS
             else [s["id"] for s in available_sources() if s["ready"]])
    results, tried, errors = [], [], []
    for sid in order:
        tried.append(sid)
        try:
            got = _PROVIDERS[sid](query, count)
        except Exception as e:
            errors.append(f"{sid}: {str(e)[:80]}")
            continue
        results.extend(got)
        if len(results) >= count:
            break

    # 差的排后面,但仍然列出来 —— 用户说了"没有好素材也要能看到质量如何"
    results.sort(key=lambda x: -x["quality"]["score"])
    return {"results": results[:count], "query": query, "tried": tried,
            "errors": errors or None}


def download(image_url: str) -> tuple[bytes, str, str]:
    """把选中的素材下载回来,返回 (内容, 文件名, MIME)。用于转存到 NewsBreak。"""
    # ⚠️ User-Agent 不能省:Wikimedia(Openverse 的主要来源)明文规定必须带一个
    # 能说明来源的 UA,不带直接 **403 Forbidden**。实测踩到过。
    # 两种 UA 都要备着:图库那边(Wikimedia)只认上面那个政策格式,
    # 而竞品平台的 CDN 往往只认浏览器 UA。先用政策格式,被 403 了再换浏览器的。
    for ua in (UA, BROWSER_UA):
        try:
            return _fetch(image_url, ua)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 403 or ua is BROWSER_UA:
                raise
    raise RuntimeError("unreachable")


def _fetch(image_url: str, ua: str) -> tuple[bytes, str, str]:
    with httpx.Client(timeout=60.0, trust_env=False, follow_redirects=True,
                      headers={"User-Agent": ua}) as c:
        with c.stream("GET", image_url) as r:
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
            if ctype and not ctype.startswith(("image/", "video/")):
                raise ValueError(f"这个地址返回的不是图片而是 {ctype},不能当素材用")
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    raise ValueError(f"素材超过 {MAX_BYTES // 1024 // 1024}MB,太大了")
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
           "image/webp": ".webp"}.get(ctype, ".jpg")
    name = image_url.rstrip("/").split("/")[-1].split("?")[0]
    if not name or "." not in name:
        name = "stock" + ext
    return bytes(buf), name, ctype or "image/jpeg"
