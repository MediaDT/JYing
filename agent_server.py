"""
广告投放小助手 —— 聊天后端(你的 agent 的"大脑中转站")。

它做两件事:
  1. 把聊天页面(static/index.html)端出来给浏览器;
  2. 提供 /api/chat 接口:收到聊天记录 → 转发给 Claude → 把回答传回去。

为什么要有这个后端?因为调 Claude 需要 API 钥匙,钥匙绝不能放进网页里,
所以网页只跟这个后端说话,钥匙留在服务器这边。

运行方法(在 my-agent 目录下):
    ./venv/bin/uvicorn agent_server:app --host 0.0.0.0 --port 18100
"""

import contextvars
import json
import os
import queue
import threading
from pathlib import Path

import anthropic
import openai
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel


def load_env_file() -> None:
    """把 .env 文件里的配置装进环境变量(.env 里填了值的,以最新值为准)。"""
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value:  # 空着的格子不往环境变量里塞,免得"空钥匙"占坑
            os.environ[key] = value


def _read_env_value(key: str) -> str:
    """直接从 .env 文件读某个键的当前值(不经过环境变量)。

    为什么不用 os.environ:load_env_file 只把「非空值」写进环境变量,
    所以一个原本留空、后来才填上的键(比如 APP_PASSWORD),
    在运行中的进程里永远读不到。密码这种安全开关必须每次现读文件。
    """
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return ""
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip()
    except Exception:
        pass
    return ""


# 启动时先读一次 .env:密码门在收到任何请求之前就必须生效
# (每次请求还会再读一次,所以改钥匙/改密码都不用重启)
load_env_file()

# docs_url=None:关掉 FastAPI 自带的接口文档(/docs、/redoc、/openapi.json),
# 免得把"操作说明书"暴露给任何能访问这个端口的人
app = FastAPI(title="广告投放小助手", docs_url=None, redoc_url=None, openapi_url=None)


# ============ 访问密码(可选)============
# 在 .env 里设 APP_PASSWORD 就启用;不设则不拦(自己单机用不受影响)。
# 用浏览器自带的登录弹窗(HTTP Basic),用户名随便填,密码要对。
import base64  # noqa: E402
import secrets  # noqa: E402

from fastapi import Request  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402


import accounts as acc  # noqa: E402
import platforms as plat  # noqa: E402

SESSION_COOKIE = "adbot_session"

# 当前请求是谁发的。AI 的工具函数(比如登记定时任务)藏在很深的调用链里,
# 拿不到 request,所以在中间件里设一次,它们直接读。
# 空字符串 = 不在请求里(定时任务线程 / 命令行 / 测试)。
CURRENT_USER_ID: contextvars.ContextVar = contextvars.ContextVar("adbot_user_id", default="")

# 流式回复时,用来把「进度/文字」一小段一小段递给前端的回调。
# None = 这次不是流式请求,大脑那边照常一次性返回。
CURRENT_EMIT: contextvars.ContextVar = contextvars.ContextVar("adbot_emit", default=None)


_TOOL_LABELS = {
    "list_organizations": "正在查组织…",
    "list_ad_accounts": "正在查广告账户…",
    "list_campaigns": "正在查广告计划…",
    "list_ad_sets": "正在查广告组…",
    "list_ads": "正在查广告…",
    "list_conversion_events": "正在查转化事件…",
    "get_report": "正在拉报表数据…",
    "propose_status_change": "正在登记开关待办…",
    "propose_create_campaign": "正在登记建广告待办…",
    "propose_schedule": "正在登记定时任务…",
    "confirm_action": "正在执行你确认的操作…",
    "cancel_action": "正在取消待办…",
    "list_pending_actions": "正在查待办…",
    "list_schedules": "正在查定时任务…",
    "cancel_schedule": "正在取消定时任务…",
}


def _tool_label(name: str) -> str:
    """把工具名说成人话 —— 用户不该看到 list_ad_sets 这种东西。"""
    return _TOOL_LABELS.get(name, "正在查数据…")


def _emit(kind: str, **data) -> None:
    """播报一个事件给前端(不是流式请求就什么也不做)。"""
    fn = CURRENT_EMIT.get()
    if fn:
        fn({"type": kind, **data})

# 不需要登录就能访问的地址:登录页本身、登录/注册接口、静态资源
_PUBLIC_PATHS = {"/login", "/api/login", "/api/register", "/api/auth-status", "/favicon.ico"}


class AuthMiddleware(BaseHTTPMiddleware):
    """登录门。没登录的一律挡在外面(除了登录页和它要用的接口)。

    一个账号都没有时也照样跳登录页 —— 登录页会自动切到"注册第一个账号"的界面。
    这样打开网站永远是登录页,不会让人对着聊天页发懵。
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        user = acc.session_user(request.cookies.get(SESSION_COOKIE, ""))
        if user:
            request.state.user = user
            # 把「这个人绑的 NewsBreak 凭据」放进上下文。设在 call_next 之前,
            # 下游(包括 FastAPI 丢进线程池跑的同步接口)才读得到。
            # 没绑的人这里是空 dict —— 关键是**不为 None**,这样 _token() 就知道
            # "在用户上下文里,他没绑",会直接报错而不是偷偷用 .env 里的公用 token。
            nb.CURRENT_CREDS.set(acc.get_creds(user["id"], "newsbreak"))
            CURRENT_USER_ID.set(user["id"])
            return await call_next(request)

        # 页面请求 → 跳登录页;接口请求 → 回 401 让前端处理
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"error": "请先登录", "need_login": True})
        return RedirectResponse("/login", status_code=302)


app.add_middleware(AuthMiddleware)

# 把 static/ 目录挂出来:页面里就能引用 /static/marked.min.js 这类文件
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")


def _scheduled_execute(level: str, object_id: str, status: str, user_id: str = ""):
    """定时任务到点时真正干活的函数(交给 scheduler 的看表线程调用)。

    看表线程不在任何请求里,没有"当前用户",所以要**用当初登记这条任务的人**
    的凭据来执行 —— 否则按人隔离之后,定时任务就不知道该用谁的账户了。
    """
    try:
        if user_id:
            nb.CURRENT_CREDS.set(acc.get_creds(user_id, "newsbreak"))
        return nb.update_status(level, object_id, status)
    except Exception as e:
        return {"error": str(e)}


@app.on_event("startup")
def _boot_scheduler():
    """服务起来后挂上"看表"线程,每 30 秒检查有没有到点的定时任务。"""
    sched.start_worker(_scheduled_execute)

# 系统提示词:给 AI 大脑设定人设和职责。
SYSTEM_PROMPT = """你是「广告投放小助手」,帮助用户管理 NewsBreak 平台上的广告投放。

你的用户是彻头彻尾的投放小白,你的首要职责是「手把手带着做」:
- 用简体中文、通俗易懂的大白话交流;涉及术语(campaign、CPC、ROAS 等)顺带用一句话解释;
- 一步只问、只讲一件事,绝不一次抛出一堆问题或一大板信息;
- 每次向用户要信息时,都要带三件套:这是什么(大白话)+ 一个具体例子 + 推荐默认值,
  让用户永远可以只回一句"用默认"就往下走;
- 多步骤流程(比如以后建计划)要报进度,如「第2步/共5步:设置预算」,并预告下一步;
- 用户答完一步,先一句话确认收到,再进入下一步;
- 用户表现出困惑或答非所问时,改成给 2~3 个选项让他挑,不要抛开放性问题;
- 用户想做的操作如果有风险(如暂停在跑的计划、大幅调预算),先提醒后建议。

你有一批「只读查询工具」可以拿到 NewsBreak 上的真实数据:
- 查组织 → 查广告账户 → 查广告计划/广告组/广告,是一层套一层的:
  先用 list_organizations 拿到 org_id,再用 list_ad_accounts 拿到账户 id,
  再用账户 id 去查 campaign / ad set / ad。
- get_report 查投放效果,支持按 campaign/ad_set/ad 汇总和自选日期段;
  用户问"花了多少钱、效果怎么样"就用它。每行**自带 name 名字**,直接用,不要再去调列表工具对 id;
  字段:name/id/cost花费/revenue收入/roas回报率/impressions展示/clicks点击/conversions转化/
  cpm千次展示成本/cpc单次点击成本/cpa单次转化成本/ctr点击率/cvr转化率,都是现成字符串直接用;
  "N/A"表示平台没这项数据(常见于还没有转化的计划),不要说成 0;汇报时优先用表格。
- 用户问数据时,直接调用工具查真的,不要编造;查完用大白话汇报重点,别原样倒 JSON。
- 各种 id 都是长数字字符串,引用时保持原样。
- 不要让用户手动"选账户":只有一个组织/账户时直接用它;
  真的有多个时,才把选项列出来请用户挑一个,并在本轮对话里记住这个选择。

「建计划向导」:用户想投新广告时,**只问必须由他决定的 4 件事**,预算和文案你来配默认值
(一步一问 + 报进度「第N步/共5步」):
第1步 落地页:要推广的链接(解释:用户点广告后打开的网页);
第2步 转化事件:用 list_conversion_events 列出账户里的事件让用户挑一个
   (解释:平台靠它统计"广告带来了多少成果";用户不懂就推荐 submit form 或第一个);
   出价不用问:系统用平台自动出价(MAX_CONVERSION),平台会自动优化;
第3步 素材:请用户点击输入框左侧的 📎 按钮上传图片或视频;上传成功后会自动出现一条带 assetUrl 的消息,记住其中的 assetUrl 和文件名;
第4步 命名:请用户给一个英文关键词(如 gutter),名字自动生成为「关键词-月日」;用户想手动指定也行。
第5步 **预算和文案:不要问,直接配好给他看,并讲清为什么**。一次性列出这几项:
   · 日预算 $20 —— 理由:平台最低 $10,但太低跑不出量、几天都攒不够数据看不出效果;
     $20 一两天就能看出苗头,又不至于烧太多。**而且建好是暂停的,你确认前一分钱不花。**
   · 用日预算而不是总预算 —— 理由:日预算随时能关,不会一次性花光。
   · 标题和描述 —— 你根据落地页主题和关键词拟,拟完把原文贴给他看;
     说明这是根据他的落地页拟的,长度已卡在平台限制内(标题≤90 字符、描述 3~90 字符)。
   · 品牌名(默认用关键词)、按钮文案(默认 Learn More)。
   列完必须明确说一句:**「这些是我替你配的默认值,想改哪一项直接告诉我,比如『预算改成 50』
   或『标题换成 XXX』。」** 用户说改就照改,不要争辩,也不要再问一遍其它项。
收集齐后调 propose_create_campaign 登记,把返回的整单内容用表格完整复述,等用户下一条消息确认后再 confirm_action。
**返回里的 defaults_used 列出了哪些值是系统默认的,复述时要把这几项单独点出来说明。**
创建结果出来后:告知三层 id、强调目前是暂停(OFF)状态、说「开启 计划名」即可开始投放。

「写操作」(开启/暂停、新建广告)必须走这套流程,一步不能少:
1) 先用查询工具核实对象,拿到准确的 id 和名字(绝不凭记忆猜 id);
2) 调 propose_status_change / propose_create_campaign 登记待办 → 向用户复述将要做的事,
   并且必须把待办编号(action_id)写进回复里,请用户确认;
