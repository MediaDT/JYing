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
import hashlib
import html as _html
import json
import os
import queue
import re as _re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import httpx
import openai
from fastapi import FastAPI
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse, RedirectResponse)
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
# 这次请求真实的站点地址(形如 http://localhost:18100/)。
# **为什么必须有**:落地页预览原来只给模型一个相对路径 `/landing-pages/xxx.html`,
# 于是它自己配了个 host —— 线上实测编出了 `http://localhost:3000/...`,
# 打到用户本机另一个项目上,看到的是那个项目的 404。给它完整地址就没得编。
CURRENT_BASE_URL: contextvars.ContextVar = contextvars.ContextVar("adbot_base_url", default="")
CURRENT_USER_ID: contextvars.ContextVar = contextvars.ContextVar("adbot_user_id", default="")

# 流式回复时,用来把「进度/文字」一小段一小段递给前端的回调。
# None = 这次不是流式请求,大脑那边照常一次性返回。
CURRENT_EMIT: contextvars.ContextVar = contextvars.ContextVar("adbot_emit", default=None)
CURRENT_CHAT_MODE: contextvars.ContextVar = contextvars.ContextVar("adbot_chat_mode", default="campaign")


_TOOL_LABELS = {
    "list_organizations": "正在查组织…",
    "list_ad_accounts": "正在查广告账户…",
    "list_campaigns": "正在查广告计划…",
    "list_ad_sets": "正在查广告组…",
    "list_ads": "正在查广告…",
    "list_conversion_events": "正在查转化事件…",
    "recommend_creatives": "正在从你的历史广告里挑好素材…",
    "search_stock_creatives": "正在从授权图库里找素材…",
    "search_competitor_ads": "正在查竞品正在投的广告…",
    "search_competitor_landing_pages": "正在找疑似优质落地页…",
    "decompose_landing_page": "正在拆解落地页结构…",
    "summarize_landing_page_patterns": "正在汇总规律并生成两个新落地页…",
    "list_cloudflare_landing_resources": "正在读取 Cloudflare 项目和域名…",
    "list_clickflare_campaigns": "正在读取 ClickFlare 的 campaign…",
    "describe_clickflare_campaign": "正在看这条 campaign 现在挂着什么…",
    "create_clickflare_landers": "正在把 A/B 两版登记进 ClickFlare…",
    "propose_swap_campaign_landers": "正在核对这条 campaign 现在挂着什么…",
    "clickflare_publish_kit": "正在从 ClickFlare 取追踪地址和脚本…",
    "propose_publish_landing_pages": "正在登记 A/B 页面发布待办…",
    "my_ad_categories": "正在看你的账户在投什么品类…",
    "platform_kind": "正在判断这是哪类投放平台…",
    "native_market_scan": "正在扫原生平台上什么在跑得最好…",
    "decompose_creative": "正在拆解这条广告的创意结构…",
    "summarize_creative_patterns": "正在归纳套路、拟我们自己的方案…",
    "use_found_creative": "正在把选中的素材存进你的账户…",
    "propose_make_creatives": "正在准备生成广告图…",
    "get_delivery_tree": "正在看这条计划底下的广告组和广告…",
    "get_report": "正在拉报表数据…",
    "propose_status_change": "正在登记开关待办…",
    "propose_create_campaign": "正在登记建广告待办…",
    "propose_add_ad": "正在登记加广告待办…",
    "swap_landing_image": "正在换落地页上的配图…",
    "propose_landing_images": "正在登记落地页配图待办…",
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

        # 设在 call_next **之前**,下游(含丢进线程池的同步接口)才读得到
        CURRENT_BASE_URL.set(str(request.base_url))
        user = acc.session_user(request.cookies.get(SESSION_COOKIE, ""))
        if user:
            request.state.user = user
            # 把「这个人绑的 NewsBreak 凭据」放进上下文。设在 call_next 之前,
            # 下游(包括 FastAPI 丢进线程池跑的同步接口)才读得到。
            # 没绑的人这里是空 dict —— 关键是**不为 None**,这样 _token() 就知道
            # "在用户上下文里,他没绑",会直接报错而不是偷偷用 .env 里的公用 token。
            nb.CURRENT_CREDS.set(acc.get_creds(user["id"], "newsbreak"))
            # 竞品查询的 key 同理按人放。注意它的取值规则和 NewsBreak 不同
            # (自己没贴就回落 .env 那份公用的),原因见 openadlibrary_client 顶部注释。
            CURRENT_USER_ID.set(user["id"])
            return await call_next(request)

        # 页面请求 → 跳登录页;接口请求 → 回 401 让前端处理
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"error": "请先登录", "need_login": True})
        return RedirectResponse("/login", status_code=302)


app.add_middleware(AuthMiddleware)

# 把 static/ 目录挂出来:页面里就能引用 /static/marked.min.js 这类文件
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")


def _scheduled_execute(level: str, object_id: str, status: str, user_id: str = "",
                       targets: list[dict] | None = None):
    """定时任务到点时真正干活的函数(交给 scheduler 的看表线程调用)。

    看表线程不在任何请求里,没有"当前用户",所以要**用当初登记这条任务的人**
    的凭据来执行 —— 否则按人隔离之后,定时任务就不知道该用谁的账户了。

    targets:当初用户选定要一起改的对象(开启广告要三层一起开)。
    没有 targets 的是老任务,按单个对象处理,保持兼容。
    """
    try:
        if user_id:
            nb.CURRENT_CREDS.set(acc.get_creds(user_id, "newsbreak"))
        tgts = targets or [{"level": level, "id": str(object_id), "name": ""}]
        done, failed = [], []
        for t in tgts:
            try:
                nb.update_status(t["level"], t["id"], status)
                done.append(f"{t['level']}「{t.get('name') or t['id']}」")
            except Exception as e:
                failed.append(f"{t['level']}「{t.get('name') or t['id']}」:{e}")
        if failed:
            # 部分失败要如实说是哪一个 —— 到点时没人盯着,记账含糊等于查不出问题
            return {"error": "成功:" + ("、".join(done) or "无") + ";失败:" + "、".join(failed)}
        return {"ok": True, "detail": "、".join(done)}
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
第3步 素材:**先问他"你这条广告主要推什么?有现成的图片/视频吗?"**
   · 有 → 请他点输入框左侧的 📎 按钮上传;上传成功后会自动出现一条带 assetUrl 的消息,记住 assetUrl 和文件名;
   · **没有 / 说"帮我推荐" → 先调 recommend_creatives**,把账户里效果好的历史素材列给他挑。
     (这个工具**不只用在这一步** —— 用户任何时候问「我账户里有哪些素材图」都调它,
     它是唯一能拿到账户历史素材的接口。)
     用表格展示,每个素材用 `![素材N](asset_url)` 插图让他直接看到,并附上真实的 CTR/转化/花费。
     说清两点:①这些是他自己账户投过的,**版权和平台审核都没问题**;②选哪个说编号就行。
     他选了之后,直接用那个 asset_url 继续(不用再上传)。
   · **账户里没有合适的、或者用户想找新素材 → 调 search_stock_creatives**(去正规授权图库找)。
     关键词用英文,按落地页内容给(如 roof repair)。每张图要列出**质量评价**和**许可证**;
     **质量评价是"不建议"的也照样列出来并说清差在哪**,别只挑好的报。
     用户选定后调 `use_found_creative` 转存,**拿到 assetUrl 才能建广告**
     —— 图库那个 image_url 不能直接当 asset_url 用。
   · **用户想知道"同行都在投什么" → 调 search_competitor_ads**(查竞品正在投的真实原生广告)。
     **先分清用户问的是哪一种**,这两种走完全不同的路:
     **(甲)开放探索** —— 「现在什么广告跑得好」「这平台适合跑什么单子」「有什么新机会」,
       **句子里没有品类**。→ 调 `native_market_scan` 扫全市场,给他**品类排行 + 钩子排行**。
       **绝对不要拿他账户已有的品类去框**(别老围着 roof / gutter / window 打转)——
       他问这话通常正是想看看现有生意之外还有什么方向,框回去等于没回答。
     **(乙)品类明确** —— 「我要投 roof,同行怎么打」,**用户自己说了品类**。
       → 用 `search_competitor_ads` 按那个词查。他说什么就查什么,
       哪怕和他账户在投的对不上也照查(他可能正想试新方向),不用反复确认。
     只有**用户没给品类、而你又必须填一个**的时候,才先用 `my_ad_categories`
     看他账户在投什么 —— 别自己凭空编一个词。

     **搞清楚素材该从哪儿找**:调 `platform_kind` 判断目标平台是哪一类。
     确定是**原生广告平台**(NewsBreak 就是),那就把**所有原生平台**
     (Taboola / Outbrain / Yahoo / MGID / Revcontent…)上跑同品类的广告都捞来学 ——
     **不要只盯着目标平台自己的广告**。这类平台之间创意套路通用,池子大得多、规律更可靠。
     返回里的 `素材来自这些原生平台` 要报给用户看,让他知道覆盖面。
     **关键词绝不许自己编**:用户没说品类时,**先调 my_ad_categories** 看他账户实际在投什么,
     按那个查,并明说「我按 <品类> 查的,要看别的品类跟我说」。账户里也认不出来时**直接问他**
     「你们主要做什么?」。编一个关键词的后果是:查回来全是**别的行业**的广告,
     用户看半天才发现对不上(实测发生过:只问了句"同行在跑什么",AI 自己编了 roof,
     而账户投的是 gutter 和 window)。
     返回里带 `⚠️关键词提醒` 时,**必须先把这件事告诉用户并确认**,不许直接往下讲结果。
     它比图库多了**市场验证**:投得久、铺的版位多 = 广告主愿意持续为它花钱。列表里要带上
     **投放天数**和**版位数**,并帮用户**总结这些高效广告的共同点**
     (画面风格、有没有真人、有没有价格/优惠字样、文案角度)—— 这比单纯给图有用。
     **必须提醒**:这是别人的广告素材,直接投有版权风险、平台可能拒审;
     更稳的做法是照着思路自己拍或用图库图。用户坚持要用,才调 use_found_creative。
   · **拆解回答的是「它为什么跑得动」,不是「它长什么样」。** 每条会给出:
     为什么有展示 / 为什么被点(钩子类型+原话+机制)/ 为什么有转化 /
     **亮点**(只有一个)/ 亮点为什么成立 / **如何发挥这个亮点** / 可迁移的公式。
     讲给用户时**先讲亮点和怎么用**,画面细节次要。
     **平台不给点击率和转化率**,所有结论都是从版位数和投放天数倒推的 ——
     转述时要保留这个口径,**绝不许说出"点击率 X%"这类数字**,那是编的。
   · **用户想"照着同行的套路做一版" → 先 decompose_creative 拆几条(至少2条),
     再 summarize_creative_patterns 出方案**。产出的是**文案 + 画面方案,不是图**,
     这一点要跟用户说清楚,别让他以为图已经有了。
     方案里带「⚠️查重」标记的,说明那句文案和竞品原句太像,要提醒用户换个说法。
     拿到方案后有三条路拿到图,**都要告诉用户**:
     **(a) 让系统直接做出来 → propose_make_creatives** ——
       出的是**干净的实拍画面,图上一个字都没有**。标题、描述和行动按钮是
       NewsBreak **自己渲染**的独立字段,建广告时填进去即可;烧在图上会重复一遍,
       还会和平台的真按钮撞在一起。每一版换一种镜头(远景/特写/仰拍…),
       画面真的不同,才测得出哪种构图好使。
       **要花钱**(每张约 $0.20),所以走确认关卡:先报价请用户点头,才真的生成。
       做好会直接传进素材库,说一句「用第N张建广告」就能接着建;
     (b) 用 search_stock_creatives 去授权图库找一张对得上的;
     (c) 按「画面怎么拍」自己拍。
   · **仍然绝对不许**:自己编素材链接、声称能生成图片、或从上面三个工具之外的地方拿图。
     素材来源必须可查证 —— 要么是他自己账户投过的,要么是工具从授权图库搜来的(带许可证)。
     两个工具都找不到时如实说没有,并告诉他上传自己的图就行(建议 1200×628 以上、清晰、别放大段文字);
第4步 落地页类型(决定命名):
   · **落地页能对上「现有的落地页类型」里的某一个** → 直接用它,并告诉用户一声
     (例:「你这个落地页是屋顶维修,我按 roof 来命名」),不用反复确认;
   · **一个都对不上** → **必须问用户「这次用什么关键词命名?」**,
     并把现有的几个列出来供参考。**绝对不许自己编一个类型词** ——
     命名是团队约定,编出来的词会让以后按名字筛数据时对不上号。
   名字会按团队规范自动生成,复述时要把三个名字都列出来:
   · 计划   `NB-Roof-260817-01`  —— NB-类型-年月日-今天这个类型的第几支(两位)
   · 广告组 `260817-Roof-001`    —— 年月日-类型-这支计划里的第几个组(三位)
   · 广告   `AD-260817-Roof-001` —— AD-年月日-类型-这个组里的第几条广告(三位)
   序号是查过平台上已有的名字自动往下排的,不用你算。用户想完全自己指定计划名也行(传 campaign_name)。
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

「开启广告」的特别规矩(**最容易出错的地方,必须照做**):
广告要真的跑起来,**campaign / ad set / ad 三层必须都是 ON**,是"与"的关系。
只把 campaign 打开、底下的广告组和广告还关着 = 一条广告都出不去,
用户会以为在投、实际白等几天。所以用户说「开启 XXX」时:
1) 先调 get_delivery_tree(campaign_id) 看清底下有哪些广告组和广告、各自开没开;
2) **底下只有一个广告组、一个广告** → 直接连同它们一起登记(不用多问);
3) **有多个广告组或多个广告** → 先把清单列成表格给用户看(名字 + 当前是开是关),
   问他「**全部打开,还是只打开某几个?**」—— 别替他决定,多开一条就是多花一份钱;
4) 用户选定后,调 propose_status_change,把选中的对象放进 extra_targets 一起登记,
   复述时**把每一条都列出来**让他确认。
**定时开启(propose_schedule)完全同理,而且更要紧** —— 到点时没人盯着,
只定了 campaign 一层的话,第二天才会发现一条广告都没跑。
所以定时开启也要先 get_delivery_tree、也要问用户开哪些、也要把它们放进 extra_targets。
反过来,「暂停」不用这么麻烦:关掉 campaign,底下的自然都不投了,单独登记一条即可。

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
Step 3 Creative: **first ask "what is this ad promoting, and do you already have an image/video?"**
   · If yes → ask them to click the 📎 button to the left of the input box and upload. After a successful upload
     a message with an assetUrl appears automatically — remember that assetUrl and the filename;
   · If no / they ask for suggestions → **call recommend_creatives first** and show the account's best past
     (this tool is NOT limited to this step — call it whenever the user asks what creatives their account has;
     it is the ONLY way to reach the account's past creatives)
     creatives. Use a table, embed each one with `![Creative N](asset_url)` so they can actually see it, and
     include the real CTR / conversions / spend. Make two things clear: (1) these are from their own account, so
     licensing and ad review are not an issue; (2) they just reply with a number to pick one. Then reuse that
     asset_url directly.
   · **If the account has nothing suitable, or they want fresh creatives → call search_stock_creatives**, which
     searches properly licensed stock libraries. Use an English keyword based on the landing page (e.g. "roof
     repair"). For every image you MUST show its **quality verdict** and its **license**; **include the ones rated
     "not recommended" too, explaining what is wrong with them** — do not quietly show only the good ones.
     Once they pick one, call `use_found_creative` to transfer it in; **you need the returned assetUrl to create
     the ad** — the library's image_url is NOT an asset_url.
   · **Tell the two questions apart first.** (a) An **open question** with no category
     ("what's working right now", "what kind of offers suit this platform") → call
     **native_market_scan** for a whole-market category + hook ranking. **Do NOT narrow it
     to whatever their account already runs** — they are usually asking precisely because
     they want to see beyond their current business. (b) A **stated category**
     ("I want to run roof") → search_competitor_ads with that word, exactly as they said it,
     even if it differs from what their account runs.
   · **Never invent the keyword.** If the user did not say which category, call
     **my_ad_categories** first to see what their own account actually advertises, search that,
     and say plainly "I searched <category> — tell me if you want a different one".
     If the account reveals nothing, **ask them** what business they are in.
     When the result carries `⚠️关键词提醒`, raise that with the user and confirm
     BEFORE presenting any findings.
   · **If they want to know what competitors are running → call search_competitor_ads**, which pulls real ads
     other advertisers are currently running. Its advantage over stock is **market validation**: an ad that has
     run for months across many placements demonstrably works. Include **days running** and **placements**, and **summarise
     what the high-performing ones have in common** (visual style, real people or not, price/offer callouts,
     copy angle) — that is far more useful than the images alone.
     **You must warn them**: these are other advertisers' creatives; using one directly carries copyright risk
     and the platform may reject it. The safer play is to shoot your own or use a stock image following the same
     idea. Only if they still want it, call use_found_creative.
   · **If they want to "build one following what competitors do" → call decompose_creative on a few
     After a brief is ready, there are **three** ways to get the actual image —
     tell the user all three: **(a) have the system make it → propose_make_creatives**
     (a **clean photograph with no text on it at all** — NewsBreak renders the headline,
     description and call-to-action itself as separate fields, so burning them into the
     image duplicates the copy and collides with the platform's real button. Each variant
     uses a different shot — wide, close-up, low-angle — so the pictures genuinely differ
     and an A/B test measures something. **It costs money** (~$0.20 per image), so it goes
     through the confirm gate: quote first, generate only after the user agrees); (b) find a matching licensed photo via search_stock_creatives;
     (c) shoot it themselves following the art direction.
     (at least 2), then summarize_creative_patterns**. What comes back is **copy plus an art-direction
     brief — not an image**; say so plainly so they do not think the image already exists.
     Any variant carrying a `⚠️查重` flag is too close to a competitor's own wording — tell them to reword it.
     From there they can shoot to the brief themselves, or use search_stock_creatives to find a matching photo.
   · **Still absolutely forbidden**: inventing asset URLs, claiming you can generate images, or sourcing images
     from anywhere other than those three tools. Every creative must be traceable — either from their own account
     or fetched by the tool from a licensed library (with its license shown). If neither tool finds anything, say
     so plainly and ask them to upload their own (suggest 1200×628 or larger, sharp, not covered in text);
Step 4 Landing-page type (drives naming):
   · **If the landing page matches one of the KNOWN LANDING-PAGE TYPES** → just use it and mention it
     (e.g. "This page is about roof repair, so I'll name things with `roof`"); no need to keep asking;
   · **If none match** → **you MUST ask the user which keyword to use**, listing the known ones for reference.
     **Never invent a type word** — naming is a team convention, and an invented one breaks filtering by name later.
   Names follow the team convention automatically; list all three when you restate the order:
   · Campaign `NB-Roof-260817-01`  — NB-Type-YYMMDD-Nth campaign of this type today (2 digits)
   · Ad set   `260817-Roof-001`    — YYMMDD-Type-Nth ad set in this campaign (3 digits)
   · Ad       `AD-260817-Roof-001` — AD-YYMMDD-Type-Nth ad in this ad set (3 digits)
   The sequence number is derived from what already exists on the platform, so you don't compute it.
   They can still override the campaign name entirely (campaign_name).
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

"TURNING ADS ON" (the easiest thing to get wrong — follow this exactly):
For an ad to actually run, **the campaign, the ad set AND the ad must all be ON** — it's an AND.
Turning on only the campaign while the ad set/ad stay off means nothing is delivered at all;
the user thinks they're live and waits days for nothing. So when they say "turn on XXX":
1) Call get_delivery_tree(campaign_id) first to see the ad sets and ads and their current status;
2) **Exactly one ad set and one ad** → include them automatically, no need to ask;
3) **More than one ad set or ad** → show the list as a table (name + currently on/off) and ask
   "**turn on all of them, or only some?**" — don't decide for them; each extra one costs more money;
4) Once they choose, call propose_status_change with the selected objects in extra_targets, and
   restate **every single one** for confirmation.
**Scheduling a turn-on (propose_schedule) works exactly the same way, and matters even more** —
nobody is watching when it fires, so if only the campaign was scheduled, they won't find out until
the next day that nothing ran. So scheduled turn-ons also need get_delivery_tree, also need to ask
which ones, and also need them in extra_targets.
Pausing is simpler: turning the campaign OFF stops everything under it, so one entry is enough.

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
import creative_search as cs  # noqa: E402
import creative_lab as lab  # noqa: E402
import creative_render as cr  # noqa: E402
import landing_lab as lp  # noqa: E402
import cloudflare_pages as cfp  # noqa: E402
import clickflare_scripts as cfs  # noqa: E402
import clickflare_client as cfc  # noqa: E402
import ad_platform_kinds as apk  # noqa: E402
import openadlibrary_client as oal  # noqa: E402


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


def get_delivery_tree(campaign_id: str, ad_account_id: str = "") -> dict:
    """看一条广告计划底下的完整结构:它有哪些广告组、每个组下有哪些广告,各自是开还是关。

    **开启广告前必须先调它。** 因为三层是"与"的关系:
    campaign / ad set / ad **全都是 ON,广告才会真的跑**。
    只把 campaign 打开、底下的组和广告还关着,等于没开 —— 用户会以为在投,其实一条都没出去。

    返回里 `all_on` 说明是不是三层都开着;`need_turn_on` 列出为了让它跑起来
    还差哪些对象没开(直接拿去 propose_status_change 的 extra_targets)。
    """
    try:
        acct = ad_account_id or _default_ad_account_id()
        cid = str(campaign_id)
        camp = next((c for c in nb.list_campaigns(acct, limit=100).get("items", [])
                     if str(c.get("id")) == cid), None)
        if not camp:
            return {"error": f"这个账户下找不到 id 为 {cid} 的广告计划"}

        # 接口不支持按父级过滤,只能拉回来自己筛(返回里带 campaignId / adSetId)
        sets = [s for s in nb.list_ad_sets(acct, limit=100).get("items", [])
                if str(s.get("campaignId")) == cid]
        ads = [a for a in nb.list_ads(acct, limit=200).get("items", [])
               if str(a.get("campaignId")) == cid]

        need = []
        if camp.get("status") != "ON":
            need.append({"level": "campaign", "id": cid, "name": camp.get("name")})
        tree = []
        for s in sets:
            if s.get("status") != "ON":
                need.append({"level": "ad_set", "id": str(s["id"]), "name": s.get("name")})
            kids = []
            for a in ads:
                if str(a.get("adSetId")) != str(s["id"]):
                    continue
                if a.get("status") != "ON":
                    need.append({"level": "ad", "id": str(a["id"]), "name": a.get("name")})
                kids.append({"id": str(a["id"]), "name": a.get("name"),
                             "status": a.get("status"), "在投状态": a.get("onlineStatus")})
            tree.append({"id": str(s["id"]), "name": s.get("name"),
                         "status": s.get("status"), "ads": kids})

        return {
            "campaign": {"id": cid, "name": camp.get("name"), "status": camp.get("status")},
            "ad_sets": tree,
            "all_on": not need,
            "need_turn_on": need,
            "note": ("三层全 ON 才会真的投放。若 need_turn_on 非空,说明现在开不起来。"
                     "**广告组或广告多于一个时,先把清单列给用户,问他要全开还是只开某几个**,"
                     "别替他决定 —— 多开一条就是多花一份钱。"
                     "确定要开哪些之后,调 propose_status_change 并把其余对象放进 extra_targets 一起登记。"),
        }
    except Exception as e:
        return {"error": str(e)}


def recommend_creatives(ad_account_id: str = "", days: int = 90, top_n: int = 5) -> dict:
    """**列出/查看这个账户已有的素材图** —— 从它自己投过的广告里取,顺带按效果排好序。

    **这是本项目唯一能拿到账户历史素材的工具,没有别的接口。**
    所以用户只要问到「我账户里有哪些图」「有什么素材能用」「历史素材看一下」
    「帮我推荐素材」,一律调它。**绝不许回答「接口限制/没有权限/拿不到」** ——
    线上实测出过这种事:工具明明可用,模型却编了一句「由于系统接口的限制,
    我暂时无法为您拉取账户里的历史素材图」,用户完全看不出那是编的。

    建新广告第3步(素材)只是**其中一个**使用场景,不是唯一场景 ——
    原来的说明把动作锁死在"推荐"、场景锁死在"第3步",于是「账户上的素材图有哪些」
    这种问法对不上号,模型就去编理由了。

    为什么只推荐账户自己的素材:①有真实投放数据背书,不是凭空说"这个好";
    ②版权干净 —— 从网上找图投广告会有法律风险,平台也可能拒审。
    返回里带 asset_url(可直接用于建广告)、尺寸、类型、当时的文案,以及真实的
    花费/点击/CTR/转化。按「有转化的优先,其次 CTR 高的」排序。
    没有历史广告时会如实说没有,不要编。
    """
    try:
        acct = ad_account_id or _default_ad_account_id()
        ads = nb.list_ads(acct, limit=50).get("items", [])
        if not ads:
            return {"creatives": [], "note": "这个账户还没有投过广告,没有可推荐的历史素材"}

        # 拉 ad 层的真实数据,用来给素材排序(没数据的排最后,但仍可选)
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=max(1, min(days, 180)))
        stats = {}
        try:
            for row in nb.get_report_raw("ad", start.isoformat(), end.isoformat(), acct):
                stats[str(row.get("id"))] = row
        except Exception:
            pass   # 报表拿不到不影响推荐,只是没法按效果排序

        seen, out = set(), []
        for ad in ads:
            c = (ad.get("creative") or {}).get("content") or {}
            url = c.get("assetUrl")
            if not url or url in seen:
                continue          # 同一张图被多条广告用过,只推荐一次
            seen.add(url)
            m = stats.get(str(ad.get("id")), {})
            out.append({
                "asset_url": url,
                "type": (ad.get("creative") or {}).get("type") or "IMAGE",
                "size": f"{c.get('width') or '?'}×{c.get('height') or '?'}",
                "来自广告": ad.get("name"),
                "当时的标题": c.get("headline"),
                "当时的描述": c.get("description"),
                "花费": m.get("cost"), "点击": m.get("clicks"),
                "CTR": m.get("ctr"), "转化": m.get("conversions"),
            })

        out.sort(key=lambda x: (-(x["转化"] or 0), -(x["CTR"] or 0), -(x["花费"] or 0)))
        out = out[:max(1, min(top_n, 10))]
        for item in out:                       # 记住类型,复用时才不会判错图片/视频
            _remember_asset_type(item["asset_url"], item["type"])

        return {
            "creatives": out,
            "period": f"{start} ~ {end}",
            "note": ("请用**表格**列给用户看,并把每个素材用 Markdown 图片语法 "
                     "![素材N](asset_url) 插进去让他直接看到图;"
                     "带上真实数据(花费/CTR/转化)说明为什么推荐它。"
                     "明确告诉用户:①这些是他自己账户投过的素材,版权和审核都没问题;"
                     "②想用哪个就说编号,也可以自己点 📎 传新的。"
                     "**没有数据的素材要如实说'这条还没跑出数据',不要编效果。**"),
        }
    except Exception as e:
        return {"error": str(e)}


# ===== 素材查找(外部授权图库)=====
# 和 recommend_creatives 的分工:那个查**自己账户投过的**素材(有真实数据背书),
# 这个查**外部图库的新素材**(账户里没有合适的、或者想换个风格时用)。
# 两者都不许"从网上随便找图" —— 这里只接正规授权图库,每条结果都带许可证。

# 搜出来过的素材地址。**AI 只能从这里面选**,不能自己编一个链接让系统去下载 ——
# 编链接是这个项目反复防的幻觉行为(见 CLAUDE.md 第八节「AI 幻觉执行」)。
#
# **必须按人分桶。** 原来是模块级一份全局表,而同类的 `_CREATIVE_MODELS_BY_USER` /
# `_CREATIVE_PLANS_BY_USER` / `_LANDING_PAGES_BY_USER` 早就分过了 —— 就这张漏了。
# 后果有两层:①B 能直接拿 A 搜出来的地址去 `use_found_creative` 转存、去
# `decompose_creative` 拆解,等于看见了 A 在研究哪些竞品素材;②200 条的上限是全局共享的,
# A 多搜几次就把 B 刚搜到的挤掉,B 再选就被当成"编的地址"拒掉 —— 而他明明刚看到过。
# 这就是坑表里「缓存键忘了带用户 = 数据串号」那条。
_SEARCHED_ASSETS_BY_USER: dict[str, dict] = {}
# 每人最多记多少条搜索结果。原来 200 是**所有人共用**的,现在是每人各一份。
MAX_SEARCHED_ASSETS = 200


def _searched() -> dict:
    """当前这个人搜出来过的素材。不在用户上下文时用 "-" 这个桶
    (和 `_models()` / `_plans()` 完全同一个写法)。"""
    return _SEARCHED_ASSETS_BY_USER.setdefault(CURRENT_USER_ID.get() or "-", {})


def _remember_assets(items: list) -> None:
    """把一批搜索结果记进**这个人自己的**登记表,并按上限淘汰最旧的。"""
    mine = _searched()
    for item in items:
        mine[item["image_url"]] = item
    # 服务是长驻进程,搜的次数多了这张表会一直长。留最近 200 条够用了
    # (用户总是从"刚搜出来的"那批里选,不会回头挑几百次之前的)。
    while len(mine) > MAX_SEARCHED_ASSETS:
        mine.pop(next(iter(mine)))


def search_stock_creatives(keyword: str, count: int = 6, source: str = "auto") -> dict:
    """从**正规授权图库**里找可以合法投广告的图片素材,并给每张图打质量分。

    什么时候用:账户里没有合适的历史素材、或者用户想找新风格的图时。
    (想复用账户自己投过的素材请用 recommend_creatives —— 那个有真实投放数据背书。)

    keyword:英文搜索词,按落地页内容给,如 "roof repair" / "gutter cleaning"。
    count:要几张(1~12,默认6);source:图库,默认 auto(有钥匙的商业图库优先)。

    返回的每张图都带:尺寸、许可证、出处链接,以及**质量评价**
    (推荐/可用/不建议 + 为什么)。只会返回允许商用的素材,不会出现禁止商用的。
    """
    try:
        r = cs.search(keyword, count=count, source=source)
        if r.get("error"):
            return r
        _remember_assets(r["results"])

        if not r["results"]:
            ready = [s["name"] for s in cs.available_sources() if s["ready"]]
            return {"results": [], "note": (
                f"没搜到「{keyword}」的可商用素材(已试:{'/'.join(ready)})。"
                "如实告诉用户没找到,并给两个出路:①换个更通用的英文搜索词试试;"
                "②让他自己点 📎 上传图片。**不要编造素材链接。**")}

        return {
            **r,
            "note": ("用**表格**列给用户看,每张图用 `![素材N](thumbnail)` 插进去让他直接看到。"
                     "每张都必须写明:①**质量评价**(quality.verdict 和 reasons 里的原话);"
                     "②**许可证**(license 字段)——告诉他这些都是允许商用的,可以放心投。"
                     "**质量是'不建议'的也要列出来并说明差在哪**,别偷偷藏起来。"
                     "用户选了哪个,就用那张图的 image_url 调 use_found_creative 转存,"
                     "拿到 assetUrl 之后才能建广告。**不要把 image_url 直接当 asset_url 用。**"),
        }
    except Exception as e:
        return {"error": f"搜素材失败:{str(e)[:200]}"}


