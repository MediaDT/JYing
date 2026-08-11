"""
投放平台注册表 —— 决定"登录后能选哪些平台"。

设计原则:**没有真正对接代码的平台,一律标成 coming(敬请期待),不许假装能用。**
否则用户满怀期待点进去,发现什么都干不了,比不列出来更糟。

将来接新平台(比如 Nextdoor)要做三件事:
  1. 写一个 nextdoor_client.py(照 newsbreak_client.py 的样子);
  2. 在 agent_server 里给它加工具函数;
  3. 把这里的 status 从 "coming" 改成 "ready",并实现它的 bound() 检查。
"""

def _newsbreak_bound(user_id: str = "") -> bool:
    """这个人绑没绑 NewsBreak —— **按人查**,不看 .env。

    以前是"看 .env 里有没有 token",那是全局一份:A 绑好之后 B 登录进来
    直接就能操作 A 的广告账户。现在每人绑自己的。

    没传 user_id(比如没登录)一律算没绑。
    """
    if not user_id:
        return False
    import accounts as acc
    return bool(acc.get_creds(user_id, "newsbreak").get("token"))


# 注册表。status: ready=能用 / coming=还没对接
PLATFORMS = [
    {
        "id": "newsbreak",
        "name": "NewsBreak",
        "icon": "📰",
        "status": "ready",
        "desc_zh": "美国本地新闻 App。查数据、看报表、开关投放、对话式建广告、定时开关,全都支持。",
        "desc_en": "US local-news app. Query data, pull reports, pause/launch, build campaigns by chat, schedule on/off.",
        "bound": _newsbreak_bound,
        "bind_hint_zh": "去 NewsBreak Ad Manager → Resources → API Access Tokens 生成 token,粘贴进来即可。",
        "bind_hint_en": "Generate a token in NewsBreak Ad Manager → Resources → API Access Tokens, then paste it here.",
    },
    {
        "id": "nextdoor",
        "name": "Nextdoor",
        "icon": "🏘️",
        "status": "coming",
        "desc_zh": "美国社区邻里社交平台。对接开发中,还不能用。",
        "desc_en": "US neighbourhood social network. Integration in progress — not usable yet.",
        "bound": lambda user_id="": False,
        "bind_hint_zh": "",
        "bind_hint_en": "",
    },
    {
        "id": "meta",
        "name": "Meta (Facebook / Instagram)",
        "icon": "📘",
        "status": "coming",
        "desc_zh": "Facebook / Instagram 广告。对接开发中,还不能用。",
        "desc_en": "Facebook / Instagram ads. Integration in progress — not usable yet.",
        "bound": lambda user_id="": False,
        "bind_hint_zh": "",
        "bind_hint_en": "",
    },
]

_BY_ID = {p["id"]: p for p in PLATFORMS}
DEFAULT_ID = "newsbreak"


def get(platform_id: str) -> dict | None:
    return _BY_ID.get((platform_id or "").strip().lower())


def is_ready(platform_id: str) -> bool:
    p = get(platform_id)
    return bool(p and p["status"] == "ready")


def is_bound(platform_id: str, user_id: str = "") -> bool:
    p = get(platform_id)
    return bool(p and p["status"] == "ready" and p["bound"](user_id))


def public_list(user_id: str = "") -> list[dict]:
    """给前端用的列表(不含函数,带上**这个人**的绑定状态)。"""
    out = []
    for p in PLATFORMS:
        out.append({
            "id": p["id"],
            "name": p["name"],
            "icon": p["icon"],
            "status": p["status"],
            "bound": p["status"] == "ready" and p["bound"](user_id),
            "desc_zh": p["desc_zh"],
            "desc_en": p["desc_en"],
            "bind_hint_zh": p["bind_hint_zh"],
            "bind_hint_en": p["bind_hint_en"],
        })
    return out
