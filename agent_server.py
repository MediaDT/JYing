"""
广告投放小助手 —— 聊天后端(你的 agent 的"大脑中转站")。

它做两件事:
  1. 把聊天页面(static/index.html)端出来给浏览器;
  2. 提供 /api/chat 接口:收到聊天记录 → 转发给 Claude → 把回答传回去。

为什么要有这个后端?因为调 Claude 需要 API 钥匙,钥匙绝不能放进网页里,
所以网页只跟这个后端说话,钥匙留在服务器这边。

运行方法(在 my-agent 目录下):
    ./venv/bin/uvicorn agent_server:app --host 0.0.0.0 --port 8100
"""

import json
import os
from pathlib import Path

import anthropic
import openai
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
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


class PasswordMiddleware(BaseHTTPMiddleware):
    """给整个站点加一道密码门。没设密码就直接放行。"""

    async def dispatch(self, request: Request, call_next):
        # 每次请求都现读 .env:改密码立刻生效,不用重启
        # (不能只靠启动时读——原本为空的键不会进环境变量,后来填了也读不到)
        password = _read_env_value("APP_PASSWORD")
        if not password:
            return await call_next(request)      # 没设密码 = 不启用

        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                _, _, supplied = decoded.partition(":")
                # compare_digest:防止通过"猜多久报错"来试密码
                if secrets.compare_digest(supplied, password):
                    return await call_next(request)
            except Exception:
                pass

        return JSONResponse(
            status_code=401,
            content={"error": "需要密码才能访问"},
            # 注意:HTTP 头只能是 latin-1,不能写中文,否则会 500
            headers={"WWW-Authenticate": 'Basic realm="AdBot"'},
        )


app.add_middleware(PasswordMiddleware)

# 把 static/ 目录挂出来:页面里就能引用 /static/marked.min.js 这类文件
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")

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

「建计划向导」:用户想投新广告时,严格按 6 步逐项收集(一步一问 + 报进度「第N步/共6步」):
第1步 落地页:要推广的链接(解释:用户点广告后打开的网页);
第2步 预算:先问日预算还是总预算(推荐日预算),再问金额(最低$10,新手建议$10~$50);
第3步 转化事件:用 list_conversion_events 列出账户里的事件让用户挑一个
   (解释:平台靠它统计"广告带来了多少成果";用户不懂就推荐 submit form 或第一个);
   出价不用问:系统用平台自动出价(MAX_CONVERSION),平台会自动优化;
