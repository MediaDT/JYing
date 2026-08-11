"""
NewsBreak 只读客户端 —— agent 的"手",目前只会"看",不会"动"。

写法照抄 qx-ad-bot 的生产代码规矩:
  - 窗口地址: https://business.newsbreak.com/business-api/v1
  - 钥匙放请求头: Access-Token
  - 回话里 code == 0 才算成功,数据在 data 里
  - 列表接口参数: adAccountId + pageNo + pageSize (+ search)

这里全部是"查询"类操作(GET),不会修改 NewsBreak 上的任何东西,放心用。
"""

import contextvars
import os
from pathlib import Path

import httpx

NEWSBREAK_API_BASE = "https://business.newsbreak.com/business-api/v1"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class NewsBreakError(Exception):
    """NewsBreak 返回了错误,message 里是人能看懂的原因。"""


# ===== 「当前是谁在用」的上下文 =====
# 每个登录用户绑自己的 NewsBreak token。但工具函数散落在很深的调用链里,
# 一层层传参数不现实,所以用 contextvars:在每个请求的入口设一次,
# 这条调用链上的所有代码(包括 FastAPI 丢进线程池的同步函数)都能读到。
#
# 取值约定:
#   None  = 不在"某个登录用户"的上下文里(定时任务线程 / 命令行脚本 / 测试)→ 回落到 .env
#   dict  = 在用户上下文里,就**只认**这个人的凭据;他没绑就报错,
#           绝不偷偷回落到 .env —— 否则 A 绑过之后 B 进来会直接用上 A 的账户,
#           那就等于没隔离。
CURRENT_CREDS: contextvars.ContextVar = contextvars.ContextVar("nb_current_creds", default=None)

NOT_BOUND_MSG = "你还没绑定 NewsBreak 账号 —— 点顶栏的 🔗 按钮,粘贴你自己的 Access Token 就能用了"


def current_account_id() -> str:
    """当前用户选定的广告账户 id(没在用户上下文里就返回空)。"""
    cur = CURRENT_CREDS.get()
    return (cur or {}).get("account_id", "") or ""


def _token() -> str:
    """拿 NewsBreak 钥匙:先看当前用户绑的,不在用户上下文才回落到 .env。"""
    cur = CURRENT_CREDS.get()
    if cur is not None:
        tok = (cur.get("token") or "").strip()
        if not tok:
            raise NewsBreakError(NOT_BOUND_MSG)
        return tok

    token = os.environ.get("NEWSBREAK_ACCESS_TOKEN", "").strip()
    if not token:
        env_path = Path(__file__).with_name(".env")
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("NEWSBREAK_ACCESS_TOKEN=") and "=" in line:
                    token = line.split("=", 1)[1].strip()
    if not token:
        raise NewsBreakError("没配置 NEWSBREAK_ACCESS_TOKEN(去 .env 里填)")
    return token


def _parse_or_raise(response: httpx.Response) -> dict:
    """统一解读平台回话:成功返回 data;失败优先把平台写的原因(errMsg)翻出来,
    而不是甩一句 'HTTP 409' 这种人看不懂的代码。"""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict) and payload.get("code") == 0:
        return payload.get("data") or {}
    if isinstance(payload, dict):
        reason = payload.get("errMsg") or payload.get("message")
        if reason:
            raise NewsBreakError(f"{reason}(HTTP {response.status_code})")
        raise NewsBreakError(f"平台返回异常(HTTP {response.status_code}):{str(payload)[:200]}")
    raise NewsBreakError(f"平台返回了无法解析的内容(HTTP {response.status_code})")


def _get_json(path: str, params: list[tuple[str, str]] | None = None) -> dict:
    """带钥匙发一次 GET 请求,做完所有例行检查,返回 data 部分。"""
    headers = {"Access-Token": _token(), "Content-Type": "application/json"}
    with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
        response = client.get(f"{NEWSBREAK_API_BASE}{path}", headers=headers, params=params)
    return _parse_or_raise(response)


# ============ 下面是给 agent 用的"工具动作" ============