3) 用户在下一条消息里明确同意(如"确认/是/OK")后,直接用编号调 confirm_action 执行;
   忘了编号就先调 list_pending_actions 查,严禁把同一件事重新登记一遍;
   用户拒绝或改主意就调 cancel_action 取消;
4) 系统有保险丝:同一条消息里登记+执行会被拦截,不要尝试绕过;
5) 执行结果只能来自 confirm_action 的返回值:所有 id 必须原样引用返回内容,严禁自己编造;
   工具返回 error 时必须如实告知失败和原因,绝不允许把失败说成成功;
   没有调用 confirm_action,就绝对不能说"已创建/已执行/已提交"。
6) 调预算等其他写操作还没接上,涉及时说明需去后台手动操作。

「定时任务」用户想让广告到点自动开/关时(如"每天早上9点打开"、"明天10点暂停"):
- 用 propose_schedule 登记(同样要用户确认才生效);时间一律按**北京时间**理解;
- kind 选 once(只一次)还是 daily(每天重复);不确定就问用户;
- list_schedules 查看现有任务和上次执行结果,cancel_schedule 取消;
- **必须提醒用户**:定时任务靠本服务持续运行,服务器关掉期间不会触发;
  且错过超过15分钟的任务不会自动补跑(避免无人看管时突然开始花钱)。"""


# 英文模式的人设。规则与中文版一一对应,只是换成英文表达 ——
# 用户在页面上点 🌐 切换语言时,这里决定 AI 用哪种语言回答。
SYSTEM_PROMPT_EN = """LANGUAGE RULE (absolute, overrides everything else): reply in **English only**.
This holds even when the user writes to you in Chinese, and even when earlier messages in this
conversation are in Chinese — the user has explicitly chosen English in the interface. Never switch
languages to match the user's input. (Proper nouns such as account names may stay in their original script.)

You are the "Ad Campaign Assistant", helping users manage advertising on NewsBreak.

Your users are complete beginners at ad buying. Your first duty is to walk them through things step by step:
- Write in plain everyday English; when a term comes up (campaign, CPC, ROAS...), explain it in one short sentence;
- Ask about, and explain, ONE thing at a time — never dump a list of questions or a wall of information;
- Every time you ask for input, include three things: what it is (plain language) + a concrete example + a recommended default,
  so the user can always just say "use the default" and keep moving;
- For multi-step flows, show progress like "Step 2 of 5: Set your budget", and preview what comes next;
- After each answer, acknowledge it in one line before moving on;
- If the user seems confused or answers something else, switch to offering 2-3 options instead of an open question;
- If an action carries risk (pausing a live campaign, a big budget change), warn first, then advise.

You have read-only tools that pull real data from NewsBreak:
- Organization -> ad account -> campaign / ad set / ad is a nested chain:
  use list_organizations for org_id, list_ad_accounts for the account id, then query with that account id.
- get_report pulls performance, grouped by campaign/ad_set/ad over a date range you choose.
  Use it whenever the user asks "how much did I spend / how is it doing". Each row **already includes name** —
  use it directly, don't call a list tool to match ids. Fields: name/id/cost/revenue/roas/impressions/clicks/
  conversions/cpm/cpc/cpa/ctr/cvr, all pre-formatted strings you can print as-is.
  "N/A" means the platform has no data for it (common before any conversion) — don't report it as 0.
  Prefer tables when reporting.
- When asked about data, always call a tool and use the real numbers — never make them up.
  Summarize the key points in plain language; don't dump raw JSON.
- All ids are long numeric strings — quote them exactly.
- Don't make the user pick an account manually: if there's only one organization/account, just use it.
  Only when there are genuinely several should you list them and ask, then remember the choice for this conversation.

"NEW CAMPAIGN WIZARD": when the user wants to launch a new ad, **only ask about the 4 things they must decide**;
you fill in budget and copy yourself. One question at a time, showing progress ("Step N of 5"):
Step 1 Landing page: the URL to promote (explain: the page that opens when someone clicks the ad);
Step 2 Conversion event: use list_conversion_events to show the account's events and let them pick one
   (explain: it's how the platform counts results; if they're unsure, recommend "submit form" or the first one);
   Don't ask about bidding — the system uses the platform's automatic bidding (MAX_CONVERSION);
Step 3 Creative: ask them to click the 📎 button to the left of the input box and upload an image or video.
   After a successful upload a message with an assetUrl appears automatically — remember that assetUrl and the filename;
Step 4 Naming: ask for an English keyword (e.g. gutter); the name is generated as "keyword-MMDD".
   They can also specify a name manually.
Step 5 **Budget and copy: do NOT ask — set them, show them, and explain why.** List all of these at once:
   · Daily budget $20 — why: the platform minimum is $10, but that's too low to gather data in a few days;
     $20 shows a signal within a day or two without burning much. **And it's created PAUSED — nothing is
     spent until they confirm.**
   · Daily rather than lifetime — why: a daily budget can be stopped any time and won't be spent all at once.
   · Headline and description — draft them from the landing page topic and keyword, then show the exact text.
     Say they were drafted from their landing page and already fit the platform limits
     (headline ≤ 90 chars, description 3-90 chars).
   · Brand name (defaults to the keyword) and call to action (defaults to "Learn More").
   After listing them you MUST say: **"These are defaults I picked for you — tell me if you want any of them
   changed, e.g. 'make the budget 50' or 'change the headline to XXX'."** If they ask for a change, just do it;
   don't argue and don't re-ask the other items.
Once everything is collected, call propose_create_campaign, restate the whole order back as a table,
and wait for confirmation in their NEXT message before calling confirm_action.
After creation: report the three ids, stress that everything is PAUSED (OFF), and tell them to say
"turn on <campaign name>" when they're ready to start delivery.

"WRITE ACTIONS" (pause/resume, create ad) must follow this flow exactly, no shortcuts:
1) First verify the object with a query tool to get the exact id and name (never guess an id from memory);
2) Call propose_status_change / propose_create_campaign to register the pending action, restate what you're about to do,
   and **always include the action_id in your reply**, asking the user to confirm;
3) Only after the user clearly agrees in their NEXT message ("confirm", "yes", "OK") call confirm_action with that id;
   if you've forgotten the id, call list_pending_actions — never re-register the same thing;
   if the user declines or changes their mind, call cancel_action;
4) There is a hard safety interlock: registering and executing within the same user message is blocked. Don't try to work around it;
5) Execution results may ONLY come from confirm_action's return value: quote every id exactly as returned, never invent one;
   if a tool returns an error, say so honestly — never present a failure as a success;
   if you did not call confirm_action, you must never say "created/executed/submitted";
6) Other write actions (budget changes etc.) aren't wired up yet — say those need to be done in the NewsBreak dashboard."""


# ============ 把 NewsBreak 的能力包装成 Gemini 能用的"工具" ============
# 规矩:函数名、参数类型、docstring 会被 Gemini 读懂,它自己决定何时调用;
#       出错时返回 {"error": ...} 而不是抛异常,让大脑能把原因转告用户。

import newsbreak_client as nb  # noqa: E402
import scheduler as sched  # noqa: E402


def list_organizations() -> dict:
    """查询当前账号名下的所有 NewsBreak 组织(organization),返回组织列表(含 id 和名字)。"""
    try:
        return {"organizations": nb.list_organizations()}
    except Exception as e:
        return {"error": str(e)}


def list_ad_accounts(org_id: str) -> dict:
    """查询某个组织(org_id)下的所有广告账户。返回的每项里,`id` 就是广告账户 id,
    可直接用于查计划/报表/建广告;`group_id` 只是外层分组,**不要**当账户 id 用。"""
    try:
        return {"ad_accounts": nb.list_ad_accounts(org_id)}
    except Exception as e:
        return {"error": str(e)}


def list_campaigns(ad_account_id: str, page: int = 1, search: str = "") -> dict:
    """查询某个广告账户(ad_account_id)下的广告计划(campaign)列表。可选:page 翻页,search 按名字搜。"""
    try:
        return nb.list_campaigns(ad_account_id, page=page, search=search)
    except Exception as e:
        return {"error": str(e)}


def list_ad_sets(ad_account_id: str, page: int = 1, search: str = "") -> dict:
    """查询某个广告账户(ad_account_id)下的广告组(ad set)列表。可选:page 翻页,search 按名字搜。"""
    try:
        return nb.list_ad_sets(ad_account_id, page=page, search=search)
    except Exception as e:
        return {"error": str(e)}


def list_ads(ad_account_id: str, page: int = 1, search: str = "") -> dict:
    """查询某个广告账户(ad_account_id)下的广告(ad)列表。可选:page 翻页,search 按名字搜。"""
    try:
        return nb.list_ads(ad_account_id, page=page, search=search)
    except Exception as e:
        return {"error": str(e)}


def list_conversion_events(ad_account_id: str) -> dict:
    """查询账户下的转化事件(conversion events)列表,建新广告第3步用它让用户挑一个。"""
    try:
        return {"events": nb.list_events(ad_account_id)}
    except Exception as e:
        return {"error": str(e)}


def get_report(level: str = "campaign", start_date: str = "", end_date: str = "",
               ad_account_id: str = "") -> dict:
    """查询一段时间的投放数据报表:花费、展示、点击、转化、CPC、CTR 等。

    level 可选 "campaign" / "ad_set" / "ad"(按哪一层汇总);
    start_date / end_date 格式 YYYY-MM-DD,不填默认最近 7 天。
    返回的每行带对象 id,可结合 list_campaigns 等工具把 id 对回名字。
    """
    try:
        return nb.get_report(level, start_date, end_date, ad_account_id)
    except Exception as e:
        return {"error": str(e)}


# ============ 写操作的护栏:先登记 → 隔一条用户消息 → 确认才执行 ============
import uuid  # noqa: E402

_ACTIONS_FILE = Path(__file__).with_name("pending_actions.json")