第4步 素材:请用户点击输入框左侧的 📎 按钮上传图片或视频;上传成功后会自动出现一条带 assetUrl 的消息,记住其中的 assetUrl 和文件名;
第5步 文案:标题和描述;用户嫌麻烦可以由你根据落地页主题代拟再请他过目;品牌名和按钮文案给默认值;
第6步 命名:请用户给一个英文关键词(如 gutter),名字自动生成为「关键词-月日」;用户想手动指定也行。
收集齐后调 propose_create_campaign 登记,把返回的整单内容用表格完整复述,等用户下一条消息确认后再 confirm_action。
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
6) 调预算等其他写操作还没接上,涉及时说明需去后台手动操作。"""


# ============ 把 NewsBreak 的能力包装成 Gemini 能用的"工具" ============
# 规矩:函数名、参数类型、docstring 会被 Gemini 读懂,它自己决定何时调用;
#       出错时返回 {"error": ...} 而不是抛异常,让大脑能把原因转告用户。

import newsbreak_client as nb  # noqa: E402


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


def propose_create_campaign(
    ad_account_id: str,
    keyword: str,
    landing_url: str,
    budget_dollars: float,
    headline: str,
    description: str,
    asset_url: str,
    asset_filename: str = "",
    tracking_id: str = "",
    budget_type: str = "DAILY",
    brand_name: str = "",
    call_to_action: str = "Learn More",
    campaign_name: str = "",
) -> dict:
    """登记一个「新建广告」待办(不会立即执行!),会一次建好 campaign+ad set+ad 三层。

    必填:ad_account_id(广告账户id)、keyword(英文命名关键词,如 gutter)、
    landing_url(落地页链接)、budget_dollars(预算,美元,最低10)、
    headline(标题)、description(描述)、asset_url(素材地址,来自用户上传后的系统消息)。
    出价无需提供:系统使用平台自动出价(MAX_CONVERSION)。
    tracking_id(转化事件id)可选:不填则自动选用账户里的 submit form 或第一个事件。
    可选:asset_filename(素材文件名,用于判断图片/视频)、budget_type(DAILY日预算/TOTAL总预算)、
    brand_name(品牌名,默认用关键词)、call_to_action(按钮文案)、campaign_name(手动指定计划名)。
    登记后必须用表格向用户完整复述整单,等用户下一条消息确认后再 confirm_action。
    """
    # ---- 参数体检:把明显的问题挡在登记之前 ----
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
            "落地页": landing_url, budget_word: f"${budget_dollars:g}", "出价": "自动(MAX_CONVERSION)",
            "转化事件": tracking_id or "自动选用(优先 submit form)",
            "标题": headline, "描述": description, "品牌名": a["brand_name"],
            "按钮": a["call_to_action"], "素材": asset_filename or asset_url,
            "创建后状态": "暂停(OFF),需用户确认无误后再开启",
        },
        "note": "已登记待办,尚未执行。请用表格向用户完整复述以上内容并等确认。",
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
        if action.get("type") == "create_campaign":
            result = _execute_create_campaign(action)
        else:
            result = {"done": True, "detail": nb.update_status(action["level"], action["object_id"], action["status"])}
        print(f"[write-op] 待办 {action_id} 结果: {json.dumps(result, ensure_ascii=False)[:300]}", flush=True)

        # 把"真实发生了什么"记入代码层台账(聊天回复会盖'系统核验'钢印)
        if result.get("done"):
            detail = json.dumps(result.get("created") or result.get("detail") or "", ensure_ascii=False)[:220]
            _EXECUTED_THIS_REQUEST.append(f"待办 {action_id} 执行成功 → {detail}")
            PENDING_ACTIONS.pop(action_id, None)
            _save_actions()
        else:
            _EXECUTED_THIS_REQUEST.append(f"待办 {action_id} 执行失败 → {str(result.get('error'))[:220]}")
        return {"executed": {k: v for k, v in action.items() if k != "seq"}, **(result if isinstance(result, dict) else {"detail": result})}
    except Exception as e:
        print(f"[write-op] 待办 {action_id} 异常: {e}", flush=True)
        _EXECUTED_THIS_REQUEST.append(f"待办 {action_id} 执行失败 → {str(e)[:220]}")
        return {"error": str(e)}


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
]


class ChatMessage(BaseModel):
    role: str      # "user"(用户说的) 或 "assistant"(助手说的)
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]  # 完整的聊天记录(API 不记事,每次都要全量发)


@app.get("/")
def index():
    """把聊天页面端给浏览器。"""
    return FileResponse(Path(__file__).with_name("static") / "index.html")


# ============ 素材上传中转:浏览器 → 这里 → NewsBreak ============
from fastapi import File, UploadFile  # noqa: E402

_CACHED_ACCOUNT_ID = ""
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

    只有一个账户时直接用(绝大多数情况);**有多个时明确报错**,而不是默默挑第一个——
    静默挑错账户会把素材传进别人的账户,属于"不报错但结果全错"的坑。
    """
    global _CACHED_ACCOUNT_ID
    if _CACHED_ACCOUNT_ID:
        return _CACHED_ACCOUNT_ID
    accounts = _all_ad_accounts()
    if not accounts:
        raise nb.NewsBreakError("名下没有任何广告账户")
    if len(accounts) > 1:
        listed = "、".join(f"{a['name']}(id={a['id']})" for a in accounts)
        raise nb.NewsBreakError(f"你名下有 {len(accounts)} 个广告账户,请指定用哪个:{listed}")
    _CACHED_ACCOUNT_ID = accounts[0]["id"]
    return _CACHED_ACCOUNT_ID


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