# 「这个账户到底在投什么品类」缓存一小会儿。查一次要拉两个列表接口,
# 而一轮对话里可能问好几次竞品,每次都重拉太慢。
_MY_CATS_CACHE: dict = {}
_MY_CATS_TTL = 600.0


def _account_categories(ad_account_id: str = "") -> dict:
    """看这个账户**实际在投什么品类** —— 依据是计划名和广告标题里的真实文字。

    **为什么要有这个**:用户问"同行都在投什么"时并没说品类,AI 就自己编了一个
    (实测编出 roof,而该账户投的其实是 gutter 和 window)。编出来的关键词
    查回来的是**别人行业**的广告,用户看半天全是无关的。
    和命名那条「类型词对不上必须问用户、不许自己编」是同一条规矩。
    """
    # **缓存键必须带上是谁**。原来大家不指定账户时都落在同一个 "_default" 键上,
    # B 登录后会直接读到 A 的品类 —— 每人各绑各的账号(第六之四节),
    # 派生出来的数据同样不能串。
    key = f"{CURRENT_USER_ID.get() or '-'}::{ad_account_id or '_default'}"
    hit = _MY_CATS_CACHE.get(key)
    if hit and (time.time() - hit["at"]) < _MY_CATS_TTL:
        return hit["val"]

    texts: list[str] = []
    try:
        acct = ad_account_id or _default_ad_account_id()
        # limit 只能取平台允许的固定值([5,10,20,50,100,200,500]),50 在列表里
        for c in (nb.list_campaigns(acct, limit=50).get("items") or []):
            texts.append(str(c.get("name") or ""))
        for a in (nb.list_ads(acct, limit=50).get("items") or []):
            texts.append(str(a.get("name") or ""))
            ct = (a.get("creative") or {}).get("content") or {}
            texts.append(str(ct.get("headline") or ""))
            texts.append(str(ct.get("description") or ""))
    except Exception as e:
        return {"error": f"看不了账户在投什么:{str(e)[:120]}"}

    blob = " ".join(texts).lower()
    counts = {t: blob.count(t) for t in KNOWN_AD_TYPES if blob.count(t) > 0}
    ranked = sorted(counts, key=lambda t: -counts[t])
    val = {"品类": ranked, "出现次数": counts,
           "依据条数": len([t for t in texts if t.strip()])}
    _MY_CATS_CACHE[key] = {"at": time.time(), "val": val}
    return val


def native_market_scan(top_n: int = 200, active_only: bool = False) -> dict:
    """**不指定品类**,看整个原生广告市场上什么类型的单子跑得最好。

    什么时候用:用户问「现在什么广告跑得好」「这个平台适合跑什么单子」
    「有什么新机会」这类**开放问题**时。
    **这种时候不要拿他账户已有的品类去框** —— 那会把他困在现有生意里,看不到别的方向。
    (反过来,他明确说了"我要投 roof"时,才用 search_competitor_ads 按品类查。)

    按版位数取全库最靠前的一批,汇总成**品类排行**和**钩子排行**。
    top_n 看多少条(20~500,默认 200)。
    """
    try:
        r = oal.market_scan(top_n=top_n, active_only=active_only)
    except oal.OpenAdLibraryError as e:
        return {"error": str(e),
                "note": "把这段话原样告诉用户。竞品查询用的是公用的一份 key,"
                        "key 出问题不是用户能自己解决的,请他找管理员。"}
    except Exception as e:
        return {"error": f"扫描失败:{str(e)[:200]}"}
    return {**r, "note": (
        "讲给用户听时:①先说**哪几个品类铺得最广**(用版位合计说话),并点几条真实标题当例子;"
        "②再说**哪几种钩子最吃香** —— 那是可以跨品类照搬的,比品类本身更有用;"
        "③如实说明「其它」占了多少,那是词典没覆盖到,**不是市场上没有**;"
        "④**口径必须讲清楚**:版位数=铺了多少个位置,平台不给展示量和点击率,"
        "**绝不许说成点击率或转化率**。"
        "最后问他:要不要挑其中一个品类深入看看(那时再用 search_competitor_ads)。")}


def platform_kind(platform: str = "") -> dict:
    """判断一个投放平台属于哪一类(原生广告平台 / 大媒体 / DSP),
    并说清**该去哪儿找竞品素材来学**。

    用户说「我要在 X 上投 Y」时先调这个:确定 X 是原生平台,就去竞品库里
    捞**所有原生平台**上跑 Y 的素材来拆(不只是 X 自己的),因为同类型平台之间
    创意套路是通用的,池子大得多。认不出的平台**会明说认不出,不许猜**。
    """
    name = (platform or "").strip() or plat.DEFAULT_ID
    d = apk.describe(name)
    if d.get("认不出"):
        return d
    return {**d, "note": (
        f"告诉用户:{name} 属于**{d['类型']}**。"
        f"所以查竞品时不该只看 {name} 上的广告,而是把**所有{d['类型']}**上"
        f"跑同一个品类的广告都捞来学 —— 这类平台之间创意套路是通用的,"
        f"素材池大得多,规律也更可靠。查回来的结果里带 `素材来自这些原生平台`,"
        f"可以把覆盖面报给用户看。")}


def my_ad_categories(ad_account_id: str = "") -> dict:
    """看这个广告账户**自己在投什么品类**(从计划名和广告文案里认)。

    问"同行在投什么"却没说品类时,**先用这个**,别自己编一个关键词。
    """
    r = _account_categories(ad_account_id)
    if r.get("error"):
        return {**r, "note": "看不出来就**直接问用户做什么品类**,不要自己编一个关键词。"}
    if not r.get("品类"):
        return {**r, "note": "账户里认不出已知品类,**必须问用户**「你们主要做什么?」,"
                             "拿到答复再去查竞品,不许自己编关键词。"}
    return {**r, "note": f"这个账户主要在投:{'、'.join(r['品类'])}。"
                         f"查竞品时用这个当关键词,并**告诉用户你是按哪个查的、可以换**。"}


def search_competitor_ads(keyword: str, count: int = 8, country: str = "US",
                          active_only: bool = True, sort_by: str = "placements",
                          min_days: int = 0) -> dict:
    """查**竞品正在投的真实原生广告**(OpenAdLibrary),看同行的广告长什么样、投了多久。

    什么时候用:用户想知道同行在投什么、想找有市场验证的素材和创意思路时。
    比图库强的地方:这些是**真金白银在投的广告**,铺的版位越多、投得越久,
    说明广告主越愿意为它花钱 —— 图库的图只是"好看",没有这个背书。

    keyword:英文关键词,按品类给,如 "gutter guard" / "roof repair"。
      **用户没说做什么品类时,先调 my_ad_categories 看他自己在投什么,不要自己编一个**
      —— 编出来的关键词查回来的是别的行业,用户看半天全是无关的广告;
    count:要几条(默认8,最多50);
    country:两位大写国家码,默认 US(NewsBreak 是美国平台);留空则不限;
    active_only:只看**现在还在投**的,默认开(实测能把结果收窄到约七分之一);
    sort_by:placements(按版位数,默认,铺得越广越说明肯花钱)/ days(按投放天数)/
      recent(最近才开始投的);min_days:只要投放天数 ≥ 这个数的。

    返回每条带:素材、投放天数、版位数、还在不在投、广告网络和投放媒体。
    """
    try:
        r = oal.search(keyword, count=count, country=country, active_only=active_only,
                       sort_by=sort_by, min_days=min_days, pages=2)
    except oal.OpenAdLibraryError as e:
        # key 无效 / 额度用尽 / 平台自己挂了 —— 这几种原因要原样带给用户,
        # 含糊说一句"查不到"的话,他根本不知道该换 key 还是该等一会儿。
        return {"error": str(e),
                "note": "把这段话原样告诉用户。竞品查询用的是**公用的一份 key**"
                        "(配在服务器的 .env 里,5000 次/天),所以 key 出问题时"
                        "**不是用户能自己解决的**,请他找管理员看一下,别让他去翻设置。"}
    except Exception as e:
        return {"error": f"查竞品广告失败:{str(e)[:200]}"}

    # 关键词跟这个账户实际在投的品类对不上时,**代码层直接点出来**。
    # 实测:用户只问了一句"同行都在跑什么广告",AI 自己编了 roof,
    # 而该账户投的是 gutter 和 window —— 查回来的全是别人行业的广告。
    # 光靠提示词嘱咐是不够的(和文案查重、素材登记表是同一个思路)。
    # **素材池要跨平台**:用户在 NewsBreak 上投,该学的不是"NewsBreak 上的广告",
    # 而是**所有同类型平台上的同品类广告** —— 原生广告的玩法在 Taboola /
    # Outbrain / Yahoo 上是通用的,池子大得多,规律也更可靠。
    # 这里把查回来的网络分布如实报出来,让用户看得见覆盖面。
    nets: dict = {}
    for item in r["results"]:
        n = str(item.get("广告网络") or "").strip() or "(未标注)"
        nets[n] = nets.get(n, 0) + 1
    r["素材来自这些原生平台"] = nets

    cats = _account_categories()
    mine = cats.get("品类") or []
    if mine and not any(t in (keyword or "").lower() for t in mine):
        # 只是**提示**,不是拦截。用户自己说了要看别的品类是完全正常的
        # (他可能正想找新方向),不该反过来把他框回现有生意里。
        # 真正要拦的是"用户没给品类、模型自己编一个"那种情况。
        r["ℹ️品类提示"] = (
            f"你搜的是「{keyword}」,而这个账户目前在投的是:{'、'.join(mine)}。"
            f"**这个词要是用户自己说的,就照他说的查,别多问** —— 他可能正想看新方向;"
            f"**只有当这个词是你自己填的**(用户没指定品类)时,才跟他确认一下。"
            f"还有:用户问的要是「现在什么广告跑得好」这类**没有品类的开放问题**,"
            f"就不该在这儿编一个词 —— 应该改用 native_market_scan 扫全市场。")

    _remember_assets(r["results"])

    if not r["results"]:
        return {"results": [], "note": (
            f"没查到「{keyword}」的竞品广告(共匹配 {r.get('总匹配数', 0)} 条)。"
            "如实告诉用户,并建议:①换个更通用的英文词;②把 country 留空试试不限国家;"
            "③关掉 active_only 看看历史投过的。**不要编造广告数据或素材链接。**")}

    return {
        **r,
        "note": ("用**表格**列给用户看,每条用 `![广告N](thumbnail)` 插图。"
                 "**必须带上「投放天数」和「版位数」** —— 这是这个功能的价值所在:"
                 "版位数 = 这条广告铺在多少个位置上,平台不提供曝光量,这是最接近的指标。"
                 "投得久、铺得广 = 广告主愿意持续为它花钱,比图库的图多了市场验证。"
                 "顺便帮用户**总结这些高效广告的共同点**(画面风格、有没有真人、"
                 "有没有价格/优惠字样、文案角度),这比单纯给图有用得多。"
                 "\n\n**必须提醒用户一句**:这些是其它广告主正在投的广告素材,"
                 "**直接拿来投有版权风险,平台也可能拒审**;更稳妥的用法是照着它的"
                 "思路自己拍一张或找张图库的图,或者用 decompose_creative 拆开学套路。"
                 "用户坚持要用的话,调 use_found_creative 转存。"),
    }


# ===== 落地页研究与生成 =====
_LANDING_CANDIDATES_BY_USER: dict[str, dict[str, dict]] = {}
_LANDING_MODELS_BY_USER: dict[str, dict[str, dict]] = {}
_LANDING_PAGES_BY_USER: dict[str, list[dict]] = {}


def _landing_candidates() -> dict[str, dict]:
    return _LANDING_CANDIDATES_BY_USER.setdefault(CURRENT_USER_ID.get() or "-", {})


def _landing_models() -> dict[str, dict]:
    return _LANDING_MODELS_BY_USER.setdefault(CURRENT_USER_ID.get() or "-", {})


def _absolute_previews(pages: list[dict]) -> list[dict]:
    """把 `preview_url` 补成**完整地址**再交给模型。

    只给相对路径的话,模型会自己配一个 host —— 实测编出了 `localhost:3000`,
    用户点开是他本机另一个项目的 404,而且看不出问题出在哪。
    拿不到站点地址(命令行/测试)时保持相对路径,不瞎拼。
    """
    base = str(CURRENT_BASE_URL.get() or "").rstrip("/")
    for page in pages:
        rel = str(page.get("preview_url") or "")
        if base and rel.startswith("/"):
            page["preview_url"] = base + rel
    return pages


def _landing_pages() -> list[dict]:
    return _LANDING_PAGES_BY_USER.setdefault(CURRENT_USER_ID.get() or "-", [])


def search_competitor_landing_pages(keyword: str, count: int = 8, country: str = "US",
                                    active_only: bool = True, min_days: int = 0) -> dict:
    """找疑似优质落地页候选。

    口径和竞品素材一致:从 OpenAdLibrary 按品类找还在投、版位数多、投放久的广告。
    OpenAdLibrary 的公共 API 不给原始点击 URL，但会给 landingDomain；再用落地页
    语料接口补充截图、抓取次数和关联广告数。截图可用时可以直接做视觉拆解。
    """
    try:
        r = oal.search_landing_candidates(keyword, count=count, country=country,
                                          active_only=active_only, min_days=min_days)
    except oal.OpenAdLibraryError as e:
        return {"error": str(e),
                "note": "把错误原样告诉用户。OpenAdLibrary 用的是服务器公用 key。"}
    except Exception as e:
        return {"error": f"查落地页候选失败:{str(e)[:200]}"}

    items = list(r.get("results") or [])
    domain_count = len({str(x.get("落地页域名") or "").lower() for x in items
                        if str(x.get("落地页域名") or "").strip()})
    # 精确短语常命中一批 search-arbitrage 广告，它们为了 leak-safe 不给域名。
    # 候选太少时只放宽一次到品类主词，仍然逐条做相关性筛选，不无限重试。
    words = [w for w in str(keyword or "").split() if len(w) > 2]
    already_broadened = "自动降级为品类主词" in str(r.get("数据状态") or "")
    if domain_count < min(3, max(1, int(count or 8))) and len(words) > 1 and not already_broadened:
        try:
            wider = oal.search_landing_candidates(words[0], count=min(12, max(6, int(count or 8))),
                                                   country=country, active_only=active_only,
                                                   min_days=min_days)
            known = {str(x.get("image_url") or "") for x in items}
            items.extend(x for x in (wider.get("results") or [])
                         if str(x.get("image_url") or "") not in known)
            r["候选池"] = str(r.get("候选池") or "") + f"；域名不足时补查主词 {words[0]}"
        except Exception:
            pass

    out = []
    bucket = _landing_candidates()
    seen_domains = set()
    screenshot_failures = 0
    for item in items:
        domain = str(item.get("落地页域名") or "").strip()
        url = str(item.get("落地页链接") or "").strip()
        marker = domain.lower() or url
        if (not domain and not url) or marker in seen_domains:
            continue
        seen_domains.add(marker)
        landing_info = {}
        if domain:
            try:
                landing_info = oal.landing_page_info(domain)
            except Exception as e:
                landing_info = {"detail_error": str(e)[:160]}
        reported_shot = str(landing_info.get("screenshotUrl") or "")
        shot_ok = bool(reported_shot and oal.screenshot_available(reported_shot))
        if reported_shot and not shot_ok:
            screenshot_failures += 1
        cid = uuid.uuid4().hex[:8]
        candidate = {
            "id": cid,
            "keyword": keyword,
            "title": item.get("title"),
            "body": item.get("文案"),
            "landing_url": url,
            "landing_domain": domain,
            "screenshot_url": reported_shot if shot_ok else "",
            "reported_screenshot_url": reported_shot,
            "screenshot_available": shot_ok,
            "ad_reference_image": item.get("image_url"),
            "landing_evidence": {
                "抓取次数": landing_info.get("captures"),
                "关联广告数": landing_info.get("linkedAdCount"),
                "首次抓取": landing_info.get("firstCapturedAt"),
                "最近抓取": landing_info.get("lastCapturedAt"),
            },
            "can_decompose": bool(url or domain or shot_ok),
            "performance_signal": {
                "版位数": item.get("版位数"),
                "投放天数": item.get("投放天数"),
                "还在投": item.get("还在投"),
                "广告网络": item.get("广告网络"),
            },
            "confidence": "中高" if shot_ok else ("中" if url else "低"),
            "why_candidate": ("有 OpenAdLibrary 落地页截图，可结合页面文字拆解"
                              if shot_ok else "截图暂不可用，将按域名当前公开页面文字做降级拆解"),
        }
        bucket[cid] = candidate
        out.append(candidate)
        if len(out) >= int(count or 8):
            break
    while len(bucket) > 80:
        bucket.pop(next(iter(bucket)))

    return {
        "results": out,
        "query": keyword,
        "候选池": r.get("候选池"),
        "筛选": r.get("筛选"),
        "数据状态": r.get("数据状态", "OpenAdLibrary 实时查询成功"),
        "截图异常数": screenshot_failures,
        "note": ("用表格列出候选，必须带 candidate id、域名、版位数、投放天数、抓取次数和置信度。"
                 "对 `screenshot_available=true` 的候选，必须用 `![落地页参考](screenshot_url)` 直接展示截图；"
                 "图片可点击放大。若截图不可用，可以展示 `ad_reference_image`，但必须明确标为广告素材参考，"
                 "绝不能说成落地页截图。提醒用户：优质仅指持续投放/版位/抓取等可观察信号，不是实际 CTR/CVR。"
                 "用户选中后，把 candidate id 传给 decompose_landing_page。"),
    }


def decompose_landing_page(candidate_id: str = "", url: str = "", lang: str = "zh") -> dict:
    """拆解一个落地页:排版、文字、offer、表单、信任背书、CTA 节奏。

    candidate_id 来自 search_competitor_landing_pages;也允许用户直接给 url。
    拆解结果会存起来,之后 summarize_landing_page_patterns 会汇总这些页面并生成 2 个新页面。
    """
    candidate, page = {}, {}
    if candidate_id:
        candidate = _landing_candidates().get(candidate_id.strip()) or {}
        if not candidate:
            return {"error": "没找到这个 candidate_id。请先查落地页候选,或直接传 url。"}
        url = str(candidate.get("landing_url") or url or "")
        domain = str(candidate.get("landing_domain") or "")
        if url:
            page = lp.fetch_page(url)
            page["evidence_scope"] = "用户或平台提供的完整落地页 URL"
        elif domain:
            page = lp.fetch_page("https://" + domain)
            page["evidence_scope"] = "落地页域名的当前公开首页，可能不是广告当时的完整路径"
    elif url:
        page = lp.fetch_page(url)
        page["evidence_scope"] = "用户直接提供的完整 URL"
    else:
        return {"error": "请传 candidate_id 或完整落地页 URL。"}

    screenshot_url = str(candidate.get("screenshot_url") or "")
    image, mime_or_error = (lp.fetch_image(screenshot_url) if screenshot_url else (None, "没有可用截图"))
    if image:
        model = lp.decompose_screenshot(image, mime_or_error, page, ad_context=candidate, lang=lang)
        evidence_used = ["OpenAdLibrary 落地页截图", page.get("evidence_scope")]
    elif not page.get("error"):
        model = lp.decompose(page, ad_context=candidate, lang=lang)
        evidence_used = [page.get("evidence_scope"), f"截图未使用：{mime_or_error}"]
    else:
        return {"error": "截图和页面文字都无法取得，不能可靠拆解。",
                "截图错误": mime_or_error, "页面错误": page.get("error"),
                "note": "请换一个候选，或让用户提供完整 URL/页面截图。"}
    if model.get("error"):
        return model
    key = candidate_id.strip() or page.get("url") or url
    wrapped = {"candidate": candidate, "page": {k: v for k, v in page.items() if k != "text"},
               "evidence_used": evidence_used, "analysis": model}
    _landing_models()[key] = wrapped
    while len(_landing_models()) > 40:
        _landing_models().pop(next(iter(_landing_models())))
    return {
        "落地页拆解": wrapped,
        "已拆解总数": len(_landing_models()),
        "note": ("重点讲三个部分:①为什么可能表现好;②关键词和关键信息;"
                 "③哪些优点可以迁移、哪些不能照搬。拆够 2 个后,"
                 "可以调用 summarize_landing_page_patterns 汇总并生成两个新落地页。"),
    }


def summarize_landing_page_patterns(brand: str = "", offer: str = "",
                                    audience: str = "", lang: str = "zh",
                                    n_variants: int = 2) -> dict:
    """汇总已拆解落地页,并生成新的 HTML 落地页草稿。

    要先拆解至少 1 个真实页面;2 个以上更稳。生成结果会保存到本地,
    返回 preview_url 供用户点击预览。

    `n_variants`:**用户说要几个就是几个**(1 或 2)。默认 2 是为了做 A/B;
    他明确说「只要一个」时必须传 1 —— 多生成一版是白花的模型钱,
    而且逼他在两个里挑,等于没听他说话。
    """
    models = list(_landing_models().values())
    if not models:
        return {"error": "还没有拆解过落地页。请先用 decompose_landing_page 拆至少 1 个页面。"}
    want = 2 if int(n_variants or 2) >= 2 else 1
    out = lp.summarize(models, brand=brand, offer=offer, audience=audience,
                       lang=lang, n_variants=want)
    if out.get("error"):
        return out
    pages = _absolute_previews(lp.save_pages(out.get("新落地页") or [], limit=want,
                                             owner=CURRENT_USER_ID.get() or ""))
    out["新落地页"] = pages
    _LANDING_PAGES_BY_USER[CURRENT_USER_ID.get() or "-"] = list(pages)
    return {
        **out,
        "依据页面数": len(models),
        "不能发布的版本": [p.get("file") for p in pages if not p.get("可发布")],
        "本次生成版本数": len(pages),
        "note": (f"先总结共同规律和 offer 设计建议,再把这 {len(pages)} 版的 preview_url "
                 "**逐条列给用户,让他能点开看**。"
                 "**preview_url 必须原样照抄,一个字都不许改** —— 尤其不许自己拼 "
                 "host 或端口(编出来的地址打开是别的东西的 404)。"
                 "提醒用户:这是可预览的 HTML 初稿,学的是结构和说服逻辑,没有复制竞品页面。"
                 "**任何一版的「可发布」是 false 时,必须在回复里明确说出来**:"
                 "把「⚠️问题」原样讲给用户(通常是没有 CTA 占位符,或者 CTA 是 alert 这类假按钮),"
                 "说明这一版**发布时会被拒绝**,并主动问他要不要重新生成 —— "
                 "绝不能只报 preview_url 就完事,那样他会一路走到发布才发现白做。"),
    }


def list_cloudflare_landing_resources(query: str = "", limit: int = 50) -> dict:
    """发布前读取可选域名、既有 Pages 项目和域名映射；只读，不会修改 Cloudflare。

    顺带返回这个人的**脚本库**存了哪些追踪域名(只给域名和日期,不回显脚本原文)——
    这样 AI 收集发布材料时能直接说清哪些域名不用再贴脚本。
    """
    try:
        result = cfp.list_resources(query=query, limit=limit)
        result["已存脚本的追踪域名"] = cfs.listing(CURRENT_USER_ID.get()) or "还没存过（第一次发布要贴一次）"
        return result
    except Exception as e:
        return {"error": str(e)[:500]}


def _generated_landing_file(filename: str) -> Path:
    """按文件名取出这个人自己生成的落地页。**别人的取不到**(见 lp.resolve_preview)。"""
    path = lp.resolve_preview(filename, CURRENT_USER_ID.get() or "")
    if path is None:
        raise ValueError(f"找不到生成的落地页文件：{Path(str(filename or '')).name or '(空)'}")
    return path


def swap_landing_image(landing_file: str, slot: int, query: str) -> dict:
    """把已经生成好的落地页里**某一张图**换成按新关键词重新找的一张,别的一个字不动。

    用户说「第 2 张图不合适,换成正在装屋顶的工人」时用它。
    `landing_file` 是生成结果里的 `file`;`slot` 是第几张(页面里 data-slot 的编号);
    `query` **必须是英文**关键词,写具体些(`metal roof installation crew` 比 `roof` 好)。

    **别为了换一张图去重新生成整页** —— 那要等模型再出一整页、再花一次钱,
    而且文案版式全会跟着变,用户刚看顺眼的东西就没了。
    """
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        return {"error": "slot 要是个数字(第几张图)"}
    r = lp.swap_image(landing_file, slot, str(query or "").strip(),
                      owner=CURRENT_USER_ID.get() or "")
    if "error" in r:
        return r
    return _absolute_previews([r | {"preview_url": f"/landing-pages/{Path(landing_file).name}"}])[0]


def propose_landing_images(landing_file: str, slots: str = "") -> dict:
    """登记一个「**用 AI 把落地页上没配上的图生出来**」的待办(不会立即执行!)。

    什么时候用:生成落地页之后,返回里说「第 N 张图没配上,位置留着但不显示」——
    那是图库没找到(家装类的可商用图本来就薄)。用这个把它们生出来。

    **生图要花钱**(每张约 $%.2f),所以走和建广告一样的保险箱:先报价、
    用户在**下一条消息**明确同意,才 `confirm_action`。

    `landing_file` 是生成结果里的 `file`;
    `slots` 可选,像 "1,3" 那样指定只生哪几张,留空 = 全部没配上的都生。
    画面描述**直接用当初写在页面上的那句**,不许在这一步另写 ——
    否则报价时说的是 A、做出来的是 B。
    """ % cr.COST_PER_IMAGE_USD
    owner = CURRENT_USER_ID.get() or ""
    if slots.strip():
        want = set()
        for piece in _re.split(r"[^0-9]+", slots):
            if piece.strip():
                want.add(int(piece))
        # **点名了就允许重做已经有图的那一张。** 没配图库钥匙时 `swap_landing_image`
        # 永远成功不了,「换掉这张图」只剩这一条路 —— 不放行就是个死胡同
        # (第六之二十节:拒绝必须带出路,而出路得是真走得通的那条)。
        every = lp.pending_image_slots(landing_file, owner, include_filled=True)
        todo = [x for x in every if x["slot"] in want]
        if not todo:
            return {"error": "第 %s 张图这一版里没有。这一版的图位是:%s"
                             % ("、".join(str(n) for n in sorted(want)),
                                "、".join("第%d张%s" % (x["slot"], "(已有图)" if x["filled"] else "(空着)")
                                          for x in every) or "一个都没有")}
    else:
        todo = lp.pending_image_slots(landing_file, owner)
        if not todo:
            return {"error": "这一版落地页没有「还没配上图」的位置。",
                    "note": "**这不是出错** —— 要么图已经配齐了,要么这一版本来就没安排配图。"
                            "想**换掉**某一张已有的图,再说一次并点名第几张"
                            "(slots 填「2」),我会用 AI 把那一张重做一遍。"}

    cost = len(todo) * cr.COST_PER_IMAGE_USD
    # **免费又可能失败的事排在花钱之前**:余额查一下,不够就现在说,别等他点完头才发现
    left = None
    try:
        left = cr.check_balance()
    except Exception:
        pass
    if left is not None and left < cost:
        return {"error": "生图通道余额不够:还剩 $%.2f,这一单大约要 $%.2f。请先充值。" % (left, cost),
                "note": "**没有登记任何待办**,充完值直接再说一次就行。"}

    candidate = {
        "type": "make_landing_images",
        "landing_file": Path(str(landing_file)).name,
        # **快照**:报价时给他看的是这几句画面描述,执行时照着这份做。
        # 不存「第几张」再回去现查 —— 页面可能被 swap_landing_image 改过。
        "slots": todo,
        "user_id": CURRENT_USER_ID.get(), "seq": _seq(),
    }
    dup = _find_duplicate(candidate)
    if dup:
        return {"action_id": dup, "note": f"这一单之前已经登记过(编号 {dup}),不要重复登记,"
                                          f"用户同意后直接 confirm_action('{dup}')。"}
    aid = uuid.uuid4().hex[:8]
    _put_action(aid, candidate)
    # **重做要单独说一遍。** 花钱换掉一张他已经看过的图,和"把空位补上"是两件事,
    # 混在一起说他不会注意到原来那张要没了(和「提案里给的地址,用户一定会点」同一条)。
    redone = [x["slot"] for x in todo if x.get("filled")]
    redo_note = ("**必须单独讲清楚:第 %s 张原本已经有图了,重做会把原来那张换掉、"
                 "而且照样收费。** 问他是不是真要重做。"
                 % "、".join(str(n) for n in redone)) if redone else ""
    print(f"[write-op] 登记落地页配图待办 {aid}: {len(todo)} 张"
          + (f"(其中重做 {len(redone)} 张)" if redone else ""), flush=True)
    return {
        "action_id": aid,
        "要生几张": len(todo),
        "每张画什么": [{"第几张": x["slot"], "画面": x["want"],
                        **({"⚠️": "这个位置**已经有图了**,重做会把原来那张换掉"}
                           if x.get("filled") else {})} for x in todo],
        "尺寸": f"{cr.AD_SIZE[0]}×{cr.AD_SIZE[1]}",
        "预估花费": f"约 ${cost:.2f}(每张约 ${cr.COST_PER_IMAGE_USD:.2f})",
        **({"生图通道余额": f"${left:.2f}"} if left is not None else {}),
        "note": ("向用户复述:要生几张、每张画什么、**大概花多少钱**、以及待办编号 %s。"
                 "说明两件事:①出的是**干净的实拍照片,图上一个字都没有**"
                 "(落地页的标题文案是 HTML 排的,图上再来一遍就重复了);"
                 "②**确认之后立刻扣钱**,生完直接填进页面,刷新预览就能看到。"
                 "**在他明确同意之前绝不许 confirm_action。**%s" % (aid, redo_note)),
    }