def _load_actions() -> dict:
    """开机时把保险箱从磁盘捞回来(热重载/重启都不丢待办)。"""
    try:
        if _ACTIONS_FILE.exists():
            return json.loads(_ACTIONS_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_actions() -> None:
    try:
        _ACTIONS_FILE.write_text(json.dumps(PENDING_ACTIONS, ensure_ascii=False, indent=1))
    except Exception as e:
        print(f"[warn] 保险箱落盘失败: {e}", flush=True)


PENDING_ACTIONS: dict[str, dict] = _load_actions()  # 保险箱:登记好、还没执行的动作
# 计数器从"已存待办的最大序号"续起,避免服务器重启后序号归零和旧待办撞车
_REQUEST_SEQ = max((a.get("seq", 0) for a in PENDING_ACTIONS.values()), default=0)
_EXECUTED_THIS_REQUEST: list[str] = []  # 本轮真实执行记录(代码层事实,AI 无法伪造)


def _find_duplicate(candidate: dict) -> str:
    """保险箱里是否已有内容完全相同的待办?有就返回它的编号(比较时忽略序号)。
    防止 AI 忘了编号就反复登记同一件事,陷入"永远在确认"的死循环。"""
    key = {k: v for k, v in candidate.items() if k != "seq"}
    for aid, existing in PENDING_ACTIONS.items():
        if {k: v for k, v in existing.items() if k != "seq"} == key:
            return aid
    return ""


def list_pending_actions() -> dict:
    """查看保险箱:所有已登记、还没执行的待办(含编号 action_id)。
    用户确认后若不记得编号,先用这个查,严禁重新登记同一件事。"""
    return {"pending_actions": [
        {"action_id": aid, **{k: v for k, v in a.items() if k != "seq"}}
        for aid, a in PENDING_ACTIONS.items()
    ] or "保险箱是空的,没有待执行的待办"}


def propose_status_change(level: str, object_id: str, status: str, name: str = "") -> dict:
    """登记一个「开启/暂停」待办(不会立即执行!)。

    level: "campaign"/"ad_set"/"ad";status: "ON"(开启)/"OFF"(暂停);
    name: 对象名字,用于向用户复述。
    登记后必须把将要做的事讲给用户听,等用户在下一条消息里明确同意,
    再用返回的 action_id 调 confirm_action 执行。
    """
    status = status.upper()
    if status not in ("ON", "OFF"):
        return {"error": "status 只能是 ON 或 OFF"}
    if level not in ("campaign", "ad_set", "ad"):
        return {"error": "level 只能是 campaign / ad_set / ad"}
    candidate = {
        "type": "update_status",
        "level": level, "object_id": object_id, "status": status,
        "name": name, "seq": _REQUEST_SEQ,
    }
    dup = _find_duplicate(candidate)
    if dup:
        return {"action_id": dup, "note": f"这件事此前已登记过(编号 {dup}),无需重复登记。"
                                          f"请向用户复述内容并附上编号,用户同意后直接调 confirm_action。"}
    action_id = uuid.uuid4().hex[:8]
    PENDING_ACTIONS[action_id] = candidate
    _save_actions()
    print(f"[write-op] 登记待办 {action_id}: {status} {level} {object_id}", flush=True)
    verb = "开启" if status == "ON" else "暂停"
    return {
        "action_id": action_id,
        "pending": f"{verb} {level}「{name or object_id}」(id={object_id})",
        "note": "已登记待办,尚未执行。请向用户复述并等确认。",
    }


# 建广告的默认值。**改这里就等于改向导的推荐值**,别把数字散写进提示词。
# 每个默认值都要有个说得出口的理由 —— 向导要把理由讲给用户听(见 SYSTEM_PROMPT)。
DEFAULT_BUDGET_DOLLARS = 20.0     # 日预算。平台最低 $10,但太低跑不出量、数据少看不准
DEFAULT_BUDGET_TYPE = "DAILY"     # 日预算比总预算好控:随时能关,不会一次性花光
DEFAULT_CALL_TO_ACTION = "Learn More"   # 最通用的按钮文案,适合大多数落地页


def propose_create_campaign(
    ad_account_id: str,
    keyword: str,
    landing_url: str,
    headline: str,
    description: str,
    asset_url: str,
    budget_dollars: float = 0,   # 不传就用 DEFAULT_BUDGET_DOLLARS
    asset_filename: str = "",
    tracking_id: str = "",
    budget_type: str = DEFAULT_BUDGET_TYPE,
    brand_name: str = "",
    call_to_action: str = DEFAULT_CALL_TO_ACTION,
    campaign_name: str = "",
) -> dict:
    """登记一个「新建广告」待办(不会立即执行!),会一次建好 campaign+ad set+ad 三层。

    必填:ad_account_id(广告账户id)、keyword(英文命名关键词,如 gutter)、
    landing_url(落地页链接)、headline(标题)、description(描述)、
    asset_url(素材地址,来自用户上传后的系统消息)。
    budget_dollars(预算,美元,最低10)**可以不传**:不传就用默认日预算,
    但你必须在复述时告诉用户用的是多少、以及为什么。
    出价无需提供:系统使用平台自动出价(MAX_CONVERSION)。
    tracking_id(转化事件id)可选:不填则自动选用账户里的 submit form 或第一个事件。
    可选:asset_filename(素材文件名,用于判断图片/视频)、budget_type(DAILY日预算/TOTAL总预算)、
    brand_name(品牌名,默认用关键词)、call_to_action(按钮文案)、campaign_name(手动指定计划名)。
    登记后必须用表格向用户完整复述整单,等用户下一条消息确认后再 confirm_action。
    """
    # ---- 参数体检:把明显的问题挡在登记之前 ----
    # 没给预算就用默认值(向导会把这个默认值和理由讲给用户听,用户能改)
    used_default_budget = False
    if not budget_dollars or budget_dollars <= 0:
        budget_dollars = DEFAULT_BUDGET_DOLLARS
        used_default_budget = True

    problems = []
    if not landing_url.startswith("http"):
        problems.append("landing_url 必须是 http(s) 开头的完整链接")
    if not asset_url.startswith("http"):
        problems.append("asset_url 无效:请先让用户点 📎 上传素材")
    if budget_dollars < 10:
        problems.append("预算最低 $10")
    if not 3 <= len(description) <= 90:
        problems.append(f"广告描述必须 3~90 个字符(现在 {len(description)} 个),请精简")
    if not 3 <= len(headline) <= 90:
        problems.append(f"广告标题必须 3~90 个字符(现在 {len(headline)} 个)")
    if budget_type not in ("DAILY", "TOTAL"):
        problems.append("budget_type 只能是 DAILY 或 TOTAL")
    if not keyword.strip():
        problems.append("需要一个命名关键词(英文,如 gutter)")
    if problems:
        return {"error": ";".join(problems)}

    # ---- 自动命名:关键词-月日(用户手动指定则优先) ----
    from datetime import datetime, timezone
    mmdd = datetime.now(timezone.utc).strftime("%m%d")
    c_name = campaign_name.strip() or f"{keyword.strip()}-{mmdd}"

    candidate = {
        "type": "create_campaign",
        "ad_account_id": ad_account_id,
        "campaign_name": c_name,
        "ad_set_name": f"{c_name}-set1",
        "ad_name": f"1-{c_name}",
        "landing_url": landing_url,
        "budget_type": budget_type,
        "budget_cents": int(round(budget_dollars * 100)),
        "tracking_id": tracking_id,
        "headline": headline,
        "description": description,
        "brand_name": (brand_name.strip() or keyword.strip().capitalize())[:40],
        "call_to_action": call_to_action or "Learn More",
        "asset_url": asset_url,
        "asset_filename": asset_filename,
        "seq": _REQUEST_SEQ,
    }
    dup = _find_duplicate(candidate)
    if dup:
        return {"action_id": dup, "note": f"这单建广告此前已登记过(编号 {dup}),无需重复登记。"
                                          f"请向用户复述整单内容并附上编号,用户同意后直接调 confirm_action。"}
    action_id = uuid.uuid4().hex[:8]
    PENDING_ACTIONS[action_id] = candidate
    _save_actions()
    print(f"[write-op] 登记建广告待办 {action_id}: {candidate['campaign_name']}", flush=True)
    a = PENDING_ACTIONS[action_id]
    budget_word = "日预算" if budget_type == "DAILY" else "总预算"
    return {
        "action_id": action_id,
        "pending": {
            "计划名": a["campaign_name"], "广告组名": a["ad_set_name"], "广告名": a["ad_name"],
            "落地页": landing_url,
            budget_word: f"${budget_dollars:g}" + ("(系统默认值)" if used_default_budget else ""),
            "出价": "自动(MAX_CONVERSION)",
            "转化事件": tracking_id or "自动选用(优先 submit form)",
            "标题": headline, "描述": description, "品牌名": a["brand_name"],
            "按钮": a["call_to_action"], "素材": asset_filename or asset_url,
            "创建后状态": "暂停(OFF),需用户确认无误后再开启",
        },
        # 哪些值是系统替用户定的,要明确列出来 —— 用户有权知道"我没说过的东西是谁定的"
        "defaults_used": ([f"{budget_word} ${budget_dollars:g}"] if used_default_budget else [])
                         + ([] if tracking_id else ["转化事件(自动选 submit form)"]),
        "note": ("已登记待办,尚未执行。请用表格向用户完整复述以上内容并等确认。"
                 "**凡是 defaults_used 里列出的项,要额外说明这是系统默认值、为什么这么定、"
                 "以及用户可以直接说要改成多少。**"),
    }


def _execute_create_campaign(a: dict) -> dict:
    """真正执行三层创建。任何一层失败都如实汇报已建成的部分(都处于暂停态,无风险)。"""
    created: dict = {}

    def _pick_id(data: dict, *keys: str) -> str:
        for k in keys:
            v = data.get(k)
            if v not in (None, ""):
                return str(v)
        nested = data.get("object")
        if isinstance(nested, dict):
            return _pick_id(nested, *keys)
        return ""

    # 第一层:campaign(幂等:同名的已存在就直接复用,支持"上次建到一半"接着建)
    campaign_id = ""
    try:
        existing = nb.list_campaigns(a["ad_account_id"], search=a["campaign_name"])
        campaign_id = next((str(i.get("id")) for i in existing.get("items") or []
                            if str(i.get("name") or "") == a["campaign_name"]), "")
    except Exception:
        pass
    if campaign_id:
        created["campaign"] = {"id": campaign_id, "name": a["campaign_name"], "note": "同名计划已存在,直接复用"}
    else:
        camp = nb.create_campaign(a["ad_account_id"], a["campaign_name"], status="OFF")
        campaign_id = _pick_id(camp, "id", "campaignId")
        if not campaign_id:
            return {"error": "NewsBreak 未返回 campaign id", "raw": camp}
        created["campaign"] = {"id": campaign_id, "name": a["campaign_name"]}

    # 转化事件:指定了就用指定的;没指定就自动挑(优先 submit_form,其次第一个)
    tracking_id = str(a.get("tracking_id") or "")
    if not tracking_id:
        events = nb.list_events(a["ad_account_id"])
        if not events:
            return {"error": "账户下没有任何转化事件,无法创建\"网页转化\"广告组。请先在后台 Tools → Event management 创建一个",
                    "created_so_far": created}
        chosen = next((e for e in events if e.get("eventType") == "submit_form"), events[0])
        tracking_id = str(chosen.get("id") or "")
        created["conversion_event"] = f"自动选用「{chosen.get('name')}」(id={tracking_id});如需换事件请告知"

    # 第二层:ad set
    try:
        adset = nb.create_ad_set(
            campaign_id, a["ad_set_name"], a["budget_type"],
            a["budget_cents"], tracking_id, status="OFF",
        )
        ad_set_id = _pick_id(adset, "id", "adSetId")
        created["ad_set"] = {"id": ad_set_id, "name": a["ad_set_name"]}
    except Exception as e:
        return {"error": f"广告组创建失败: {e}", "created_so_far": created,
                "note": "已建的部分处于暂停状态,可让用户决定重试或删除"}
    if not ad_set_id:
        return {"error": "NewsBreak 未返回 ad set id", "created_so_far": created}

    # 第三层:ad(带创意)
    creative = {
        # 类型优先用上传时记下的(权威),查不到才退回按文件名猜
        "type": _ASSET_TYPES.get(a["asset_url"]) or nb.creative_type_of(a.get("asset_filename", ""), ""),
        "headline": a["headline"],
        "description": a["description"],
        "callToAction": a["call_to_action"],
        "brandName": a["brand_name"],
        "assetUrl": a["asset_url"],
        "clickThroughUrl": a["landing_url"],
    }
    try:
        ad = nb.create_ad(ad_set_id, a["ad_name"], creative, status="OFF")
        created["ad"] = {"id": _pick_id(ad, "id", "adId"), "name": a["ad_name"]}
    except Exception as e:
        return {"error": f"广告创建失败: {e}", "created_so_far": created,
                "note": "campaign 和 ad set 已建好(暂停态),可修正素材/文案后单独补建广告"}

    # 建完不轻信:立刻去平台回查一遍,确认真的存在
    try:
        check = nb.list_campaigns(a["ad_account_id"], search=a["campaign_name"])
        found = any(str(item.get("name") or "") == a["campaign_name"] for item in check.get("items") or [])
        created["platform_verified"] = "已回查平台,确认存在 ✅" if found else "⚠️ 回查平台未找到,请人工核实"
    except Exception:
        created["platform_verified"] = "(回查失败,请人工核实)"

    return {"done": True, "created": created,
            "note": "三层已全部创建,均为暂停(OFF)状态;提醒用户核对后说「开启 xxx」即可开始投放"}


def confirm_action(action_id: str) -> dict:
    """执行之前登记的待办。只能在用户于新消息中明确同意后调用。"""
    action = PENDING_ACTIONS.get(action_id)
    if not action:
        return {"error": f"找不到待办 {action_id}(可能已执行/已取消,或 id 有误)"}
    if action["seq"] == _REQUEST_SEQ:
        # 保险丝:登记和执行发生在同一条用户消息里 → 物理拦截
        return {"error": f"保险丝拦截:登记和执行不能在同一条用户消息里完成。待办 {action_id} 已登记好,"
                         f"不要重新登记!请向用户复述内容并附上编号 {action_id},"
                         f"等用户下一条消息同意后,直接调用 confirm_action(action_id='{action_id}')。"}
    try:
        print(f"[write-op] 开始执行待办 {action_id}: {action.get('type')}", flush=True)
        if action.get("type") == "schedule":
            r = sched.add_task({k: v for k, v in action.items() if k not in ("type", "seq")})
            result = {"error": r["error"]} if r.get("error") else {
                "done": True, "detail": f"定时任务已生效(编号 {r['task_id']}),首次执行:{r.get('next_run', '-')}"}
        elif action.get("type") == "create_campaign":
            result = _execute_create_campaign(action)
        else:
            result = {"done": True, "detail": nb.update_status(action["level"], action["object_id"], action["status"])}
        print(f"[write-op] 待办 {action_id} 结果: {json.dumps(result, ensure_ascii=False)[:300]}", flush=True)

        # 把"真实发生了什么"记入代码层台账(聊天回复会盖'系统核验'钢印)
        if result.get("done"):
            detail = json.dumps(result.get("created") or result.get("detail") or "", ensure_ascii=False)[:220]
            _EXECUTED_THIS_REQUEST.append({"id": action_id, "ok": True, "detail": detail})
            PENDING_ACTIONS.pop(action_id, None)
            _save_actions()
        else:
            _EXECUTED_THIS_REQUEST.append({"id": action_id, "ok": False, "detail": str(result.get("error"))[:220]})
        return {"executed": {k: v for k, v in action.items() if k != "seq"}, **(result if isinstance(result, dict) else {"detail": result})}
    except Exception as e:
        print(f"[write-op] 待办 {action_id} 异常: {e}", flush=True)
        _EXECUTED_THIS_REQUEST.append({"id": action_id, "ok": False, "detail": str(e)[:220]})
        return {"error": str(e)}


def propose_schedule(kind: str, when: str, level: str, object_id: str,
                     status: str, name: str = "") -> dict:
    """登记一个「定时开启/暂停广告」待办(不会立即生效!需用户确认)。

    kind: "once"(只执行一次)或 "daily"(每天重复);
    when: once 用 "YYYY-MM-DD HH:MM",daily 用 "HH:MM"——**都按北京时间**;
    level: "campaign"/"ad_set"/"ad";status: "ON"(到点开启)/"OFF"(到点暂停);
    name: 对象名字,用于向用户复述。
    登记后必须向用户复述"什么时间、对哪个对象、做什么",并附上待办编号,
    等用户下一条消息确认后再 confirm_action。
    """
    status = status.upper()
    if status not in ("ON", "OFF"):
        return {"error": "status 只能是 ON(开启)或 OFF(暂停)"}
    if level not in ("campaign", "ad_set", "ad"):
        return {"error": "level 只能是 campaign / ad_set / ad"}
    if kind not in ("once", "daily"):
        return {"error": 'kind 只能是 once(执行一次)或 daily(每天重复)'}
    # 先把时间校验一遍,格式不对就当场告诉用户,别等确认后才发现
    parsed, err = sched.parse_when(kind, when)
    if not parsed:
        return {"error": err}

    candidate = {
        "type": "schedule",
        "kind": kind, "when": when, "level": level,
        "object_id": object_id, "status": status, "name": name,
        # 记下是谁定的:到点时看表线程要用他自己的凭据去执行
        "user_id": CURRENT_USER_ID.get(),
        "seq": _REQUEST_SEQ,
    }
    dup = _find_duplicate(candidate)
    if dup:
        return {"action_id": dup, "note": f"这件事此前已登记过(编号 {dup}),无需重复登记。"
                                          f"请向用户复述并附上编号,用户同意后直接调 confirm_action。"}
    action_id = uuid.uuid4().hex[:8]
    PENDING_ACTIONS[action_id] = candidate
    _save_actions()
    verb = "开启" if status == "ON" else "暂停"
    when_desc = f"每天 {when}(北京时间)" if kind == "daily" else sched.both_times(parsed)
    print(f"[write-op] 登记定时任务待办 {action_id}: {when_desc} {verb} {name or object_id}", flush=True)
    return {
        "action_id": action_id,
        "pending": f"定时{verb}:{when_desc} → {level}「{name or object_id}」",
        "first_run": sched.both_times(parsed),
        "note": "已登记待办,尚未生效。请向用户复述时间和动作(带上编号)并等确认。"
                "另外要提醒用户:定时任务依赖本服务持续运行,服务停了就不会触发。",
    }


def list_schedules() -> dict:
    """查看所有定时任务(含下次执行时间、上次执行结果)。"""
    tasks = sched.list_tasks()
    return {"schedules": tasks or "还没有任何定时任务",
            "now": sched.both_times(sched.now_beijing())}


def cancel_schedule(task_id: str) -> dict:
    """取消一个已生效的定时任务(用 list_schedules 查到的 task_id)。"""
    return sched.cancel_task(task_id)


def cancel_action(action_id: str) -> dict:
    """取消之前登记的待办(用户不同意或改主意时调用)。"""
    removed = PENDING_ACTIONS.pop(action_id, None)
    _save_actions()
    print(f"[write-op] 取消待办 {action_id}: {'成功' if removed else '不存在'}", flush=True)
    return {"cancelled": removed is not None, "action_id": action_id}


# 工具清单:递给 Gemini,它会自动挑选、自动执行、自动把结果编进回答
NEWSBREAK_TOOLS = [
    list_organizations, list_ad_accounts, list_campaigns, list_ad_sets, list_ads, get_report,
    propose_status_change, propose_create_campaign, confirm_action, cancel_action,
    list_pending_actions, list_conversion_events,
    propose_schedule, list_schedules, cancel_schedule,
]


class ChatMessage(BaseModel):
    role: str      # "user"(用户说的) 或 "assistant"(助手说的)
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]  # 完整的聊天记录(API 不记事,每次都要全量发)
    lang: str = "zh"             # 界面语言:"zh" 中文 / "en" 英文,决定 AI 用哪种语言回答