def _system_prompt_now() -> str:
    """人设 + 今天的真实日期 + 保险箱现状(AI 跨轮会忘记待办编号,直接喂给它)。"""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = SYSTEM_PROMPT + f"\n\n今天的日期(UTC)是 {today},计算\"最近N天\"等日期范围时以此为准。"
    if PENDING_ACTIONS:
        lines = []
        for aid, a in PENDING_ACTIONS.items():
            if a.get("type") == "create_campaign":
                lines.append(f"- 编号 {aid}:新建广告「{a.get('campaign_name')}」(已登记,待执行)")
            else:
                lines.append(f"- 编号 {aid}:{a.get('status')} {a.get('level')}「{a.get('name') or a.get('object_id')}」(已登记,待执行)")
        prompt += ("\n\n【保险箱现状】以下待办已登记完毕,严禁重新登记:\n" + "\n".join(lines) +
                   "\n用户已确认/同意时,直接调 confirm_action(用上面的编号)执行,不要再要求确认。")
    return prompt


def ask_gemini(messages: list[ChatMessage]) -> str:
    """大脑 A:Gemini。钥匙从环境变量 GEMINI_API_KEY 自动读取。"""
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
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_system_prompt_now(),
                    # 把工具递给它:Gemini 会"自动工具调用"——自己挑工具、自己执行、
                    # 拿到结果接着想,循环到能回答为止,最后只把人话答案给我们
                    tools=NEWSBREAK_TOOLS,
                ),
            )
            return response.text or "(Gemini 没有返回文字)"
        except genai_errors.APIError as e:
            if e.code == 429:   # 这个型号的额度用完了,换备胎接着试
                last_error = e
                continue
            raise
    raise last_error


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
              "budget_dollars": {"type": "number", "description": "预算(美元),最低10"},
              "tracking_id": {"type": "string", "description": "转化事件id(可选,不填自动选)"},
              "headline": {"type": "string", "description": "广告标题"},
              "description": {"type": "string", "description": "广告描述"},
              "asset_url": {"type": "string", "description": "素材地址(用户上传后系统消息里的 assetUrl)"},
              "asset_filename": {"type": "string", "description": "素材文件名(用于判断图片/视频)"},
              "budget_type": {"type": "string", "enum": ["DAILY", "TOTAL"], "description": "DAILY=日预算 TOTAL=总预算"},
              "brand_name": {"type": "string", "description": "品牌名,默认用关键词"},
              "call_to_action": {"type": "string", "description": "按钮文案,默认 Learn More"},
              "campaign_name": {"type": "string", "description": "手动指定计划名(可选)"}},
             ["ad_account_id", "keyword", "landing_url", "budget_dollars", "headline", "description", "asset_url"]),
    _oa_tool("confirm_action", "执行之前登记的待办(仅在用户新消息中明确同意后)", {"action_id": {"type": "string"}}, ["action_id"]),
    _oa_tool("cancel_action", "取消之前登记的待办", {"action_id": {"type": "string"}}, ["action_id"]),
    _oa_tool("list_pending_actions", "查看保险箱里所有已登记待确认的待办(含编号);忘了编号用它查,严禁重复登记", {}, []),
    _oa_tool("list_conversion_events", "查询账户下的转化事件列表(建新广告第3步让用户挑)", {"ad_account_id": _ID}, ["ad_account_id"]),
]

# 工具名 → 真实函数 的对照表(ChatGPT 说要调哪个,我们就去执行哪个)
OPENAI_TOOL_FUNCS = {fn.__name__: fn for fn in [
    list_organizations, list_ad_accounts, list_campaigns, list_ad_sets, list_ads,
    get_report, propose_status_change, propose_create_campaign, confirm_action, cancel_action,
    list_pending_actions, list_conversion_events,
]}