def _execute_make_landing_images(a: dict) -> dict:
    """真生图、真花钱,然后填进落地页。

    **有失败的要点名说是哪几张**(和三层开关、多条广告同一条规矩);
    成功的那几张钱已经花了,页面必须留下来。
    """
    owner = a.get("user_id") or ""
    todo = a.get("slots") or []
    need = len(todo) * cr.COST_PER_IMAGE_USD
    # 执行时再查一次余额 —— 待办可能在保险箱里躺了很久
    try:
        left = cr.check_balance()
    except Exception:
        left = None
    if left is not None and left < need:
        return {"error": "生图通道余额不够:还剩 $%.2f,这一单大约要 $%.2f。**一张都没生,没扣钱。**"
                         "充完值直接说一声重新确认就行(待办还在)。" % (left, need)}

    made, failed = [], []
    for i, one in enumerate(todo):
        tag = "第%s张" % one.get("slot")
        try:
            # 落地页配图**不叠字**:标题是 HTML 排的,图上再来一遍就重复了。
            # variant=i 让同一页的几张换镜头语言,不然几张长得一样。
            img = cr.render(str(one.get("want") or ""), variant=i)
            # **先落盘再改页面** —— 到这一步钱已经花掉了
            name = lp.fill_image_slot(a["landing_file"], int(one["slot"]), img, ".jpg", owner=owner)
            made.append({"第几张": one.get("slot"), "画面": one.get("want"), "文件": name})
        except Exception as e:
            failed.append({"第几张": one.get("slot"), "原因": str(e)[:180]})

    if not made:
        return {"error": "一张都没生成:" + ";".join("%s %s" % (f["第几张"], f["原因"]) for f in failed),
                "note": "**没有扣到钱的那几张不会重复计费**,查清原因后可以重新登记。"}
    out = {"done": True, "生好并填进页面": made, "张数": "%d / %d" % (len(made), len(todo)),
           "preview_url": "/landing-pages/" + Path(str(a["landing_file"])).name}
    if failed:
        out["没生成的"] = failed
    out["note"] = ("图已经填进落地页了,**让用户刷新预览看看**。"
                   + (f"⚠️ 有 {len(failed)} 张没生成(见「没生成的」),**必须点名告诉用户是哪几张、为什么**。"
                      if failed else ""))
    return _absolute_previews([out])[0]


def propose_publish_landing_pages(domain: str, slug: str, cta_url: str,
                                   tracking_script: str = "", variant_a_file: str = "",
                                   variant_b_file: str = "",
                                   allow_replace: bool = False,
                                   tracking_script_b: str = "",
                                   allow_identical: bool = False) -> dict:
    """登记 Cloudflare Pages A/B 发布待办；用户下一条消息确认后才真正发布。

    tracking_script **可以不传**:同一个追踪域名的脚本只需要贴一次,
    以后按 CTA 的域名从脚本库里自动取(见 clickflare_scripts.py)。
    第一次用某个追踪域名时没得取,会明确报错请用户贴一次,**绝不许自己编**。

    allow_identical:A/B 两版内容完全相同时才用得上,**只有用户明确说要做 A/A 测试
    (拿两个一样的页面验证分流准不准)才允许传 true**。

    allow_replace:**只有用户明确说了「覆盖发布」才允许传 true**。
    发布是整站替换,本地历史目录丢了的话这一下会把线上旧实验全删掉
    (ClickFlare 里的 Lander 就会指向 404)。默认 false = 遇到这种情况直接拒绝。
    """
    try:
        domain = cfp.normalize_domain(domain)
        slug = cfp.normalize_slug(slug)
        uid = CURRENT_USER_ID.get()
        cta_url = str(cta_url or "").strip()
        script = str(tracking_script or "").strip()
        reused = None
        if not script:
            # 没给脚本 → 按 CTA 的域名到**这个人**的脚本库里找。
            # 能这么干是因为实测过:同一追踪域名下所有 Lander 的脚本逐字节相同。
            host = (urlparse(cta_url).hostname or "").lower()
            reused = cfs.lookup(uid, host) if host else None
            if not reused and host and cfc.configured():
                # 脚本库里没有,就直接从 ClickFlare 现取(实测:模板 + 域名拼出来的
                # 和用户手工复制的脚本 md5 完全相同)。这样他一次都不用贴。
                try:
                    fetched = cfc.lander_script(host)
                    cfs.remember(uid, fetched)
                    reused = cfs.lookup(uid, host)
                except Exception:
                    reused = None      # 取不到就走下面的老路子,请用户贴一次
            if not reused:
                stored = "、".join(x["追踪域名"] for x in cfs.listing(uid)) or "(还没存过任何脚本)"
                return {"error": (
                    f"没有登记发布待办:脚本库里没有 {host or '这个域名'} 的 Lander Tracking Script。"
                    f"你已经存过的追踪域名:{stored}。"),
                    "note": ("没能从 ClickFlare 自动取到(可能这个域名不在这个账号里,或接口暂时不可用)。"
                             "请用户到 ClickFlare 后台复制**这个追踪域名**的 Lander Tracking Script "
                             "完整贴过来(含 <script>…</script>)。**同一个追踪域名只用贴这一次**,"
                             "以后同域名的发布会自动复用。**严禁自己编造、拼接或改写脚本。**")}
            script = reused["script"]
        cfp.validate_clickflare(cta_url, script)
        # B 版有自己的脚本时,同样要校验(留空 = 两版共用 A 的那段)
        if str(tracking_script_b or "").strip():
            cfp.validate_clickflare(cta_url, tracking_script_b)
        # 校验通过了才存 —— 存进去的必然是「形状正确、且和 CTA 同一个追踪域名」的那段。
        # 提前存(而不是等发布成功)的好处:这次哪怕因为域名冲突被拒,重来时也不用再贴一遍。
        saved = {"stored": False}
        if reused is None:
            saved = cfs.remember(uid, script)
        else:
            cfs.touch(uid, reused["domain"])
        latest = _landing_pages()
        if not variant_a_file and len(latest) >= 1:
            variant_a_file = str(latest[0].get("file") or "")
        if not variant_b_file and len(latest) >= 2:
            variant_b_file = str(latest[1].get("file") or "")
        file_a = _generated_landing_file(variant_a_file)
        file_b = _generated_landing_file(variant_b_file)
        # 提案阶段就检查代码层占位符，不能等用户确认后才发现页面无法安全注入。
        cfp.inject_tracking(file_a.read_text(encoding="utf-8"), cta_url, script)
        cfp.inject_tracking(file_b.read_text(encoding="utf-8"), cta_url,
                            str(tracking_script_b or "").strip() or script)
        # A/B 两版一模一样 = 这个实验从一开始就问不出任何东西:花一周的钱,
        # 分出来的「胜者」只是噪音。而且**从外面完全看不出来** ——
        # 两个网址不同、两页都正常、数据也照常上报。所以只能在这里拦。
        # (A/A 测试是个真实用法:拿两个一样的页面验证分流准不准。所以留了口子,
        #  但必须用户明确要求,不许 AI 自己带上。)
        sha_a = hashlib.sha256(file_a.read_bytes()).hexdigest()
        sha_b = hashlib.sha256(file_b.read_bytes()).hexdigest()
        if sha_a == sha_b and not allow_identical:
            same_file = file_a.name == file_b.name
            return {"error": (
                f"没有登记发布待办。A/B 两版{'用的是同一个文件' if same_file else '内容一模一样'}"
                f"({file_a.name}{'' if same_file else ' / ' + file_b.name})——"
                "这样发出去,两个网址各跑一个**完全相同**的页面,钱照花、数据照上报、"
                "页面也一切正常,但这个实验问不出任何东西,分出来的「胜者」只是噪音。"),
                "note": ("如实讲给用户,并问他要哪一种:"
                         "①重新生成两版**方向不同**的页面(推荐,A/B 的意义就在这);"
                         "②他确实是想做 **A/A 测试**(用两个一样的页面验证分流准不准),"
                         "请他明确说一句,你再带 allow_identical=true 重新登记。"
                         "**不许自己替他决定,更不许直接带上这个参数。**")}
        zone = cfp.owned_zone(domain)
        resources = cfp.list_resources(query=zone, limit=10)
        # **提案阶段就把覆盖风险查出来**,和占位符检查一个道理:
        # 不能等用户点了头、真要上传时才发现"这一下会删掉线上的旧页面"
        risk = cfp.replace_risk(domain, slug)
        # 目标域名上已经有东西在跑?**提案阶段就拦住** ——
        # 绑定会接管主机名,把现有页面弄下线,而那可能是一个在投的落地页
        conflict = cfp.domain_conflict(domain)
    except Exception as e:
        return {"error": str(e)[:500]}

    if conflict["conflict"]:
        what = "；".join(f"{r['type']} → {r['content']}" for r in conflict["records"])
        return {"error": (
            f"没有登记发布待办。{domain} 上已经有 DNS 记录在服务（{what}）。"
            "把它绑给 Pages 项目会**接管这个主机名，现有页面当场下线** —— "
            "如果它正在承接广告流量，转化会静静地归零。"),
            "note": (f"如实讲给用户，并建议他换一个没被占用的子域名，"
                     f"比如 lp.{conflict['zone']} 或 go.{conflict['zone']}。"
                     "**不要自作主张替他挑域名**，问清楚再登记。"
                     "他确实要用这个域名的话，请他先自己到 Cloudflare 后台删掉那条记录。")}

    if risk["risky"] and not allow_replace:
        lost = "、".join(risk["missing_locally"]) or "无法列出(本地记录也丢了)"
        return {"error": (
            f"没有登记发布待办。Cloudflare 上项目 {risk['project']} 已经存在,"
            f"但本地 data/ 里没有任何历史实验目录 —— 发布是**整站替换**,"
            f"这一次会把线上现有页面全部删掉(已知的旧实验:{lost})。"
            "ClickFlare 里的 Lander 若还指着它们,买来的流量就会落到 404 上。"),
            "note": ("如实把上面这段讲给用户,并给两条路:"
                     "①先恢复服务器上 data/ 目录的备份,再来发布(推荐);"
                     "②确实要整站重来,就请他明确说一句「覆盖发布」,"
                     "你再带 allow_replace=true 重新登记。**不许自己替他决定。**")}

    candidate = {
        "type": "publish_landing_pages",
        "domain": domain,
        "slug": slug,
        "cta_url": cta_url.strip(),
        "tracking_script": script,
        "tracking_script_b": str(tracking_script_b or "").strip(),
        "variant_a_file": file_a.name,
        "variant_b_file": file_b.name,
        "variant_a_sha256": sha_a,
        "variant_b_sha256": sha_b,
        "allow_replace": bool(allow_replace),
        "user_id": CURRENT_USER_ID.get(),
        "seq": _seq(),
    }
    dup = _find_duplicate(candidate)
    if dup:
        return {"action_id": dup,
                "note": f"相同发布待办已经存在（编号 {dup}），请复述后等待用户确认。"}
    action_id = uuid.uuid4().hex[:8]
    _put_action(action_id, candidate)
    mapping = (resources.get("domain_project_mappings") or {}).get(domain) or {}
    project = mapping.get("project") or cfp.project_name_for_domain(domain)
    # 脚本从哪来,要如实讲给用户 —— 复用的那段是他上次贴的,他有权知道用的是哪份、什么时候存的
    if reused is not None:
        script_line = f"复用脚本库里 {reused['domain']} 存过的那段(存于 {reused['saved_at']})"
    elif saved.get("stored"):
        script_line = (f"用户这次提供的,{saved['action']}(追踪域名 {saved['domain']});"
                       "以后同一个追踪域名不用再贴")
    else:
        script_line = "用户这次提供的" + (f";{saved.get('reason')}" if saved.get("reason") else "")
    if str(tracking_script_b or "").strip():
        script_line = f"A/B 各一段 —— A:{script_line};B:用户单独提供的一段"
    script_line += "(内容不在聊天中回显)"
    return {
        "action_id": action_id,
        "pending": {
            "Cloudflare域名": domain,
            "Pages项目": project,
            "项目处理": "复用现有项目" if mapping else "新域名，自动创建项目",
            # **键名里就要带上"现在打不开"**。原来叫 "A版"/"B版",模型照着渲染成
            # 「A版 正式网址」,用户当场点了过去 —— 拿到 ERR_NAME_NOT_RESOLVED,
            # 以为出错了。其实这个域名的 DNS 记录要等确认发布那一刻才创建,
            # 打不开是**对的**。把话写进键名里,模型就没法把它说成"正式网址"。
            "A版(确认发布后才存在,现在打不开)": f"https://{domain}/{slug}/a/",
            "B版(确认发布后才存在,现在打不开)": f"https://{domain}/{slug}/b/",
            "CTA地址": cta_url,
            "追踪脚本": script_line,
            **({"⚠️脚本时效": (reused or {}).get("stale_note")}
               if (reused or {}).get("stale_note") else {}),
            "线上保留的旧实验": "、".join(risk["local_slugs"]) or "无（这是该域名的第一个实验）",
            **({"⚠️覆盖发布": "线上现有页面会被整站替换掉，这是用户明确同意的"}
               if allow_replace and risk["risky"] else {}),
        },
        "note": ("这里只登记了发布待办，尚未创建项目、改 DNS 或上传页面。"
                 "请完整复述域名、项目、A/B 地址和 CTA，等用户下一条消息明确确认后调用 confirm_action。"
                 "**必须主动说明:上面这两个网址现在打不开是正常的** —— 这个域名的 DNS 记录"
                 "要等你确认发布那一刻才创建。别让用户现在去点，点了只会看到"
                 "「无法访问此网站 / ERR_NAME_NOT_RESOLVED」，会以为出错了。"),
    }


def _execute_publish_landing_pages(action: dict) -> dict:
    file_a = _generated_landing_file(action.get("variant_a_file", ""))
    file_b = _generated_landing_file(action.get("variant_b_file", ""))
    sha_a = hashlib.sha256(file_a.read_bytes()).hexdigest()
    sha_b = hashlib.sha256(file_b.read_bytes()).hexdigest()
    if sha_a != action.get("variant_a_sha256"):
        return {"error": "A 版页面在确认前发生了变化，已停止发布，请重新登记。"}
    if sha_b != action.get("variant_b_sha256"):
        return {"error": "B 版页面在确认前发生了变化，已停止发布，请重新登记。"}
    # **执行时要再查一遍「两版是不是一样的」,不能只信登记那一刻查过。**
    # 待办会在保险箱里躺很久(重启也不丢),而检查是后来才加的 ——
    # 实测保险箱里就躺着一条加检查之前登记的假 A/B(A、B 是同一个文件)。
    # 那条的两个 sha 都能对上,于是一路放行,发出去两个网址跑同一个页面。
    # 这是「两阶段流程里,校验只做在前一阶段」的通病(和"待办里要存快照"同一类)。
    if sha_a == sha_b and not action.get("allow_identical"):
        return {"error": "没有发布。这个待办里的 A/B 两版内容**完全相同**"
                         f"({action.get('variant_a_file')} / {action.get('variant_b_file')})——"
                         "发出去两个网址跑的是同一个页面,钱照花、数据照上报,"
                         "但分出来的「胜者」只是噪音。请重新生成两版方向不同的页面;"
                         "确实要做 A/A 测试就明确说一句,重新登记时带 allow_identical=true。"}
    try:
        return cfp.publish_ab(action["domain"], action["slug"], file_a, file_b,
                              action["cta_url"], action["tracking_script"],
                              user_id=action.get("user_id") or "",
                              allow_replace=bool(action.get("allow_replace")),
                              tracking_script_b=action.get("tracking_script_b") or "")
    except Exception as e:
        return {"error": str(e)[:1500]}


# ===== 创意拆解与方案(P0:只出文字,不出图)=====
# 拆解过的素材模型,按 image_url 存着。summarize 那步要一次看多条,
# 靠 AI 把几百行 JSON 在工具参数里传来传去不现实,也容易被截断 ——
# 和 _SEARCHED_ASSETS_BY_USER 一个路数,存在这边,只传编号。
# **按人分开**:B 归纳"共同点"时混进 A 拆过的素材,得出的结论就是错的
# (和 _CREATIVE_PLANS_BY_USER 同理)。
_CREATIVE_MODELS_BY_USER: dict[str, dict[str, dict]] = {}


def _models() -> dict:
    """当前这个人拆解过的素材模型。不在用户上下文时用 "-" 这个桶。"""
    return _CREATIVE_MODELS_BY_USER.setdefault(CURRENT_USER_ID.get() or "-", {})


def decompose_creative(image_url: str) -> dict:
    """拆解一张广告素材:看懂它的版式、画面、文字层、配色、CTA 和文案角度。

    用户想知道"这条广告为什么好""它是怎么设计的"时调它。
    只接受 search_competitor_ads / search_stock_creatives 结果里出现过的地址。

    拆出来的结果会记下来,之后可以用 summarize_creative_patterns 把多条一起归纳。
    """
    info = _searched().get((image_url or "").strip())
    if not info:
        return {"error": "这个素材地址不在搜索结果里。请先用 search_competitor_ads 或 "
                         "search_stock_creatives 搜一次,再拆解结果里的素材。"}
    if info.get("media_type") == "VIDEO":
        return {"error": "这是一条视频广告,现在只能拆解图片。"
                         "视频可以先看它的标题、投放天数和曝光量来判断,"
                         "或者让用户自己看一遍再描述给你。"}
    try:
        data, _name, mime = cs.download(info["image_url"])
        # 把这条广告的文案一并传进去 —— 原生广告的文字不在图上,
        # 只看图的话「文案角度」判断不出来(实测三张图的文字层全是空)
        # 把投放实绩一并喂进去 —— 拆解要回答的是"它为什么跑得动",
        # 只看一张图是答不了的。只传平台真给的字段,不给模型编的余地。
        model = lab.decompose(
            data, mime,
            headline=str(info.get("title") or ""),
            body=str(info.get("文案") or ""),
            perf={"版位数(铺了多少个位置)": info.get("版位数"),
                  "投放天数": info.get("投放天数"),
                  "现在还在投": info.get("还在投"),
                  "广告网络": info.get("广告网络"),
                  "投放媒体": info.get("投放媒体")})
    except Exception as e:
        return {"error": f"拆解失败:{str(e)[:200]}"}
    if model.get("error"):
        return model

    # 把这条素材的战绩带上,归纳时"哪条更值得学"才有依据
    model["_来源"] = {
        "标题": info.get("title"), "投放天数": info.get("投放天数"),
        "预估曝光": info.get("预估曝光"), "落地页域名": info.get("落地页域名"),
        "尺寸": f"{info.get('width')}×{info.get('height')}",
    }
    _models()[info["image_url"]] = model
    while len(_models()) > 60:
        _models().pop(next(iter(_models())))

    return {
        "素材模型": model,
        "已拆解总数": len(_models()),
        "note": ("把「版式/画面主体/文字层/文案角度」讲给用户听,重点说**它为什么有效**。"
                 "「竞品标识」里如果有品牌名,提醒用户那是别人的品牌,我们的方案里不会用。"
                 "拆完两三条之后,可以问用户要不要调 summarize_creative_patterns 出方案。"),
    }


def summarize_creative_patterns(brand: str = "", landing_url: str = "",
                                n_variants: int = 3, lang: str = "zh") -> dict:
    """把已拆解的多条竞品素材归纳成共同套路,并照着写出**我们自己的**文案和画面方案。

    这是"看完同行之后我该怎么做"的那一步。要先用 decompose_creative
    拆过至少 2 条(拆得越多归纳越准)。

    brand:我们自己的品牌名;landing_url:我们的落地页;n_variants:出几版方案(默认3,最多5)。
    产出的方案里不会出现竞品品牌,也不会照抄竞品的具体价格/时效承诺。
    """
    models = list(_models().values())
    if len(models) < 2:
        return {"error": f"目前只拆解了 {len(models)} 条,至少要 2 条才归纳得出规律。"
                         "请先多用 decompose_creative 拆几条竞品素材。"}
    try:
        out = lab.summarize(models, brand, landing_url, n_variants, lang)
    except Exception as e:
        return {"error": f"归纳失败:{str(e)[:200]}"}
    if out.get("error"):
        return out
    _plans().clear()
    _plans().extend(out.get("方案") or [])
    return {
        **out,
        "依据条数": len(models),
        "note": ("完整讲给用户:先说**共同点**(带出现次数),再把**每一版方案**"
                 "用表格列出来(主标题/描述/画面怎么拍/学的是哪一条)。"
                 "说清三件事:①这些方案是照着同行的**套路**写的,不是抄他们的图或文案;"
                 "②里面不含任何竞品品牌名和具体价格承诺;"
                 "③**现在只有方案还没有图**,拿到图有三条路,一并告诉用户:"
                 "**(a) 让系统直接做出来** —— 调 propose_make_creatives,"
                 "出的是**干净的实拍图,图上没有任何文字**(标题和按钮由 NewsBreak 自己渲染,"
                 "建广告时填进去就行),每版换一种镜头(要花一点钱,会先问过他);"
                 "(b) 用 search_stock_creatives 去授权图库找一张对得上的;"
                 "(c) 按「画面怎么拍」自己拍。"
                 "用户选定某一版后,可以直接用那版的主标题和描述去建广告。"),
    }


# 最近一次 summarize 出的方案(供生图工具照做)。**按人分开存** ——
# 全局一份的话,B 一归纳就把 A 的方案冲掉,A 接着生图就照着 B 的方案画了,
# 而生图是要花钱的($0.20/张)。和 _MY_CATS_CACHE 那次串号是同一类问题。
_CREATIVE_PLANS_BY_USER: dict[str, list[dict]] = {}


def _plans() -> list[dict]:
    """当前这个人的创意方案。不在用户上下文(命令行/测试)时用 "-" 这个桶。"""
    return _CREATIVE_PLANS_BY_USER.setdefault(CURRENT_USER_ID.get() or "-", [])

# 生成好的图先存这儿再上传。生图是花过钱的,上传万一失败也不能把图弄丢。
# data/ 已被 .gitignore 排除。
_GENERATED_DIR = Path(__file__).parent / "data" / "generated"
_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
_KEEP_GENERATED = 200        # 本地只留最近这么多张


def _prune_generated() -> None:
    """本地留档只留最近 200 张。

    这些图**已经传进 NewsBreak 素材库**了,本地这份只是"上传失败时别把钱花的东西弄丢"
    的保险。不设上限的话,长驻服务跑几个月磁盘就被它吃掉了
    (每张 1200×628 的 JPG 几百 KB,而且只增不减)。
    """
    try:
        files = sorted(_GENERATED_DIR.glob("*.jpg"), key=lambda f: f.stat().st_mtime)
        for f in files[:-_KEEP_GENERATED]:
            f.unlink(missing_ok=True)
    except Exception as e:
        print(f"[warn] 清理本地留档失败(不影响功能): {e}", flush=True)


def propose_make_creatives(variants: str = "", ad_account_id: str = "") -> dict:
    """把归纳出的方案**做成可投放的广告图**(干净实拍,图上不放文字),登记成待办等确认。

    要先跑过 summarize_creative_patterns —— 只能照着**真归纳出来的方案**做图,
    不接受现编的文案(和 use_found_creative 一个道理,防止 AI 自己造内容去渲染)。

    variants:要做哪几版,如 "1,3";留空=全做。
    **生图要花钱**,所以走确认关卡:先登记,用户同意后才真的生成。
    """
    if not _plans():
        return {"error": "还没有可用的方案。请先用 decompose_creative 拆几条竞品素材,"
                         "再用 summarize_creative_patterns 归纳出方案,然后才能做图。"}

    picked = list(range(len(_plans())))
    if (variants or "").strip():
        picked = []
        for chunk in str(variants).replace(",", ",").split(","):
            chunk = chunk.strip()
            if chunk.isdigit() and 1 <= int(chunk) <= len(_plans()):
                picked.append(int(chunk) - 1)
        picked = sorted(set(picked))
        if not picked:
            return {"error": f"没看懂要做哪几版。现在有 {len(_plans())} 版,"
                             f"请用编号,比如 variants='1,3'。"}

    plans = [_plans()[i] for i in picked]
    cost = len(plans) * cr.COST_PER_IMAGE_USD
    action = {
        "type": "make_creatives", "user_id": CURRENT_USER_ID.get(), "seq": _seq(),
        # **把方案原样快照进待办**,不能只存编号:编号指向的是"当前这份方案列表",
        # 用户还没点头就又归纳了一次的话,列表整个换掉,同样的编号指到别的方案 ——
        # 报价时给他看的是 A,真做出来、真花钱的是 B。快照之后"看到什么就做什么"。
        "plans": plans,
        "indexes": picked, "ad_account_id": ad_account_id or "",
        "summary": f"生成 {len(plans)} 张广告图(第 {'、'.join(str(i + 1) for i in picked)} 版)",
    }
    dup = _find_duplicate(action)
    if dup:
        return {"action_id": dup, "note": f"这一单之前已经登记过,编号 {dup},不要重复登记,"
                                          f"用户同意后直接 confirm_action('{dup}')。"}
    aid = uuid.uuid4().hex[:8]
    _put_action(aid, action)
    print(f"[write-op] 登记待办 {aid}: 生成 {len(plans)} 张广告图", flush=True)
    return {
        "action_id": aid,
        "要做的图": [{"第几版": i + 1, "命名": p.get("命名", ""),
                      "主标题": p.get("主标题", ""), "描述": p.get("描述", ""),
                      "画面": str(p.get("画面怎么拍", ""))[:90],
                      # 关键词是这版图长什么样的真正依据,确认前要能看到
                      "生图关键词": p.get("生图关键词") or {}}
                     for i, p in zip(picked, plans)],
        "尺寸": f"{cr.AD_SIZE[0]}×{cr.AD_SIZE[1]}",
        "预估花费": f"约 ${cost:.2f}(每张约 ${cr.COST_PER_IMAGE_USD:.2f})",
        "note": (f"向用户复述:要做哪几版(列出主标题)、成品尺寸、**大概花多少钱**、"
                 f"以及待办编号 {aid}。说明白两件事:①出的是**干净的实拍图,图上没有文字** ——"
                 f"标题和按钮由 NewsBreak 自己渲染,建广告时填进去就行;"
                 f"②每一版用**不同的镜头**,画面真的不一样,方便测出哪种好使。"
                 f"**这条消息里不许调 confirm_action**,等用户下一条消息同意。"),
    }


def _execute_make_creatives(a: dict) -> dict:
    """真生成:逐版画一张干净的实拍图 → 传进 NewsBreak 换 assetUrl。"""
    # 优先用待办里的快照 —— 那才是当初报价给用户看的东西。
    # 没有快照的是**旧待办**(升级前登记的),回落到按编号取,并如实说明风险。
    plans = a.get("plans")
    if not isinstance(plans, list) or not plans:
        plans = [_plans()[i] for i in a.get("indexes", []) if i < len(_plans())]
    if not plans:
        return {"error": "方案已经不在了(可能中途重新归纳过,或服务重启过)。请重新登记一次。"}
    try:
        acct = a.get("ad_account_id") or _default_ad_account_id()
    except Exception as e:
        return {"error": f"拿不到广告账户:{e}"}

    # 生图很贵(实测约 $0.20/张),所以**先看够不够钱**再动手。
    # 不查的话会出现"3 张生到第 2 张余额见底",前面的钱照花、活没干完。
    need = len(plans) * cr.COST_PER_IMAGE_USD
    left = cr.check_balance()
    if left is not None and left < need:
        return {"error": f"生图通道余额不够:还剩 ${left:.2f},这一单大约要 ${need:.2f}。"
                         f"请先充值,充完直接说一声重新确认就行(待办还在)。"}

    import re as _re

    made, failed = [], []
    for idx, plan in zip(a.get("indexes", []), plans):
        tag = f"第{idx + 1}版「{plan.get('命名') or ''}」"
        local = None      # 每轮清一次:不清的话这一版失败时会报出**上一版**的文件路径
        try:
            # **所有不花钱、但可能失败的准备工作都放在生图之前。**
            # 血泪:第一版把文件名放在 cr.render() 之后算,结果图已经生成、钱已经付了,
            # 却卡在起名这种零成本的事上,那一张的钱就白花了。
            slug = _re.sub(r"[^A-Za-z0-9]+", "-", plan.get("命名") or "").strip("-")[:24]
            fname = f"gen-{slug or 'ad'}-{idx + 1}.jpg"
            scene = str(plan.get("画面怎么拍") or "")
            kws = plan.get("生图关键词")
            kws = kws if isinstance(kws, dict) and any(
                str(v).strip() for v in kws.values()) else None
            if not scene.strip() and not kws:
                raise RuntimeError("这一版既没有「生图关键词」也没有「画面怎么拍」,没法生图")

            # **默认出干净的实拍图,图上不放任何文字和按钮。**
            # NewsBreak 的 headline / description / callToAction 是和 assetUrl
            # 并列的独立字段,平台自己会渲染;图上再来一遍就是重复,
            # 画个假按钮更是和平台的真按钮并排出现。理由详见 cr.render 的注释。
            # variant=idx 让每一版换一种镜头语言 —— 否则三张画面几乎一样,A/B 测不出东西。
            # **优先用方案里的「生图关键词」** —— 那是从竞品素材真拆出来、
            # 再归纳出来的英文专业术语(九个角度见 creative_lab.KEYWORD_DIMENSIONS),
            # 比中文散文精确得多。中文的「画面怎么拍」当兜底。
            img = cr.render(scene, variant=idx, keywords=kws)

            # **图一生成就先落盘。** 到这一步钱已经花掉了($0.20/张),
            # 后面上传再失败的话,不留个副本就是"钱付了、东西没了"。
            # (实测踩过两次:一次卡在起文件名,一次卡在 mediaName 必填。)
            local = _GENERATED_DIR / fname
            try:
                local.write_bytes(img)
                _prune_generated()      # 只留最近若干张,别让磁盘被慢慢吃掉
            except Exception as e:
                print(f"[write-op] 本地留档失败(不影响上传):{e}", flush=True)

            # mediaName 是必填的 —— 存进媒体库时不给就报 400(实测)
            media_name = (str(plan.get("主标题") or plan.get("命名") or "AI creative"))[:60]
            try:
                data = nb.upload_asset(acct, fname, img, "image/jpeg",
                                       save_to_library=True, media_name=media_name)
            except Exception as up:
                # 平台按内容查重,重了就不存媒体库再传一次(和 use_found_creative 同款降级)
                if "409" not in str(up) and "already exists" not in str(up).lower():
                    raise
                data = nb.upload_asset(acct, fname, img, "image/jpeg", save_to_library=False)
            url = data.get("assetUrl") or data.get("url") or ""
            if not url:
                raise RuntimeError("平台没返回素材地址")
            _remember_asset_type(url, "IMAGE")
            made.append({"第几版": idx + 1, "命名": plan.get("命名", ""),
                         "主标题": plan.get("主标题", ""), "描述": plan.get("描述", ""),
                         "asset_url": url})
            print(f"[write-op] 生成素材 {tag} → {url}", flush=True)
        except Exception as e:
            # 图已经生成(钱已花)但后续出错时,把本地副本的位置说出来 —— 别让钱白花
            saved = f"(图已存在 {local})" if local is not None and local.exists() else ""
            failed.append(f"{tag}:{str(e)[:150]}{saved}")

    if not made:
        return {"error": "一张都没做成 —— " + ";".join(failed)}
    detail = f"做好 {len(made)} 张广告图" + (f";另有 {len(failed)} 张失败:" + ";".join(failed) if failed else "")
    return {"done": True, "detail": detail, "created": made,
            "note": ("把每张图用 `![第N版](asset_url)` 插进回复让用户直接看到,"
                     "并列出对应的主标题和描述。说清三件事:"
                     "①**图上是干净的画面,没有文字** —— 标题、描述和行动按钮是"
                     "NewsBreak 自己渲染的独立字段,建广告时填进去就行,烧在图上反而重复;"
                     "②每一版用了**不同的镜头**(远景/特写/仰拍…),方便真正测出哪种画面好使;"
                     "③**图已经传进素材库**,说一句「用第N张建广告」就能直接拿去建。"
                     + ("有失败的要如实点名说明。" if failed else ""))}