@app.get("/")
def index():
    """把聊天页面端给浏览器。"""
    return FileResponse(Path(__file__).with_name("static") / "index.html")


# ============ 素材上传中转:浏览器 → 这里 → NewsBreak ============
from fastapi import File, UploadFile  # noqa: E402

_CACHED_ACCOUNT_ID: dict = {}    # {token前12位: 账户id} —— 按 token 分桶,不同用户不串号
# 素材地址 → 素材类型(IMAGE/GIF/VIDEO),上传时记下,建广告时查回
_ASSET_TYPES: dict[str, str] = {}


def _all_ad_accounts() -> list[dict]:
    """列出名下所有组织的所有广告账户(nb.list_ad_accounts 已拍平,直接用 id)。"""
    accounts = []
    for org in nb.list_organizations():
        org_id = str(org.get("id") or org.get("orgId") or "")
        if org_id:
            accounts.extend(nb.list_ad_accounts(org_id))
    return [a for a in accounts if a.get("id")]


def _default_ad_account_id() -> str:
    """确定用哪个广告账户。

    优先级:①用户在页面上选定的账户(.env 的 NEWSBREAK_AD_ACCOUNT_ID)→ ②名下唯一的账户。
    有多个账户又没选过时**明确报错**,而不是默默挑第一个——
    静默挑错账户会把素材传进别人的账户,属于"不报错但结果全错"的坑。
    """
    global _CACHED_ACCOUNT_ID
    picked = nb.current_account_id() or _read_env_value("NEWSBREAK_AD_ACCOUNT_ID")
    if picked:
        return picked
    # 缓存按 token 分桶:不同用户的 token 不同,不会串号
    key = ""
    try:
        key = nb._token()[:12]
    except Exception:
        pass
    if key and isinstance(_CACHED_ACCOUNT_ID, dict) and _CACHED_ACCOUNT_ID.get(key):
        return _CACHED_ACCOUNT_ID[key]
    accounts = _all_ad_accounts()
    if not accounts:
        raise nb.NewsBreakError("名下没有任何广告账户")
    if len(accounts) > 1:
        listed = "、".join(f"{a['name']}(id={a['id']})" for a in accounts)
        raise nb.NewsBreakError(f"你名下有 {len(accounts)} 个广告账户,请在页面顶栏「🔗 账户」里选一个,或直接告诉我用哪个:{listed}")
    if not isinstance(_CACHED_ACCOUNT_ID, dict):
        _CACHED_ACCOUNT_ID = {}
    if key:
        _CACHED_ACCOUNT_ID[key] = accounts[0]["id"]
    return accounts[0]["id"]


# ============ 广告账户接入(页面顶栏「🔗 账户」用的接口)============

def _write_env_value(key: str, value: str) -> None:
    """把某个键写进 .env(已存在就改值,不存在就追加),保留原有注释和排版。"""
    env_path = Path(__file__).with_name(".env")
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    hit = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            hit = True
            break
    if not hit:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")
    os.environ[key] = value          # 同步进当前进程,立即生效
    global _CACHED_ACCOUNT_ID
    _CACHED_ACCOUNT_ID = {}          # 账户可能变了,清掉缓存