def ask_openai(messages: list[ChatMessage]) -> str:
    """大脑 B:ChatGPT。钥匙从 OPENAI_API_KEY 读取(转发服务再加 OPENAI_BASE_URL)。"""
    client = openai.OpenAI()
    msgs = [{"role": "system", "content": _system_prompt_now()}] + [
        {"role": m.role, "content": m.content} for m in messages
    ]
    # 型号候补:前面的不可用(404)就换下一个。
    # 用中转站时可在 .env 里用 OPENAI_MODEL 指定它家支持的型号,优先尝试
    models_to_try = ["gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"]
    custom_model = os.environ.get("OPENAI_MODEL", "").strip()
    if custom_model:
        models_to_try = [custom_model] + [m for m in models_to_try if m != custom_model]

    for _ in range(10):  # 工具调用循环:一轮没答完就继续,设个上限防转圈
        response = None
        for model in models_to_try:
            try:
                response = client.chat.completions.create(
                    model=model, messages=msgs, tools=OPENAI_TOOL_SCHEMAS)
                models_to_try = [model]  # 记住能用的型号,后面几轮不再试错
                break
            except openai.NotFoundError:
                continue
        if response is None:
            raise openai.NotFoundError.__new__(openai.NotFoundError)  # 型号全不可用

        msg = response.choices[0].message
        if not msg.tool_calls:
            return msg.content or "(ChatGPT 没有返回文字)"

        # 它想用工具:替它执行,把结果贴回对话,再让它接着想
        msgs.append({"role": "assistant", "content": msg.content,
                     "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            fn = OPENAI_TOOL_FUNCS.get(tc.function.name)
            try:
                args = json.loads(tc.function.arguments or "{}")
                result = fn(**args) if fn else {"error": f"未知工具 {tc.function.name}"}
            except Exception as e:
                result = {"error": str(e)}
            msgs.append({"role": "tool", "tool_call_id": tc.id,
                         "content": json.dumps(result, ensure_ascii=False)})
    return "(工具调用轮数过多,已中止,请换个问法)"


def ask_claude(messages: list[ChatMessage]) -> str:
    """大脑 B:Claude。钥匙从环境变量 ANTHROPIC_API_KEY 自动读取。"""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-8",           # 当前推荐的主力模型
        max_tokens=16000,                  # 单次回答的长度上限
        thinking={"type": "adaptive"},     # 自适应思考:难题多想想,简单题直接答
        system=_system_prompt_now(),
        messages=[m.model_dump() for m in messages],
    )
    # 回答里可能既有"思考块"又有"文字块",只取文字部分
    return "".join(block.text for block in response.content if block.type == "text")


# AI 回复里出现这些词、却没有真实执行记录且保险箱还有待办 → 大概率在"谎报军情"
_CLAIM_KEYWORDS = ["成功创建", "已创建", "创建并提交", "已提交", "已执行", "已开启", "已暂停", "已为您暂停", "已为您开启"]


def _finalize(reply: str) -> dict:
    """给回复盖"系统核验"钢印:真执行了什么、有没有谎报,以代码层记录为准。"""
    if _EXECUTED_THIS_REQUEST:
        stamp = ";".join(_EXECUTED_THIS_REQUEST)
        return {"reply": f"{reply}\n\n---\n🔒 **系统核验**(代码层记录,非 AI 生成):{stamp}"}
    if PENDING_ACTIONS and any(kw in reply for kw in _CLAIM_KEYWORDS):
        ids = "、".join(PENDING_ACTIONS.keys())
        return {"reply": f"{reply}\n\n---\n⚠️ **系统核验**:本轮实际上没有执行任何操作,保险箱里仍有待办({ids})。"
                         f"如果上面说\"已创建/已执行\",那是 AI 的幻觉,请回复「执行待办 {ids}」重试。"}
    return {"reply": reply}


@app.post("/api/chat")
def chat(req: ChatRequest):
    """核心接口:收聊天记录 → 问 AI 大脑 → 回答案。填了哪家的钥匙就用哪家。"""
    # 保险丝计数:用户每发一条消息 +1,用来区分"登记"和"确认"是不是同一条消息
    global _REQUEST_SEQ
    _REQUEST_SEQ += 1
    _EXECUTED_THIS_REQUEST.clear()  # 本轮"真实执行台账"清零

    # 每次都现读 .env:这样刚填好钥匙不用重启服务器,发条消息就生效
    load_env_file()

    # BRAIN 可在 .env 里指定主力大脑:gemini / openai(含 ofox 中转) / claude;
    # 不填或填 auto = 走默认三级火箭(Gemini 免费优先,额度尽了自动接力)
    brain = os.environ.get("BRAIN", "auto").strip().lower()

    try:
        if brain == "openai" and os.environ.get("OPENAI_API_KEY"):
            return _finalize(ask_openai(req.messages))
        if brain == "claude" and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            return _finalize(ask_claude(req.messages))

        # 三级火箭:Gemini(免费)→ 额度尽了切 ChatGPT/ofox → 都没有再看 Claude
        if os.environ.get("GEMINI_API_KEY"):
            try:
                return _finalize(ask_gemini(req.messages))
            except genai_errors.APIError as e:
                if e.code == 429 and os.environ.get("OPENAI_API_KEY"):
                    return _finalize(ask_openai(req.messages))  # Gemini 额度尽,ChatGPT 顶上
                raise
        if os.environ.get("OPENAI_API_KEY"):
            return _finalize(ask_openai(req.messages))
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return _finalize(ask_claude(req.messages))
        return JSONResponse(
            status_code=500,
            content={"error": "还没配置 AI 大脑的钥匙:打开 .env,填 GEMINI_API_KEY(免费)/ OPENAI_API_KEY / ANTHROPIC_API_KEY 任意一把,填完直接重发消息即可。"},
        )

    # ===== Gemini 的错误翻译 =====
    except genai_errors.APIError as e:
        if e.code in (400, 401, 403):
            return JSONResponse(status_code=500, content={"error": "Gemini 钥匙无效(检查 .env 里的 GEMINI_API_KEY 是否粘贴完整)"})
        if e.code == 429:
            return JSONResponse(status_code=429, content={"error": "Gemini 免费额度暂时用完/太频繁,稍等一分钟再试"})
        return JSONResponse(status_code=502, content={"error": f"Gemini 服务返回错误({e.code}),稍后再试"})

    # ===== ChatGPT 的错误翻译 =====
    except openai.AuthenticationError:
        return JSONResponse(status_code=500, content={"error": "ChatGPT 钥匙无效(检查 .env 里的 OPENAI_API_KEY)"})
    except openai.RateLimitError:
        return JSONResponse(status_code=429, content={"error": "ChatGPT 也限流了(免费额度/余额可能不足),稍后再试"})
    except openai.NotFoundError:
        return JSONResponse(status_code=502, content={"error": "ChatGPT 候选型号都不可用(key 可能没开通这些模型)"})
    except openai.APIStatusError as e:
        return JSONResponse(status_code=502, content={"error": f"ChatGPT 服务返回错误({e.status_code}),稍后再试"})
    except openai.APIConnectionError:
        return JSONResponse(status_code=502, content={"error": "连不上 ChatGPT 服务(如用转发服务,检查 OPENAI_BASE_URL)"})

    # ===== Claude 的错误翻译(从具体到笼统依次接住)=====
    except anthropic.AuthenticationError:
        return JSONResponse(status_code=500, content={"error": "Claude 钥匙无效(检查 .env 里的 ANTHROPIC_API_KEY)"})
    except anthropic.RateLimitError:
        return JSONResponse(status_code=429, content={"error": "请求太频繁,被限流了,稍等一会儿再试"})
    except anthropic.APIStatusError as e:
        return JSONResponse(status_code=502, content={"error": f"Claude 服务返回错误({e.status_code}),稍后再试"})
    except anthropic.APIConnectionError:
        return JSONResponse(status_code=502, content={"error": "连不上 AI 服务,请检查网络"})