def use_found_creative(image_url: str, ad_account_id: str = "") -> dict:
    """把用户选中的素材转存进 NewsBreak,换回建广告要用的 assetUrl。

    **两个搜索工具共用这一个转存**:search_stock_creatives(授权图库)和
    search_competitor_ads(竞品广告)的结果都用它,用户选定哪一张就把那张的
    image_url 原样传进来。
    只接受**搜索结果里出现过的**地址(防止编造链接)。
    """
    info = _searched().get((image_url or "").strip())
    if not info:
        return {"error": "这个素材地址不在刚才的搜索结果里。请先调 search_stock_creatives 搜一次,"
                         "让用户从结果里选,不要自己拼地址。"}
    try:
        acct = ad_account_id or _default_ad_account_id()
        content, filename, ctype = cs.download(info["image_url"])
        try:
            data = nb.upload_asset(acct, filename, content, ctype, save_to_library=True,
                                   media_name=info.get("title", "")[:60])
        except Exception as e:
            # 平台按文件内容查重,同一张图传过就报 409;降级重传照样能拿到 assetUrl
            if "409" not in str(e) and "already exists" not in str(e).lower():
                raise
            data = nb.upload_asset(acct, filename, content, ctype, save_to_library=False)
        asset_url = str(data.get("assetUrl") or data.get("url") or "")
        if not asset_url:
            return {"error": "平台没有返回素材地址,请让用户改用 📎 手动上传"}
        _remember_asset_type(asset_url, nb.creative_type_of(filename, ctype))
        print(f"[stock] 转存素材 {info['source']} → {asset_url}", flush=True)
        return {"asset_url": asset_url, "asset_filename": filename,
                "来自": info["source"], "许可证": info["license"],
                "尺寸": f"{info['width']}×{info['height']}",
                "note": "素材已存进账户素材库,现在可以用这个 asset_url 继续建广告了。"
                        "告诉用户素材来自哪个图库、许可证是什么。"}
    except Exception as e:
        return {"error": f"转存素材失败:{str(e)[:200]}"}


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

_ACTIONS_FILE = Path(__file__).with_name("pending_actions.json")


def _load_actions() -> dict:
    """开机时把保险箱从磁盘捞回来(热重载/重启都不丢待办)。"""
    try:
        if _ACTIONS_FILE.exists():
            got = json.loads(_ACTIONS_FILE.read_text())
            if isinstance(got, dict):
                # 重启前登记的编号也要认 —— 否则重启之后模型一复述旧编号就被当成编的
                for aid in got:
                    _remember_action_id(aid)
                return got
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
# `_REQUEST_SEQ` 只是**发号器**(单调递增,重启后从保险箱里已有的最大号续起)。
# 真正"这一轮是第几号"和"这一轮执行了什么",**必须按请求隔离** ——
# 页面现在允许几段对话同时提问(见 static/index.html 的多线程改造),
# 两个请求同时在飞时,模块级全局会互相踩:
#   · A 刚记下"我执行了待办 X",B 一开头就把台账清空 → A 的回复盖不上 🔒 核验章,
#     甚至因为"有待办没执行 + 回复里有'已创建'"被误判成谎报,当着用户面自我拆穿;
#   · 反过来,B 的回复可能盖上 A 的执行记录,**声称做了它根本没做的事**;
#   · 保险丝更要命:A 登记时是 5 号,B 一来全局变成 6 号,A 同一条消息里再调
#     confirm_action 就**拦不住了** —— 两阶段确认整个失效。
# 所以用 contextvars(和 CURRENT_USER_ID / CURRENT_CREDS 一个路数):
# 每个请求一份,线程里 copy_context() 带得过去,互不干扰。
_REQUEST_SEQ = max((a.get("seq", 0) for a in PENDING_ACTIONS.values()), default=0)
_SEQ_LOCK = threading.Lock()
CURRENT_SEQ: contextvars.ContextVar = contextvars.ContextVar("adbot_seq", default=0)
CURRENT_EXECUTED: contextvars.ContextVar = contextvars.ContextVar("adbot_executed", default=None)


def _seq() -> int:
    """这一轮用户消息的序号(保险丝比对用)。"""
    return CURRENT_SEQ.get()


def _executed() -> list:
    """这一轮的真实执行台账。**不能用可变对象当 ContextVar 的默认值**
    ——那样所有请求会共用同一个 list,等于没隔离。"""
    lst = CURRENT_EXECUTED.get()
    if lst is None:
        lst = []
        CURRENT_EXECUTED.set(lst)
    return lst


# **登记过的编号永远算数。** 拆穿章要回答的是「这个编号是不是编的」,
# 而合法的编号会离开保险箱:确认执行掉了、用户取消掉了、或者模型只是在复述前几轮的事。
# 只看「现在还在不在箱子里」的话,**用户刚成功取消完,正确的回复反而被盖章说是编的**
# (实测到了)。喊错一次狼,这道章以后就没人信了。
#
# 只存编号本身(8 位十六进制),不存内容 —— 它只用来回答「有没有过这个编号」。
# 上限是防长驻进程无限长大,真到了上限,最老的那些早就不会再被提起了。
_KNOWN_ACTION_IDS: set = set()
MAX_KNOWN_ACTION_IDS = 5000


def _remember_action_id(action_id: str) -> None:
    if not action_id:
        return
    if len(_KNOWN_ACTION_IDS) >= MAX_KNOWN_ACTION_IDS:
        _KNOWN_ACTION_IDS.clear()          # 简单粗暴够用:清空只会退回"多提醒一次"
    _KNOWN_ACTION_IDS.add(str(action_id).lower())


def _put_action(action_id: str, candidate: dict) -> None:
    """把待办放进保险箱 —— **所有登记都必须走这里**。

    在这儿把一次关:少了 `user_id` 就直接抛,让它在开发时就炸出来,
    而不是安安静静地留个洞。实测漏过三种(update_status / create_campaign /
    make_creatives),后果是**归属检查形同虚设**(B 能执行也能删掉 A 的待办),
    外加 `_find_duplicate` 会把 A 和 B 的同样请求**归并成同一条**。
    这两样都不会报错,只会在某天变成"我的待办怎么没了/怎么被别人执行了"。
    """
    if "user_id" not in candidate:
        raise RuntimeError(
            f"待办 {candidate.get('type')!r} 登记时没带 user_id —— "
            "归属检查和查重都会失效,不许登记。请在 candidate 里加上 "
            '"user_id": CURRENT_USER_ID.get()')
    PENDING_ACTIONS[action_id] = candidate
    _remember_action_id(action_id)
    _save_actions()


def _find_duplicate(candidate: dict) -> str:
    """保险箱里是否已有内容完全相同的待办?有就返回它的编号(比较时忽略序号)。
    防止 AI 忘了编号就反复登记同一件事,陷入"永远在确认"的死循环。"""
    key = {k: v for k, v in candidate.items() if k != "seq"}
    for aid, existing in PENDING_ACTIONS.items():
        if {k: v for k, v in existing.items() if k != "seq"} == key:
            return aid
    return ""


# 每个工作室能看见、能确认的待办类型。**加新的待办类型时只改这里** ——
# 原来三处(待办清单 / 提示词注入 / confirm_action 的模式闸门)各写死一个字符串,
# 漏改任何一处的表现都不一样:待办登记得了却看不见、或者确认时被拒。
CREATIVE_ACTION_TYPES = ("make_creatives",)
LANDING_ACTION_TYPES = ("publish_landing_pages", "swap_campaign_landers",
                        "make_landing_images")


# 「工作室 → 它管哪几类待办」,同样收成一份。投放助手不走这张表(它全都能看/能确认)。
STUDIO_ACTION_TYPES: dict[str, tuple] = {
    "creative": CREATIVE_ACTION_TYPES,
    "landing": LANDING_ACTION_TYPES,
}


def _mode_action_types(mode: str) -> tuple:
    """这个工作室能看见 / 能确认哪些类型的待办。

    **认不出的模式返回空**(什么都看不见、什么都确认不了),这是 fail-closed 的一半;
    另一半在 `_tool_call` —— 那道闸门不能再硬编码 ("creative","landing"),
    否则模式一改名它整段跳过,归属检查跟着失效。
    """
    return STUDIO_ACTION_TYPES.get(mode, ())



def _my_action(a: dict) -> bool:
    """这条待办归不归**当前这个人**看?

    判据和 `confirm_action` 的归属检查**逐字一致**(宽松那一版):
      · 不在用户上下文(命令行 / 测试 / 后台线程)→ 全都算;
      · 待办上没有归属(按人隔离之前登记的老单子)→ 也算,否则老待办谁都管不了;
      · 两边都有且不相等 → **不是他的**。

    **读和写必须用同一把尺子。** 上一轮只把 confirm/cancel 这一头堵上了,
    读这一头三个地方一个字没改,而 campaign 是默认模式、绝大多数人就待在那儿:
    B 一句「查一下待办」就能拿到 A 待办的**全部原文** —— 广告账户 id、落地页地址、
    预算、标题描述、素材地址、发布用的域名和 CTA(里面就是 A 的 campaign id)。
    更糟的是 `_system_prompt_now` 每轮把它拼进 B 的提示词,还写着
    「用户确认时直接调 confirm_action(用上面的编号)」—— B 照做只会撞上
    「这个待办属于另一个账号」,是个死胡同。
    """
    me = CURRENT_USER_ID.get()
    owner = a.get("user_id")
    return (not me) or (not owner) or owner == me


def list_pending_actions() -> dict:
    """查看保险箱:所有已登记、还没执行的待办(含编号 action_id)。
    用户确认后若不记得编号,先用这个查,严禁重新登记同一件事。"""
    mode = CURRENT_CHAT_MODE.get()
    visible = [(aid, a) for aid, a in PENDING_ACTIONS.items()
               if _my_action(a)
               and (mode in FULL_ACCESS_MODES or a.get("type") in _mode_action_types(mode))]
    return {"pending_actions": [
        # `user_id` 过滤之后必然等于当前用户,**直接不输出**,别写成「不回显」——
        # 那是「有值但不能给你看」(tracking_script 那种),这个是「没有信息量」,
        # 白占提示词 token 还让模型以为这儿藏着什么要跟用户交代的东西。
        {"action_id": aid, **{k: ("[已保存，不回显]" if k in ("tracking_script", "tracking_script_b")
                                    else "[已算好，不回显]" if k == "put_body" else v)
                              for k, v in a.items() if k not in ("seq", "user_id")}}
        for aid, a in visible
    ] or "保险箱是空的,没有待执行的待办"}


def propose_status_change(level: str, object_id: str, status: str, name: str = "",
                         extra_targets: list[dict] | None = None) -> dict:
    """登记一个「开启/暂停」待办(不会立即执行!)。可以一次带上多个对象。

    level: "campaign"/"ad_set"/"ad";status: "ON"(开启)/"OFF"(暂停);
    name: 对象名字,用于向用户复述。
    extra_targets: 一起改的其它对象,格式 [{"level":"ad_set","id":"123","name":"xxx"}, ...]。

    **开启(ON)时通常必须带 extra_targets**:三层是"与"的关系,
    campaign / ad set / ad 全 ON 才会真的投放。只开 campaign 等于没开。
    先用 get_delivery_tree 看清结构,广告组或广告多于一个时**问用户要开哪些**,
    再把选中的放进 extra_targets 一起登记。
    (暂停 OFF 则不需要:关掉 campaign,底下的自然都不投了。)

    登记后必须把**将要改动的完整清单**讲给用户听,等用户在下一条消息里明确同意,
    再用返回的 action_id 调 confirm_action 执行。
    """
    status = status.upper()
    if status not in ("ON", "OFF"):
        return {"error": "status 只能是 ON 或 OFF"}
    if level not in ("campaign", "ad_set", "ad"):
        return {"error": "level 只能是 campaign / ad_set / ad"}

    targets = [{"level": level, "id": str(object_id), "name": name}]
    for t in (extra_targets or []):
        lv, oid = (t.get("level") or "").strip(), str(t.get("id") or "").strip()
        if lv not in ("campaign", "ad_set", "ad") or not oid:
            return {"error": f"extra_targets 里有不合法的对象:{t}"}
        if not any(x["id"] == oid for x in targets):      # 同一个对象别改两遍
            targets.append({"level": lv, "id": oid, "name": t.get("name") or ""})

    candidate = {
        "type": "update_status",
        "level": level, "object_id": str(object_id), "status": status,
        "name": name, "targets": targets,
        "user_id": CURRENT_USER_ID.get(), "seq": _seq(),
    }
    dup = _find_duplicate(candidate)
    if dup:
        return {"action_id": dup, "note": f"这件事此前已登记过(编号 {dup}),无需重复登记。"
                                          f"请向用户复述内容并附上编号,用户同意后直接调 confirm_action。"}
    action_id = uuid.uuid4().hex[:8]
    _put_action(action_id, candidate)
    print(f"[write-op] 登记待办 {action_id}: {status} × {len(targets)} 个对象", flush=True)
    verb = "开启" if status == "ON" else "暂停"
    listed = [f"{verb} {t['level']}「{t['name'] or t['id']}」(id={t['id']})" for t in targets]
    out = {
        "action_id": action_id,
        "pending": listed if len(listed) > 1 else listed[0],
        "note": "已登记待办,尚未执行。请把**上面每一条**都复述给用户并等确认。",
    }
    if status == "ON" and len(targets) == 1 and level == "campaign":
        out["warning"] = ("只开了 campaign 一层!三层全 ON 才会真的投放,"
                          "现在这样广告跑不起来。请先用 get_delivery_tree 看看底下的"
                          "广告组和广告,问用户要开哪些,再重新登记。")
    return out


# ===== 命名规范(按落地页类型)=====
# 例:落地页是屋顶维修 → 类型 roof
#   计划   NB-Roof-260817-01     序号 = 今天这个类型的第几支计划(两位)
#   广告组 260817-Roof-001       序号 = 这支计划里的第几个组(三位)
#   广告   AD-260817-Roof-001    序号 = 这个组里的第几条广告(三位)
# 日期是 **年月日 6 位**(YYMMDD),不是月日 4 位 —— 少了年份的话,
# 明年同月同日的序号会接着今年往下排(查已有名字取最大值 +1),而不是重新从 01 开始。
# 广告比广告组多一个 `AD-` 前缀:两层要是同名,按名字搜就分不出你指的是哪一层
#(平台的列表接口是按名字搜的,报表导出后也只有一列 name)。

# 团队现有的落地页类型。落地页里能对上其中一个就直接用,对不上**必须问用户**,
# 不许自己编一个 —— 命名是团队约定,编出来的类型会让报表对不上号。
# 加新类型只改这个列表,提示词会自动跟着变(见 _system_prompt_now)。
KNOWN_AD_TYPES = ["roof", "gutter", "window", "bathroom"]


def match_known_type(text: str) -> str:
    """在落地页地址/描述里找已知类型,找到返回规范化后的词(如 `Roof`),没有返回空。"""
    low = (text or "").lower()
    for t in KNOWN_AD_TYPES:
        if t in low:
            return _type_word(t)
    return ""


def _type_word(raw: str) -> str:
    """把落地页类型规范成命名用的词:`roof` / `ROOF` / `roof 修缮` → `Roof`。"""
    import re as _re
    w = _re.sub(r"[^A-Za-z0-9]", "", (raw or "").strip())
    return (w[:1].upper() + w[1:].lower()) if w else "Ad"


def _campaign_name(type_word: str, ymd: str, existing_names: list) -> str:
    """算出今天这个类型的下一支计划名。ymd 是 6 位年月日,如 `260817`。

    扫已有的 `NB-Roof-260817-NN`,取最大序号 +1 —— 这样中途删过、
    或者别人也在建,都不会撞名。
    """
    import re as _re
    pat = _re.compile(rf"^NB-{_re.escape(type_word)}-{ymd}-(\d+)$", _re.I)
    mx = 0
    for n in existing_names:
        m = pat.match((n or "").strip())
        if m:
            try:
                mx = max(mx, int(m.group(1)))
            except ValueError:
                pass
    return f"NB-{type_word}-{ymd}-{mx + 1:02d}"


def _child_names(type_word: str, ymd: str, n_ads: int = 1) -> tuple[str, list[str]]:
    """新建计划底下的广告组名,和它下面 n_ads 条广告的名字。

    **广告组序号固定 001** —— 计划是全新的,底下不可能已有别的。
    **广告序号从 001 顺排到 00N**:一个广告组里放多条广告是常规做法
    (同一份预算、同一批人群,只换素材和文案 —— 这才是干净的素材 A/B)。
    原来这里写死只出一条 `001`,于是用户说「一个组下面两条广告」时**根本没有地方
    能接住第二条**,模型只能退回去再建一整条计划 → 同一条计划底下两个同名广告组、
    各带一份日预算(实测用户以为 $20/天,实际 $40/天)。
    广告多带一个 `AD-` 前缀,好和同序号的广告组区分开。
    """
    return (f"{ymd}-{type_word}-001",
            [f"AD-{ymd}-{type_word}-{i:03d}" for i in range(1, max(1, n_ads) + 1)])


def _norm_creatives(primary: dict, extras: list[dict] | None) -> tuple[list[dict], list[str]]:
    """把「主素材 + extra_creatives」拉平成一个广告清单,并逐条体检。

    **一条不合格就整单拒绝**,不做「跳过坏的、把好的建了」——
    用户要的是两条,给他一条还报成功,他要到平台后台才发现少了一条。
    """
    problems: list[str] = []
    out: list[dict] = []
    for idx, c in enumerate([primary] + list(extras or []), start=1):
        if not isinstance(c, dict):
            problems.append(f"第{idx}条广告的素材格式不对(要一个对象,至少含 "
                            f"asset_url / headline / description)")
            continue
        url = str(c.get("asset_url") or "").strip()
        head = str(c.get("headline") or "").strip()
        desc = str(c.get("description") or "").strip()
        if not url.startswith("http"):
            problems.append(f"第{idx}条广告的 asset_url 无效:请先让用户点 📎 上传素材")
        # 90 字符和 3~90 是平台硬规则(实测),两条都要逐条查 —— 第二条超了照样整单被拒
        if not 3 <= len(head) <= 90:
            problems.append(f"第{idx}条广告的标题必须 3~90 个字符(现在 {len(head)} 个)")
        if not 3 <= len(desc) <= 90:
            problems.append(f"第{idx}条广告的描述必须 3~90 个字符(现在 {len(desc)} 个),请精简")
        out.append({"asset_url": url, "headline": head, "description": desc,
                    "asset_filename": str(c.get("asset_filename") or "").strip(),
                    "call_to_action": str(c.get("call_to_action") or "").strip(),
                    "brand_name": str(c.get("brand_name") or "").strip()})
    # **两条一模一样 = 白花一份钱,而且从外面完全看不出来**(两条广告都正常在跑、
    # 数据照常回来,只是分出来的"胜者"是噪音)。和落地页「A/B 两版内容相同直接拒」
    # 是同一条规矩,只能在代码层拦。
    seen: dict[tuple, int] = {}
    for i, c in enumerate(out, start=1):
        key = (c["asset_url"], c["headline"], c["description"])
        if key in seen:
            problems.append(f"第{seen[key]}条和第{i}条广告的素材、标题、描述完全一样 —— "
                            f"同一个广告组里放两条一样的广告,等于花两份钱跑同一条,"
                            f"而且数据上分不出胜负。请改掉其中一条,或者只留一条")
        seen[key] = i
    return out, problems


def _existing_ad_set(ad_account_id: str, campaign_name: str, set_name: str) -> dict:
    """这条计划底下有没有一个已经叫 `set_name` 的广告组?没有(或查不到)返回 {}。

    **查不到就当没有** —— 网络抖一下不该拦住用户建广告;真撞上了执行那一步还会再查一次
    (「两阶段流程里,校验只做在登记那一步是不够的」)。
    接口不支持按父级过滤,只能拉回来按 campaignId 自己筛(和 get_delivery_tree 一样)。
    """
    try:
        camp = next((c for c in nb.list_campaigns(ad_account_id, search=campaign_name).get("items") or []
                     if str(c.get("name") or "") == campaign_name), None)
        if not camp:
            return {}
        cid = str(camp.get("id"))
        for one in nb.list_ad_sets(ad_account_id, limit=100).get("items") or []:
            if str(one.get("campaignId")) == cid and str(one.get("name") or "") == set_name:
                return {"id": str(one.get("id")), "campaign_id": cid}
    except Exception:
        pass
    return {}


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
    extra_creatives: list[dict] | None = None,
) -> dict:
    """登记一个「新建广告」待办(不会立即执行!),会一次建好 campaign+ad set+ad 三层。

    必填:ad_account_id(广告账户id)、keyword(**落地页类型**,英文一个词,如 roof / gutter / window;用于按规范命名)、
    landing_url(落地页链接)、headline(标题)、description(描述)、
    asset_url(素材地址,来自用户上传后的系统消息)。
    budget_dollars(预算,美元,最低10)**可以不传**:不传就用默认日预算,
    但你必须在复述时告诉用户用的是多少、以及为什么。
    出价无需提供:系统使用平台自动出价(MAX_CONVERSION)。
    tracking_id(转化事件id)可选:不填则自动选用账户里的 submit form 或第一个事件。
    可选:asset_filename(素材文件名,用于判断图片/视频)、budget_type(DAILY日预算/TOTAL总预算)、
    brand_name(品牌名,默认用关键词)、call_to_action(按钮文案)、campaign_name(手动指定计划名)。

    **一个广告组下面要放几条广告,用 `extra_creatives`。**
    用户说「一个 ad set 下面两个 ad」「这两套素材各做一条广告」时,
    第一套走上面的 asset_url/headline/description,**第二套起放进 extra_creatives**:
    `[{"asset_url": "...", "headline": "...", "description": "...",
       "asset_filename": "...", "call_to_action": "...", "brand_name": "..."}]`
    广告名会自动排成 AD-年月日-类型-001 / -002 / …。
    **绝不许为了放第二套素材去建第二条计划或第二个广告组** —— 那会变成两个同名广告组
    各带一份日预算(用户以为花 $20,实际 $40),而且报表里两行同名分不出来。
    计划和广告组**已经建好了**才想加广告,用 `propose_add_ad`,不要再调这个。

    登记后必须用表格向用户完整复述整单(**每一条广告都要列出来**),
    等用户下一条消息确认后再 confirm_action。
    """
    # ---- 参数体检:把明显的问题挡在登记之前 ----
    # 没给预算就用默认值(向导会把这个默认值和理由讲给用户听,用户能改)
    used_default_budget = False
    if not budget_dollars or budget_dollars <= 0:
        budget_dollars = DEFAULT_BUDGET_DOLLARS
        used_default_budget = True

    # 每条广告各查一遍(第二条的标题超长也要整单拒,不能只查第一条)
    ads, problems = _norm_creatives(
        {"asset_url": asset_url, "headline": headline, "description": description,
         "asset_filename": asset_filename, "call_to_action": call_to_action,
         "brand_name": brand_name},
        extra_creatives)
    if not landing_url.startswith("http"):
        problems.append("landing_url 必须是 http(s) 开头的完整链接")
    if budget_dollars < 10:
        problems.append("预算最低 $10")
    if budget_type not in ("DAILY", "TOTAL"):
        problems.append("budget_type 只能是 DAILY 或 TOTAL")
    if not keyword.strip():
        problems.append("需要一个命名关键词(英文,如 gutter)")
    if problems:
        return {"error": ";".join(problems)}

    # ---- 自动命名:按落地页类型走团队的命名规范(用户手动指定则优先) ----
    from datetime import datetime, timezone
    # 日期按**北京时间**取(用户说几号就是他那边的几号)。
    # 别用 UTC:北京时间凌晨 0~8 点建的广告会被写成前一天,用户按日期筛就漏掉了。
    ymd = sched.now_beijing().strftime("%y%m%d")            # 260817,带年份
    tw = _type_word(keyword)
    try:
        existing = [c.get("name") or "" for c in
                    nb.list_campaigns(ad_account_id, limit=100).get("items", [])]
    except Exception:
        existing = []            # 查不到就从 01 起,总比建不出来强
    c_name = campaign_name.strip() or _campaign_name(tw, ymd, existing)
    set_name, ad_names = _child_names(tw, ymd, len(ads))
    fallback_brand = (brand_name.strip() or keyword.strip().capitalize())[:40]
    for one_name, c in zip(ad_names, ads):
        c["name"] = one_name
        c["brand_name"] = (c["brand_name"] or fallback_brand)[:40]
        c["call_to_action"] = c["call_to_action"] or call_to_action or "Learn More"

    # **同名广告组已经存在就别再建一个。** 复用已有计划时(用户又提了一次同类型同日期的),
    # 执行那一步会「同名计划已存在,直接复用」,而广告组是**无条件新建**的 ——
    # 结果是同一条计划底下两个同名广告组、各带一份日预算。这是免费的检查,排在登记之前。
    clash = _existing_ad_set(ad_account_id, c_name, set_name)
    if clash:
        return {
            "error": f"计划「{c_name}」底下已经有一个叫「{set_name}」的广告组(id {clash['id']}),"
                     f"不能再建一个同名的。",
            "两条路,请让用户选": {
                "A·把这些素材加进已有的那个组":
                    f"改调 propose_add_ad(ad_set_id=\"{clash['id']}\", ...),"
                    f"广告序号会接着组里已有的往下排(已有 001 就叫 002)。**多数情况选这个。**",
                "B·确实要另起一个广告组":
                    "请用户给这个新组换个名字(传 campaign_name 换一条计划,"
                    "或者明确说要用哪个序号),不能和已有的同名。",
            },
            "note": "**绝不许硬着头皮再建一个同名的。** 两个同名广告组各带一份日预算 —— "
                    "用户以为在花 $20,实际是 $40,而且报表里只有一列 name,"
                    "两行同名他一眼分不出哪个是哪个。**请把上面两条原样问给用户,让他选。**",
        }

    candidate = {
        "type": "create_campaign",
        "ad_account_id": ad_account_id,
        "campaign_name": c_name,
        "ad_set_name": set_name,
        # **存快照,不存「指向当前状态的引用」**:报价时给用户看的就是这几条,
        # 执行时照着这份做(见坑表「待办里要存快照」那条)。
        "ads": ads,
        "landing_url": landing_url,
        "budget_type": budget_type,
        "budget_cents": int(round(budget_dollars * 100)),
        "tracking_id": tracking_id,
        "user_id": CURRENT_USER_ID.get(), "seq": _seq(),
    }
    dup = _find_duplicate(candidate)
    if dup:
        return {"action_id": dup, "note": f"这单建广告此前已登记过(编号 {dup}),无需重复登记。"
                                          f"请向用户复述整单内容并附上编号,用户同意后直接调 confirm_action。"}
    action_id = uuid.uuid4().hex[:8]
    _put_action(action_id, candidate)
    print(f"[write-op] 登记建广告待办 {action_id}: {candidate['campaign_name']}", flush=True)
    a = PENDING_ACTIONS[action_id]
    budget_word = "日预算" if budget_type == "DAILY" else "总预算"
    return {
        "action_id": action_id,
        "pending": {
            "计划名": a["campaign_name"], "广告组名": a["ad_set_name"],
            "这个广告组下面要建几条广告": len(a["ads"]),
            "落地页": landing_url,
            budget_word: f"${budget_dollars:g}" + ("(系统默认值)" if used_default_budget else "")
                         + ("(**整个广告组共用这一份,不是每条广告各一份**)" if len(a["ads"]) > 1 else ""),
            "出价": "自动(MAX_CONVERSION)",
            "转化事件": tracking_id or "自动选用(优先 submit form)",
            "广告": [{"广告名": c["name"], "标题": c["headline"], "描述": c["description"],
                     "品牌名": c["brand_name"], "按钮": c["call_to_action"],
                     "素材": c["asset_filename"] or c["asset_url"]} for c in a["ads"]],
            "创建后状态": "暂停(OFF),需用户确认无误后再开启",
        },
        # 哪些值是系统替用户定的,要明确列出来 —— 用户有权知道"我没说过的东西是谁定的"
        "defaults_used": ([f"{budget_word} ${budget_dollars:g}"] if used_default_budget else [])
                         + ([] if tracking_id else ["转化事件(自动选 submit form)"]),
        "note": ("已登记待办,尚未执行。请用表格向用户完整复述以上内容并等确认。"
                 "**「广告」里有几条就列几条,一条都不许省** —— 用户是按条数验收的。"
                 "**凡是 defaults_used 里列出的项,要额外说明这是系统默认值、为什么这么定、"
                 "以及用户可以直接说要改成多少。**"),
    }


def _pick_id(data: dict, *keys: str) -> str:
    """从平台返回里把 id 抠出来(有时包在 `object` 里)。"""
    for k in keys:
        v = data.get(k)
        if v not in (None, ""):
            return str(v)
    nested = data.get("object")
    if isinstance(nested, dict):
        return _pick_id(nested, *keys)
    return ""


def _ads_from(a: dict) -> list[dict]:
    """待办里的广告清单。

    **老待办回落成一条广告** —— 保险箱是持久化的(重启不丢),里面完全可能躺着
    「支持多条广告」之前登记的单子,那种只有单套 ad_name/headline/asset_url 字段。
    和生图待办「老待办没快照时回落按编号取」是同一个兼容分支。
    """
    if a.get("ads"):
        return a["ads"]
    return [{"name": a.get("ad_name"), "asset_url": a.get("asset_url"),
             "asset_filename": a.get("asset_filename") or "",
             "headline": a.get("headline"), "description": a.get("description"),
             "call_to_action": a.get("call_to_action") or "Learn More",
             "brand_name": a.get("brand_name") or ""}]


def _create_ads(ad_set_id: str, ads: list[dict], landing_url: str) -> tuple[list, list]:
    """在**一个**广告组底下逐条建广告。返回 (建成的, 失败的)。

    **有失败的必须点名说是哪一条**,不能笼统说「部分失败」—— 用户是按条数验收的,
    少建了一条他要到平台后台才发现(和「三层开关有失败的要点名」同一条规矩)。
    """
    made: list = []
    failed: list = []
    for c in ads:
        creative = {
            # 类型优先用上传时记下的(权威),查不到才退回按文件名猜
            "type": _ASSET_TYPES.get(c["asset_url"]) or nb.creative_type_of(c.get("asset_filename") or "", ""),
            "headline": c["headline"],
            "description": c["description"],
            "callToAction": c.get("call_to_action") or "Learn More",
            "brandName": c.get("brand_name") or "",
            "assetUrl": c["asset_url"],
            "clickThroughUrl": landing_url,
        }
        try:
            ad = nb.create_ad(ad_set_id, c["name"], creative, status="OFF")
            made.append({"id": _pick_id(ad, "id", "adId"), "name": c["name"]})
        except Exception as e:
            failed.append({"name": c["name"], "error": str(e)})
    return made, failed