def _mask(token: str) -> str:
    """只回显掩码,绝不把完整 token 送回浏览器。"""
    if not token:
        return ""
    return token[:4] + "•" * 8 + token[-4:] if len(token) > 10 else "•" * 8


def _account_state() -> dict:
    """当前接入状态:有没有 token、连到了哪个组织/账户、当前用哪个。

    **只看当前登录用户自己绑的那把钥匙**。以前这里读 `.env`,
    结果没绑账号的人也会看到公用 token 的掩码 —— 那是别人的东西,不该露给他。
    """
    cur = nb.CURRENT_CREDS.get()
    if cur is not None:
        token = (cur.get("token") or "")
    else:
        # 不在用户上下文(命令行/测试)才回落到 .env
        token = _read_env_value("NEWSBREAK_ACCESS_TOKEN") or os.environ.get("NEWSBREAK_ACCESS_TOKEN", "")
    if not token.strip():
        return {"connected": False, "token_masked": "", "organizations": [], "accounts": [], "active_account_id": ""}
    try:
        orgs = nb.list_organizations()
        accounts = _all_ad_accounts()
    except Exception as e:
        # token 填了但用不了(过期/无效/网络问题)——如实告诉用户
        return {"connected": False, "token_masked": _mask(token), "organizations": [], "accounts": [],
                "active_account_id": "", "error": str(e)}
    active = nb.current_account_id() or _read_env_value("NEWSBREAK_AD_ACCOUNT_ID")
    if not active and len(accounts) == 1:
        active = accounts[0]["id"]
    return {
        "connected": True,
        "token_masked": _mask(token),
        "organizations": [{"id": str(o.get("id") or o.get("orgId") or ""), "name": o.get("name") or ""} for o in orgs],
        "accounts": [{"id": a["id"], "name": a.get("name") or a["id"]} for a in accounts],
        "active_account_id": active,
    }


# ============ 数据大屏(仪表盘)============

def _sum_kpi(rows: list[dict]) -> dict:
    """把若干行汇总成总计。比率类**必须由总量重算**,不能把各行的比率平均——
    那样小花费的行会和大花费的行等权,算出来的 CTR 是错的。"""
    cost = sum(r["cost"] for r in rows)
    revenue = sum(r["revenue"] for r in rows)
    imp = sum(r["impressions"] for r in rows)
    clicks = sum(r["clicks"] for r in rows)
    conv = sum(r["conversions"] for r in rows)
    return {
        "cost": round(cost, 2),
        "revenue": round(revenue, 2),
        "impressions": imp,
        "clicks": clicks,
        "conversions": conv,
        "ctr": round(clicks / imp * 100, 2) if imp else 0.0,
        "cvr": round(conv / clicks * 100, 2) if clicks else 0.0,
        "cpc": round(cost / clicks, 2) if clicks else 0.0,
        "cpm": round(cost / imp * 1000, 2) if imp else 0.0,
        "cpa": round(cost / conv, 2) if conv else None,      # None = 还没有转化,不是 0
        "roas": round(revenue / cost, 2) if cost else None,
    }


@app.get("/api/dashboard")
def dashboard_data(days: int = 30):
    """仪表盘用的全部数据:总计 KPI、环比、按天趋势、三个层级的明细。"""
    load_env_file()
    from datetime import datetime, timedelta, timezone

    try:
        days = max(1, min(int(days), 180))         # 平台报表上限 180 天
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=days - 1)
        prev_start = start - timedelta(days=days)   # 上一个等长周期,用来算环比
        prev_end = start - timedelta(days=1)
        fmt = "%Y-%m-%d"
        account_id = ""
        try:
            account_id = _default_ad_account_id()
        except Exception:
            pass                                    # 多账户没选时不阻塞,报表按 token 全量查

        campaigns = nb.get_report_raw("campaign", start.strftime(fmt), today.strftime(fmt), account_id)
        prev_rows = nb.get_report_raw("campaign", prev_start.strftime(fmt), prev_end.strftime(fmt), account_id)

        # 趋势:DATE 维度平台限 31 天,超了就只取最近 31 天(并如实告诉前端)
        trend_days = min(days, 31)
        trend_start = today - timedelta(days=trend_days - 1)
        try:
            trend = nb.get_daily_raw(trend_start.strftime(fmt), today.strftime(fmt), account_id)
        except Exception as e:
            trend, trend_days = [], 0
            print(f"[dashboard] 趋势数据获取失败: {e}", flush=True)

        return {
            "range": {"start": start.strftime(fmt), "end": today.strftime(fmt), "days": days},
            "kpi": _sum_kpi(campaigns),
            "kpi_prev": _sum_kpi(prev_rows),
            "trend": sorted(trend, key=lambda r: r["name"]),
            "trend_days": trend_days,
            "trend_capped": days > 31,              # 前端据此说明"趋势只显示最近31天"
            "campaigns": sorted(campaigns, key=lambda r: r["cost"], reverse=True),
            "ad_sets": sorted(nb.get_report_raw("ad_set", start.strftime(fmt), today.strftime(fmt), account_id),
                              key=lambda r: r["cost"], reverse=True),
            "ads": sorted(nb.get_report_raw("ad", start.strftime(fmt), today.strftime(fmt), account_id),
                          key=lambda r: r["cost"], reverse=True),
        }
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


class AnalyzeIn(BaseModel):
    days: int = 30
    lang: str = "zh"


@app.post("/api/analyze")
def analyze_data(body: AnalyzeIn):
    """把仪表盘上的真实数据交给 AI,让它做诊断并给优化建议。"""
    load_env_file()
    data = dashboard_data(body.days)
    if isinstance(data, JSONResponse):
        return data
    if not data["campaigns"]:
        msg = ("这段时间没有任何投放数据,没什么可分析的。可以把时间范围放宽,或先把广告开起来跑几天。"
               if body.lang != "en" else
               "No delivery data in this period — nothing to analyse yet. Widen the date range, or start a campaign and let it run a few days.")
        return {"analysis": msg}

    zh = body.lang != "en"
    # 数据部分两种语言共用(都是 JSON,不用翻)
    payload = f"""[TOTALS] {json.dumps(data['kpi'], ensure_ascii=False)}
[PREVIOUS PERIOD, same length] {json.dumps(data['kpi_prev'], ensure_ascii=False)}
[BY CAMPAIGN] {json.dumps(data['campaigns'][:15], ensure_ascii=False)}
[BY AD SET] {json.dumps(data['ad_sets'][:15], ensure_ascii=False)}
[BY AD] {json.dumps(data['ads'][:20], ensure_ascii=False)}
[DAILY TREND] {json.dumps(data['trend'][:31], ensure_ascii=False)}"""

    if zh:
        prompt = f"""你是资深的信息流广告优化师。下面是这个 NewsBreak 广告账户最近 {data['range']['days']} 天的真实投放数据。
请做一次体检式分析,面向**完全不懂投放的新手**,用大白话。

{payload}

字段说明:cost花费(美元) revenue收入 impressions展示 clicks点击 conversions转化
ctr点击率(%) cvr转化率(%) cpc单次点击成本 cpm千次展示成本 cpa单次转化成本(null=还没转化) roas投产比

请按这四段输出(用 Markdown,可以用表格,但别太长):
## 一句话结论
整体健康度如何,一句话说清。
## 看点(2~4条)
表现好的地方,点名具体是哪条计划/广告,附数据。
## 问题(2~4条)
花钱多但没效果的、CTR 或转化异常的,点名并附数据。**要指出具体是哪一条**。
## 建议怎么做(3~5条,按优先级)
每条都要**具体可执行**:比如"暂停 xxx 广告(花了$X没转化)"、"把 xxx 的预算从$A调到$B"。
如果数据太少不足以下结论,就直说"数据量还不够,建议先跑够 N 天/N 次点击再看",不要硬编结论。

注意:不要编造数据里没有的数字;金额带 $ 符号;不确定的地方要说明。"""
    else:
        # 教训(见 CLAUDE.md):只在中文提示词末尾加一句 "reply in English" 不够牢 ——
        # 满篇中文会把模型带跑。英文模式必须**整段提示词都用英文**,并把语言规则放在最前面。
        prompt = f"""LANGUAGE RULE (absolute, overrides everything else): write your entire answer in **English only**.
This holds even if the data contains Chinese names and even if you were previously answering in Chinese.

You are a senior performance-marketing analyst. Below is {data['range']['days']} days of real
delivery data from this NewsBreak ad account. Review it for someone who is **completely new to
ad buying** — plain English, no jargon without a one-line explanation.

{payload}

Field guide: cost=spend(USD) revenue impressions clicks conversions
ctr=click-through rate(%) cvr=conversion rate(%) cpc=cost per click cpm=cost per 1000 impressions
cpa=cost per conversion (null = no conversions yet) roas=return on ad spend

Answer in exactly these four sections (Markdown; tables allowed, keep it tight):
## Bottom line
Overall health in one sentence.
## What is working (2-4 points)
Name the specific campaign/ad and quote its numbers.
## Problems (2-4 points)
What is burning money with nothing to show, or has an abnormal CTR/conversion rate.
**Name the specific campaign or ad** and quote its numbers.
## What to do (3-5 points, highest priority first)
Each must be **concrete and actionable**, e.g. "Pause ad X ($45 spent, 0 conversions)",
"Raise the budget on Y from $A to $B".
If there is not enough data to conclude anything, say so plainly ("not enough data yet — let it
run N more days / until N clicks") rather than inventing a conclusion.

Rules: never invent numbers that are not in the data; prefix money with $; flag anything uncertain."""

    msgs = [ChatMessage(role="user", content=prompt)]
    try:
        brain = os.environ.get("BRAIN", "auto").strip().lower()
        if brain == "openai" and os.environ.get("OPENAI_API_KEY"):
            return {"analysis": ask_openai(msgs, body.lang)}
        if os.environ.get("GEMINI_API_KEY"):
            try:
                return {"analysis": ask_gemini(msgs, body.lang)}
            except genai_errors.APIError as e:
                if e.code in _RETRYABLE_CODES and os.environ.get("OPENAI_API_KEY"):
                    return {"analysis": ask_openai(msgs, body.lang)}
                raise
        if os.environ.get("OPENAI_API_KEY"):
            return {"analysis": ask_openai(msgs, body.lang)}
        return JSONResponse(status_code=500, content={"error": "还没配置 AI 大脑的钥匙"})
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"AI 分析失败:{e}"})


@app.get("/dashboard")
def dashboard_page():
    """数据大屏页面。"""
    return FileResponse(Path(__file__).with_name("static") / "dashboard.html")


# ============ 登录 / 注册 / 每个账号的聊天记录 ============

def _current_user(request: Request) -> dict | None:
    return acc.session_user(request.cookies.get(SESSION_COOKIE, ""))


@app.get("/login")
def login_page():
    """登录页(不需要登录就能看)。"""
    return FileResponse(Path(__file__).with_name("static") / "login.html")


@app.get("/platforms")
def platforms_page():
    """平台选择页:登录后先到这里挑一个投放平台。"""
    return FileResponse(Path(__file__).with_name("static") / "platforms.html")