def list_organizations() -> list[dict]:
    """查询当前钥匙名下的所有组织(organization)。"""
    return list(_get_json("/org/admin-orgs").get("list") or [])


def list_ad_accounts(org_id: str) -> list[dict]:
    """查询某个组织下的所有广告账户(ad account),返回**拍平后的账户列表**。

    ⚠️ 这里有个坑:接口返回的是"组织分组"的套娃结构 ——
        [{id: 分组id, name: 分组名, adAccounts: [{id: 真账户id, name: ...}]}]
    外层 id 是分组、不是账户,而且两者名字常常一模一样,肉眼和 AI 都分不出来。
    所以这里统一拍平,只吐真账户,避免把分组 id 当账户 id 用(会导致查不到数据)。
    """
    groups = _get_json("/ad-account/getGroupsByOrgIds", params=[("orgIds", org_id)]).get("list") or []
    accounts = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for acc in group.get("adAccounts") or []:
            if not isinstance(acc, dict):
                continue
            accounts.append({
                "id": str(acc.get("id") or ""),          # 真正的广告账户 id
                "name": acc.get("name") or "",
                "group_id": str(group.get("id") or ""),  # 外层分组 id,仅供参考
                "group_name": group.get("name") or "",
            })
    return accounts


def _list_page(path: str, ad_account_id: str, page: int, limit: int, search: str) -> dict:
    """通用的"翻页查列表":campaign / ad-set / ad 三层共用这一套参数。"""
    params = [
        ("adAccountId", ad_account_id),
        ("pageNo", str(page)),
        ("pageSize", str(limit)),
    ]
    if search:
        params.append(("search", search))
    data = _get_json(path, params=params)
    items = list(data.get("rows") or data.get("list") or [])
    return {
        "page": int(data.get("pageNo") or page),
        "total": int(data.get("total") or 0),
        "has_next": bool(data.get("hasNext")),
        "items": items,
    }


def list_campaigns(ad_account_id: str, page: int = 1, limit: int = 20, search: str = "") -> dict:
    """查询某个广告账户下的广告计划(campaign)列表,支持翻页和按名字搜索。"""
    return _list_page("/campaign/getList", ad_account_id, page, limit, search)


def list_ad_sets(ad_account_id: str, page: int = 1, limit: int = 20, search: str = "") -> dict:
    """查询某个广告账户下的广告组(ad set)列表。"""
    return _list_page("/ad-set/getList", ad_account_id, page, limit, search)


def list_ads(ad_account_id: str, page: int = 1, limit: int = 20, search: str = "") -> dict:
    """查询某个广告账户下的广告(ad)列表。"""
    return _list_page("/ad/getList", ad_account_id, page, limit, search)


# ============ 投放数据报表 & 写操作共用的请求器 ============

def _request_json(method: str, path: str, body: dict | None = None) -> dict:
    """带钥匙发一次任意方法的请求(POST/PUT 等),做完例行检查,返回 data 部分。"""
    headers = {"Access-Token": _token(), "Content-Type": "application/json"}
    with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
        response = client.request(method, f"{NEWSBREAK_API_BASE}{path}", headers=headers, json=body)
    return _parse_or_raise(response)


def _post_json(path: str, body: dict) -> dict:
    """带钥匙发一次 POST 请求(报表接口要用 POST)。"""
    return _request_json("POST", path, body)


# 报表可以按三个层级汇总;每层的「id 字段」和「名字字段」叫法不同(实测确认)
# 注意:平台不管你 metrics 里申请几个指标,都会把全部 32 个字段返回,所以指标是全的
_REPORT_LEVELS = {
    "campaign": ("CAMPAIGN", "campaignId", "campaign"),
    "ad_set": ("AD_SET", "adSetId", "adSet"),
    "ad": ("AD", "adId", "ad"),
}