def _execute_create_campaign(a: dict) -> dict:
    """真正执行三层创建。任何一层失败都如实汇报已建成的部分(都处于暂停态,无风险)。"""
    created: dict = {}
    ads = _ads_from(a)

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

    # 第二层:ad set。
    # **建之前再查一次同名的。** 登记时查过一次,但待办会在保险箱里躺着(落盘、重启不丢),
    # 期间用户完全可能又建了一条同名计划/广告组 ——「两阶段流程里,校验只做在登记那一步
    # 是不够的」。漏掉这一次复查的代价:同一条计划底下两个同名广告组、各带一份日预算。
    try:
        for one in nb.list_ad_sets(a["ad_account_id"], limit=100).get("items") or []:
            if str(one.get("campaignId")) == str(campaign_id) \
                    and str(one.get("name") or "") == a["ad_set_name"]:
                return {
                    "error": f"计划「{a['campaign_name']}」底下已经有一个叫「{a['ad_set_name']}」"
                             f"的广告组(id {one.get('id')})了,**这次一个东西都没建**。",
                    "created_so_far": created,
                    "note": "**别再建一个同名的** —— 两个同名广告组各带一份日预算,"
                            "用户以为花 $20 实际花 $40,而且报表里两行同名分不出来。"
                            f"要把这些素材加进已有的那个组,改用 "
                            f"propose_add_ad(ad_set_id=\"{one.get('id')}\", ...) 重新登记;"
                            "确实要另起一个组,请让用户给它换个名字。",
                }
    except Exception:
        pass          # 查不到就照建 —— 不能让一次网络抖动把用户挡在建广告门外
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

    # 第三层:ad(带创意)。**一个广告组下面可以有多条** —— 同一份预算、同一批人群,
    # 只换素材和文案,这才是干净的素材 A/B。
    made, failed = _create_ads(ad_set_id, ads, a["landing_url"])
    created["ads"] = made
    if failed:
        created["没建成的广告"] = failed
    if not made:
        return {"error": "广告全部创建失败:"
                         + ";".join(f"{f['name']}: {f['error']}" for f in failed),
                "created_so_far": created,
                "note": "campaign 和 ad set 已建好(暂停态),可修正素材/文案后单独补建广告"}

    # 建完不轻信:立刻去平台回查一遍,确认真的存在
    try:
        check = nb.list_campaigns(a["ad_account_id"], search=a["campaign_name"])
        found = any(str(item.get("name") or "") == a["campaign_name"] for item in check.get("items") or [])
        created["platform_verified"] = "已回查平台,确认存在 ✅" if found else "⚠️ 回查平台未找到,请人工核实"
    except Exception:
        created["platform_verified"] = "(回查失败,请人工核实)"

    return {
        "done": True, "created": created,
        "建成的广告条数": f"{len(made)} / {len(ads)}",
        "note": ("三层已全部创建,均为暂停(OFF)状态;提醒用户核对后说「开启 xxx」即可开始投放。"
                 + (f"⚠️ 有 {len(failed)} 条广告没建成(见「没建成的广告」),"
                    "**必须点名告诉用户是哪几条、为什么**,别让他以为全都建好了。"
                    if failed else "")
                 + ("这个广告组下面有多条广告,它们**共用同一份日预算**,"
                    "不是每条各一份 —— 请顺带讲给用户听。" if len(made) > 1 else "")),
    }


def _next_ad_names(ad_account_id: str, ad_set_name: str, n: int) -> list[str]:
    """这个广告组里接下来 n 条广告该叫什么。

    序号**不是凭空数的**:扫账户里同前缀的广告名,取最大序号 +1(和 `_campaign_name`
    同一个套路)。这样中途删过、上次已经加到 003、或者用户自己在后台加过,都不会撞名。
    前缀由广告组名去掉尾部序号得到:`260904-Window-001` → `AD-260904-Window-`。
    **按整个账户扫而不是只扫这个组**:同一支计划里两个组(`-001`/`-002`)的广告
    前缀是一样的,只看本组会和兄弟组撞名,而报表里只有一列 name,撞了就分不出来。
    """
    import re as _re
    base = _re.sub(r"-\d+$", "", str(ad_set_name or "").strip())
    prefix = f"AD-{base}-" if base else "AD-"
    used = []
    try:
        rows = nb.list_ads(ad_account_id, search=prefix, limit=200).get("items") or []
    except Exception:
        rows = []          # 查不到就从 001 起,总比建不出来强(和计划名同一条)
    pat = _re.compile(_re.escape(prefix) + r"(\d+)$", _re.I)
    for row in rows:
        m = pat.match(str(row.get("name") or "").strip())
        if m:
            try:
                used.append(int(m.group(1)))
            except ValueError:
                pass
    start = (max(used) + 1) if used else 1
    return [f"{prefix}{i:03d}" for i in range(start, start + max(1, n))]


def propose_add_ad(ad_set_id: str, asset_url: str, headline: str, description: str,
                   ad_account_id: str = "", asset_filename: str = "",
                   call_to_action: str = "", brand_name: str = "",
                   landing_url: str = "", ad_name: str = "",
                   extra_creatives: list[dict] | None = None) -> dict:
    """登记一个「往**已经建好的广告组**里再加广告」的待办(不会立即执行!)。

    **什么时候用它**:计划和广告组已经存在了,用户又想放一条新素材/新文案进去 ——
    「把第二套素材也加进 260904-Window-001」「这个组里再加一条广告」。

    **绝不许拿 propose_create_campaign 干这件事。** 那个是从头建一整条计划;
    对着同一支计划再跑一次,会在它底下多出**一个同名广告组**、多花一份日预算
    (实测:用户以为 $20/天,实际 $40/天,而且报表里两行同名分不出来)。

    必填:ad_set_id(广告组 id —— 用 get_delivery_tree(campaign_id) 查,别按名字猜)、
    asset_url、headline(3~90字)、description(3~90字)。
    可选:landing_url(**不填就自动沿用这个组里已有广告的落地页**)、
    ad_name(不填就按命名规范接着已有序号往下排:已有 001 就叫 002)、
    call_to_action / brand_name(不填就沿用组里已有广告的)、
    extra_creatives(一次加好几条,格式和 propose_create_campaign 的一样)。

    登记后必须用表格向用户完整复述(**每一条广告都要列**),
    等用户下一条消息确认后再 confirm_action。
    """
    try:
        acct = ad_account_id or _default_ad_account_id()
    except Exception as e:
        return {"error": str(e)}

    wanted = str(ad_set_id or "").strip()
    if not wanted:
        return {"error": "要先知道加进哪个广告组。请用 get_delivery_tree(campaign_id) "
                         "把计划底下的广告组列给用户,让他指名一个。"}
    try:
        sets = nb.list_ad_sets(acct, limit=100).get("items") or []
    except Exception as e:
        return {"error": f"查广告组失败: {e}"}
    the_set = next((x for x in sets if str(x.get("id")) == wanted), None)
    if not the_set:
        return {"error": f"这个账户下找不到 id 为 {wanted} 的广告组。",
                "note": "**这不是权限问题,也不是接口限制** —— 就是这个 id 不对。"
                        "**绝不许改用名字去猜是哪个组**(同名的广告组可能不止一个,"
                        "猜错就把广告加到别的组里去了)。请用 get_delivery_tree(campaign_id) "
                        "把结构列给用户,让他指名。"}

    # 落地页 / 按钮 / 品牌名:能从组里已有的广告借就借,别再问用户一遍,也别让模型编
    try:
        siblings = [x for x in (nb.list_ads(acct, limit=200).get("items") or [])
                    if str(x.get("adSetId")) == wanted]
    except Exception:
        siblings = []
    borrowed = []
    for x in siblings:
        content = (x.get("creative") or {}).get("content") or {}
        if not landing_url and str(content.get("clickThroughUrl") or "").startswith("http"):
            landing_url = str(content.get("clickThroughUrl"))
            borrowed.append("落地页")
        if not call_to_action and content.get("callToAction"):
            call_to_action = str(content.get("callToAction"))
            borrowed.append("按钮文案")
        if not brand_name and content.get("brandName"):
            brand_name = str(content.get("brandName"))
            borrowed.append("品牌名")
    if not str(landing_url or "").startswith("http"):
        return {"error": "缺落地页链接:这个广告组里现有的广告也没读到落地页,"
                         "请让用户给一个 http(s) 开头的完整链接(landing_url)。",
                "note": "**绝不许自己编一个落地页地址**,哪怕只是举例。"}

    ads, problems = _norm_creatives(
        {"asset_url": asset_url, "headline": headline, "description": description,
         "asset_filename": asset_filename, "call_to_action": call_to_action,
         "brand_name": brand_name},
        extra_creatives)
    if problems:
        return {"error": ";".join(problems)}

    names = _next_ad_names(acct, str(the_set.get("name") or ""), len(ads))
    if ad_name.strip():
        names[0] = ad_name.strip()          # 用户自己指定的名字优先
    taken = {str(x.get("name") or "") for x in siblings}
    for one_name, c in zip(names, ads):
        c["name"] = one_name
        c["brand_name"] = (c["brand_name"] or brand_name or "")[:40]
        c["call_to_action"] = c["call_to_action"] or call_to_action or "Learn More"
    dup = [c["name"] for c in ads if c["name"] in taken]
    if dup:
        return {"error": "这些广告名在这个组里已经有了:" + "、".join(dup) + "。",
                "note": "报表里只有一列 name,两行同名用户分不出哪个是哪个。"
                        "**不传 ad_name 就会自动接着已有序号往下排**,建议直接重登一次。"}

    candidate = {
        "type": "add_ad",
        "ad_account_id": acct,
        "ad_set_id": wanted,
        "ad_set_name": the_set.get("name"),
        "campaign_id": str(the_set.get("campaignId") or ""),
        "campaign_name": str(the_set.get("campaignName") or ""),
        "landing_url": landing_url,
        "ads": ads,                     # 快照:报给用户看的就是这几条
        "user_id": CURRENT_USER_ID.get(), "seq": _seq(),
    }
    dupe = _find_duplicate(candidate)
    if dupe:
        return {"action_id": dupe, "note": f"这件事此前已登记过(编号 {dupe}),无需重复登记。"
                                           f"请向用户复述并附上编号,用户同意后直接调 confirm_action。"}
    action_id = uuid.uuid4().hex[:8]
    _put_action(action_id, candidate)
    print(f"[write-op] 登记加广告待办 {action_id}: {candidate['ad_set_name']} +{len(ads)} 条", flush=True)
    return {
        "action_id": action_id,
        "pending": {
            "加进哪个广告组": f"{the_set.get('name')}(id {wanted})",
            "所属计划": candidate["campaign_name"] or f"(id {candidate['campaign_id']})",
            "这次要加几条广告": len(ads),
            "落地页": landing_url,
            "广告": [{"广告名": c["name"], "标题": c["headline"], "描述": c["description"],
                     "品牌名": c["brand_name"], "按钮": c["call_to_action"],
                     "素材": c["asset_filename"] or c["asset_url"]} for c in ads],
            "创建后状态": "暂停(OFF),需用户确认无误后再开启",
            "预算": "**不动** —— 新广告和组里已有的广告共用这个组原来的预算,不会多花一份",
        },
        # 借来的值也要如实说,用户有权知道"我没说过的东西是谁定的"(和 defaults_used 同一条)
        "defaults_used": [f"{w}(沿用这个广告组里已有广告的)" for w in dict.fromkeys(borrowed)],
        "note": ("已登记待办,尚未执行。请用表格向用户完整复述,**「广告」里有几条就列几条**。"
                 "**凡是 defaults_used 里列出的项,要说明这是沿用已有广告的、他可以改。**"),
    }


def _execute_add_ad(a: dict) -> dict:
    """真往已有广告组里加广告。

    执行前**再查一次**广告组还在不在、名字撞没撞 —— 待办可能在保险箱里躺了很久
    (「两阶段流程里,校验只做在登记那一步是不够的」)。
    """
    acct = a["ad_account_id"]
    ads = _ads_from(a)
    try:
        exists = any(str(x.get("id")) == a["ad_set_id"]
                     for x in (nb.list_ad_sets(acct, limit=100).get("items") or []))
    except Exception as e:
        return {"error": f"查广告组失败,**什么都没建**: {e}"}
    if not exists:
        return {"error": f"广告组 {a['ad_set_id']}(登记时叫「{a.get('ad_set_name')}」)现在找不到了,"
                         f"可能已经被删掉。**什么都没建。**"}
    try:
        taken = {str(x.get("name") or "") for x in (nb.list_ads(acct, limit=200).get("items") or [])
                 if str(x.get("adSetId")) == a["ad_set_id"]}
    except Exception:
        taken = set()
    clash = [c["name"] for c in ads if c["name"] in taken]
    if clash:
        return {"error": "这些广告名已经被占用了:" + "、".join(clash) + "。**什么都没建。**",
                "note": "登记之后这个组里又多了广告。**别硬着头皮建同名的** —— "
                        "报表里只有一列 name,两行同名分不出来。请重新登记一次"
                        "(不传 ad_name,序号会自动接着往下排)。"}

    made, failed = _create_ads(a["ad_set_id"], ads, a["landing_url"])
    out = {"done": bool(made), "加进了": f"{a.get('ad_set_name')}(id {a['ad_set_id']})",
           "建成的广告": made, "建成的广告条数": f"{len(made)} / {len(ads)}"}
    if failed:
        out["没建成的广告"] = failed
    if not made:
        return {"error": "广告全部创建失败:"
                         + ";".join(f"{f['name']}: {f['error']}" for f in failed),
                "note": "**什么都没建成。** 请把每条的原因原样告诉用户。"}
    out["note"] = ("已加进这个广告组,状态是暂停(OFF)。"
                   "**这个组的预算没有变** —— 新广告和组里原有的广告共用同一份,不会多花一份钱。"
                   + (f"⚠️ 有 {len(failed)} 条没建成(见「没建成的广告」),"
                      "**必须点名告诉用户是哪几条、为什么**。" if failed else ""))
    return out


def _no_such_action(action_id: str, verb: str = "执行") -> dict:
    """「这个编号不存在」—— **话要说死,别留想象空间。**

    原来返回的是「找不到待办 xxx(**可能已执行/已取消,或 id 有误**)」。
    2026-09-07 线上实测:模型自己**编了一个编号** `0d51be21` 报给用户(服务器日志里
    从来没有这个编号),用户回「确认」,拿到这句话之后它没有承认编错,而是从括号里
    那三个「可能」里挑了个最不用担责的,对用户说「**系统的待办编号由于超时或刷新
    重置了**」,然后重新登记了一条 —— 用户完全看不出是编的。

    这和「拒绝必须带出路,否则模型会自己编一个」是同一条,只是这次编的是**理由**:
    **含糊的措辞本身就是编造的素材。** 所以两层:
      ① 明说系统根本没有「重置编号」这回事,把那条退路堵死;
      ② **把真实存在的编号列出来** —— 正确答案摆在眼前就不用编了
        (和落地页预览 404 页面直接列出盘上真实文件是同一个做法)。
    只列**他自己的、这个工作室管得了的**:别人的编号不该给他看,更不该指使他去确认。
    """
    mine = []
    mode = CURRENT_CHAT_MODE.get()
    me = CURRENT_USER_ID.get()
    for aid, one in PENDING_ACTIONS.items():
        if one.get("user_id") and me and one.get("user_id") != me:
            continue
        if mode in FULL_ACCESS_MODES or one.get("type") in _mode_action_types(mode):
            mine.append(f"{aid}({one.get('type')})")
    return {
        "error": f"没有编号为 {action_id or '(空)'} 的待办,什么都没{verb}。",
        "现在保险箱里有": mine or "(一条都没有)",
        "note": ("**代码里没有任何一种机制会让待办编号消失或对不上** —— 不存在"
                 "超时、重置、刷新、归档、索引、路径、同步、缓存这类事。"
                 "所以**绝不许发明一个系统故障当理由**(上一版只禁了几个词,"
                 "模型换个说法就绕过去了,所以这里说的是:任何这类理由都是编的)。"
                 "**唯一诚实的说法是「刚才那个编号是我弄错的」。**"
                 "编号不存在只有两种可能:记错了,或者根本没登记过。**这也不是权限问题。**"
                 + ("请从「现在保险箱里有」里挑正确的那个编号再试。" if mine else
                    "保险箱是空的,说明这件事还没登记过 —— 请重新登记,并如实告诉用户"
                    "「刚才那个编号是我记错了」,别编一个系统故障出来。")),
    }


def confirm_action(action_id: str) -> dict:
    """执行之前登记的待办。只能在用户于新消息中明确同意后调用。"""
    action = PENDING_ACTIONS.get(action_id)
    if not action:
        return _no_such_action(action_id, "执行")
    if action.get("user_id") and CURRENT_USER_ID.get() and action.get("user_id") != CURRENT_USER_ID.get():
        return {"error": "这个待办属于另一个账号，不能执行。"}
    if action["seq"] == _seq():
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
        elif action["type"] == "make_landing_images":
            result = _execute_make_landing_images(action)
        elif action["type"] == "add_ad":
            result = _execute_add_ad(action)
        elif action.get("type") == "make_creatives":
            result = _execute_make_creatives(action)
        elif action.get("type") == "publish_landing_pages":
            result = _execute_publish_landing_pages(action)
        elif action.get("type") == "swap_campaign_landers":
            result = _execute_swap_campaign_landers(action)
        else:
            # 一次可能要改好几个对象(开启广告要三层一起开)。逐个改、逐个记账,
            # 有失败的也要如实说明是哪一个 —— 别让用户以为全成了。
            tgts = action.get("targets") or [{"level": action["level"],
                                              "id": action["object_id"], "name": action.get("name", "")}]
            done, failed = [], []
            for t in tgts:
                try:
                    nb.update_status(t["level"], t["id"], action["status"])
                    done.append(f"{t['level']}「{t.get('name') or t['id']}」")
                except Exception as e:
                    failed.append(f"{t['level']}「{t.get('name') or t['id']}」:{e}")
            if failed:
                result = {"error": "部分没改成 —— 成功:" + ("、".join(done) or "无")
                                   + ";失败:" + "、".join(failed)}
            else:
                result = {"done": True, "detail": "已" + ("开启" if action["status"] == "ON" else "暂停")
                                                  + "、".join(done)}
        print(f"[write-op] 待办 {action_id} 结果: {json.dumps(result, ensure_ascii=False)[:300]}", flush=True)

        # 把"真实发生了什么"记入代码层台账(聊天回复会盖'系统核验'钢印)
        if result.get("done"):
            detail = json.dumps(result.get("created") or result.get("detail") or "", ensure_ascii=False)[:220]
            _executed().append({"id": action_id, "ok": True, "detail": detail})
            PENDING_ACTIONS.pop(action_id, None)
            _save_actions()
        else:
            _executed().append({"id": action_id, "ok": False, "detail": str(result.get("error"))[:220]})
        safe_action = {k: ("[已保存，不回显]" if k in ("tracking_script", "tracking_script_b")
                                    else "[已算好，不回显]" if k == "put_body" else v)
                       for k, v in action.items() if k != "seq"}
        return {"executed": safe_action,
                **(result if isinstance(result, dict) else {"detail": result})}
    except Exception as e:
        print(f"[write-op] 待办 {action_id} 异常: {e}", flush=True)
        _executed().append({"id": action_id, "ok": False, "detail": str(e)[:220]})
        return {"error": str(e)}


def propose_schedule(kind: str, when: str, level: str, object_id: str,
                     status: str, name: str = "", extra_targets: list[dict] | None = None) -> dict:
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

    # 和「立刻开启」同一条规矩:三层全 ON 才会真的投放。
    # 定时任务更要紧 —— 到点没人盯着,只开了一层的话第二天才发现一条都没跑。
    targets = [{"level": level, "id": str(object_id), "name": name}]
    for t in (extra_targets or []):
        lv, oid = (t.get("level") or "").strip(), str(t.get("id") or "").strip()
        if lv not in ("campaign", "ad_set", "ad") or not oid:
            return {"error": f"extra_targets 里有不合法的对象:{t}"}
        if not any(x["id"] == oid for x in targets):
            targets.append({"level": lv, "id": oid, "name": t.get("name") or ""})
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
        "targets": targets,
        # 记下是谁定的:到点时看表线程要用他自己的凭据去执行
        "user_id": CURRENT_USER_ID.get(),
        "seq": _seq(),
    }
    dup = _find_duplicate(candidate)
    if dup:
        return {"action_id": dup, "note": f"这件事此前已登记过(编号 {dup}),无需重复登记。"
                                          f"请向用户复述并附上编号,用户同意后直接调 confirm_action。"}
    action_id = uuid.uuid4().hex[:8]
    _put_action(action_id, candidate)
    verb = "开启" if status == "ON" else "暂停"
    when_desc = f"每天 {when}(北京时间)" if kind == "daily" else sched.both_times(parsed)
    print(f"[write-op] 登记定时任务待办 {action_id}: {when_desc} {verb} {name or object_id}", flush=True)
    listed = [f"{t['level']}「{t['name'] or t['id']}」" for t in targets]
    out = {
        "action_id": action_id,
        "pending": f"定时{verb}:{when_desc} → " + "、".join(listed),
        "first_run": sched.both_times(parsed),
        "note": "已登记待办,尚未生效。请向用户复述时间和**要改的每一个对象**(带上编号)并等确认。"
                "另外要提醒用户:定时任务依赖本服务持续运行,服务停了就不会触发。",
    }
    if status == "ON" and len(targets) == 1 and level == "campaign":
        out["warning"] = ("只定了 campaign 一层!三层全 ON 才会真的投放,到点了广告照样跑不起来 —— "
                          "而且定时任务执行时没人盯着,可能第二天才发现一条都没投。"
                          "请先用 get_delivery_tree 看清底下的广告组和广告,问用户要开哪些,"
                          "再把它们放进 extra_targets 重新登记。")
    return out


def list_clickflare_campaigns(search: str = "") -> dict:
    """列出 ClickFlare 里的 campaign(只读)。用户说不清要跑哪条时用它找。"""
    try:
        return cfc.list_campaigns(search=search)
    except Exception as e:
        return {"error": str(e)[:500]}


def describe_clickflare_campaign(campaign: str) -> dict:
    """看一条 campaign 现在挂着哪些落地页和 offer、权重各是多少(只读)。

    `campaign` 直接把用户给的 **Campaign Tracking URL** 传进来就行 ——
    它形如 `https://追踪域名/cf/r/<24位id>?…`(id 在**路径**里),代码会抠出来;
    也接受 `…?cpid=<id>` 这种落地页地址,或直接给 24 位 id。
    **抠不出来时会明确报错 —— 那时绝不许改用名字去搜是哪条 campaign。**
    **改任何投放之前都要先调它**,把现状摆给用户看。
    """
    try:
        return cfc.describe_campaign(campaign)
    except Exception as e:
        return {"error": str(e)[:500]}


def create_clickflare_landers(campaign: str, variant_a_url: str, variant_b_url: str,
                              cta_url: str, name_prefix: str = "") -> dict:
    """把已经发布好的 A/B 两个地址登记成 ClickFlare 的两个 Lander(**只新增**)。

    建好的 Lander **还不会承接任何流量** —— 要等它被挂进 campaign 的 flow 里才算数,
    而那一步会改变正在花钱的投放,是单独的写操作、要用户确认。

    两个关键值都是**推导**出来的,不问用户也不会挑错:
    · `workspace_id` 从这条 campaign 自己读(账号里有 22 个 workspace);
    · `tracking_domain_id` 按 `cta_url` 的域名反查(有 74 个追踪域名)。
    """
    # A/B 两个地址一样 = 这个实验问不出任何东西(和发布那道闸门同一个道理)。
    # **这个检查不花一次请求,所以排在所有联网调用之前** —— 和生图那条
    # 「免费又可能失败的步骤先做完」是同一条规矩。
    if str(variant_a_url or "").strip() == str(variant_b_url or "").strip():
        return {"error": "A/B 两个地址是同一个,这样建出来的 A/B 分流没有意义。"}
    try:
        info = cfc.describe_campaign(campaign)
        workspace_id = str(info.get("workspace_id") or "")
        if not workspace_id:
            return {"error": "这条 campaign 上读不到 workspace_id,没法建 Lander。"}
        domain_id = cfc.tracking_domain_id_for(cta_url)
        raw = str(name_prefix or "").strip()
        prefix = "".join(c if (c.isalnum() or c in "-_") else "-" for c in raw).strip("-")
        if not prefix:
            parts = [x for x in urlparse(str(variant_a_url)).path.split("/") if x]
            prefix = parts[0] if parts else "lp"
        made = []
        for label, url in (("A", variant_a_url), ("B", variant_b_url)):
            made.append(cfc.create_landing(
                name=f"{prefix}-{label}", url=str(url).strip(), workspace_id=workspace_id,
                tracking_domain_id=domain_id, cta_count=1,
                notes="由投放助手创建;A/B 实验用"))
    except Exception as e:
        return {"error": str(e)[:500]}
    return {
        "created": made,
        "campaign": {"id": info.get("campaign_id"), "名字": info.get("名字")},
        "这条campaign现在挂着的": info.get("paths"),
        "note": ("两个 Lander 已经建好,但**还没挂进 campaign,一点流量都不会走它们**。"
                 "请把上面『现在挂着的』讲给用户听,并说明下一步是把这两个 Lander "
                 "换进这条 campaign、权重各 50% —— 那会**立刻改变正在花钱的投放**,"
                 "必须等他明确同意。**不许自己去改。**"),
    }


def propose_swap_campaign_landers(campaign: str, lander_a_id: str, lander_b_id: str,
                                  weight_a: int = 50, weight_b: int = 50,
                                  path_name: str = "", allow_identical: bool = False) -> dict:
    """登记「把这条 campaign 的落地页换成 A/B 两个」的待办;用户下一条消息确认才真改。

    **这是全项目风险最高的一个写操作**:改完**立刻生效**,当场就在拿买来的流量
    往新页面送。所以走和建广告相同的保险箱,而且提案里必须把
    「现在挂的是哪些 / 换成什么 / 权重多少 / offer 动不动」全列出来。

    `campaign` 直接传用户给的 Campaign Tracking URL(形如 `…/cf/r/<24位id>`)。
    **抠不出 id 就报错,不许改用名字去搜。**
    `path_name`:这条 flow 有多条启用中的 path 时才需要 —— **代码不会替用户挑**。
    """
    try:
        landers = [{"id": lander_a_id, "weight": int(weight_a)},
                   {"id": lander_b_id, "weight": int(weight_b)}]
    except (TypeError, ValueError):
        return {"error": "权重要是整数,比如各 50。"}
    try:
        plan = cfc.plan_lander_swap(campaign, landers, path_name=path_name,
                                    allow_identical=allow_identical)
    except Exception as e:
        return {"error": str(e)[:500]}
    if plan.get("offer有没有动") != "没动":
        return {"error": "算出来的新配置动了 offer —— 这是 bug,已经停下,没有改任何东西。"}

    candidate = {
        "type": "swap_campaign_landers",
        "campaign_id": str(plan["campaign"].get("id") or ""),
        "campaign_name": str(plan["campaign"].get("名字") or ""),
        "campaign_url": str(plan["campaign"].get("这条计划的投放链接") or ""),
        "flow_id": plan["flow_id"],
        "path_name": str((plan.get("path") or {}).get("名字") or ""),
        "before": plan["换之前"],
        "after": plan["换之后"],
        "put_body": plan["put_body"],
        "fingerprint": plan["fingerprint"],
        "expect_landers": [str(x.get("id")) for x in landers],
        "user_id": CURRENT_USER_ID.get(),
        "seq": _seq(),
    }
    dup = _find_duplicate(candidate)
    if dup:
        return {"action_id": dup,
                "note": f"相同的待办已经存在(编号 {dup}),请复述后等用户确认,不要重新登记。"}
    action_id = uuid.uuid4().hex[:8]
    _put_action(action_id, candidate)
    return {
        "action_id": action_id,
        "pending": {
            "campaign": f"{candidate['campaign_name']}({candidate['campaign_id']})",
            # **把这条计划自己的投放链接摆出来给用户对**:名字他核对不了
            # (账号里 50 条、名字高度相似),而他手里正好有那条链接。
            # 实测踩过:模型抠不出 id 就改成按名字搜,搜错了计划,
            # 差一点把买来的流量换到别人的计划上 —— 是靠肉眼比对 id 才发现的。
            "核对用·这条计划的投放链接": candidate["campaign_url"] or "(平台没给)",
            "改的是哪条path": candidate["path_name"] or "(这条 flow 只有一条启用中的 path)",
            "现在挂着": plan["换之前"],
            "换成": plan["换之后"],
            "offer": "一个字都不动",
            **({"⚠️必须讲给用户": plan["warnings"]} if plan.get("warnings") else {}),
        },
        "note": ("这里**只登记了待办,一点都还没改**。请把上面「现在挂着」和「换成」"
                 "完整复述给用户,并明确告诉他:**确认之后立刻生效**,新页面马上开始接"
                 "买来的流量;这条 campaign 的历史转化数据会横跨两批落地页。"
                 "**必须把「核对用·这条计划的投放链接」原样贴给用户,请他和自己手里那条比一比**"
                 " —— 找错计划的后果是把买来的流量换到别人的计划上,而只看名字他核对不出来。"
                 "等他下一条消息明确同意,再调 confirm_action。"),
    }


def _execute_swap_campaign_landers(action: dict) -> dict:
    """真改。执行前会拿指纹再比一次 —— 登记之后被人在后台动过就停下不写。"""
    # 和发布那条同理:**登记时查过不代表执行时还成立**。待办会在保险箱里躺很久,
    # 而"两个 Lander 指向同一个网址"这道检查是后加的,老待办身上没做过。
    want = [str(x).lower() for x in (action.get("expect_landers") or [])]
    if len(want) > 1 and len(set(want)) == 1 and not action.get("allow_identical"):
        return {"error": "没有改。这个待办要挂上去的两个 Lander 是**同一个**,"
                         "分流出去两边跑的是同一个页面 —— 实验问不出任何东西。"
                         "请重新登记;确实要做 A/A 测试就明确说一句。"}
    try:
        result = cfc.apply_flow(action.get("flow_id", ""), action.get("put_body") or {},
                                expect_fingerprint=action.get("fingerprint", ""),
                                expect_landers=action.get("expect_landers") or [])
    except Exception as e:
        # 走到这儿说明 **PUT 本身没发出去或被拒**(指纹不符、网络断、平台报错)——
        # 线上没有变化,重试是安全的。写成功之后的失败不会走到这里:
        # apply_flow 从 PUT 那一行往后自己兜住,返回 done=True 加一条警告。
        return {"error": str(e)[:500],
                "note": "**投放没有被改动**(改动请求没发出去)。可以查清原因后重新登记。"}
    result["切换时间"] = sched.both_times(sched.now_beijing())
    warn = result.get("⚠️回查失败") or result.get("⚠️对不上")
    result["note"] = ("已生效。**从这一刻起买来的流量走新落地页** —— 这条 campaign 的"
                      "转化数据从现在开始跨两批页面,复盘时记得以上面这个切换时间为界。"
                      + ("" if not warn else
                         " ⚠️ 但是:" + warn + " **务必把这句原样告诉用户,并强调不要重试** —— "
                         "改已经发出去了,再确认一次只会把事情弄乱。"))
    return result