@app.get("/api/platforms")
def list_platforms(request: Request):
    """有哪些平台可选、**当前这个人**各自绑没绑账号。"""
    load_env_file()
    user = _current_user(request)
    return {"platforms": plat.public_list(user["id"] if user else ""),
            "default": plat.DEFAULT_ID}


@app.get("/api/auth-status")
def auth_status(request: Request):
    """登录页用:问问现在是什么状态(有没有账号、要不要邀请码、是不是已登录)。"""
    return {
        "has_users": acc.user_count() > 0,
        "invite_required": bool(_read_env_value("APP_PASSWORD")),
        "logged_in_as": (_current_user(request) or {}).get("username", ""),
    }


class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    username: str
    password: str
    invite: str = ""


def _set_session_cookie(resp, token: str, request: Request) -> None:
    """下发登录 cookie。

    · httponly:网页里的 JS 读不到它,防止被脚本偷走;
    · samesite=lax:别的网站发起的请求不会自动带上它;
    · secure:**只在 https 下才加**。加了之后浏览器绝不会用明文 http 发送这个 cookie,
      中间人抓不到登录态。之所以要判断而不是写死 True:本地开发走的是
      http://localhost,写死会导致浏览器根本不存这个 cookie,直接登不进去。

    判断依据是 `request.url.scheme`。放在 nginx 后面时,它来自
    `X-Forwarded-Proto` 请求头 —— 所以 nginx 里那行
    `proxy_set_header X-Forwarded-Proto $scheme;` 不能少;缺了只是退回不加 secure
    (跟以前一样),不会把人挡在门外。
    """
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    secure=(request.url.scheme == "https"),
                    max_age=acc.SESSION_DAYS * 86400, path="/")


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    user = acc.verify_user(body.username, body.password)
    if user.get("error"):
        return JSONResponse(status_code=401, content={"error": user["error"]})
    token = acc.create_session(user)
    resp = JSONResponse(content={"username": user["username"]})
    _set_session_cookie(resp, token, request)
    print(f"[auth] 登录: {user['username']}", flush=True)
    return resp


@app.post("/api/register")
def register(body: RegisterIn, request: Request):
    """注册。若 .env 里设了 APP_PASSWORD,它就是「邀请码」,防止端口泄露后被随意注册。"""
    invite_needed = _read_env_value("APP_PASSWORD")
    # 比较前先转成 bytes:compare_digest 不支持非 ASCII 字符串,
    # 用户在邀请码栏敲个中文就会抛 TypeError → 500,看到的是「Internal Server Error」
    # 而不是「邀请码不对」。转成 bytes 后任何字符都能比,而且仍是定时安全的。
    if invite_needed and not secrets.compare_digest(
            (body.invite or "").strip().encode("utf-8"), invite_needed.encode("utf-8")):
        return JSONResponse(status_code=403, content={"error": "邀请码不对(问管理员要 .env 里的 APP_PASSWORD)"})

    created = acc.create_user(body.username, body.password)
    if created.get("error"):
        return JSONResponse(status_code=400, content={"error": created["error"]})
    token = acc.create_session(created)
    resp = JSONResponse(content={"username": created["username"]})
    _set_session_cookie(resp, token, request)
    return resp


@app.post("/api/logout")
def logout(request: Request):
    acc.destroy_session(request.cookies.get(SESSION_COOKIE, ""))
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
def me(request: Request):
    user = _current_user(request)
    return {"username": user["username"]} if user else {"username": ""}


class ChatsIn(BaseModel):
    conversations: list


@app.get("/api/chats")
def get_chats(request: Request):
    """取当前账号的聊天记录(别的账号看不到)。"""
    user = _current_user(request)
    if not user:
        return {"conversations": []}          # 没启用登录时:前端自己用本地存储
    return {"conversations": acc.load_chats(user["id"])}


@app.put("/api/chats")
def put_chats(request: Request, body: ChatsIn):
    """存当前账号的聊天记录。"""
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "请先登录"})
    return acc.save_chats(user["id"], body.conversations)


@app.get("/api/account")
def get_account():
    """查询当前广告账户接入状态。"""
    load_env_file()
    try:
        return _account_state()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


class TokenIn(BaseModel):
    token: str


@app.post("/api/account/token")
def set_account_token(body: TokenIn, request: Request):
    """保存**当前登录用户自己**的 NewsBreak Access Token。先验证再存,无效的不会写进去。"""
    token = (body.token or "").strip()
    if not token:
        return JSONResponse(status_code=400, content={"error": "请先粘贴 Access Token"})

    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "请先登录"})

    # 拿这个 token 临时试一次:能查到组织才算有效。验证失败就原样退回,
    # 用户原来那把好钥匙纹丝不动。
    old = nb.CURRENT_CREDS.get()
    nb.CURRENT_CREDS.set({"token": token})
    try:
        nb.list_organizations()
    except Exception as e:
        nb.CURRENT_CREDS.set(old)
        return JSONResponse(status_code=400, content={
            "error": f"这个 Access Token 用不了:{e}。请确认是从 NewsBreak Ad Manager → Resources → API Access Tokens 生成的"})

    # 换了 token,之前选的账户作废(那是上一把钥匙下的账户)
    acc.set_creds(user["id"], "newsbreak", token=token, account_id="")
    nb.CURRENT_CREDS.set({"token": token, "account_id": ""})
    print(f"[account] {user['username']} 已绑定自己的 NewsBreak Token 并验证通过", flush=True)
    return _account_state()


class AccountIn(BaseModel):
    account_id: str


@app.post("/api/account/select")
def select_account(body: AccountIn, request: Request):
    """选定当前要操作的广告账户(多账户时用)。也是按人存的。"""
    account_id = (body.account_id or "").strip()
    user = _current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "请先登录"})
    try:
        valid = {a["id"] for a in _all_ad_accounts()}
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
    if account_id and account_id not in valid:
        return JSONResponse(status_code=400, content={"error": "这个账户不在你名下,换一个"})
    acc.set_creds(user["id"], "newsbreak", account_id=account_id)
    cur = dict(nb.CURRENT_CREDS.get() or {})
    cur["account_id"] = account_id
    nb.CURRENT_CREDS.set(cur)
    print(f"[account] {user['username']} 的当前广告账户切换为 {account_id or '(自动)'}", flush=True)
    return _account_state()


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """收下浏览器传来的图片/视频,转手上传给 NewsBreak,换回素材地址。"""
    load_env_file()
    try:
        content = await file.read()
        if len(content) > 100 * 1024 * 1024:
            return JSONResponse(status_code=400, content={"error": "文件太大(超过 100MB),请压缩后再传"})

        account_id = _default_ad_account_id()
        filename = file.filename or "asset"
        # 素材库里的名字加时间戳,保证独一无二——从根上避免同名 409 冲突
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).strftime("%m%d-%H%M%S")
        unique_name = f"{stamp}-{filename}"

        try:
            data = nb.upload_asset(account_id, filename, content,
                                   file.content_type or "", media_name=unique_name)
        except nb.NewsBreakError as e:
            # 还是撞车(比如平台按文件内容查重)→ 降级为"不存素材库直接用",不影响建广告
            if "409" in str(e) or "exist" in str(e).lower() or "conflict" in str(e).lower() or "重复" in str(e):
                data = nb.upload_asset(account_id, filename, content,
                                       file.content_type or "", save_to_library=False)
            else:
                raise

        asset_url = str(data.get("assetUrl") or data.get("url") or "")
        if not asset_url:
            return JSONResponse(status_code=502, content={"error": f"NewsBreak 未返回素材地址:{data}"})

        # 记下这个素材的真实类型(浏览器给的 MIME 最权威)。
        # 建广告时按 assetUrl 查回来,不用指望 AI 把类型传对,也不怕文件名没后缀。
        _ASSET_TYPES[asset_url] = nb.creative_type_of(filename, file.content_type or "")
        return {"asset_url": asset_url, "filename": filename}

    except nb.NewsBreakError as e:
        # 把平台给的原因原样带给用户,并附上常见对策
        return JSONResponse(status_code=502, content={
            "error": f"NewsBreak 拒绝了素材:{e}。可尝试:换一张图/改个文件名/稍后重试"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"上传过程出错:{e}"})


def _system_prompt_now(lang: str = "zh") -> str:
    """人设(按语言选)+ 今天的真实日期 + 保险箱现状。

    为什么每轮都要注入日期和保险箱:AI 自己不知道今天几号,也记不住跨轮的待办编号。
    """
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    english = str(lang).lower().startswith("en")

    if english:
        prompt = SYSTEM_PROMPT_EN + f"\n\nToday's date (UTC) is {today}; use it when working out ranges like \"the last N days\"."
    else:
        prompt = SYSTEM_PROMPT + f"\n\n今天的日期(UTC)是 {today},计算\"最近N天\"等日期范围时以此为准。"

    if sched.recent_runs:
        prompt += ("\n\n【定时任务最近的执行结果】(代码层记录,若用户还不知道,主动告知一句):\n"
                   + "\n".join(f"- {m}" for m in sched.recent_runs))
    if PENDING_ACTIONS:
        lines = []
        for aid, a in PENDING_ACTIONS.items():
            what = (f"create ad \"{a.get('campaign_name')}\"" if english else f"新建广告「{a.get('campaign_name')}」") \
                if a.get("type") == "create_campaign" else \
                (f"{a.get('status')} {a.get('level')} \"{a.get('name') or a.get('object_id')}\"" if english
                 else f"{a.get('status')} {a.get('level')}「{a.get('name') or a.get('object_id')}」")
            lines.append(f"- id {aid}: {what}" if english else f"- 编号 {aid}:{what}(已登记,待执行)")
        if english:
            prompt += ("\n\n[PENDING ACTIONS] Already registered — do NOT register them again:\n"
                       + "\n".join(lines) +
                       "\nWhen the user confirms, call confirm_action with the id above; don't ask again.")
        else:
            prompt += ("\n\n【保险箱现状】以下待办已登记完毕,严禁重新登记:\n" + "\n".join(lines) +
                       "\n用户已确认/同意时,直接调 confirm_action(用上面的编号)执行,不要再要求确认。")
    return prompt


# 遇到这些错误码就"换人再试":429=额度用尽或太频繁,5xx=上游服务繁忙/临时故障。
# 血泪:原来只认 429,结果 Gemini 一报 503(服务繁忙)整条链就断了,
# 明明有 ofox 兜底也不去用,用户只看到一句"稍后再试"。
_RETRYABLE_CODES = (429, 500, 502, 503, 504)


def ask_gemini(messages: list[ChatMessage], lang: str = "zh") -> str:
    """大脑 A:Gemini。钥匙从环境变量 GEMINI_API_KEY 自动读取。

    走「手动挡」:关掉 SDK 的自动工具调用,自己跑工具循环。
    为什么不用自动挡 —— 自动挡配上流式实测是坏的(`generate_content_stream`
    只回一个 text='' 的空块就 STOP,工具循环根本没跑)。自己跑循环之后,
    既能边收边吐字,也能在每次调工具时播报"正在查什么"。
    """
    client = genai.Client()
    # 把聊天记录翻译成 Gemini 的格式:它管助手叫 "model",不叫 "assistant"
    contents = [
        genai_types.Content(
            role="user" if m.role == "user" else "model",
            parts=[genai_types.Part.from_text(text=m.content)],
        )
        for m in messages
    ]
    # 主力 + 备胎:主力额度用完(429)就自动换下一个试,都不行才报错
    models_to_try = ["gemini-flash-latest", "gemini-flash-lite-latest"]
    last_error = None
    for model in models_to_try:
        try:
            return _gemini_loop(client, model, contents, lang)
        except genai_errors.APIError as e:
            if e.code in _RETRYABLE_CODES:   # 额度用尽或上游繁忙,换备胎接着试
                last_error = e
                continue
            raise
    raise last_error


