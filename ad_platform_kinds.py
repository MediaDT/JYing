"""广告平台分类:投放平台属于哪一类,决定去哪儿找竞品素材。

**为什么要有这个**:用户说「我要在 NewsBreak 上投 roof」,那么该拿谁的素材来学?
答案不是「只看 NewsBreak 上的广告」—— 而是**看所有同类型平台上的同品类广告**。
原生广告的玩法(标题当钩子、图是干净实拍、平台单独渲染文字)在 Taboola、
Outbrain、Yahoo 上是一样的,跨平台的素材池大得多,能学到的规律也更可靠。

反过来,如果目标是 Meta 那种大媒体,就不该拿原生广告的套路去套 ——
那边是信息流里跟朋友动态抢注意力,creative 的结构完全不同。
"""
from __future__ import annotations

# 三类平台的定义,以及各自的素材长什么样。
KINDS = {
    "native": {
        "名称": "原生广告平台",
        "说明": "广告长得像内容,混在文章推荐位里。标题是独立字段由平台渲染,"
                "图通常是干净的实拍照片,不带文字。靠标题的好奇心钩子拿点击。",
        "取材": "可以互相学:同类型平台之间的创意套路是通用的。",
    },
    "walled": {
        "名称": "大媒体(封闭生态)",
        "说明": "自家信息流,广告和好友动态、短视频抢注意力。素材形态各异,"
                "常见图上带文字、视频前三秒定生死。",
        "取材": "不要拿原生广告的套路直接套,受众心态和版位形态都不同。",
    },
    "dsp": {
        "名称": "DSP(程序化购买)",
        "说明": "跨站买展示位,多为标准尺寸的横幅/展示广告,图上通常自带文字和按钮。",
        "取材": "和原生正相反 —— 那边图要干净,这边图要自己承载信息。",
    },
}

# 平台 → 类型。键统一小写、去掉空格和标点后匹配,免得 "Yahoo/Verizon" 和
# "yahoo verizon" 对不上。
_PLATFORMS = {
    # 原生
    "newsbreak": "native", "taboola": "native", "outbrain": "native",
    "yahooverizon": "native", "yahoo": "native", "verizon": "native",
    "mgid": "native", "revcontent": "native", "zemanta": "native",
    "microsoftaudiencenetwork": "native", "nextdoor": "native",
    # 大媒体
    "meta": "walled", "facebook": "walled", "instagram": "walled",
    "google": "walled", "youtube": "walled", "tiktok": "walled",
    "snapchat": "walled", "pinterest": "walled", "reddit": "walled",
    # DSP
    "thetradedesk": "dsp", "tradedesk": "dsp", "dv360": "dsp",
    "amazondsp": "dsp", "criteo": "dsp", "stackadapt": "dsp",
}


def _norm(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def kind_of(platform: str) -> str:
    """这个平台属于哪一类。认不出返回空字符串 —— **不猜**。"""
    n = _norm(platform)
    if n in _PLATFORMS:
        return _PLATFORMS[n]
    # 允许 "NewsBreak 广告" 这种带后缀的写法
    for key, kind in _PLATFORMS.items():
        if key and key in n:
            return kind
    return ""


def peers(platform: str) -> list[str]:
    """同类型的其它平台有哪些(用来说明素材池的范围)。"""
    k = kind_of(platform)
    if not k:
        return []
    me = _norm(platform)
    seen, out = set(), []
    for name, kind in _PLATFORMS.items():
        if kind != k or name == me or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def describe(platform: str) -> dict:
    """判断平台类型并说清「所以该去哪儿找素材」。"""
    k = kind_of(platform)
    if not k:
        return {
            "平台": platform, "类型": "", "认不出": True,
            "note": f"没认出「{platform}」属于哪类平台。**不要猜** —— "
                    f"请用户说明它是原生广告平台(像 Taboola/Outbrain)、"
                    f"大媒体(像 Meta/TikTok)、还是 DSP,再决定拿谁的素材来学。",
        }
    info = KINDS[k]
    return {
        "平台": platform,
        "类型": info["名称"],
        "类型代号": k,
        "这类平台的素材特点": info["说明"],
        "取材规则": info["取材"],
        "同类平台": peers(platform)[:10],
    }


# OpenAdLibrary 收录的广告网络 → 类型。用来判断查回来的素材能不能用来学。
def kind_of_network(ad_network: str) -> str:
    return kind_of(ad_network)