def clickflare_publish_kit(campaign: str, cta_index: int = 1) -> dict:
    """一次取齐发布落地页要用的三样东西 —— **用户一样都不用手工去后台复制**。

    以前要他自己贴 CTA Click URL 和整段 Lander Tracking Script:两样都可能粘错,
    而粘错了页面看起来**完全正常**,要等数据不对劲才发现。现在全部从这条 campaign
    推导:追踪域名来自它的 `domain_id`,脚本来自 ClickFlare 自己的接口。

    `campaign` 直接传用户给的 Campaign Tracking URL(形如 `…/cf/r/<24位id>`)或 campaign id。
    """
    try:
        kit = cfc.publish_kit(campaign, cta_index=cta_index)
    except Exception as e:
        return {"error": str(e)[:500],
                "note": ("取不到的话,还可以让用户手工从 ClickFlare 后台复制 "
                         "CTA Click URL 和 Lander Tracking Script 贴过来 —— "
                         "但**严禁自己编造或拼凑**这两样。")}
    script = kit.pop("tracking_script", "")
    # 顺手存进脚本库:以后 ClickFlare 接口万一不可用,还能从本地拿回来
    saved = cfs.remember(CURRENT_USER_ID.get(), script)
    return {
        **kit,
        "追踪脚本": f"已自动取到并存好({len(script)} 字符),发布时会原样植入,不在聊天里回显",
        "脚本库": saved.get("action") or saved.get("reason") or "",
        "note": ("这三样都是从 ClickFlare 推导出来的,**不用让用户再去后台复制**。"
                 "请把「追踪域名 / CTA Click URL / Campaign Tracking URL」讲给他核对一眼,"
                 "**脚本原文不要贴进聊天**。发布时 tracking_script 留空即可自动使用。"),
    }


def list_schedules() -> dict:
    """查看所有定时任务(含下次执行时间、上次执行结果)。"""
    tasks = sched.list_tasks(CURRENT_USER_ID.get())
    return {"schedules": tasks or "还没有任何定时任务",
            "now": sched.both_times(sched.now_beijing())}


def cancel_schedule(task_id: str) -> dict:
    """取消一个已生效的定时任务(用 list_schedules 查到的 task_id)。

    **只能取消自己登记的** —— 定时任务按人存,别人的碰不得。
    取消完会把「取消掉的是什么」一起返回,请原样念给用户核对。
    """
    return sched.cancel_task(task_id, CURRENT_USER_ID.get())


def cancel_action(action_id: str) -> dict:
    """取消之前登记的待办(用户不同意或改主意时调用)。"""
    # 归属检查:和 confirm_action 一样,别人的待办不能碰。
    # 少了这条的话,B 登录进来能把 A 登记好的待办**删掉**,A 那边只会看到
    # 「找不到待办」—— 保险箱是全进程共享的一份,不是每人一份。
    holder = PENDING_ACTIONS.get(action_id)
    if holder and holder.get("user_id") and CURRENT_USER_ID.get() \
            and holder.get("user_id") != CURRENT_USER_ID.get():
        return {"error": "这个待办属于另一个账号，不能取消。"}
    removed = PENDING_ACTIONS.pop(action_id, None)
    _save_actions()
    print(f"[write-op] 取消待办 {action_id}: {'成功' if removed else '不存在'}", flush=True)
    if not removed:
        # 取消一个不存在的编号也要把话说死 —— 否则模型同样会编个「已经自动过期了」
        # 糊弄过去,而用户以为那件事被取消了,其实待办还好端端躺在保险箱里。
        return _no_such_action(action_id, "取消")
    return {"cancelled": True, "action_id": action_id}


# 工具清单:递给 Gemini,它会自动挑选、自动执行、自动把结果编进回答
NEWSBREAK_TOOLS = [
    list_organizations, list_ad_accounts, list_campaigns, list_ad_sets, list_ads, get_report,
    recommend_creatives, search_stock_creatives, search_competitor_ads, my_ad_categories, platform_kind, native_market_scan,
    search_competitor_landing_pages, decompose_landing_page, summarize_landing_page_patterns,
    list_cloudflare_landing_resources, propose_publish_landing_pages, swap_landing_image,
    propose_landing_images,
    list_clickflare_campaigns, describe_clickflare_campaign, create_clickflare_landers,
    propose_swap_campaign_landers, clickflare_publish_kit,
    decompose_creative, summarize_creative_patterns,
    use_found_creative, propose_make_creatives, get_delivery_tree,
    propose_status_change, propose_create_campaign, propose_add_ad, confirm_action, cancel_action,
    list_pending_actions, list_conversion_events,
    propose_schedule, list_schedules, cancel_schedule,
]


class ChatMessage(BaseModel):
    role: str      # "user"(用户说的) 或 "assistant"(助手说的)
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]  # 完整的聊天记录(API 不记事,每次都要全量发)
    lang: str = "zh"             # 界面语言:"zh" 中文 / "en" 英文,决定 AI 用哪种语言回答
    mode: str = "campaign"       # campaign=投放助手 creative=素材工作室 landing=落地页工作室


@app.get("/")
def index(request: Request):
    """聊天页。**但直接访问根路径要先去选平台。**

    平台选择页跳过来时带的是 `/?platform=xxx`,所以按有没有这个参数区分:
    带了 = 用户已经选过了,直接进聊天;没带 = 他是直接敲域名进来的,
    先送去 `/platforms` 挑一个。

    不这么做的话,直接访问 `/` 会拿到写死默认平台的聊天页 ——
    以后接了 Nextdoor、Meta,用户根本没机会选。
    """
    if not (request.query_params.get("platform") or "").strip():
        return RedirectResponse("/platforms", status_code=302)
    return FileResponse(Path(__file__).with_name("static") / "index.html")


# ============ 素材上传中转:浏览器 → 这里 → NewsBreak ============
from fastapi import File, UploadFile  # noqa: E402

_CACHED_ACCOUNT_ID: dict = {}    # {token前12位: 账户id} —— 按 token 分桶,不同用户不串号
# 素材地址 → 素材类型(IMAGE/GIF/VIDEO),上传时记下,建广告时查回
# 素材地址 → 类型。和 _SEARCHED_ASSETS_BY_USER / 拆解结果一样要有上限,
# 否则进程活得越久它越大(长驻服务是按月算的)。
_ASSET_TYPES: dict[str, str] = {}


def _remember_asset_type(url: str, kind: str) -> None:
    _ASSET_TYPES[url] = kind
    while len(_ASSET_TYPES) > 500:
        _ASSET_TYPES.pop(next(iter(_ASSET_TYPES)))


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


def _resolve_range(days: int, start_s: str, end_s: str):
    """把「近 N 天」或「自定义起止日期」统一算成 (start, end, days)。

    **日期按北京时间取**:用户说的"今天"是他那边的今天。用 UTC 的话,
    北京时间 00:00~08:00 这 8 小时里 UTC 还停在昨天,大屏会少一天数据
    (和建广告命名那条是同一个坑)。

    自定义范围要在这里把所有不合法的情况挡住并**说人话** ——
    直接甩给平台的话,用户看到的是一句英文的 Invalid parameters。
    """
    from datetime import date, timedelta
    today = sched.now_beijing().date()
    if (start_s or "").strip() and (end_s or "").strip():
        try:
            start = date.fromisoformat(start_s.strip())
            end = date.fromisoformat(end_s.strip())
        except ValueError:
            raise ValueError("日期格式不对,要写成 2026-08-27 这样")
        if start > end:
            start, end = end, start          # 反了就替他调过来,不用报错烦他
        if end > today:
            end = today                      # 未来的数据不存在,截到今天
        if start > today:
            raise ValueError("开始日期比今天还晚,平台上不会有数据")
        n = (end - start).days + 1
        if n > 180:
            raise ValueError(f"这段跨了 {n} 天,平台报表最多只能查 180 天,请把范围缩小一点")
        return start, end, n
    n = max(1, min(int(days or 30), 180))    # 平台报表上限 180 天
    return today - timedelta(days=n - 1), today, n


@app.get("/api/dashboard")
def dashboard_data(days: int = 30, start: str = "", end: str = ""):
    """仪表盘用的全部数据:总计 KPI、环比、按天趋势、三个层级的明细。

    时间范围两种给法:`days=N`(近 N 天),或 `start`/`end` 自定义起止日期。
    """
    load_env_file()
    from datetime import timedelta

    try:
        try:
            start_d, end_d, days = _resolve_range(days, start, end)
        except ValueError as ve:
            return JSONResponse(status_code=400, content={"error": str(ve)})
        start = start_d
        prev_start = start - timedelta(days=days)   # 上一个等长周期,用来算环比
        prev_end = start - timedelta(days=1)
        fmt = "%Y-%m-%d"
        account_id = ""
        try:
            account_id = _default_ad_account_id()
        except Exception:
            pass                                    # 多账户没选时不阻塞,报表按 token 全量查

        campaigns = nb.get_report_raw("campaign", start.strftime(fmt), end_d.strftime(fmt), account_id)
        prev_rows = nb.get_report_raw("campaign", prev_start.strftime(fmt), prev_end.strftime(fmt), account_id)

        # 趋势:DATE 维度平台限 31 天,超了就只取最近 31 天(并如实告诉前端)
        trend_days = min(days, 31)
        # 从**这段区间的结尾**往回数,不是从"今天"往回数 ——
        # 自定义了一段历史区间时,从今天往回数会取到区间外面去
        trend_start = end_d - timedelta(days=trend_days - 1)
        try:
            trend = nb.get_daily_raw(trend_start.strftime(fmt), end_d.strftime(fmt), account_id)
        except Exception as e:
            trend, trend_days = [], 0
            print(f"[dashboard] 趋势数据获取失败: {e}", flush=True)

        return {
            "range": {"start": start.strftime(fmt), "end": end_d.strftime(fmt), "days": days},
            "kpi": _sum_kpi(campaigns),
            "kpi_prev": _sum_kpi(prev_rows),
            "trend": sorted(trend, key=lambda r: r["name"]),
            "trend_days": trend_days,
            "trend_capped": days > 31,              # 前端据此说明"趋势只显示最近31天"
            "campaigns": sorted(campaigns, key=lambda r: r["cost"], reverse=True),
            "ad_sets": sorted(nb.get_report_raw("ad_set", start.strftime(fmt), end_d.strftime(fmt), account_id),
                              key=lambda r: r["cost"], reverse=True),
            "ads": sorted(nb.get_report_raw("ad", start.strftime(fmt), end_d.strftime(fmt), account_id),
                          key=lambda r: r["cost"], reverse=True),
        }
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


class AnalyzeIn(BaseModel):
    days: int = 30
    start: str = ""      # 自定义范围(和大屏保持一致,否则诊断的是另一段时间)
    end: str = ""
    lang: str = "zh"


@app.post("/api/analyze")
def analyze_data(body: AnalyzeIn):
    """把仪表盘上的真实数据交给 AI,让它做诊断并给优化建议。"""
    load_env_file()
    data = dashboard_data(body.days, body.start, body.end)
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


# 页面里写的是相对路径 `img/xxx.jpg`,预览时会被解析成 `/landing-pages/img/xxx.jpg`。
# **必须排在 `/landing-pages/{filename}` 前面**登记,虽然路径参数不跨 `/`、
# 今天不会冲突,但顺序摆对了以后加通配也不会打架。
@app.get("/landing-pages/img/{filename}")
def landing_page_image(filename: str):
    """预览页里的配图。**只给当前这个人自己的** —— 和页面本身同一条规矩。"""
    name = Path(str(filename or "")).name
    if not name or name != filename or name.startswith("."):
        return JSONResponse(status_code=400, content={"error": "无效的图片名"})
    base = lp._img_dir(CURRENT_USER_ID.get() or "")
    path = (base / name).resolve()
    if path.parent != base.resolve() or not path.is_file():
        return JSONResponse(status_code=404, content={"error": "图片不存在"})
    return FileResponse(path, headers={"X-Content-Type-Options": "nosniff",
                                       "Referrer-Policy": "no-referrer",
                                       "Cache-Control": "public, max-age=3600"})


@app.get("/landing-pages/{filename}")
def landing_page_preview(filename: str):
    """预览本机生成的落地页；只允许访问生成目录下的 HTML 文件。"""
    if "/" in filename or "\\" in filename or not filename.lower().endswith(".html"):
        return JSONResponse(status_code=400, content={"error": "无效的预览文件名"})
    path = lp.resolve_preview(filename, CURRENT_USER_ID.get() or "")
    if path is None:
        # **别甩一句 JSON**:用户看到 `{"error":"...不存在或已过期"}` 只会以为功能坏了,
        # 而真相多半是"这个地址是模型编的"。直接把盘上真实存在的预览列出来,
        # 他一眼就能点到对的那个,不用再回聊天里追问。
        real = lp.recent_previews(8, owner=CURRENT_USER_ID.get() or "")
        items = "".join(
            '<li><a href="/landing-pages/%s">%s</a></li>' % (r["file"], r["file"])
            for r in real)
        body = ("<!doctype html><meta charset=\"utf-8\">"
                "<title>这个预览不存在</title>"
                "<style>body{font:15px/1.7 system-ui,sans-serif;max-width:760px;"
                "margin:48px auto;padding:0 20px;color:#222}"
                "code{background:#f4f4f5;padding:2px 6px;border-radius:4px}"
                "li{margin:6px 0}</style>"
                "<h2>这个落地页预览不存在</h2>"
                "<p>你打开的是 <code>" + _html.escape(filename) + "</code>,"
                "服务器上没有这个文件。<b>最常见的原因是助手把地址编出来了</b>"
                "(它这一轮其实没有真的生成页面),其次是这份预览已经被清理掉了。</p>"
                + ("<p><b>下面是服务器上真实存在的预览</b>,点一下就能看:</p><ul>"
                   + items + "</ul>" if real
                   else "<p>服务器上现在<b>一个生成好的预览都没有</b>。</p>")
                + "<p>回到聊天里说一句「重新生成落地页」,让它真跑一遍。</p>")
        return HTMLResponse(content=body, status_code=404)
    return FileResponse(path, media_type="text/html; charset=utf-8", headers={
        # `img-src 'self'` 是配图之后加的:页面用相对路径 `img/xxx.jpg` 引自家的图。
        # **仍然不给 https:** —— 外部图片一律在生成那一步就被摘掉了,
        # 这里再堵一道,免得哪天漏进来一个外链把用户的浏览行为报给第三方。
        "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                    "img-src 'self' data:; "
                                    "font-src data:; form-action 'none'; base-uri 'none'; sandbox"),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })


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
    """当前登录的是谁。

    带上 `id`:前端要靠它判断"本地缓存的聊天记录是不是这个人的"。
    只回用户名不够 —— 换个人登录时前端认不出来,会把上一个人的记录
    当成自己的显示出来,甚至上传到新账号里。
    """
    user = _current_user(request)
    return {"username": user["username"], "id": user["id"]} if user else {"username": "", "id": ""}


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


class SpyCredsIn(BaseModel):
    api_key: str = ""


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
        _remember_asset_type(asset_url, nb.creative_type_of(filename, file.content_type or ""))
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

    mode = CURRENT_CHAT_MODE.get()
    if english:
        prompt = SYSTEM_PROMPT_EN + f"\n\nToday's date (UTC) is {today}; use it when working out ranges like \"the last N days\"."
        prompt += ("\n\nKNOWN LANDING-PAGE TYPES: " + " / ".join(KNOWN_AD_TYPES)
                   + ". If the landing page matches one of these, use it for naming (and say so). "
                     "**If it matches none, you MUST ask the user which keyword to use — never invent one.**")
    else:
        prompt = SYSTEM_PROMPT + f"\n\n今天的日期(UTC)是 {today},计算\"最近N天\"等日期范围时以此为准。"
        prompt += ("\n\n【现有的落地页类型】" + " / ".join(KNOWN_AD_TYPES)
                   + "。落地页里能对上其中一个就直接用它命名(告诉用户一声);"
                     "**对不上就必须问用户「这次用什么关键词命名?」,不许自己编一个。**")

    if mode == "creative":
        prompt += ("\n\n[CREATIVE STUDIO MODE] You are a creative-production partner, not a campaign operator. "
                   "Help research, upload, analyze, plan and generate ad creatives. You may generate images only after "
                   "the normal cost-confirmation step. You must not create ads, change delivery status, or schedule actions."
                   if english else
                   "\n\n【素材工作室模式】你现在是素材制作搭档,不是投放操作员。专注于找素材、上传、拆解、"
                   "归纳创意、写标题描述和生成广告图。生图仍须先报价并等待用户确认。"
                   "严禁创建广告、开启/暂停投放或登记定时任务;素材做好后只能建议用户切换到投放助手。")
    elif mode == "landing":
        prompt += ("\n\n[LANDING PAGE STUDIO MODE] You are a direct-response landing-page strategist. "
                   "Find candidates through OpenAdLibrary, clearly distinguish performance signals from verified "
                   "conversion data, show available landing-page screenshots directly, and analyze screenshots plus "
                   "extracted public page text with explicit evidence limits. Extract layout, "
                   "copy, offer, trust, form and CTA patterns, then create two original HTML landing-page drafts. "
                   "Never copy competitor branding or claims, and never create, pause, or schedule ads. "
                   "Generate 2 variants by default for A/B; pass n_variants=1 when the user asks for only one. "
                   "Always list every preview_url verbatim so the user can open it — never rebuild the host or port. "
                   "**NEVER write a /landing-pages/... link unless summarize_landing_page_patterns "
                   "actually returned it in THIS turn.** Do not reuse the shape of an old filename "
                   "and invent a new hex prefix — code checks every link you give against the disk "
                   "and will publicly flag a made-up one. If you have not generated the pages yet, "
                   "say so and generate them. "
                   "For publishing, call clickflare_publish_kit(campaign) first: it derives the tracking domain, "
                   "When the user gives a tracking link, pass THAT LINK verbatim as `campaign` — "
                   "the code deterministically extracts the id from the /cf/r/<id> path. "
                   "**If extraction fails it errors out; NEVER fall back to searching campaigns by name** — "
                   "there are 50 campaigns with near-identical names and picking the wrong one "
                   "sends paid traffic to someone else's campaign. Ask for a correct link instead. "
                   "It gets the tracking domain, "
                   "CTA Click URL and Campaign Tracking URL from ClickFlare and fetches the lander script "
                   "automatically, so do NOT ask the user for them — ask only which campaign he is running. "
                   "NEVER offer to publish without tracking code: pages missing the CTA placeholder are refused "
                   "at the code level, and an untracked landing page records nothing while the ads still cost money. "
                   "Never invent or "
                   "rewrite them. List Cloudflare resources if the domain is unknown. Register publishing with "
                   "propose_publish_landing_pages and only call confirm_action after confirmation in the next message."
                   if english else
                   "\n\n【落地页工作室模式】你现在是直效落地页策略师。先从 OpenAdLibrary 找仍在投、"
                   "投放较久或版位较多的候选，并明确这些只是表现信号，不是真实 CTR/CVR。截图可用时必须直接"
                   "展示，并结合截图与公开页面文字拆解；只有域名首页时必须说明不一定是当时的完整投放路径。"
                   "从排版、文案、offer、信任背书、表单和 CTA 节奏分析，提炼关键词"
                   "与可迁移优点，最后生成可预览的原创 HTML 落地页。严禁照抄竞品品牌、"
                   "承诺和具体优惠，也严禁创建、启停广告或登记定时任务。\n"
                   "【生成落地页】默认出 2 版做 A/B；**用户说「只要一个」就传 n_variants=1**，"
                   "不许多给。生成完必须把每一版的 preview_url **原样列出来让他点开看**，"
                   "**没有真的调用 summarize_landing_page_patterns 拿到 preview_url 之前，"
                   "一个 /landing-pages/... 链接都不许写出来** —— 不许照着旧文件名的样子"
                   "自己编一串十六进制。代码会把你给的每个链接拿到硬盘上回查一遍，"
                   "编的会被当众标出来。还没生成就如实说还没生成，然后去生成。"
                   "地址一个字都不许改、更不许自己拼 host 或端口。\n"
                   "【追踪信息不用问用户】先调 clickflare_publish_kit(campaign)，"
                   "**用户给了投放链接时，一律把那条链接原样传进 campaign 参数** —— "
                   "代码会从 /cf/r/<id> 的路径里确定性地抠出 campaign id。"
                   "**抠不出来会报错；那时绝不许改用 list_clickflare_campaigns 按名字搜一个顶上**，"
                   "账号里有 50 条计划、名字高度相似，猜错就是把买来的流量换到别人的计划上。"
                   "报错时请用户重新给一条正确的链接。挂落地页前还要把待办里"
                   "「核对用·这条计划的投放链接」贴给他，让他和自己手里那条比一比。\n"
                   "它会从 ClickFlare 直接给出追踪域名、CTA Click URL、Campaign Tracking URL，"
                   "追踪脚本也会自动取好。你只需要向用户要**他这次要投的 Campaign Tracking URL**"
                   "(或 campaign 名字，用 list_clickflare_campaigns 找)。"
                   "取不到时才请他手工从 ClickFlare 后台复制，**任何情况下都严禁编造或举例编一个地址** —— "
                   "写出 `https://track.clickflare.com/click/1` 这种假例子会被当成真的照抄。\n"
                   "【绝不许承诺「先不放追踪代码就发布」】没有 CTA 占位符的页面**代码层会直接拒绝发布**，"
                   "答应了也做不到；而且没有追踪的落地页 = 买来的流量一条都统计不到，钱白花。\n"
                   "域名没定时先读取 Cloudflare 可选域名。发布必须先用 propose_publish_landing_pages 登记，"
                   "向用户完整复述，只有用户下一条消息明确确认后才能调用 confirm_action。")

    # **只注入他自己的**:这些消息里带着计划名,而后面那句「主动告知一句」
    # 会让 AI 把别人在投什么念给他听。
    my_runs = sched.runs_for(CURRENT_USER_ID.get())
    if mode in FULL_ACCESS_MODES and my_runs:
        prompt += ("\n\n【定时任务最近的执行结果】(代码层记录,若用户还不知道,主动告知一句):\n"
                   + "\n".join(f"- {m}" for m in my_runs))
    visible_actions = {aid: a for aid, a in PENDING_ACTIONS.items()
                       if _my_action(a)
                       and (mode in FULL_ACCESS_MODES or a.get("type") in _mode_action_types(mode))}
    if visible_actions:
        lines = []
        for aid, a in visible_actions.items():
            if a.get("type") == "create_campaign":
                what = f"create ad \"{a.get('campaign_name')}\"" if english else f"新建广告「{a.get('campaign_name')}」"
            elif a.get("type") == "swap_campaign_landers":
                # **权重要照实说,不能写死 50/50** —— 这句是每轮注进提示词的,
                # 模型跨轮全靠它记住待办内容。用户明明定的是 70/30,
                # 这里说成各 50%,他复述给用户听的就是错的,而他自己无从发现。
                ws = "/".join(str(x.get("weight")) for x in
                              ((a.get("after") or {}).get("落地页") or [])) or "?"
                what = (f"swap campaign \"{a.get('campaign_name')}\" landers to A/B {ws}"
                        if english else
                        f"把 campaign「{a.get('campaign_name')}」的落地页换成 A/B({ws})")
            elif a.get("type") == "publish_landing_pages":
                what = (f"publish landing A/B to {a.get('domain')}/{a.get('slug')}"
                        if english else f"发布落地页 A/B 到 {a.get('domain')}/{a.get('slug')}")
            else:
                what = (f"{a.get('status')} {a.get('level')} \"{a.get('name') or a.get('object_id')}\"" if english
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


CREATIVE_TOOL_NAMES = {
    "list_organizations", "list_ad_accounts", "recommend_creatives",
    "search_stock_creatives", "search_competitor_ads", "my_ad_categories",
    "platform_kind", "native_market_scan", "decompose_creative",
    "summarize_creative_patterns", "use_found_creative",
    "propose_make_creatives", "confirm_action", "cancel_action", "list_pending_actions",
}

LANDING_TOOL_NAMES = {
    "list_organizations", "list_ad_accounts", "my_ad_categories", "platform_kind",
    "native_market_scan", "search_competitor_ads", "search_competitor_landing_pages",
    "decompose_landing_page", "summarize_landing_page_patterns",
    "list_cloudflare_landing_resources", "propose_publish_landing_pages",
    "swap_landing_image", "propose_landing_images",
    "list_clickflare_campaigns", "describe_clickflare_campaign", "create_clickflare_landers",
    "propose_swap_campaign_landers", "clickflare_publish_kit",
    "confirm_action", "cancel_action", "list_pending_actions",
}


# 「工作室 → 它能用哪些工具」的**唯一事实来源**。加一个新工作室 = 这里加一行。
STUDIO_TOOL_SETS: dict[str, set] = {
    "creative": CREATIVE_TOOL_NAMES,
    "landing": LANDING_TOOL_NAMES,
}
# 全放行的模式。**必须是显式白名单** —— 不能靠「不是工作室就全放行」那种写法,
# 那样任何拼错的、或者新加而忘了登记的模式名都会 fail-open 成全权限。
FULL_ACCESS_MODES = frozenset({"campaign"})
VALID_MODES = FULL_ACCESS_MODES | set(STUDIO_TOOL_SETS)
# 所有工作室都放行的那几个工具 —— 认不出模式时按这个来(最严的一档)。
# 用交集而不是「挑最短的那个名单」:名单以后怎么变,这里都还是最严的。
STRICTEST_TOOL_NAMES = frozenset(set.intersection(*(set(v) for v in STUDIO_TOOL_SETS.values())))


def _tool_allowed(name: str) -> bool:
    mode = CURRENT_CHAT_MODE.get()
    if mode in FULL_ACCESS_MODES:
        return True
    names = STUDIO_TOOL_SETS.get(mode)
    if names is None:
        # **认不出的模式按最严的来,绝不当成投放助手。**
        # 这样「加了工作室忘了登记」会立刻表现成"这个工作室什么都干不了"(一眼看见),
        # 而不是"这个工作室什么都能干"(悄无声息,直到出事)。
        names = STRICTEST_TOOL_NAMES
    return name in names


STUDIO_LABELS = {"campaign": "投放助手", "creative": "素材工作室", "landing": "落地页工作室"}


def _studio_that_can(name: str) -> str:
    """这个工具在哪个工作室能用?返回模式名。

    **投放助手放行全部工具**,所以任何被拦下的工具在那儿一定能用;
    但如果隔壁那个工作室也有,就优先指到隔壁 —— 离用户正在做的事更近。
    """
    here = CURRENT_CHAT_MODE.get()
    sibling = "landing" if here == "creative" else "creative"
    sib_set = LANDING_TOOL_NAMES if sibling == "landing" else CREATIVE_TOOL_NAMES
    return sibling if name in sib_set else "campaign"


def _handoff(name: str, why: str, say: str = "") -> dict:
    """被工作室闸门拦下时,返回一张**带去处的移交单**,而不是一句死话。

    **为什么不能只说「当前工作室不能执行这项操作」**:那是死胡同 ——
    用户不知道该去哪个工作室、切过去之后刚才聊的还在不在。更糟的是,
    模型拿到一句光秃秃的拒绝,就会自己**编一个理由**转述给用户
    (线上实测:「我刚才切到落地页工作室的视角了,没有权限直接在图库里帮您找图」)。
    把「去哪、为什么、到那边第一句说什么」由**代码**写好,模型只能原样转述。
    """
    to = _studio_that_can(name)
    return {
        "handoff": {
            "到哪个工作室": STUDIO_LABELS.get(to, to),
            "switch_to": to,
            "为什么": why,
            "到那边第一句可以说": say or "接着刚才的事继续",
        },
        "note": ("**这件事是能做的,只是要换个工作室 —— 绝不许说成「我没有权限」"
                 "「接口限制」「系统不支持」。** 请把上面这张移交单讲给用户:"
                 f"顶栏点「{STUDIO_LABELS.get(to, to)}」就能过去,"
                 "**切换时选「带着这段对话过去」,刚才聊的内容不会丢**。"
                 "然后就停在这儿等他,别再自己找别的办法绕。"),
    }


def _tool_call(name: str, args: dict) -> dict:
    """执行模型工具；工作室模式在代码层拦住投放写操作。"""
    if not _tool_allowed(name):
        # `_tool_label` 出的是进度用语(「正在从授权图库里找素材…」),
        # 直接塞进句子读着别扭,剥掉「正在」和省略号再用
        what = _tool_label(name).replace("正在", "").rstrip("…").strip() or name
        return _handoff(
            name,
            f"「{what}」这件事当前工作室做不了,"
            f"要在「{STUDIO_LABELS.get(_studio_that_can(name))}」里做。",
            say=f"帮我{what}")
    if CURRENT_CHAT_MODE.get() not in FULL_ACCESS_MODES and name in ("confirm_action", "cancel_action"):
        action = PENDING_ACTIONS.get(str(args.get("action_id") or ""))
        allowed = _mode_action_types(CURRENT_CHAT_MODE.get())
        if not action or action.get("type") not in allowed:
            # 标签也从那份表里取 —— 写死 "creative"/"落地页" 的话,
            # 加一个工作室这里就会指鹿为马(说成「落地页工作室」)
            label = STUDIO_LABELS.get(CURRENT_CHAT_MODE.get(), "当前")
            if not action:
                # 措辞和 confirm_action 用同一份 —— 两处话不一样的话,模型会挑软的那句来编
                return _no_such_action(str(args.get("action_id") or ""),
                                       "执行" if name == "confirm_action" else "取消")
            # 待办确实存在,只是不归这个工作室管 —— 同样给去处,别让用户卡住
            return {
                "handoff": {
                    "到哪个工作室": STUDIO_LABELS["campaign"],
                    "switch_to": "campaign",
                    "为什么": f"待办 {args.get('action_id')} 是「{action.get('type')}」类型的,"
                              f"{label}工作室管不了它。",
                    "到那边第一句可以说": f"执行待办 {args.get('action_id')}",
                },
                "note": ("**别说成「没有权限」** —— 这个待办好好地存着,只是要在投放助手里确认。"
                         "请把去处讲给用户,并提醒他切换时选「带着这段对话过去」。"),
            }
    fn = OPENAI_TOOL_FUNCS.get(name)
    return fn(**args) if fn else {"error": f"未知工具 {name}"}


# 遇到这些错误码就"换人再试":429=额度用尽或太频繁,5xx=上游服务繁忙/临时故障。
# 血泪:原来只认 429,结果 Gemini 一报 503(服务繁忙)整条链就断了,
# 明明有 ofox 兜底也不去用,用户只看到一句"稍后再试"。
_RETRYABLE_CODES = (429, 500, 502, 503, 504)

# openai / anthropic 这两个 SDK 的**默认超时是 600 秒、还自动重试 2 次** ——
# 最坏情况一个请求能挂 30 分钟。而前端 180 秒就 abort 了:用户早看到
# 「等太久了」,后端还在那儿跑(中转商那边可能照样计费)。
# 所以显式设一个**小于前端 180 秒**的值,让后端先失败、把真实原因说出来。
BRAIN_TIMEOUT_S = float(os.environ.get("BRAIN_TIMEOUT_S", "75"))
BRAIN_RETRIES = int(os.environ.get("BRAIN_RETRIES", "1"))
# 一次性长文生成(归纳创意、生成整页落地页)和聊天不是一回事:
# **实测 gpt-5.5 出一整页落地页 HTML 要 61 秒**,提示词里再塞进几份竞品拆解结果
# 就会超过 BRAIN_TIMEOUT_S,被 SDK 掐掉、重试一次,总共 150 秒还是失败 ——
# 用户看到的是「等太久了」,而模型其实一直在正常干活。
# 必须**小于前端的 IDLE_TIMEOUT_MS(5 分钟)**:后端先失败,才能把真实原因说出来。
GEN_TIMEOUT_S = float(os.environ.get("GEN_TIMEOUT_S", "240"))

# ---- 「别再烧下去了」闸门 ----------------------------------------------------
# 模型有时会在工具循环里原地打转:查一次、想一想、再用**一模一样的条件**查一次,
# 永远得不出结论。用户那头只看到"正在查…"转个不停,而每转一轮都是一次完整的
# 模型调用 —— 实测一轮 ≈ 6.8K 输入 + 7~9K 输出,推理型模型的输出占成本约 89%,
# 十轮下来一条消息就能花掉一两美元,**而且什么答案都没有**。
# 所以这里守三条,任何一条踩到就立刻停,并如实告诉用户停在哪、花了多少。
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "10"))
# 同一个工具 + 完全相同的参数,调到第几次算"绕住了"。
# 2 次可能是一次正常重试(上次超时或报错),3 次就是没在往前走了。
TOOL_REPEAT_LIMIT = int(os.environ.get("TOOL_REPEAT_LIMIT", "3"))
# 一条用户消息累计允许用掉多少 token(输入 + 输出 + 看不见的思考)。
# 按一轮 ≈ 15K 估,10 万大约是第 6~7 轮的位置 —— 正常问答 1~3 轮根本碰不到,
# 而真绕住的时候能在花掉一半之前就掐住。填 0 = 不限。
# **这个数算的是「输出(含思考)」,不是输入+输出。** 一轮重推理 ≈ 7~9K 输出,
# 所以 40K 大约是第 5 轮的位置,在轮数上限(10)之前,能拦住"轮数不多但每轮极贵"。
# 填 0 = 不限。别把它调成十万级:那样永远轮不到它,等于没有这道闸门。
TURN_TOKEN_BUDGET = int(os.environ.get("TURN_TOKEN_BUDGET", "40000"))