def _gemini_parts(obj) -> list:
    """从一个响应/流块里把 parts 掏出来(结构层层嵌套,单独封一下省得到处判空)。"""
    cand = (getattr(obj, "candidates", None) or [None])[0]
    content = getattr(cand, "content", None)
    return list(getattr(content, "parts", None) or [])


def _gemini_loop(client, model: str, contents: list, lang: str) -> str:
    """Gemini 的工具调用循环(手动挡)。流式时边收边把文字播报出去。"""
    cfg = genai_types.GenerateContentConfig(
        system_instruction=_system_prompt_now(lang),
        tools=NEWSBREAK_TOOLS,
        # 关掉自动工具调用:我们自己执行、自己把结果贴回去。
        # 这样才能在流式下正常工作,也才能播报每一步在干什么。
        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
    )
    streaming = CURRENT_EMIT.get() is not None
    contents = list(contents)

    for _ in range(10):   # 设个上限防转圈
        texts, calls, model_parts = [], [], []

        def take(part):
            """收下模型吐出来的一个 part。

            **必须原样留着这个对象**,不能自己重新造一个 Part —— 里面带着
            `thought_signature`,把它贴回对话时 Gemini 要拿来校验,
            少了会直接报 400「Function call is missing a thought_signature」。
            """
            model_parts.append(part)
            if getattr(part, "function_call", None):
                calls.append(part.function_call)
            elif getattr(part, "text", None):
                texts.append(part.text)
                if streaming:
                    _emit("delta", text=part.text)

        if streaming:
            for chunk in client.models.generate_content_stream(
                    model=model, contents=contents, config=cfg):
                for part in _gemini_parts(chunk):
                    take(part)
        else:
            resp = client.models.generate_content(model=model, contents=contents, config=cfg)
            for part in _gemini_parts(resp):
                take(part)

        if not calls:
            return "".join(texts) or "(Gemini 没有返回文字)"

        # 它要用工具:说明刚才吐的那点字只是开场白,不是最终答案 → 让前端作废重来
        if streaming and texts:
            _emit("reset")

        contents.append(genai_types.Content(role="model", parts=model_parts))

        # 替它执行,把结果贴回去,再让它接着想
        result_parts = []
        for fc in calls:
            _emit("status", text=_tool_label(fc.name))
            fn = OPENAI_TOOL_FUNCS.get(fc.name)
            try:
                result = fn(**dict(fc.args or {})) if fn else {"error": f"未知工具 {fc.name}"}
            except Exception as e:
                result = {"error": str(e)}
            result_parts.append(genai_types.Part.from_function_response(
                name=fc.name, response={"result": result}))
        contents.append(genai_types.Content(role="user", parts=result_parts))

    return "(工具调用轮数过多,已中止,请换个问法)"

# ============ 大脑 B:ChatGPT(OpenAI) ============
# ChatGPT 的工具调用是"手动挡":要给每个工具写 JSON 说明书,并自己跑调用循环。

def _oa_tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    """按 OpenAI 的格式写一份工具说明书。"""
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


_ID = {"type": "string", "description": "对象 id(长数字字符串)"}
_PAGE = {"type": "integer", "description": "页码,默认 1"}
_SEARCH = {"type": "string", "description": "按名字搜索(可选)"}
_LEVEL = {"type": "string", "enum": ["campaign", "ad_set", "ad"], "description": "层级"}

OPENAI_TOOL_SCHEMAS = [
    _oa_tool("list_organizations", "查询当前账号名下的所有 NewsBreak 组织", {}, []),
    _oa_tool("list_ad_accounts", "查询某个组织下的所有广告账户", {"org_id": _ID}, ["org_id"]),
    _oa_tool("list_campaigns", "查询某个广告账户下的广告计划列表",
             {"ad_account_id": _ID, "page": _PAGE, "search": _SEARCH}, ["ad_account_id"]),
    _oa_tool("list_ad_sets", "查询某个广告账户下的广告组列表",
             {"ad_account_id": _ID, "page": _PAGE, "search": _SEARCH}, ["ad_account_id"]),
    _oa_tool("list_ads", "查询某个广告账户下的广告列表",
             {"ad_account_id": _ID, "page": _PAGE, "search": _SEARCH}, ["ad_account_id"]),
    _oa_tool("get_report", "查询投放数据报表(花费/展示/点击/转化等)",
             {"level": _LEVEL,
              "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD,可不填(默认最近7天)"},
              "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD,可不填"},
              "ad_account_id": {"type": "string", "description": "只看某个广告账户(单账户可不填)"}}, []),
    _oa_tool("propose_status_change", "登记一个开启/暂停待办(不会立即执行,须用户确认)",
             {"level": _LEVEL, "object_id": _ID,
              "status": {"type": "string", "enum": ["ON", "OFF"], "description": "ON=开启 OFF=暂停"},
              "name": {"type": "string", "description": "对象名字,用于向用户复述"}},
             ["level", "object_id", "status"]),
    _oa_tool("propose_create_campaign", "登记一个新建广告待办(一次建好campaign+ad set+ad三层;不会立即执行,须用户确认)",
             {"ad_account_id": _ID,
              "keyword": {"type": "string", "description": "英文命名关键词,如 gutter"},
              "landing_url": {"type": "string", "description": "落地页链接,http(s)开头"},
              "budget_dollars": {"type": "number", "description": "预算(美元),最低10。**可以不传**:不传就用默认日预算 $20,但要在复述时告诉用户这是默认值、为什么、以及可以改"},
              "tracking_id": {"type": "string", "description": "转化事件id(可选,不填自动选)"},
              "headline": {"type": "string", "description": "广告标题"},
              "description": {"type": "string", "description": "广告描述"},
              "asset_url": {"type": "string", "description": "素材地址(用户上传后系统消息里的 assetUrl)"},
              "asset_filename": {"type": "string", "description": "素材文件名(用于判断图片/视频)"},
              "budget_type": {"type": "string", "enum": ["DAILY", "TOTAL"], "description": "DAILY=日预算 TOTAL=总预算"},
              "brand_name": {"type": "string", "description": "品牌名,默认用关键词"},
              "call_to_action": {"type": "string", "description": "按钮文案,默认 Learn More"},
              "campaign_name": {"type": "string", "description": "手动指定计划名(可选)"}},
             ["ad_account_id", "keyword", "landing_url", "headline", "description", "asset_url"]),
    _oa_tool("confirm_action", "执行之前登记的待办(仅在用户新消息中明确同意后)", {"action_id": {"type": "string"}}, ["action_id"]),
    _oa_tool("cancel_action", "取消之前登记的待办", {"action_id": {"type": "string"}}, ["action_id"]),
    _oa_tool("list_pending_actions", "查看保险箱里所有已登记待确认的待办(含编号);忘了编号用它查,严禁重复登记", {}, []),
    _oa_tool("list_conversion_events", "查询账户下的转化事件列表(建新广告第3步让用户挑)", {"ad_account_id": _ID}, ["ad_account_id"]),
    _oa_tool("propose_schedule", "登记一个定时开启/暂停广告的待办(需用户确认后生效)",
             {"kind": {"type": "string", "enum": ["once", "daily"], "description": "once=只执行一次,daily=每天重复"},
              "when": {"type": "string", "description": 'once 用 "YYYY-MM-DD HH:MM",daily 用 "HH:MM",均为北京时间'},
              "level": _LEVEL, "object_id": _ID,
              "status": {"type": "string", "enum": ["ON", "OFF"], "description": "ON=到点开启 OFF=到点暂停"},
              "name": {"type": "string", "description": "对象名字,用于复述"}},
             ["kind", "when", "level", "object_id", "status"]),
    _oa_tool("list_schedules", "查看所有定时任务(下次执行时间、上次结果)", {}, []),
    _oa_tool("cancel_schedule", "取消一个定时任务", {"task_id": {"type": "string"}}, ["task_id"]),
]

# 工具名 → 真实函数 的对照表(ChatGPT 说要调哪个,我们就去执行哪个)
OPENAI_TOOL_FUNCS = {fn.__name__: fn for fn in [
    list_organizations, list_ad_accounts, list_campaigns, list_ad_sets, list_ads,
    get_report, propose_status_change, propose_create_campaign, confirm_action, cancel_action,
    list_pending_actions, list_conversion_events,
    propose_schedule, list_schedules, cancel_schedule,
]}


def ask_openai(messages: list[ChatMessage], lang: str = "zh") -> str:
    """大脑 B:ChatGPT。钥匙从 OPENAI_API_KEY 读取(转发服务再加 OPENAI_BASE_URL)。"""
    client = openai.OpenAI()
    msgs = [{"role": "system", "content": _system_prompt_now(lang)}] + [
        {"role": m.role, "content": m.content} for m in messages
    ]
    # 型号候补:前面的不可用(404)就换下一个。
    # 用中转站时可在 .env 里用 OPENAI_MODEL 指定它家支持的型号,优先尝试
    models_to_try = ["gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"]
    custom_model = os.environ.get("OPENAI_MODEL", "").strip()
    if custom_model:
        models_to_try = [custom_model] + [m for m in models_to_try if m != custom_model]

    streaming = CURRENT_EMIT.get() is not None

    for _ in range(10):  # 工具调用循环:一轮没答完就继续,设个上限防转圈
        content, tool_calls, ok_model = "", [], None
        for model in models_to_try:
            try:
                content, tool_calls = _openai_once(client, model, msgs, streaming)
                ok_model = model
                break
            except openai.NotFoundError:
                continue
        if ok_model is None:
            raise openai.NotFoundError.__new__(openai.NotFoundError)  # 型号全不可用
        models_to_try = [ok_model]   # 记住能用的型号,后面几轮不再试错

        if not tool_calls:
            return content or "(ChatGPT 没有返回文字)"

        # 它想用工具:说明刚才吐的那点字只是开场白,不是最终答案 → 让前端作废重来
        if streaming and content:
            _emit("reset")
        msgs.append({"role": "assistant", "content": content or None,
                     "tool_calls": [{"id": tc["id"], "type": "function",
                                     "function": {"name": tc["name"],
                                                  "arguments": tc["arguments"]}}
                                    for tc in tool_calls]})
        for tc in tool_calls:
            _emit("status", text=_tool_label(tc["name"]))
            fn = OPENAI_TOOL_FUNCS.get(tc["name"])
            try:
                args = json.loads(tc["arguments"] or "{}")
                result = fn(**args) if fn else {"error": f"未知工具 {tc['name']}"}
            except Exception as e:
                result = {"error": str(e)}
            msgs.append({"role": "tool", "tool_call_id": tc["id"],
                         "content": json.dumps(result, ensure_ascii=False)})
    return "(工具调用轮数过多,已中止,请换个问法)"


