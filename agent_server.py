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
import time
from pathlib import Path

import anthropic
import httpx
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
    "recommend_creatives": "正在从你的历史广告里挑好素材…",
    "search_stock_creatives": "正在从授权图库里找素材…",
    "search_competitor_ads": "正在查竞品正在投的广告…",
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
    """推荐素材:从**这个账户自己投过的广告**里,挑效果最好的几个素材给用户复用。

    建新广告第3步(素材)时,用户说"没有素材"/"帮我推荐"就调它。

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
_SEARCHED_ASSETS: dict[str, dict] = {}


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
        for item in r["results"]:
            _SEARCHED_ASSETS[item["image_url"]] = item
        # 服务是长驻进程,搜的次数多了这张表会一直长。留最近 200 条够用了
        # (用户总是从"刚搜出来的"那批里选,不会回头挑几百次之前的)。
        while len(_SEARCHED_ASSETS) > 200:
            _SEARCHED_ASSETS.pop(next(iter(_SEARCHED_ASSETS)))

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

    for item in r["results"]:
        _SEARCHED_ASSETS[item["image_url"]] = item
    while len(_SEARCHED_ASSETS) > 200:
        _SEARCHED_ASSETS.pop(next(iter(_SEARCHED_ASSETS)))

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


# ===== 创意拆解与方案(P0:只出文字,不出图)=====
# 拆解过的素材模型,按 image_url 存着。summarize 那步要一次看多条,
# 靠 AI 把几百行 JSON 在工具参数里传来传去不现实,也容易被截断 ——
# 和 _SEARCHED_ASSETS 一个路数,存在这边,只传编号。
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
    info = _SEARCHED_ASSETS.get((image_url or "").strip())
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
        "type": "make_creatives", "seq": _seq(),
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
    PENDING_ACTIONS[aid] = action
    _save_actions()
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
    info = _SEARCHED_ASSETS.get((image_url or "").strip())
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
        "name": name, "targets": targets, "seq": _seq(),
    }
    dup = _find_duplicate(candidate)
    if dup:
        return {"action_id": dup, "note": f"这件事此前已登记过(编号 {dup}),无需重复登记。"
                                          f"请向用户复述内容并附上编号,用户同意后直接调 confirm_action。"}
    action_id = uuid.uuid4().hex[:8]
    PENDING_ACTIONS[action_id] = candidate
    _save_actions()
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


def _child_names(type_word: str, ymd: str) -> tuple[str, str]:
    """新建计划底下的第一个广告组和第一条广告的名字。

    我们一次只建一组一条,所以序号固定是 001 —— 计划是全新的,底下不可能已有别的。
    广告多带一个 `AD-` 前缀,好和同序号的广告组区分开。
    """
    return f"{ymd}-{type_word}-001", f"AD-{ymd}-{type_word}-001"


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

    必填:ad_account_id(广告账户id)、keyword(**落地页类型**,英文一个词,如 roof / gutter / window;用于按规范命名)、
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
    set_name, ad_name = _child_names(tw, ymd)

    candidate = {
        "type": "create_campaign",
        "ad_account_id": ad_account_id,
        "campaign_name": c_name,
        "ad_set_name": set_name,
        "ad_name": ad_name,
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
        "seq": _seq(),
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
        elif action.get("type") == "make_creatives":
            result = _execute_make_creatives(action)
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
        return {"executed": {k: v for k, v in action.items() if k != "seq"}, **(result if isinstance(result, dict) else {"detail": result})}
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
    PENDING_ACTIONS[action_id] = candidate
    _save_actions()
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
    recommend_creatives, search_stock_creatives, search_competitor_ads, my_ad_categories, platform_kind, native_market_scan,
    decompose_creative, summarize_creative_patterns,
    use_found_creative, propose_make_creatives, get_delivery_tree,
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
# 素材地址 → 类型。和 _SEARCHED_ASSETS / 拆解结果一样要有上限,
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

# openai / anthropic 这两个 SDK 的**默认超时是 600 秒、还自动重试 2 次** ——
# 最坏情况一个请求能挂 30 分钟。而前端 180 秒就 abort 了:用户早看到
# 「等太久了」,后端还在那儿跑(中转商那边可能照样计费)。
# 所以显式设一个**小于前端 180 秒**的值,让后端先失败、把真实原因说出来。
BRAIN_TIMEOUT_S = float(os.environ.get("BRAIN_TIMEOUT_S", "75"))
BRAIN_RETRIES = int(os.environ.get("BRAIN_RETRIES", "1"))

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
        tools=NEWSBREAK_TOOLS,
        # 关掉自动工具调用:我们自己执行、自己把结果贴回去。
        # 这样才能在流式下正常工作,也才能播报每一步在干什么。
        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
    )
    streaming = CURRENT_EMIT.get() is not None
    contents = list(contents)
    retried_empty = False        # 空回复只原地重试一次

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
              "campaign_name": {"type": "string", "description": "手动指定计划名(可选)"}},
             ["ad_account_id", "keyword", "landing_url", "headline", "description", "asset_url"]),
    _oa_tool("confirm_action", "执行之前登记的待办(仅在用户新消息中明确同意后)", {"action_id": {"type": "string"}}, ["action_id"]),
    _oa_tool("cancel_action", "取消之前登记的待办", {"action_id": {"type": "string"}}, ["action_id"]),
    _oa_tool("list_pending_actions", "查看保险箱里所有已登记待确认的待办(含编号);忘了编号用它查,严禁重复登记", {}, []),
    _oa_tool("get_delivery_tree",
             "看一条广告计划底下有哪些广告组和广告、各自开还是关。**开启广告前必须先调它**:"
             "三层全 ON 才会真的投放,只开 campaign 等于没开",
             {"campaign_id": {"type": "string", "description": "广告计划 id"},
              "ad_account_id": _ID}, ["campaign_id"]),
    _oa_tool("recommend_creatives",
             "推荐素材:从这个账户投过的广告里挑效果最好的几个素材给用户复用(带真实数据,版权干净)。"
             "建新广告第3步用户说没素材/要推荐时调它",
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
    decompose_creative, summarize_creative_patterns,
    use_found_creative, propose_make_creatives, get_delivery_tree,
    list_organizations, list_ad_accounts, list_campaigns, list_ad_sets, list_ads,
    get_report, propose_status_change, propose_create_campaign, confirm_action, cancel_action,
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
    for _ in range(10):  # 工具调用循环:一轮没答完就继续,设个上限防转圈
        content, tool_calls, finish, ok_model = "", [], "", None
        for model in models_to_try:
            try:
                content, tool_calls, finish = _openai_once(client, model, msgs, streaming)
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


def _openai_once(client, model: str, msgs: list, streaming: bool) -> tuple[str, list, str]:
    """问一轮 OpenAI/ofox,返回 (文字, 工具调用清单, finish_reason)。

    流式时边收边把文字播报出去;工具调用是分片来的(名字和参数会拆成好几块),
    要按 index 拼起来才完整。

    **`finish_reason` 一定要带回去**:模型偶尔会一个字都不吐、也不调工具,
    上游只有这个字段能说明为什么(截断?被安全策略拦了?)。原来完全没读它,
    于是那种情况只能回一句"没有返回文字",用户和日志都拿不到任何线索。
    """
    if not streaming:
        r = client.chat.completions.create(model=model, messages=msgs, tools=OPENAI_TOOL_SCHEMAS)
        m = r.choices[0].message
        return (m.content or "",
                [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or ""}
                 for tc in (m.tool_calls or [])],
                r.choices[0].finish_reason or "")

    parts, slots, finish = [], {}, ""
    for chunk in client.chat.completions.create(
            model=model, messages=msgs, tools=OPENAI_TOOL_SCHEMAS, stream=True):
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
    return "".join(parts), [slots[i] for i in sorted(slots)], finish


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
        client = openai.OpenAI(timeout=BRAIN_TIMEOUT_S, max_retries=BRAIN_RETRIES)
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


def _new_turn() -> None:
    """每条用户消息开始时要做的三件事(两个接口共用)。

    **必须在起线程 / copy_context() 之前调**,否则设的上下文带不进线程。
    """
    global _REQUEST_SEQ
    with _SEQ_LOCK:                 # 几段对话可能同时发消息,发号要串行
        _REQUEST_SEQ += 1
        seq = _REQUEST_SEQ
    CURRENT_SEQ.set(seq)            # 保险丝:区分"登记"和"确认"是不是同一条消息
    CURRENT_EXECUTED.set([])        # 本轮"真实执行台账",一轮一份
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