class LoopGuard:
    """一轮用户消息的刹车片。三条判据任一命中就停下。

    **为什么不能只靠 `for _ in range(10)`**:那个上限只数轮数,不看内容也不看花费。
    模型用同样的参数查第三遍时它一声不吭,非要等满十轮才停;而十轮的钱早就花完了,
    最后还只甩一句"轮数过多,请换个问法" —— 用户既不知道发生了什么,也不知道花了多少。
    """

    def __init__(self) -> None:
        self.rounds = 0
        self.calls: dict[str, int] = {}   # 工具签名 → 调过几次
        self.tokens_in = 0
        self.tokens_out = 0
        self.usage_seen = False           # 上游到底给没给用量(没给就别装作在管钱)
        self.stop = ""                    # 命中的判据
        self.detail = ""                  # 命中时的补充信息(比如是哪个工具在打转)
        self.detail_en = ""

    # ---- 记账 ----
    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def add_usage(self, prompt: int, completion: int) -> None:
        """记一轮的用量。上游没给就别记 —— 见 `usage_seen`。"""
        if not prompt and not completion:
            return
        self.usage_seen = True
        self.tokens_in += int(prompt or 0)
        self.tokens_out += int(completion or 0)

    # ---- 判据 ----
    def before_round(self) -> str:
        """开下一轮之前问一句:还能继续吗?返回空串 = 可以。"""
        self.rounds += 1
        if self.rounds > MAX_TOOL_ROUNDS:
            self.stop = "rounds"
            return self.stop
        # **只有真拿到用量才判花费**。上游不给用量时(有些中转不返回 usage),
        # 本地估不出模型的思考 token —— 那才是大头,估出来的数会小得离谱,
        # 拿它当闸门等于没有闸门。宁可这条不生效,也不能给人"已经管住了"的错觉。
        # **只算输出。** 原来算的是 输入+输出,而输入会随对话变长而涨
        # (整段历史每轮都重发一遍):实测一段长对话里每轮输入 15K+,
        # 7 轮就 111K —— 闸门当场误杀,而模型**根本没在打转**,是在正常干活。
        # 输出才是该盯的:①它占成本约 89%(见第六之十九节那笔账);
        # ②它只随「真的又想了一轮」而涨,不随对话长度涨。
        if TURN_TOKEN_BUDGET and self.tokens_out > TURN_TOKEN_BUDGET:
            self.stop = "tokens"
            return self.stop
        return ""

    def note_call(self, name: str, args) -> str:
        """记一次工具调用。返回非空 = 这个调用重复太多次,该停了。"""
        try:
            key = name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True,
                                          default=str)
        except Exception:
            key = name + "|" + str(args)
        self.calls[key] = self.calls.get(key, 0) + 1
        if self.calls[key] >= TOOL_REPEAT_LIMIT:
            self.stop = "repeat"
            # 中英各存一份:`_tool_label` 出的是中文标签,英文模式下直接用工具原名,
            # 不然英文回复里会冒出一句中文,用户以为是乱码。
            self.detail = "%s,连着第 %d 次,条件一模一样" % (_tool_label(name), self.calls[key])
            self.detail_en = "%s, %d times in a row with identical arguments" % (
                name, self.calls[key])
            return self.stop
        return ""

    # ---- 收尾 ----
    def spent(self, english: bool = False) -> str:
        """花了多少,说人话。上游没给用量就如实说查不到,不许编一个数出来。"""
        if english:
            if not self.usage_seen:
                return "%d model calls (this provider returns no usage data)" % self.rounds
            return ("%d model calls, roughly %.0fK tokens (%.0fK in + %.0fK out/thinking; "
                    "the budget counts output only)"
                    % (self.rounds, self.tokens / 1000,
                       self.tokens_in / 1000, self.tokens_out / 1000))
        if not self.usage_seen:
            return "问了 %d 轮模型(这家中转没有返回用量,具体多少查不到)" % self.rounds
        return ("问了 %d 轮模型,大约 %.1f 万 token(输入 %.0fK + 输出/思考 %.0fK;"
                "**闸门只按输出算**,输入会随对话变长而涨,拿它当闸门会误杀)"
                % (self.rounds, self.tokens / 10000,
                   self.tokens_in / 1000, self.tokens_out / 1000))

    def message(self, lang: str = "zh") -> str:
        """给用户的交代。

        **不能只说"已中止"** —— 那是死胡同:用户不知道发生了什么、花了多少、
        下一步该干嘛,多半会原样再发一次,于是再烧一遍。
        """
        english = str(lang).lower().startswith("en")
        print("[guard] 中止:%s rounds=%d tokens=%d+%d %s"
              % (self.stop, self.rounds, self.tokens_in, self.tokens_out, self.detail),
              flush=True)
        br = "\n\n"
        if english:
            why = {
                "repeat": ("I kept running the exact same lookup over and over (%s) "
                           "and getting the same thing back — I was going in circles, "
                           "not making progress." % (self.detail_en or self.detail)),
                "rounds": ("I went %d rounds of looking things up without reaching "
                           "a conclusion." % (self.rounds - 1)),
                "tokens": ("This one question burned through its thinking budget "
                           "(%.0fK output tokens) without reaching a conclusion."
                           % (self.tokens_out / 1000)),
            }.get(self.stop, "I stopped making progress.")
            return ("⚠️ **I stopped on purpose — going further would just cost money "
                    "for nothing.**" + br
                    + "**What happened:** " + why + br
                    + "**Spent on this message:** " + self.spent(True) + "." + br
                    + "**What you can do:**\n"
                    + "· Be more specific (name the campaign, or give a date range);\n"
                    + "· Or split it into two smaller questions and ask them one at a time.")
        why = {
            "repeat": ("我连着用**一模一样的条件**去查同一样东西(%s),每次拿回来的都一样"
                       " —— 说明我卡住了,没有在往前走。" % self.detail),
            "rounds": "我查了 %d 轮还是没能得出结论。" % (self.rounds - 1),
            "tokens": ("这一条问题已经想掉了 %.0fK 的输出额度(思考占大头),"
                       "还是没能得出结论。" % (self.tokens_out / 1000)),
        }.get(self.stop, "我没有继续往前走了。")
        return ("⚠️ **我主动停下来了 —— 再问下去只是白花钱。**" + br
                + "**发生了什么**:" + why + br
                + "**这一条消息花掉**:" + self.spent() + "。" + br
                + "**你可以这样做**:\n"
                + "· 把问题说得更具体一点(带上计划名字、或者一个日期范围);\n"
                + "· 或者把它拆成两个小问题,一个一个问。")


CURRENT_GUARD: contextvars.ContextVar = contextvars.ContextVar("adbot_guard", default=None)


def _guard() -> "LoopGuard":
    """这一轮的刹车片。

    **不能用可变对象当 ContextVar 的默认值** —— 那样所有请求会共用同一份,
    A 的轮数会算到 B 头上,等于没隔离(和 CURRENT_EXECUTED 是同一个坑)。
    """
    g = CURRENT_GUARD.get()
    if g is None:
        g = LoopGuard()
        CURRENT_GUARD.set(g)
    return g

# ===== Gemini 的超时与熔断 =====
# 实测(2026-08-26,开发机):`generativelanguage.googleapis.com` 的 TLS 握手 16ms、
# GET 根路径 0.13s 就回 404 —— **网络是通的**。但 `generateContent` 这个 POST
# 发出去**没有回音**(httpx ReadTimeout,20s / 60s 各试一次都一样)。
# 三件事叠在一起,表现就是"回答很慢、最后还报错":
#   ① SDK 默认不设超时 → 干等到 TCP 自己放弃;
#   ② 抛出来的是 httpx.ReadTimeout,**不是 genai APIError** → 下面的接力捕不到,
#      明明配了 ofox 也不切,直接把错误甩给用户;
#   ③ 就算切了,下一条消息又从 Gemini 重来一遍,每条都白等一次。
# 所以:给它超时、把超时也算进接力条件、并且**记住它刚才没回应**,
# 冷却期内直接走备用通道(有备用通道才跳过,没有的话还是要试)。
GEMINI_TIMEOUT_MS = int(os.environ.get("GEMINI_TIMEOUT_MS", "30000"))
GEMINI_COOLDOWN_S = int(os.environ.get("GEMINI_COOLDOWN_S", "300"))
_GEMINI_DOWN_UNTIL = 0.0


def _gemini_client(api_key: str | None = None) -> genai.Client:
    """建 Gemini 客户端。**一定要带超时**,理由见上面那段。"""
    opts = genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_MS)   # 单位是毫秒
    return genai.Client(api_key=api_key, http_options=opts) if api_key else genai.Client(http_options=opts)


def _is_network_fail(e: Exception) -> bool:
    """是"连不上/没回音"这类网络故障吗?(区别于平台明确回的错误码)"""
    return isinstance(e, (httpx.TimeoutException, httpx.TransportError))


def _should_fallback(e: Exception) -> bool:
    """这个错该不该切备用通道。**401/403 这类钥匙问题绝不切** ——
    切了只会用别的通道把配置错误盖过去,用户永远看不到真实原因。"""
    if isinstance(e, genai_errors.APIError):
        return e.code in _RETRYABLE_CODES
    return _is_network_fail(e)


def _gemini_ready() -> bool:
    """现在该不该走 Gemini。刚超时过、而且有备用通道 → 先跳过,别让用户白等。"""
    if not os.environ.get("GEMINI_API_KEY"):
        return False
    if time.time() < _GEMINI_DOWN_UNTIL and os.environ.get("OPENAI_API_KEY"):
        return False
    return True


def _slow_fail(e: Exception) -> bool:
    """这次失败是"干等了半天"型的吗?——网络没回音,或平台的 503/504(超时/过载)。
    429 那种是**秒回**的,不浪费时间,不算。"""
    if _is_network_fail(e):
        return True
    return isinstance(e, genai_errors.APIError) and e.code in (503, 504)


def _mark_gemini_down(e: Exception) -> None:
    """记下 Gemini 刚才干等了半天,冷却一段时间,别让下一条消息再等一遍。

    **只在备用通道确实顶上了之后才调**:备用通道也坏的话跳过 Gemini,
    等于一个能用的都不剩(实测撞上过 —— Gemini 504 + ofox 余额为负全 402)。
    """
    global _GEMINI_DOWN_UNTIL
    if _slow_fail(e):
        _GEMINI_DOWN_UNTIL = time.time() + GEMINI_COOLDOWN_S
        print(f"[brain] Gemini 等了 {GEMINI_TIMEOUT_MS/1000:.0f}s 还没结果,"
              f"接下来 {GEMINI_COOLDOWN_S//60} 分钟直接走备用通道", flush=True)



def ask_gemini(messages: list[ChatMessage], lang: str = "zh") -> str:
    """大脑 A:Gemini。钥匙从环境变量 GEMINI_API_KEY 自动读取。

    走「手动挡」:关掉 SDK 的自动工具调用,自己跑工具循环。
    为什么不用自动挡 —— 自动挡配上流式实测是坏的(`generate_content_stream`
    只回一个 text='' 的空块就 STOP,工具循环根本没跑)。自己跑循环之后,
    既能边收边吐字,也能在每次调工具时播报"正在查什么"。
    """
    client = _gemini_client()
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
        tools=[fn for fn in NEWSBREAK_TOOLS if _tool_allowed(fn.__name__)],
        # 关掉自动工具调用:我们自己执行、自己把结果贴回去。
        # 这样才能在流式下正常工作,也才能播报每一步在干什么。
        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
    )
    streaming = CURRENT_EMIT.get() is not None
    contents = list(contents)
    retried_empty = False        # 空回复只原地重试一次

    guard = _guard()
    while True:
        # **每轮开始前先问闸门**:轮数到顶 / 这条消息的花费到顶 → 立刻停,
        # 并把"停在哪、花了多少、下一步怎么办"如实告诉用户(见 LoopGuard)。
        if guard.before_round():
            _emit("reset")
            return guard.message(lang)
        texts, calls, model_parts = [], [], []
        usage = [0, 0]

        def take_usage(obj):
            """记这一轮的 token 用量。流式时每个 chunk 都带**累计值**,
            所以是覆盖不是累加 —— 累加会算出好几倍,闸门会提前误踩。"""
            u = getattr(obj, "usage_metadata", None)
            if u is None:
                return
            # **取 max,不是直接覆盖。** 累计值只会往上走,而**最后一个 chunk
            # 可能只带 prompt 不带 candidates**(实测过一整轮下来输出被记成 0)——
            # 直接覆盖就把前面记到的输出抹掉了,花费闸门跟着失灵。
            usage[0] = max(usage[0], int(getattr(u, "prompt_token_count", 0) or 0))
            usage[1] = max(usage[1],
                           int(getattr(u, "candidates_token_count", 0) or 0)
                           + int(getattr(u, "thoughts_token_count", 0) or 0))

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
                take_usage(chunk)
                for part in _gemini_parts(chunk):
                    take(part)
        else:
            resp = client.models.generate_content(model=model, contents=contents, config=cfg)
            take_usage(resp)
            for part in _gemini_parts(resp):
                take(part)
        guard.add_usage(usage[0], usage[1])

        if not calls:
            out = "".join(texts)
            if out.strip():
                return out
            # 和 ChatGPT 那条一样:空回复要重试 + 说清原因,不能只甩一句"没有返回文字"
            print(f"[brain] Gemini 空回复 model={model}", flush=True)
            if not retried_empty:
                retried_empty = True
                if streaming:
                    _emit("reset")
                _emit("status", text="没收到内容,正在重试…")
                continue
            return _empty_reply_msg("", lang)

        # 它要用工具:说明刚才吐的那点字只是开场白,不是最终答案 → 让前端作废重来
        if streaming and texts:
            _emit("reset")

        contents.append(genai_types.Content(role="model", parts=model_parts))

        # 替它执行,把结果贴回去,再让它接着想
        result_parts = []
        for fc in calls:
            # 同一个工具 + 同一份参数反复调 = 它在原地打转,别再往下执行了
            if guard.note_call(fc.name, dict(fc.args or {})):
                return guard.message(lang)
            _emit("status", text=_tool_label(fc.name))
            try:
                result = _tool_call(fc.name, dict(fc.args or {}))
            except Exception as e:
                result = {"error": str(e)}
            result_parts.append(genai_types.Part.from_function_response(
                name=fc.name, response={"result": result}))
        contents.append(genai_types.Content(role="user", parts=result_parts))

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
    _oa_tool("propose_status_change",
             "登记一个开启/暂停待办(不会立即执行,须用户确认)。**开启时必须把底下的广告组和广告"
             "一起放进 extra_targets**,三层全 ON 才会真的投放,只开 campaign 等于没开",
             {"level": _LEVEL, "object_id": _ID,
              "status": {"type": "string", "enum": ["ON", "OFF"], "description": "ON=开启 OFF=暂停"},
              "name": {"type": "string", "description": "对象名字,用于向用户复述"},
              "extra_targets": {
                  "type": "array",
                  "description": "一起改的其它对象。开启广告时把要开的 ad_set / ad 都放进来"
                                 "(先用 get_delivery_tree 看结构,多于一个时问用户开哪些)",
                  "items": {"type": "object", "properties": {
                      "level": {"type": "string", "description": "campaign/ad_set/ad"},
                      "id": {"type": "string", "description": "对象 id"},
                      "name": {"type": "string", "description": "对象名字"}}}}},
             ["level", "object_id", "status"]),
    _oa_tool("propose_create_campaign", "登记一个新建广告待办(一次建好campaign+ad set+ad三层;不会立即执行,须用户确认)",
             {"ad_account_id": _ID,
              "keyword": {"type": "string", "description": "落地页类型(英文一个词,如 roof/gutter/window),用于按规范命名 NB-Roof-年月日-序号"},
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
              "campaign_name": {"type": "string", "description": "手动指定计划名(可选)"},
              "extra_creatives": {
                  "type": "array",
                  "description": "**一个广告组下面要放第二条、第三条广告时用这个**(用户说"
                                 "「一个 ad set 下面两个 ad」「这两套素材各做一条」)。第一套走上面的 "
                                 "asset_url/headline/description,第二套起放这里,广告名自动排 001/002/…。"
                                 "**绝不许为了放第二套素材去建第二条计划或第二个广告组**",
                  "items": {"type": "object", "properties": {
                      "asset_url": {"type": "string"}, "headline": {"type": "string"},
                      "description": {"type": "string"}, "asset_filename": {"type": "string"},
                      "call_to_action": {"type": "string"}, "brand_name": {"type": "string"}}}}},
             ["ad_account_id", "keyword", "landing_url", "headline", "description", "asset_url"]),
    _oa_tool("propose_add_ad",
             "往**已经建好的广告组**里再加一条(或几条)广告。"
             "用户说「把第二套素材也加进 xxx 广告组」「这个组里再加一条广告」时用它。"
             "**绝不许改用 propose_create_campaign 干这件事** —— 那会在同一支计划底下"
             "多出一个同名广告组、多花一份日预算,而且报表里两行同名分不出来。不会立即执行,须用户确认",
             {"ad_set_id": {"type": "string", "description": "广告组 id,用 get_delivery_tree 查,别按名字猜"},
              "asset_url": {"type": "string", "description": "素材地址(用户上传后系统消息里的 assetUrl)"},
              "headline": {"type": "string", "description": "广告标题,3~90 字符"},
              "description": {"type": "string", "description": "广告描述,3~90 字符"},
              "ad_account_id": _ID,
              "asset_filename": {"type": "string", "description": "素材文件名(用于判断图片/视频)"},
              "call_to_action": {"type": "string", "description": "按钮文案(可选,不填沿用组里已有广告的)"},
              "brand_name": {"type": "string", "description": "品牌名(可选,不填沿用组里已有广告的)"},
              "landing_url": {"type": "string", "description": "落地页(可选,**不填就自动沿用组里已有广告的**)"},
              "ad_name": {"type": "string", "description": "广告名(可选,不填就接着已有序号往下排:已有 001 就叫 002)"},
              "extra_creatives": {"type": "array", "description": "一次加多条时,第二条起放这里",
                                  "items": {"type": "object", "properties": {
                                      "asset_url": {"type": "string"}, "headline": {"type": "string"},
                                      "description": {"type": "string"}, "asset_filename": {"type": "string"},
                                      "call_to_action": {"type": "string"}, "brand_name": {"type": "string"}}}}},
             ["ad_set_id", "asset_url", "headline", "description"]),
    _oa_tool("propose_landing_images",
             "**用 AI 把落地页上没配上的图生出来**(图库没找到时用)。"
             "生图要花钱,所以只登记不执行 —— 报价后要用户在下一条消息明确同意才 confirm_action",
             {"landing_file": {"type": "string", "description": "生成结果里的 file 字段"},
              "slots": {"type": "string",
                        "description": "可选,像 \"1,3\" 指定只生哪几张;留空 = 所有空着的位置"}},
             ["landing_file"]),
    _oa_tool("swap_landing_image",
             "把已生成落地页里的**某一张配图**换掉,别的内容一个字不动。"
             "用户说「第2张图换成xxx」时用它,**别为了换图重新生成整页**",
             {"landing_file": {"type": "string", "description": "生成结果里的 file 字段"},
              "slot": {"type": "integer", "description": "第几张图(页面里 data-slot 的编号)"},
              "query": {"type": "string", "description": "**英文**搜索关键词,越具体越好"}},
             ["landing_file", "slot", "query"]),
    _oa_tool("confirm_action", "执行之前登记的待办(仅在用户新消息中明确同意后)", {"action_id": {"type": "string"}}, ["action_id"]),
    _oa_tool("cancel_action", "取消之前登记的待办", {"action_id": {"type": "string"}}, ["action_id"]),
    _oa_tool("list_pending_actions", "查看保险箱里所有已登记待确认的待办(含编号);忘了编号用它查,严禁重复登记", {}, []),
    _oa_tool("get_delivery_tree",
             "看一条广告计划底下有哪些广告组和广告、各自开还是关。**开启广告前必须先调它**:"
             "三层全 ON 才会真的投放,只开 campaign 等于没开",
             {"campaign_id": {"type": "string", "description": "广告计划 id"},
              "ad_account_id": _ID}, ["campaign_id"]),
    _oa_tool("recommend_creatives",
             "**列出/查看这个账户已有的素材图**(从它自己投过的广告里取，带真实 CTR/花费，版权干净)。"
             "用户问「我账户里有哪些图」「有什么素材能用」「看下历史素材」「帮我推荐素材」都调它。"
             "**这是本项目唯一能拿到账户历史素材的工具，没有别的接口** —— "
             "所以绝不许回答「接口限制/没有权限/拿不到」，调一次就知道。"
             "建新广告第3步只是其中一个使用场景，不是唯一场景",
             {"ad_account_id": _ID,
              "days": {"type": "integer", "description": "看最近多少天的数据,默认90"},
              "top_n": {"type": "integer", "description": "推荐几个,默认5,最多10"}}, []),
    _oa_tool("search_stock_creatives",
             "从正规授权图库(Pexels/Pixabay/Openverse)找可以合法投广告的新素材,"
             "每张带尺寸、许可证和质量评价。账户里没有合适的历史素材、或用户想找新风格的图时用。"
             "只返回允许商用的素材",
             {"keyword": {"type": "string", "description": "英文搜索词,按落地页内容给,如 roof repair"},
              "count": {"type": "integer", "description": "要几张,1~12,默认6"},
              "source": {"type": "string", "enum": ["auto", "pexels", "pixabay", "openverse"],
                         "description": "图库,默认 auto(有钥匙的商业图库优先)"}}, ["keyword"]),
    _oa_tool("native_market_scan",
             "不指定品类,看整个原生广告市场上什么类型的单子跑得最好(品类排行+钩子排行)。"
             "用户问「现在什么广告跑得好」「这平台适合跑什么单子」这类**开放问题**时用 —— "
             "这时不要拿他账户已有的品类去框",
             {"top_n": {"type": "integer", "description": "看多少条,20~500,默认200"},
              "active_only": {"type": "boolean", "description": "只看还在投的"}}, []),
    _oa_tool("platform_kind",
             "判断投放平台属于哪一类(原生广告平台/大媒体/DSP),并说清该去哪儿找竞品素材。"
             "用户说要在某平台投某品类时先调这个 —— 确定是原生平台,就去竞品库捞"
             "**所有原生平台**上跑同品类的素材来拆,不只是那一家",
             {"platform": {"type": "string", "description": "平台名,留空=当前平台"}}, []),
    _oa_tool("my_ad_categories",
             "看这个广告账户自己在投什么品类(从计划名和广告文案里认)。"
             "用户问「同行在投什么」却没说品类时,先调这个,不要自己编关键词",
             {"ad_account_id": _ID}, []),
    _oa_tool("search_competitor_ads",
             "查竞品正在投的真实原生广告(OpenAdLibrary):素材长什么样、投了多久、铺了多少版位。"
             "用户想知道同行在投什么、或想找有市场验证的创意思路时用。"
             "比图库强在这些是真金白银在投的广告",
             {"keyword": {"type": "string", "description": "英文关键词,按品类给,如 roof repair"},
              "count": {"type": "integer", "description": "要几条,默认8,最多50"},
              "country": {"type": "string", "description": '两位大写国家码,默认 "US";留空则不限国家'},
              "active_only": {"type": "boolean", "description": "只看现在还在投的,默认 true"},
              "sort_by": {"type": "string", "enum": ["placements", "days", "recent"],
                          "description": "placements=按版位数(默认,铺得广=肯花钱) days=按投放天数 recent=最近新上的"},
              "min_days": {"type": "integer", "description": "只要投放天数≥这个数的,默认0不筛"}},
             ["keyword"]),
    _oa_tool("search_competitor_landing_pages",
             "从 OpenAdLibrary 查疑似表现较好的落地页候选。按仍在投、投放天数、版位数筛选；"
             "这些是表现信号而非真实转化数据。返回 candidate_id、落地页截图、域名和抓取证据；"
             "截图可用时应直接用 Markdown 图片展示",
             {"keyword": {"type": "string", "description": "英文品类关键词，如 roof repair"},
              "count": {"type": "integer", "description": "候选数量，默认8"},
              "country": {"type": "string", "description": "两位国家码，默认 US"},
              "active_only": {"type": "boolean", "description": "是否只看仍在投的广告"},
              "min_days": {"type": "integer", "description": "最低投放天数，默认0"}},
             ["keyword"]),
    _oa_tool("decompose_landing_page",
             "抓取并拆解一个落地页，分析排版、文案、offer、信任、表单、CTA、关键词和可迁移优点。"
             "优先传搜索结果的 candidate_id，也可传用户提供的完整 URL",
             {"candidate_id": {"type": "string", "description": "候选结果里的 id"},
              "url": {"type": "string", "description": "用户直接提供的完整 http(s) URL"},
              "lang": {"type": "string", "description": "zh 或 en"}}, []),
    _oa_tool("summarize_landing_page_patterns",
             "汇总已拆解页面的共性、关键词和 offer 设计，并生成原创 HTML 落地页。"
             "默认 2 版做 A/B；**用户说「只要一个」时必须传 n_variants=1**",
             {"n_variants": {"type": "integer", "description":
                 "生成几版：1 或 2，默认 2。用户明确只要一个就传 1，不要多给"},
              "brand": {"type": "string", "description": "自己的品牌名，可空"},
              "offer": {"type": "string", "description": "自己的 offer，可空，空时使用占位表达"},
              "audience": {"type": "string", "description": "目标人群，可空"},
              "lang": {"type": "string", "description": "zh 或 en"}}, []),
    _oa_tool("list_cloudflare_landing_resources",
             "只读列出 Cloudflare 可选域名、现有 Pages 项目和域名映射。用户准备发布但没指定域名时先调用",
             {"query": {"type": "string", "description": "按域名关键词筛选，可空"},
              "limit": {"type": "integer", "description": "最多返回多少个域名，默认50，最多100"}}, []),
    _oa_tool("list_clickflare_campaigns",
             "只读列出 ClickFlare 里的 campaign。用户说不清要跑哪条时用它找",
             {"search": {"type": "string", "description": "按名字关键词筛选，可空"}}, []),
    _oa_tool("describe_clickflare_campaign",
             "只读:看一条 campaign 现在挂着哪些落地页和 offer、权重多少。"
             "**改任何投放之前都要先调它，把现状摆给用户看**",
             {"campaign": {"type": "string", "description":
                 "用户给的 Campaign Tracking URL（形如 https://追踪域名/cf/r/<24位id>），"
                 "或 24 位 campaign id。**严禁改成用名字搜索的结果**"}},
             ["campaign"]),
    _oa_tool("create_clickflare_landers",
             "把已发布的 A/B 两个地址登记成 ClickFlare 的两个 Lander。**只新增，建好也不会承接流量**；"
             "要挂进 campaign 是另一步、必须用户确认。workspace 和追踪域名由代码推导，严禁自己填",
             {"campaign": {"type": "string", "description":
                 "Campaign Tracking URL（形如 https://追踪域名/cf/r/<24位id>）或 24 位 campaign id。"
                 "**严禁改成用名字搜索的结果**"},
              "variant_a_url": {"type": "string", "description": "A 版正式地址"},
              "variant_b_url": {"type": "string", "description": "B 版正式地址"},
              "cta_url": {"type": "string", "description": "本次的 ClickFlare CTA Click URL，用来反查追踪域名"},
              "name_prefix": {"type": "string", "description": "Lander 命名前缀，一般用实验名，可空"}},
             ["campaign", "variant_a_url", "variant_b_url", "cta_url"]),
    _oa_tool("clickflare_publish_kit",
             "一次取齐发布落地页要用的追踪域名、CTA Click URL 和 Campaign Tracking URL。"
             "**用户不用再手工去 ClickFlare 后台复制任何东西**，追踪脚本也会自动取好存起来。"
             "准备发布落地页时先调它",
             {"campaign": {"type": "string", "description":
                 "用户给的 Campaign Tracking URL（形如 https://追踪域名/cf/r/<24位id>）"
                 "或 24 位 campaign id"},
              "cta_index": {"type": "integer", "description": "CTA 出口序号，默认 1"}},
             ["campaign"]),
    _oa_tool("propose_swap_campaign_landers",
             "登记「把这条 campaign 的落地页换成 A/B 两个、各 50%」的待办。"
             "**这是外部写操作且确认后立刻生效**——只登记，必须完整复述现状与改动、"
             "等用户下一条消息明确同意后才能 confirm_action。offer 不会动",
             {"campaign": {"type": "string", "description": "Campaign Tracking URL(带 cpid=)或 campaign id"},
              "lander_a_id": {"type": "string", "description": "A 版 Lander 的 id(create_clickflare_landers 返回的)"},
              "lander_b_id": {"type": "string", "description": "B 版 Lander 的 id"},
              "weight_a": {"type": "integer", "description": "A 的权重，默认 50"},
              "weight_b": {"type": "integer", "description": "B 的权重，默认 50，两者相加必须是 100"},
              "path_name": {"type": "string", "description":
                  "这条 flow 有多条启用中的 path 时才要填，代码不会替用户挑；只有一条时留空"},
              "allow_identical": {"type": "boolean", "description":
                  "两个 Lander 指向同一个网址时才用得上。默认 false。"
                  "**只有用户明确说要做 A/A 测试才传 true**，严禁自己带上"}},
             ["campaign", "lander_a_id", "lander_b_id"]),
    _oa_tool("propose_publish_landing_pages",
             "登记把两个已生成页面发布为 Cloudflare Pages A/B 版本的待办。新域名自动建项目，旧域名复用；"
             "这是外部写操作，只登记，必须等用户下一条消息确认后再 confirm_action",
             {"domain": {"type": "string", "description": "本次选择的域名或子域名，不带路径"},
              "slug": {"type": "string", "description": "实验路径名，如 roof-260902"},
              "cta_url": {"type": "string", "description": "用户原样提供的 ClickFlare HTTPS CTA Click URL，严禁编造"},
              "tracking_script": {"type": "string", "description":
                  "用户原样提供的 ClickFlare Lander Tracking Script，严禁编造、拼接或改写。"
                  "**同一个追踪域名只需贴一次**：这个 CTA 域名以前贴过的话就留空，系统会自动复用脚本库里那段。"
                  "不确定贴没贴过就先调 list_cloudflare_landing_resources 看「已存脚本的追踪域名」"},
              "variant_a_file": {"type": "string", "description": "生成结果中 A 版的 file；可空则用最近生成页"},
              "variant_b_file": {"type": "string", "description": "生成结果中 B 版的 file；可空则用最近生成页"},
              "allow_identical": {"type": "boolean", "description":
                  "A/B 两版内容完全相同时才允许发布。默认 false。"
                  "**只有用户明确说要做 A/A 测试(用两个一样的页面验证分流是否准确)才传 true**,"
                  "绝不许自己替用户决定"},
              "tracking_script_b": {"type": "string",
                                    "description": "B 版单独的 Lander Tracking Script。"
                                                   "**留空 = 两版共用上面那段**。"
                                                   "只有当用户明确说「两个 Lander 的脚本不一样」并给了第二段时才填,"
                                                   "严禁编造或改写"},
              "allow_replace": {"type": "boolean",
                                "description": "整站覆盖发布。默认 false。**只有用户明确说了「覆盖发布」才可以传 true** —— "
                                               "发布是整站替换,本地历史目录丢失时这一下会删掉线上所有旧实验,"
                                               "ClickFlare 里的 Lander 会指向 404。绝不许自己替用户决定"}},
             ["domain", "slug", "cta_url"]),
    _oa_tool("decompose_creative",
             "拆解一张广告素材:看懂它的版式、画面、文字层、配色、CTA 和文案角度。"
             "用户想知道某条广告为什么好、怎么设计的时候调它。只接受搜索结果里出现过的地址",
             {"image_url": {"type": "string", "description": "搜索结果里那张图的 image_url,原样传"}},
             ["image_url"]),
    _oa_tool("summarize_creative_patterns",
             "把已拆解的多条竞品素材归纳成共同套路,并照着写出我们自己的文案和画面方案。"
             "要先 decompose_creative 拆过至少 2 条。产出不含竞品品牌和具体价格承诺",
             {"brand": {"type": "string", "description": "我们自己的品牌名"},
              "landing_url": {"type": "string", "description": "我们的落地页链接"},
              "n_variants": {"type": "integer", "description": "出几版方案,默认3,最多5"},
              "lang": {"type": "string", "description": "zh 或 en,跟随界面语言"}}, []),
    _oa_tool("propose_make_creatives",
             "把 summarize_creative_patterns 归纳出的方案做成可投放的广告图"
             f"({cr.AD_SIZE[0]}×{cr.AD_SIZE[1]}):干净的实拍画面,**图上不放任何文字** —— "
             "标题和按钮由 NewsBreak 自己渲染。每版换一种镜头。"
             "要花钱(约 $0.20/张),所以只登记待办,等用户确认后才生成",
             {"variants": {"type": "string", "description": "要做哪几版,如 '1,3';留空=全做"},
              "ad_account_id": _ID}, []),
    _oa_tool("use_found_creative",
             "把用户在 search_stock_creatives 结果里选中的那张图转存进 NewsBreak,"
             "换回建广告要用的 assetUrl。只接受搜索结果里出现过的地址",
             {"image_url": {"type": "string", "description": "搜索结果里那张图的 image_url,原样传"},
              "ad_account_id": _ID}, ["image_url"]),
    _oa_tool("list_conversion_events", "查询账户下的转化事件列表(建新广告第3步让用户挑)", {"ad_account_id": _ID}, ["ad_account_id"]),
    _oa_tool("propose_schedule", "登记一个定时开启/暂停广告的待办(需用户确认后生效)",
             {"kind": {"type": "string", "enum": ["once", "daily"], "description": "once=只执行一次,daily=每天重复"},
              "when": {"type": "string", "description": 'once 用 "YYYY-MM-DD HH:MM",daily 用 "HH:MM",均为北京时间'},
              "level": _LEVEL, "object_id": _ID,
              "status": {"type": "string", "enum": ["ON", "OFF"], "description": "ON=到点开启 OFF=到点暂停"},
              "name": {"type": "string", "description": "对象名字,用于复述"},
              "extra_targets": {
                  "type": "array",
                  "description": "到点时一起改的其它对象。**定时开启广告时必须带上底下的广告组和广告**,"
                                 "否则到点只翻了 campaign 一层,广告照样不投,而且没人盯着,"
                                 "可能第二天才发现",
                  "items": {"type": "object", "properties": {
                      "level": {"type": "string", "description": "campaign/ad_set/ad"},
                      "id": {"type": "string", "description": "对象 id"},
                      "name": {"type": "string", "description": "对象名字"}}}}},
             ["kind", "when", "level", "object_id", "status"]),
    _oa_tool("list_schedules", "查看所有定时任务(下次执行时间、上次结果)", {}, []),
    _oa_tool("cancel_schedule", "取消一个定时任务", {"task_id": {"type": "string"}}, ["task_id"]),
]