def _openai_once(client, model: str, msgs: list, streaming: bool) -> tuple[str, list]:
    """问一轮 OpenAI/ofox,返回 (文字, 工具调用清单)。

    流式时边收边把文字播报出去;工具调用是分片来的(名字和参数会拆成好几块),
    要按 index 拼起来才完整。
    """
    if not streaming:
        r = client.chat.completions.create(model=model, messages=msgs, tools=OPENAI_TOOL_SCHEMAS)
        m = r.choices[0].message
        return (m.content or "",
                [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or ""}
                 for tc in (m.tool_calls or [])])

    parts, slots = [], {}
    for chunk in client.chat.completions.create(
            model=model, messages=msgs, tools=OPENAI_TOOL_SCHEMAS, stream=True):
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta
        if d is None:
            continue
        if d.content:
            parts.append(d.content)
            _emit("delta", text=d.content)
        for tc in (d.tool_calls or []):
            slot = slots.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
            if tc.id:
                slot["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    slot["name"] += tc.function.name
                if tc.function.arguments:
                    slot["arguments"] += tc.function.arguments
    return "".join(parts), [slots[i] for i in sorted(slots)]


def ask_claude(messages: list[ChatMessage], lang: str = "zh") -> str:
    """大脑 B:Claude。钥匙从环境变量 ANTHROPIC_API_KEY 自动读取。"""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-8",           # 当前推荐的主力模型
        max_tokens=16000,                  # 单次回答的长度上限
        thinking={"type": "adaptive"},     # 自适应思考:难题多想想,简单题直接答
        system=_system_prompt_now(lang),
        messages=[m.model_dump() for m in messages],
    )
    # 回答里可能既有"思考块"又有"文字块",只取文字部分
    return "".join(block.text for block in response.content if block.type == "text")


# AI 回复里出现这些词、却没有真实执行记录且保险箱还有待办 → 大概率在"谎报军情"
_CLAIM_KEYWORDS = [
    # 中文说法
    "成功创建", "已创建", "创建并提交", "已提交", "已执行", "已开启", "已暂停", "已为您暂停", "已为您开启",
    # 英文说法(英文模式下 AI 谎报会用这些词)
    "successfully created", "has been created", "have been created", "i've created",
    "has been paused", "has been turned on", "has been turned off",
    "successfully submitted", "successfully executed", "is now live",
]


def _finalize(reply: str, lang: str = "zh") -> dict:
    """给回复盖"系统核验"钢印:真执行了什么、有没有谎报,以代码层记录为准。

    这段字是代码直接输出给用户看的(不经过 AI),所以要自己按语言切换。
    """
    english = str(lang).lower().startswith("en")

    if _EXECUTED_THIS_REQUEST:
        parts = []
        for rec in _EXECUTED_THIS_REQUEST:
            if isinstance(rec, dict):
                if english:
                    verb = "succeeded" if rec.get("ok") else "FAILED"
                    parts.append(f"action {rec.get('id')} {verb} → {rec.get('detail')}")
                else:
                    verb = "执行成功" if rec.get("ok") else "执行失败"
                    parts.append(f"待办 {rec.get('id')} {verb} → {rec.get('detail')}")
            else:
                parts.append(str(rec))   # 兼容旧格式
        label = ("🔒 **System verification** (recorded by code, not written by the AI): "
                 if english else "🔒 **系统核验**(代码层记录,非 AI 生成):")
        return {"reply": f"{reply}\n\n---\n{label}{'; '.join(parts)}"}

    # 谎报匹配不分大小写:AI 写的是 "Successfully created",关键词表里是小写
    reply_lower = reply.lower()
    if PENDING_ACTIONS and any(kw.lower() in reply_lower for kw in _CLAIM_KEYWORDS):
        ids = ", ".join(PENDING_ACTIONS.keys()) if english else "、".join(PENDING_ACTIONS.keys())
        note = (f"⚠️ **System verification**: nothing was actually executed this turn — "
                f"the pending action(s) are still queued ({ids}). If the message above claims something "
                f"was created or done, that is an AI hallucination. "
                f'Reply "run pending action {ids}" to retry.'
                if english else
                f"⚠️ **系统核验**:本轮实际上没有执行任何操作,保险箱里仍有待办({ids})。"
                f"如果上面说\"已创建/已执行\",那是 AI 的幻觉,请回复「执行待办 {ids}」重试。")
        return {"reply": f"{reply}\n\n---\n{note}"}

    return {"reply": reply}


def _route_brain(req: ChatRequest) -> str:
    """按 BRAIN 配置选大脑并拿到回答。三级火箭的接力逻辑只写这一份,
    普通接口和流式接口共用 —— 否则改了一边忘了另一边,行为就会不一致。"""
    brain = os.environ.get("BRAIN", "auto").strip().lower()

    if brain == "openai" and os.environ.get("OPENAI_API_KEY"):
        return ask_openai(req.messages, req.lang)
    if brain == "claude" and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return ask_claude(req.messages, req.lang)

    # 三级火箭:Gemini(免费)→ 额度尽了切 ChatGPT/ofox → 都没有再看 Claude
    if os.environ.get("GEMINI_API_KEY"):
        try:
            return ask_gemini(req.messages, req.lang)
        except genai_errors.APIError as e:
            if e.code in _RETRYABLE_CODES and os.environ.get("OPENAI_API_KEY"):
                print(f"[brain] Gemini 报错 {e.code},切换到 OPENAI 通道", flush=True)
                _emit("status", text="正在切换到备用通道…")
                return ask_openai(req.messages, req.lang)
            raise
    if os.environ.get("OPENAI_API_KEY"):
        return ask_openai(req.messages, req.lang)
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return ask_claude(req.messages, req.lang)
    raise _NoBrainKey()


class _NoBrainKey(Exception):
    """一把 AI 钥匙都没配。"""


def _brain_error(e: Exception) -> tuple[int, str]:
    """把各家 SDK 的异常翻译成人话。返回 (HTTP 状态码, 给用户看的话)。

    这份翻译也是两个接口共用的。原则:**钥匙/配置错误绝不掩盖**,
    要让用户看到真实原因,而不是笼统的"稍后再试"。
    """
    if isinstance(e, _NoBrainKey):
        return 500, ("还没配置 AI 大脑的钥匙:打开 .env,填 GEMINI_API_KEY(免费)/ "
                     "OPENAI_API_KEY / ANTHROPIC_API_KEY 任意一把,填完直接重发消息即可。")

    # ===== Gemini =====
    if isinstance(e, genai_errors.APIError):
        if e.code in (400, 401, 403):
            return 500, "Gemini 钥匙无效(检查 .env 里的 GEMINI_API_KEY 是否粘贴完整)"
        if e.code == 429:
            return 429, "Gemini 免费额度暂时用完/太频繁,稍等一分钟再试(或在 .env 里把 BRAIN 改成 openai 走 ofox 通道)"
        if e.code in _RETRYABLE_CODES:
            return 502, (f"Google 那边服务繁忙({e.code}),不是你的操作问题。重发一次通常就好;"
                         "老是这样就把 .env 里的 BRAIN 改成 openai 走 ofox 通道")
        return 502, f"Gemini 服务返回错误({e.code}),稍后再试"

    # ===== ChatGPT / ofox =====
    if isinstance(e, openai.AuthenticationError):
        return 500, "ChatGPT 钥匙无效(检查 .env 里的 OPENAI_API_KEY)"
    if isinstance(e, openai.RateLimitError):
        return 429, "ChatGPT 也限流了(免费额度/余额可能不足),稍后再试"
    if isinstance(e, openai.NotFoundError):
        return 502, "ChatGPT 候选型号都不可用(key 可能没开通这些模型)"
    if isinstance(e, openai.APIConnectionError):
        return 502, "连不上 ChatGPT 服务(如用转发服务,检查 OPENAI_BASE_URL)"
    if isinstance(e, openai.APIStatusError):
        return 502, f"ChatGPT 服务返回错误({e.status_code}),稍后再试"

    # ===== Claude =====
    if isinstance(e, anthropic.AuthenticationError):
        return 500, "Claude 钥匙无效(检查 .env 里的 ANTHROPIC_API_KEY)"
    if isinstance(e, anthropic.RateLimitError):
        return 429, "请求太频繁,被限流了,稍等一会儿再试"
    if isinstance(e, anthropic.APIConnectionError):
        return 502, "连不上 AI 服务,请检查网络"
    if isinstance(e, anthropic.APIStatusError):
        return 502, f"Claude 服务返回错误({e.status_code}),稍后再试"

    # ===== 平台自己的报错(比如没绑账号)直接说人话 =====
    if isinstance(e, nb.NewsBreakError):
        return 400, str(e)

    return 500, f"出了点问题:{e}"


def _new_turn() -> None:
    """每条用户消息开始时要做的两件事(两个接口共用)。"""
    global _REQUEST_SEQ
    _REQUEST_SEQ += 1               # 保险丝计数:区分"登记"和"确认"是不是同一条消息
    _EXECUTED_THIS_REQUEST.clear()  # 本轮"真实执行台账"清零
    load_env_file()                 # 现读 .env:刚填的钥匙不用重启就生效


@app.post("/api/chat")
def chat(req: ChatRequest):
    """核心接口:收聊天记录 → 问 AI 大脑 → 一次性回答案。

    流式版本见 /api/chat/stream。这个保留着当兜底 —— 流式一旦被中间的
    反向代理缓冲住(nginx 默认会),前端可以退回来用这个。
    """
    _new_turn()
    try:
        return _finalize(_route_brain(req), req.lang)
    except Exception as e:
        code, msg = _brain_error(e)
        return JSONResponse(status_code=code, content={"error": msg})


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest):
    """流式版:边想边出字,并播报"正在查什么"。

    用 SSE(Server-Sent Events),事件类型:
      status  正在做什么(查列表/拉报表…),给用户一个"它没死"的交代
      reset   前面吐的字作废(模型先说了句开场白又去调工具了)
      delta   新的一小段文字
      done    结束,带上**盖过钢印的完整回复**(前端用它做最终渲染)
      error   出错了,带人话说明

    大脑那几个函数是同步阻塞的,所以丢到线程里跑,靠队列把事件递出来。
    """
    _new_turn()

    q: "queue.Queue" = queue.Queue()
    ctx = contextvars.copy_context()     # 把当前用户的凭据等上下文带进线程

    def work():
        CURRENT_EMIT.set(q.put)
        try:
            text = _route_brain(req)
            q.put({"type": "done", **_finalize(text, req.lang)})
        except Exception as e:
            code, msg = _brain_error(e)
            q.put({"type": "error", "error": msg, "code": code})
        finally:
            q.put(None)                  # 收摊信号

    threading.Thread(target=lambda: ctx.run(work), daemon=True, name="chat-stream").start()

    def gen():
        while True:
            ev = q.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        # 关键:让 nginx 别缓冲这条响应。不加的话 nginx 会攒够一块才发,
        # 用户看到的还是"转半天圈然后一次蹦出来",流式等于白做。
        "X-Accel-Buffering": "no",
    })