def get_report(level: str = "campaign", start_date: str = "", end_date: str = "",
               ad_account_id: str = "") -> dict:
    """查询一段时间的投放数据(花费/展示/点击/转化等),按 campaign / ad_set / ad 汇总。

    日期格式 YYYY-MM-DD;不填则默认最近 7 天。
    注意换算(qx-ad-bot 踩过的坑):NewsBreak 返回的金额单位是"分"、
    百分比单位是"万分点"(188 = 1.88%),这里统一换算成美元和百分数字符串。
    """
    if level not in _REPORT_LEVELS:
        raise NewsBreakError(f"level 只能是 {list(_REPORT_LEVELS)} 之一,收到: {level}")
    dimension, id_key, name_key = _REPORT_LEVELS[level]

    from datetime import datetime, timedelta, timezone
    end = end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = start_date or (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    # 平台硬限制:报表时间跨度不能超过 180 天(实测,超了返回英文报错)。
    # 这里提前拦住并说人话,免得用户看到一句 "Report dates range can not exceed 180 days"。
    try:
        span = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
        if span < 0:
            raise NewsBreakError(f"开始日期({start})晚于结束日期({end}),请检查")
        if span > 180:
            raise NewsBreakError(
                f"查询跨度 {span} 天,超过平台上限 180 天。请缩短范围,或分几段来查")
    except ValueError:
        raise NewsBreakError(f"日期格式要是 YYYY-MM-DD,收到:{start} ~ {end}")

    body = {
        "name": f"my-agent-report-{dimension}",
        "dateRange": "FIXED",
        "startDate": start,
        "endDate": end,
        "dimensions": [dimension],
        "metrics": ["COST", "IMPRESSION", "CPM"],
    }
    # 指定账户时带上过滤(单账户可不填)。注意:平台接受这个字段但我们只有一个账户,
    # 无法验证多账户下是否真的过滤生效——多账户环境请先核对数字。
    if ad_account_id:
        body["adAccountId"] = ad_account_id
    data = _post_json("/reports/getIntegratedReport", body)

    def num(row: dict, keys: list[str], default: float = 0.0) -> float:
        for k in keys:
            v = row.get(k)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return default

    rows_out = []
    for row in data.get("rows") or []:
        if not isinstance(row, dict):
            continue
        object_id = row.get(id_key) or row.get("id")
        if object_id in (None, ""):
            continue
        cpa_cents = num(row, ["cpaDecimal", "cpa"], default=-1.0)
        roas = num(row, ["roas"], default=-1.0)          # 平台用 -1 表示"没有数据"
        revenue_cents = num(row, ["conversionValueDecimal", "conversionValue"], default=-1.0)
        rows_out.append({
            # 名字直接来自平台返回,不用再去列表接口对 id(以前丢掉了这个字段,
            # 害得 AI 要多查一次还容易对错号)
            "name": str(row.get(name_key) or ""),
            "id": str(object_id),
            "cost": f"${num(row, ['costDecimal', 'cost']) / 100:,.2f}",
            "revenue": "N/A" if revenue_cents < 0 else f"${revenue_cents / 100:,.2f}",
            "roas": "N/A" if roas < 0 else f"{roas:.2f}",
            "impressions": f"{int(num(row, ['impression', 'impressions'])):,}",
            "clicks": f"{int(num(row, ['click', 'clicks'])):,}",
            "conversions": f"{int(num(row, ['conversion', 'conversions'])):,}",
            "cpm": f"${num(row, ['cpmDecimal', 'cpm']) / 100:,.2f}",
            "cpc": f"${num(row, ['cpcDecimal', 'cpc']) / 100:,.2f}",
            "cpa": "N/A" if cpa_cents < 0 else f"${cpa_cents / 100:,.2f}",
            "ctr": f"{num(row, ['ctr']) / 100:g}%",
            "cvr": f"{num(row, ['cvr']) / 100:g}%",
        })
    return {"level": level, "start_date": start, "end_date": end, "rows": rows_out}


# ============ 写操作(危险动作,调用方必须先经过用户确认!) ============

# 三个层级对应的接口路径段(qx-ad-bot 验证过的规矩)
_OBJECT_PATHS = {"campaign": "campaign", "ad_set": "ad-set", "ad": "ad"}


def update_status(level: str, object_id: str, status: str) -> dict:
    """把某个 campaign / ad_set / ad 开启(ON)或暂停(OFF)。

    ⚠️ 这是写操作,会真实改动 NewsBreak 上的投放状态,
    调用它之前必须已经拿到用户的明确同意(护栏在 agent_server 那层)。
    """
    status = status.upper()
    if status not in ("ON", "OFF"):
        raise NewsBreakError(f"status 只能是 ON 或 OFF,收到: {status}")
    path_seg = _OBJECT_PATHS.get(level)
    if not path_seg:
        raise NewsBreakError(f"level 只能是 {list(_OBJECT_PATHS)} 之一,收到: {level}")
    return _request_json("PUT", f"/{path_seg}/updateStatus/{object_id}", {"status": status})


# ============ 建广告全家桶(写操作,均需上层护栏确认后才可调用) ============

def creative_type_of(filename: str, mime_type: str = "") -> str:
    """判断素材种类:VIDEO / GIF / IMAGE。

    **优先用浏览器给的 mime_type**(权威),文件名后缀只是兜底 ——
    因为粘贴的截图、没有后缀的文件都会让"猜后缀"失效,
    把视频当成图片提交给平台会被拒。
    """
    mime = (mime_type or "").lower()
    if mime.startswith("video/"):
        return "VIDEO"
    if mime == "image/gif":
        return "GIF"
    if mime.startswith("image/"):
        return "IMAGE"

    # 没有 mime 时退回看后缀
    lower = (filename or "").lower()
    if lower.endswith((".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv")):
        return "VIDEO"
    if lower.endswith(".gif"):
        return "GIF"
    return "IMAGE"


def upload_asset(ad_account_id: str, filename: str, content: bytes,
                 content_type: str = "", save_to_library: bool = True,
                 media_name: str = "") -> dict:
    """把图片/视频上传给 NewsBreak,换回素材地址(assetUrl)。multipart 格式。"""
    data = {
        "adAccountId": ad_account_id,
        "saveToMediaLibrary": "true" if save_to_library else "false",
    }
    if media_name:
        data["mediaName"] = media_name[:256]
    files = {"asset": (filename, content, content_type or "application/octet-stream")}
    # 注意:multipart 上传时不能手动设 Content-Type,让 httpx 自己生成边界串
    with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
        response = client.post(
            f"{NEWSBREAK_API_BASE}/ad/uploadAssets",
            headers={"Access-Token": _token()},
            data=data, files=files,
        )
    return _parse_or_raise(response)


def create_campaign(ad_account_id: str, name: str,
                    objective: str = "WEB_CONVERSION", status: str = "OFF") -> dict:
    """建第一层:广告计划(campaign)。"""
    return _request_json("POST", "/campaign/create", {
        "adAccountId": ad_account_id, "name": name,
        "objective": objective, "status": status,
    })


def list_events(ad_account_id: str) -> list[dict]:
    """查询账户下的转化事件(conversion events)。建"网页转化"广告组必须挂一个,
    否则平台报 'illegal event tracking'(实测踩过的坑)。"""
    return list(_get_json(f"/event/getList/{ad_account_id}").get("list") or [])


def create_ad_set(campaign_id: str, name: str, budget_type: str, budget_cents: int,
                  tracking_id: str, status: str = "OFF",
                  start_time: int = 0, end_time: int = 0) -> dict:
    """建第二层:广告组(ad set)。配方照抄 qx-ad-bot 线上向导(实测可用版):
    必须带 trackingId(转化事件);出价用 MAX_CONVERSION(平台自动优化,无需出价数);
    platforms / targeting 默认全不限。预算单位是"分";时间是 unix 秒。"""
    import time as _time

    def dim() -> dict:
        return {"positive": ["all"]}

    return _request_json("POST", "/ad-set/create", {
        "campaignId": campaign_id,
        "name": name,
        "budgetType": budget_type,                      # DAILY(日预算) / TOTAL(总预算)
        "budget": max(int(budget_cents), 1000),          # 平台下限 $10
        "startTime": int(start_time) or int(_time.time()),
        "endTime": int(end_time) or 2147483647,          # 不设结束=一直投
        "bidType": "MAX_CONVERSION",                     # 自动出价,平台优化转化量
        "trackingId": tracking_id,                       # 转化事件 id(必填!)
        "platforms": ["APP_AND_WEB_UNLIMITED"],
        "targeting": {
            "location": dim(), "gender": dim(), "ageGroup": dim(),
            "language": dim(), "interest": dim(), "os": dim(),
            "manufacturer": dim(), "carrier": dim(), "network": dim(),
        },
        "status": status,
    })


def create_ad(ad_set_id: str, name: str, creative: dict, status: str = "OFF") -> dict:
    """建第三层:广告(ad)。creative 里是用户真正看到的:标题/描述/按钮/素材/落地页。"""
    return _request_json("POST", "/ad/create", {
        "adSetId": ad_set_id, "name": name, "status": status, "creative": creative,
    })


# ============ 仪表盘用的「原始数字」报表 ============
# get_report 返回的是给 AI 看的格式化字符串($1.23 / 1.88%);
# 画图和算总计需要的是**纯数字**,所以单独一个函数,不改动原来的。

def get_report_raw(level: str = "campaign", start_date: str = "", end_date: str = "",
                   ad_account_id: str = "") -> list[dict]:
    """按层级拉报表,返回纯数字(金额=美元 float,百分比=百分数 float)。"""
    dimension, id_key, name_key = _REPORT_LEVELS[level]
    return _fetch_rows([dimension], id_key, name_key, start_date, end_date, ad_account_id)


def get_daily_raw(start_date: str, end_date: str, ad_account_id: str = "") -> list[dict]:
    """按天拉数据,用于画趋势图。

    ⚠️ 平台规则(实测):DATE 维度**跨度上限约 31 天**,32 天就报 400 Invalid parameters。
    调用方需自行控制区间长度。
    """
    return _fetch_rows(["DATE"], "date", "date", start_date, end_date, ad_account_id)


def _fetch_rows(dimensions: list[str], id_key: str, name_key: str,
                start_date: str, end_date: str, ad_account_id: str) -> list[dict]:
    body = {
        "name": "my-agent-dashboard",
        "dateRange": "FIXED",
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions,
        "metrics": ["COST", "IMPRESSION", "CPM"],   # 平台不管申请几个都返回全部 32 字段
    }
    if ad_account_id:
        body["adAccountIds"] = [ad_account_id]
    data = _post_json("/reports/getIntegratedReport", body)

    def num(row: dict, keys: list[str], default: float = 0.0) -> float:
        for k in keys:
            v = row.get(k)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return default

    out = []
    for row in data.get("rows") or []:
        if not isinstance(row, dict):
            continue
        key = row.get(id_key)
        if key in (None, ""):
            continue
        out.append({
            "id": str(key),
            "name": str(row.get(name_key) or key),
            # 金额:平台给的是"分",换成美元
            "cost": round(num(row, ["costDecimal", "cost"]) / 100, 2),
            "revenue": round(num(row, ["conversionValueDecimal", "conversionValue"]) / 100, 2),
            "impressions": int(num(row, ["impression", "impressions"])),
            "clicks": int(num(row, ["click", "clicks"])),
            "conversions": int(num(row, ["conversion", "conversions"])),
            # 百分比:平台给的是"万分点"(188 = 1.88%),换成百分数
            "ctr": round(num(row, ["ctr"]) / 100, 2),
            "cvr": round(num(row, ["cvr"]) / 100, 2),
            "cpc": round(num(row, ["cpcDecimal", "cpc"]) / 100, 2),
            "cpm": round(num(row, ["cpmDecimal", "cpm"]) / 100, 2),
            "cpa": round(num(row, ["cpaDecimal", "cpa"], -100.0) / 100, 2),   # -1 表示平台没这项
            "roas": round(num(row, ["roas"]) / 100, 2),
        })
    return out