# 工具名 → 真实函数 的对照表(ChatGPT 说要调哪个,我们就去执行哪个)
OPENAI_TOOL_FUNCS = {fn.__name__: fn for fn in [
    recommend_creatives, search_stock_creatives, search_competitor_ads, my_ad_categories, platform_kind, native_market_scan,
    search_competitor_landing_pages, decompose_landing_page, summarize_landing_page_patterns,
    list_cloudflare_landing_resources, propose_publish_landing_pages, swap_landing_image,
    propose_landing_images,
    list_clickflare_campaigns, describe_clickflare_campaign, create_clickflare_landers,
    propose_swap_campaign_landers, clickflare_publish_kit,
    decompose_creative, summarize_creative_patterns,
    use_found_creative, propose_make_creatives, get_delivery_tree,
    list_organizations, list_ad_accounts, list_campaigns, list_ad_sets, list_ads,
    get_report, propose_status_change, propose_create_campaign, propose_add_ad,
    confirm_action, cancel_action,
    list_pending_actions, list_conversion_events,
    propose_schedule, list_schedules, cancel_schedule,
]}


def ask_openai(messages: list[ChatMessage], lang: str = "zh") -> str:
    """大脑 B:ChatGPT。钥匙从 OPENAI_API_KEY 读取(转发服务再加 OPENAI_BASE_URL)。"""
    client = openai.OpenAI(timeout=BRAIN_TIMEOUT_S, max_retries=BRAIN_RETRIES)
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

    retried_empty = False        # 空回复只原地重试一次,别把额度耗在死循环上
    guard = _guard()
    while True:
        # 每轮开始前先问闸门(轮数 / 花费),见 Gemini 那条路上的同一段说明
        if guard.before_round():
            _emit("reset")
            return guard.message(lang)
        content, tool_calls, finish, ok_model = "", [], "", None
        for model in models_to_try:
            try:
                content, tool_calls, finish, usage = _openai_once(
                    client, model, msgs, streaming)
                guard.add_usage(usage[0], usage[1])
                ok_model = model
                break
            except openai.NotFoundError:
                continue
        if ok_model is None:
            raise openai.NotFoundError.__new__(openai.NotFoundError)  # 型号全不可用
        models_to_try = [ok_model]   # 记住能用的型号,后面几轮不再试错

        if not tool_calls:
            if content.strip():
                return content
            # **空回复**:一个字没吐,也没说要调工具。实测是偶发(同样的问题
            # 连发三次都正常),所以原地重试一次 —— 原来直接甩一句
            # "(ChatGPT 没有返回文字)",既不重试、也不留线索,用户只能干瞪眼。
            print(f"[brain] 空回复 finish_reason={finish!r} model={ok_model}", flush=True)
            if not retried_empty:
                retried_empty = True
                if streaming:
                    _emit("reset")            # 把可能吐了一半的字作废
                _emit("status", text="没收到内容,正在重试…")
                continue
            return _empty_reply_msg(finish, lang)

        # 它想用工具:说明刚才吐的那点字只是开场白,不是最终答案 → 让前端作废重来
        if streaming and content:
            _emit("reset")
        msgs.append({"role": "assistant", "content": content or None,
                     "tool_calls": [{"id": tc["id"], "type": "function",
                                     "function": {"name": tc["name"],
                                                  "arguments": tc["arguments"]}}
                                    for tc in tool_calls]})
        for tc in tool_calls:
            # 参数解析要单独兜住:解析失败时**照旧把错误告诉模型**,
            # 不能改成"用空参数调一遍"—— 那会让它以为查了个寂寞,还可能真发出去。
            bad = ""
            try:
                args = json.loads(tc["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError("参数不是一个对象")
            except Exception as e:
                args, bad = tc["arguments"], "参数解析失败:%s" % e
            # 同一个工具 + 同一份参数反复调 = 它在原地打转,别再往下执行了
            if guard.note_call(tc["name"], args):
                return guard.message(lang)
            _emit("status", text=_tool_label(tc["name"]))
            if bad:
                result = {"error": bad}
            else:
                try:
                    result = _tool_call(tc["name"], args)
                except Exception as e:
                    result = {"error": str(e)}
            msgs.append({"role": "tool", "tool_call_id": tc["id"],
                         "content": json.dumps(result, ensure_ascii=False)})


def _empty_reply_msg(finish: str, lang: str = "zh") -> str:
    """模型一个字都没吐时给用户的交代。

    **绝不能只说"没有返回文字"** —— 那是死胡同:用户不知道是自己的问题、
    还是系统坏了,也不知道该怎么办。把上游给的结束原因翻译成人话,并给出下一步。
    """
    english = str(lang).lower().startswith("en")
    why_zh = {"length": "回答太长被截断了", "content_filter": "内容被安全策略拦下了"}
    why_en = {"length": "the answer was cut off by the length limit",
              "content_filter": "the content was blocked by a safety filter"}
    if english:
        why = why_en.get(finish, f"the provider reported finish_reason={finish or 'unknown'}")
        return ("⚠️ The model returned nothing this time (" + why + "). "
                "I already retried once. Please send the question again — "
                "if it keeps happening, try rephrasing it or breaking it into smaller parts.")
    why = why_zh.get(finish, f"上游给的结束原因是 {finish or '未知'}")
    return ("⚠️ 这一轮模型一个字都没返回(" + why + ")。我已经自动重试过一次了。"
            "请把问题再发一次;如果反复这样,换个说法、或者把问题拆小一点再问。")


# 有的中转不认 `stream_options`(流式要用量必须靠它)。碰过一次 400 就记下来,
# 之后不再发这个参数 —— 而不是每轮都撞一次墙。拿不到用量时花费闸门自动失效
# (见 LoopGuard.before_round 里的说明),轮数和重复调用两道照常管用。
_STREAM_USAGE_OK = True


def _openai_once(client, model: str, msgs: list,
                 streaming: bool) -> tuple[str, list, str, tuple]:
    """问一轮 OpenAI/ofox,返回 (文字, 工具调用清单, finish_reason, (输入token, 输出token))。

    流式时边收边把文字播报出去;工具调用是分片来的(名字和参数会拆成好几块),
    要按 index 拼起来才完整。

    **`finish_reason` 一定要带回去**:模型偶尔会一个字都不吐、也不调工具,
    上游只有这个字段能说明为什么(截断?被安全策略拦了?)。原来完全没读它,
    于是那种情况只能回一句"没有返回文字",用户和日志都拿不到任何线索。
    """
    schemas = [s for s in OPENAI_TOOL_SCHEMAS
               if _tool_allowed(s.get("function", {}).get("name", ""))]
    def took(obj) -> tuple:
        """从响应里抠出用量。**思考 token 已经含在 completion_tokens 里**
        (实测一次聊天回复输出 7~9K,而屏幕上只有几百字,差额就是思考),
        所以这里不用再单独加一遍。"""
        u = getattr(obj, "usage", None)
        if u is None:
            return (0, 0)
        return (int(getattr(u, "prompt_tokens", 0) or 0),
                int(getattr(u, "completion_tokens", 0) or 0))

    if not streaming:
        r = client.chat.completions.create(model=model, messages=msgs, tools=schemas)
        m = r.choices[0].message
        return (m.content or "",
                [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or ""}
                 for tc in (m.tool_calls or [])],
                r.choices[0].finish_reason or "",
                took(r))

    global _STREAM_USAGE_OK
    kw = {"stream_options": {"include_usage": True}} if _STREAM_USAGE_OK else {}
    try:
        stream = client.chat.completions.create(
            model=model, messages=msgs, tools=schemas, stream=True, **kw)
    except openai.BadRequestError as e:
        # 只在确实是这个参数被拒时降级,别把别的 400 也当成"不支持用量"吞掉
        if not kw or "stream_options" not in str(e):
            raise
        print("[brain] 这家中转不支持 stream_options,之后不再要用量", flush=True)
        _STREAM_USAGE_OK = False
        stream = client.chat.completions.create(
            model=model, messages=msgs, tools=schemas, stream=True)

    parts, slots, finish, usage = [], {}, "", (0, 0)
    for chunk in stream:
        # **用量在最后一个 chunk 上,而那个 chunk 的 choices 是空的** ——
        # 要抢在下面 `continue` 之前读,不然永远读不到。
        if getattr(chunk, "usage", None):
            usage = took(chunk)
        if not chunk.choices:
            continue
        if chunk.choices[0].finish_reason:
            finish = chunk.choices[0].finish_reason
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
    return "".join(parts), [slots[i] for i in sorted(slots)], finish, usage


def ask_claude(messages: list[ChatMessage], lang: str = "zh") -> str:
    """大脑 B:Claude。钥匙从环境变量 ANTHROPIC_API_KEY 自动读取。"""
    client = anthropic.Anthropic(timeout=BRAIN_TIMEOUT_S, max_retries=BRAIN_RETRIES)
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


_PREVIEW_LINK = _re.compile(r"/landing-pages/([A-Za-z0-9._-]+\.html)")


# 回复里「待办编号 xxx」这种提法。**只认紧跟在「待办/编号/action_id」后面的那串**,
# 不裸搜 8 位十六进制 —— 落地页预览文件名就是 `<8位十六进制>-xxx.html`,裸搜会误伤。
_ACTION_ID_MENTION = _re.compile(
    r"(?:待办|编号|action[ _]?id|pending action)[^0-9a-zA-Z]{0,10}([0-9a-f]{8})(?![0-9a-fA-F])",
    _re.I)


def _fake_action_note(reply: str, english: bool) -> str:
    """回复里报出来的待办编号,拿保险箱回查一遍 —— **编的当场拆穿**。

    2026-09-07 线上实测两次,而且一次比一次糟:
      · 在素材工作室里模型**根本没登记**(闸门给了移交单,它收到了、也没照做),
        却编了个编号 `a21c97ef` 让用户「回复确认」;用户确认后 `confirm_action`
        说没这个编号,它又编了个理由(「**临时归档**原因没对上」)和第二个编号;
      · 发布待办 `36085d6c` **已经执行成功了**,它还说「**文件路径索引的小意外**,
        重新发起发布登记」并给了个编的编号 —— 用户以为还要再发一次。

    上一轮的修法是在报错话术里禁掉「超时/重置/刷新」这几个词。**没用** ——
    模型换个说法就绕过去了。**禁词表是打地鼠**:能编理由的位置有无限多种措辞。
    真正管用的是这道:**凡是给出去的编号,代码都回查一遍**
    (和预览链接那道 `_fake_preview_note` 一模一样的思路)。
    """
    said: list = []
    for m in _ACTION_ID_MENTION.finditer(reply):
        aid = m.group(1).lower()
        if aid not in said:
            said.append(aid)
    if not said:
        return ""
    # **判据是「有没有真的登记过」,不是「现在还在不在箱子里」。**
    # 合法的编号会离开保险箱:确认执行掉、用户取消掉、或者只是在复述前几轮的事 ——
    # 只看箱子的话,用户刚成功取消完就会被告知「这个编号是编的」(实测到了)。
    alive = set(PENDING_ACTIONS) | _KNOWN_ACTION_IDS
    for rec in _executed():
        if isinstance(rec, dict) and rec.get("id"):
            alive.add(str(rec["id"]).lower())
    fake = [a for a in said if a not in alive]
    if not fake:
        return ""
    mine = [aid for aid, a in PENDING_ACTIONS.items() if _my_action(a)]
    if english:
        return ("⚠️ **System verification**: the pending-action id(s) mentioned above "
                "(%s) **do not exist** — the code checked the safe box. The assistant most "
                "likely did not register anything this turn, so **do not reply \"confirm\"** — "
                "nothing would happen. Real pending actions right now: %s."
                % (", ".join(fake), ", ".join(mine) or "(none)"))
    return ("⚠️ **系统核验**:上面提到的待办编号(%s)**在保险箱里根本不存在**"
            "(代码回查过的)。多半是助手编的 —— 它这一轮其实没有登记任何待办,"
            "**别按它说的回复「确认」**,确认了什么也不会发生。"
            "现在真实存在的待办:%s。"
            % ("、".join(fake), "、".join(mine) or "(一条都没有)"))


def _extra_notes(reply: str, english: bool) -> str:
    """回复末尾要额外贴的几道代码层核验(编造的预览链接 / 编造的待办编号)。

    **收成一处**:原来 `_fake_preview_note` 在 `_finalize` 的三个分支里各调一次,
    加第二道核验就要改三处、漏一处就有一条路上不生效
    (和「同一个判断散在三处,加一种就漏一处」是同一条)。
    """
    return "\n\n".join(x for x in (_fake_preview_note(reply, english),
                                    _fake_action_note(reply, english)) if x)


def _fake_preview_note(reply: str, english: bool) -> str:
    """回复里给出的落地页预览链接,**逐个回到盘上查一遍**;编的就当场拆穿。

    **为什么必须守在代码层**:线上实测,模型整轮没调任何生成工具,却照着
    `<8位十六进制>-version-a---<英文名>.html` 这个形状编了一个链接给用户 ——
    点开是一句 `{"error":"落地页预览不存在或已过期"}`,用户完全看不出是编的,
    只会以为"功能坏了"。提示词里早写了"不许编地址",拦不住。
    和写操作的 🔒/⚠️ 钢印是同一个思路(第七节)。
    """
    names = list(dict.fromkeys(_PREVIEW_LINK.findall(reply or "")))
    if not names:
        return ""
    owner = CURRENT_USER_ID.get() or ""
    fake = [n for n in names if not lp.preview_exists(n, owner)]
    if not fake:
        return ""
    base = str(CURRENT_BASE_URL.get() or "").rstrip("/")
    real = lp.recent_previews(5, owner=owner)
    links = "\n".join("· %s%s" % (base, r["preview_url"]) for r in real)
    if english:
        head = ("⚠️ **System verification**: %d preview link(s) above do not exist on disk "
                "(%s). Nothing was generated this turn — the link was made up. "
                "Ask me to generate the pages again." % (len(fake), ", ".join(fake)))
        return head + (("\n\nPreviews that really do exist:\n" + links) if real else "")
    head = ("⚠️ **系统核验**:上面给出的预览链接有 %d 个在服务器上**根本不存在**"
            "(%s)——**这一轮没有真的生成页面,那个地址是编的**。"
            "请回一句「重新生成落地页」让我真跑一遍。" % (len(fake), "、".join(fake)))
    return head + (("\n\n服务器上真实存在的预览是这几个:\n" + links) if real else
                   "\n\n服务器上现在一个生成好的预览都没有。")


def _finalize(reply: str, lang: str = "zh") -> dict:
    """给回复盖"系统核验"钢印:真执行了什么、有没有谎报,以代码层记录为准。

    这段字是代码直接输出给用户看的(不经过 AI),所以要自己按语言切换。
    """
    english = str(lang).lower().startswith("en")

    executed = _executed()
    if executed:
        parts = []
        for rec in executed:
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
        stamped = f"{reply}\n\n---\n{label}{'; '.join(parts)}"
        fake = _extra_notes(reply, english)
        return {"reply": stamped + ("\n\n" + fake if fake else "")}

    # 谎报匹配不分大小写:AI 写的是 "Successfully created",关键词表里是小写
    reply_lower = reply.lower()
    # 拆穿章里要报编号,而编号是**给用户看、还叫他去执行**的 —— 只能报他自己的。
    # 原来直接把 `PENDING_ACTIONS.keys()` 全倒出来:B 随口一句「已暂停」就会被
    # 盖上一章,里面列着 A 的待办编号,还写着「请回复『执行待办 xxx』重试」。
    mine_ids = [aid for aid, a in PENDING_ACTIONS.items() if _my_action(a)]
    if mine_ids and any(kw.lower() in reply_lower for kw in _CLAIM_KEYWORDS):
        ids = ", ".join(mine_ids) if english else "、".join(mine_ids)
        note = (f"⚠️ **System verification**: nothing was actually executed this turn — "
                f"the pending action(s) are still queued ({ids}). If the message above claims something "
                f"was created or done, that is an AI hallucination. "
                f'Reply "run pending action {ids}" to retry.'
                if english else
                f"⚠️ **系统核验**:本轮实际上没有执行任何操作,保险箱里仍有待办({ids})。"
                f"如果上面说\"已创建/已执行\",那是 AI 的幻觉,请回复「执行待办 {ids}」重试。")
        fake = _extra_notes(reply, english)
        return {"reply": f"{reply}\n\n---\n{note}" + ("\n\n" + fake if fake else "")}

    # 编造的预览链接 / 编造的待办编号都是**独立**的:它们和保险箱里有没有待办无关,
    # 上面两条分支都可能没命中,而链接和编号照样是编的。
    fake = _extra_notes(reply, english)
    if fake:
        return {"reply": f"{reply}\n\n---\n{fake}"}
    return {"reply": reply}


def _plain_completion(prompt: str, lang: str = "zh") -> str:
    """纯文本推理:走和聊天同一套大脑接力,但**不带工具**。

    为什么不复用 ask_gemini / ask_openai:那两个会把 20 个工具的 schema 一起发过去,
    模型可能中途跑去查广告数据。像"归纳竞品创意"这种活是纯推理,不该碰接口 ——
    带着工具既慢又贵,还可能答出一半跑偏。
    """
    brain = os.environ.get("BRAIN", "auto").strip().lower()

    def _gemini() -> str:
        client = _gemini_client(os.environ["GEMINI_API_KEY"])
        last = None
        for model in ("gemini-flash-latest", "gemini-flash-lite-latest"):
            try:
                return client.models.generate_content(model=model, contents=prompt).text or ""
            except Exception as e:
                last = e
                if not _should_fallback(e):
                    raise
        raise last

    def _openai() -> str:
        # 长文生成用 GEN_TIMEOUT_S,不是聊天那个 75 秒(见常量处的实测说明)
        client = openai.OpenAI(timeout=GEN_TIMEOUT_S, max_retries=0)
        models = ["gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"]
        custom = os.environ.get("OPENAI_MODEL", "").strip()
        if custom:
            models = [custom] + [m for m in models if m != custom]
        for model in models:
            try:
                r = client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": prompt}])
                return r.choices[0].message.content or ""
            except openai.NotFoundError:
                continue
        raise RuntimeError("OpenAI 通道的型号都不可用")

    if brain == "openai" and os.environ.get("OPENAI_API_KEY"):
        return _openai()
    if _gemini_ready():
        try:
            return _gemini()
        except Exception as e:
            if _should_fallback(e) and os.environ.get("OPENAI_API_KEY"):
                out = _openai()
                _mark_gemini_down(e)      # 同上:备用通道成了才记
                return out
            raise
    if os.environ.get("OPENAI_API_KEY"):
        return _openai()
    raise RuntimeError("还没配置 AI 大脑的钥匙")


def _route_brain(req: ChatRequest) -> str:
    """按 BRAIN 配置选大脑并拿到回答。三级火箭的接力逻辑只写这一份,
    普通接口和流式接口共用 —— 否则改了一边忘了另一边,行为就会不一致。"""
    brain = os.environ.get("BRAIN", "auto").strip().lower()

    if brain == "openai" and os.environ.get("OPENAI_API_KEY"):
        return ask_openai(req.messages, req.lang)
    if brain == "claude" and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return ask_claude(req.messages, req.lang)

    # 三级火箭:Gemini(免费)→ 不行就切 ChatGPT/ofox → 都没有再看 Claude
    if _gemini_ready():
        try:
            return ask_gemini(req.messages, req.lang)
        except Exception as e:
            # 注意:这里**不能只捕 APIError** —— 超时抛的是 httpx.ReadTimeout,
            # 只捕 APIError 的话,明明有 ofox 也不切,直接把错误甩给用户。
            if _should_fallback(e) and os.environ.get("OPENAI_API_KEY"):
                why = "没有响应(超时)" if _is_network_fail(e) else f"报错 {getattr(e, 'code', '?')}"
                print(f"[brain] Gemini {why},切换到 OPENAI 通道", flush=True)
                _emit("status", text="正在切换到备用通道…")
                reply = ask_openai(req.messages, req.lang)
                _mark_gemini_down(e)      # 备用通道真顶上了,才敢记冷却
                return reply
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
    # 网络超时:平台没回话。**要和"钥匙不对"分开说** ——
    # 说成钥匙问题会让人跑去反复检查配置,而真正的原因是网络到不了。
    if _is_network_fail(e):
        return 504, ("AI 服务没有回应(网络超时)。可能是这台机器出不去 Google,"
                     "或对方暂时不可用。已自动尝试备用通道;老是这样就把 .env 里的 "
                     "BRAIN 改成 openai,直接走 ofox 通道。")

    if isinstance(e, _NoBrainKey):
        return 500, ("还没配置 AI 大脑的钥匙:打开 .env,填 GEMINI_API_KEY(免费)/ "
                     "OPENAI_API_KEY / ANTHROPIC_API_KEY 任意一把,填完直接重发消息即可。")

    # ===== Gemini =====
    if isinstance(e, genai_errors.APIError):
        # 400 和 401/403 是两回事,**不能混为一谈**:
        # 401/403 才是钥匙问题;400 INVALID_ARGUMENT 是**我们自己发的请求不合法**
        # (最常见的是工具 schema 写错,比如数组参数没标 items)。
        # 混着报成"钥匙无效",会让人跑去反复检查钥匙,而真正的 bug 在代码里。
        if e.code in (401, 403):
            return 500, "Gemini 钥匙无效或没权限(检查 .env 里的 GEMINI_API_KEY 是否粘贴完整)"
        if e.code == 400:
            return 500, (f"发给 Gemini 的请求不合法(400),这多半是代码里的工具定义有问题,不是你的钥匙问题。"
                         f"平台原话:{str(e)[:200]}")
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
    if isinstance(e, openai.APIStatusError) and e.status_code == 402:
        detail = ""
        try:
            detail = (e.response.json().get("error", {}) or {}).get("message", "")
        except Exception:
            pass
        return 402, ("ofox 通道余额不足,需要充值才能继续(这不是钥匙问题,别去改配置)。"
                     + (f"平台原话:{detail[:160]}" if detail else ""))
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


def _new_turn(mode: str = "campaign") -> None:
    """每条用户消息开始时要做的三件事(两个接口共用)。

    **必须在起线程 / copy_context() 之前调**,否则设的上下文带不进线程。
    """
    global _REQUEST_SEQ
    with _SEQ_LOCK:                 # 几段对话可能同时发消息,发号要串行
        _REQUEST_SEQ += 1
        seq = _REQUEST_SEQ
    CURRENT_SEQ.set(seq)            # 保险丝:区分"登记"和"确认"是不是同一条消息
    CURRENT_EXECUTED.set([])        # 本轮"真实执行台账",一轮一份
    CURRENT_GUARD.set(LoopGuard())  # 本轮的刹车片(轮数 / 重复调用 / 花费),一轮一份
    # **认不出的模式不回落成 campaign。** 回落等于 fail-open:全部 36 个工具 +
    # 能确认任何类型的待办。原样留着(截短防日志被撑爆),让下游的表查不到 →
    # 自动落到最严的一档,并在日志里喊一声,好让"加了工作室忘了登记"当场暴露。
    mode = str(mode or "campaign")[:32]
    if mode not in VALID_MODES:
        print(f"[warn] 认不出的工作室模式 {mode!r} —— 已按最严权限处理。"
              f"新增工作室要同时登记进 STUDIO_TOOL_SETS / STUDIO_ACTION_TYPES", flush=True)
    CURRENT_CHAT_MODE.set(mode)
    load_env_file()                 # 现读 .env:刚填的钥匙不用重启就生效


@app.post("/api/chat")
def chat(req: ChatRequest):
    """核心接口:收聊天记录 → 问 AI 大脑 → 一次性回答案。

    流式版本见 /api/chat/stream。这个保留着当兜底 —— 流式一旦被中间的
    反向代理缓冲住(nginx 默认会),前端可以退回来用这个。
    """
    _new_turn(req.mode)
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
    _new_turn(req.mode)

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
