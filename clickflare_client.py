"""ClickFlare 追踪器 API 封装。

接口全部是实测出来的(公开文档没有接口清单),细节见 CLAUDE.md 第六之十八节;
怀疑平台改了就跑 `./venv/bin/python check_clickflare_params.py`。

**这一版只做「读」和「新建落地页」** —— 都不影响正在跑的东西。
改 campaign 的 flow(换落地页、调权重)会**立刻影响正在花钱的投放**,
那属于写操作,必须走第七节的保险箱,不放在这个模块里裸奔。

设计上刻意避免「让用户从一长串里挑」:
- `workspace_id`(账号里有 22 个)**从 campaign 自己读** —— 用户给的追踪链接里带
  `cpid`,顺着它拿到 campaign,workspace 就确定了,不用问也不会挑错;
- `tracking_domain_id`(74 个)**从 CTA 地址的域名反查**,同样是确定性映射。
两个都对不上时**明确报错**,绝不替用户挑一个(和 `_default_ad_account_id()` 同一条规矩)。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

BASE = "https://public-api.clickflare.io"
AUTH_HEADER = "api-key"
TIMEOUT = 25.0
ROOT = Path(__file__).parent
# 列表接口一次能吐几千条(实测 offer 有 2710 个)。递给 AI 之前必须截断,
# 否则一条消息就把上下文塞满了。
MAX_ROWS = 60
_OBJECT_ID = re.compile(r"^[0-9a-f]{24}$")


class ClickFlareError(Exception):
    pass


def _key() -> str:
    """现读 .env —— 后来才填上的键,运行中的进程读不到(第八节坑表)。"""
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("CLICKFLARE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def configured() -> bool:
    return bool(_key())


def _api(method: str, path: str, *, json_body: dict | None = None,
         params: dict | None = None):
    key = _key()
    if not key:
        raise ClickFlareError(
            "还没配置 ClickFlare。请管理员到 ClickFlare 后台 "
            "Settings → Security → Generate API Key 生成一把,写进服务器 .env 的 "
            "CLICKFLARE_API_KEY,然后重启服务。")
    try:
        key.encode("latin-1")      # HTTP 头只认 latin-1,混进中文会抛看不懂的错
    except UnicodeEncodeError:
        raise ClickFlareError("CLICKFLARE_API_KEY 里混进了中文或全角字符,请重新复制一遍。")
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.request(method, BASE + path,
                               headers={AUTH_HEADER: key, "Accept": "application/json"},
                               json=json_body, params=params)
    except httpx.HTTPError as e:
        raise ClickFlareError(f"连不上 ClickFlare({type(e).__name__}),稍后再试。")
    if r.status_code == 401:
        raise ClickFlareError("ClickFlare 拒绝了这把 key(401)。它可能被删了或重新生成过,"
                              "请管理员到后台 Settings → Security 重新生成并更新 .env。")
    if r.status_code == 404:
        raise ClickFlareError(f"ClickFlare 说找不到:{path}。可能是 id 不对,"
                              "也可能平台改了接口 —— 跑 check_clickflare_params.py 对一下。")
    if r.status_code >= 400:
        detail = ""
        try:
            body = r.json()
            detail = str(body.get("message") or body.get("error") or "")[:200]
        except ValueError:
            detail = r.text[:200]
        raise ClickFlareError(f"ClickFlare 报错(HTTP {r.status_code}):{detail or '(没给说明)'}")
    try:
        return r.json()
    except ValueError:
        raise ClickFlareError("ClickFlare 返回的不是 JSON,可能平台出问题了。")


# ---------------------------------------------------------------- 只读

def list_workspaces() -> list[dict]:
    data = _api("GET", "/api/workspaces")
    return [{"id": w.get("_id"), "name": w.get("name"), "private": w.get("private")}
            for w in data if isinstance(w, dict)]


def list_domains() -> list[dict]:
    data = _api("GET", "/api/domains")
    return [{"id": d.get("_id"), "domain": d.get("domain")}
            for d in data if isinstance(d, dict)]


def list_campaigns(search: str = "", limit: int = MAX_ROWS) -> dict:
    data = _api("GET", "/api/campaigns/list")
    rows = [c for c in data if isinstance(c, dict) and not c.get("archived")]
    if search:
        needle = search.lower()
        rows = [c for c in rows if needle in str(c.get("name", "")).lower()]
    return {"total": len(rows),
            "campaigns": [{"id": c.get("_id"), "name": c.get("name"),
                           "workspace_id": c.get("workspace_id")}
                          for c in rows[:max(1, limit)]],
            "truncated": len(rows) > limit}


def list_landings(search: str = "", limit: int = MAX_ROWS) -> dict:
    data = _api("GET", "/api/landings")
    rows = [x for x in data if isinstance(x, dict)]
    if search:
        needle = search.lower()
        rows = [x for x in rows
                if needle in str(x.get("name", "")).lower()
                or needle in str(x.get("url", "")).lower()]
    return {"total": len(rows),
            "landings": [{"id": x.get("_id"), "name": x.get("name"), "url": x.get("url"),
                          "cta_count": x.get("cta_count")} for x in rows[:max(1, limit)]],
            "truncated": len(rows) > limit}


def campaign_id_from(text: str) -> str:
    """从 Campaign Tracking URL 里抠出 campaign id;也接受直接给的 id。

    **为什么这么设计**:用户说「我要跑这个追踪链接」时,手里有的就是那条链接。
    链接里带着 `cpid=<campaign_id>`,顺着它定位是**确定性**的 ——
    比让模型按名字去猜是哪个 campaign 靠谱得多(账号里有 50 条,重名很常见)。
    """
    raw = str(text or "").strip()
    if _OBJECT_ID.match(raw.lower()):
        return raw.lower()
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        for name in ("cpid", "campaign_id", "campaignId"):
            values = parse_qs(parsed.query).get(name) or []
            if values and _OBJECT_ID.match(values[0].strip().lower()):
                return values[0].strip().lower()
        raise ClickFlareError(
            "这条链接里没有 `cpid=`,认不出是哪个 campaign。请复制 ClickFlare 里那条 "
            "**Campaign Tracking URL**(形如 https://追踪域名/xxxx?cpid=…),"
            "不是落地页地址、也不是 CTA Click URL。")
    raise ClickFlareError(
        f"认不出 campaign:「{raw[:60] or '(空)'}」。请给 Campaign Tracking URL,"
        "或者 24 位的 campaign id。")


def get_campaign(campaign: str) -> dict:
    """读一条 campaign(**连它的 flow 一起返回**,落地页和权重都在 flow 里)。"""
    return _api("GET", f"/api/campaigns/{campaign_id_from(campaign)}")


def _flow_id_of(campaign_doc: dict) -> str:
    """从 campaign 上取 flow_id。

    **不能只看内嵌的 `flow`** —— 实测:老的 campaign 会把整个 flow 内嵌返回,
    而**刚建出来的 campaign 只有 `flow_id`,没有 `flow`**。只认内嵌的话,
    新建的 campaign 一律报「读不到 flow」。(这个是拿临时 campaign 跑出来的。)
    """
    embedded = campaign_doc.get("flow")
    if isinstance(embedded, dict) and embedded.get("_id"):
        return str(embedded["_id"])
    if campaign_doc.get("flow_id"):
        return str(campaign_doc["flow_id"])
    raise ClickFlareError("这条 campaign 上既没有 flow 也没有 flow_id,没法改落地页。")


def _flow_of(campaign_doc: dict) -> dict:
    """拿这条 campaign 的完整 flow(内嵌的就用内嵌的,只有 id 就去取)。"""
    embedded = campaign_doc.get("flow")
    if isinstance(embedded, dict) and embedded.get("paths"):
        return embedded
    try:
        return get_flow(_flow_id_of(campaign_doc))
    except ClickFlareError:
        return {}


def describe_campaign(campaign: str) -> dict:
    """把一条 campaign 现在挂着什么讲清楚 —— 改之前先让用户看见现状。"""
    data = get_campaign(campaign)
    flow = _flow_of(data)
    names = _name_lookup()
    paths_out = []
    for group_name, group in (flow.get("paths") or {}).items():
        if not isinstance(group, dict):
            continue
        for path in group.get("paths") or []:
            kind = path.get("destination")
            block = path.get(kind) or {}
            paths_out.append({
                "所在分组": group_name,
                "path名字": path.get("name"),
                "类型": kind,
                "启用": path.get("enabled"),
                "path权重": path.get("weight"),
                "落地页": [_row(x, names["landings"]) for x in block.get("landers") or []],
                "offer": [_row(x, names["offers"]) for x in block.get("offers") or []],
            })
    return {"campaign_id": data.get("_id"), "名字": data.get("name"),
            "workspace_id": data.get("workspace_id"), "flow_id": (flow or {}).get("_id"),
            "追踪类型": data.get("tracking_type"), "paths": paths_out,
            "note": ("落地页和权重都挂在 flow 上,不在 campaign 上。"
                     "要换落地页就是改这里的「落地页」列表和 weight。")}


def _row(item: dict, table: dict) -> dict:
    ident = str(item.get("id") or "")
    return {"id": ident, "名字": table.get(ident, "(查不到名字)"), "weight": item.get("weight")}


def _name_lookup() -> dict:
    """id → 名字。改投放前要让用户看到的是名字,不是一串 24 位十六进制。"""
    out = {"landings": {}, "offers": {}}
    for key, path in (("landings", "/api/landings"), ("offers", "/api/offers")):
        try:
            for x in _api("GET", path):
                if isinstance(x, dict) and x.get("_id"):
                    out[key][str(x["_id"])] = x.get("name") or ""
        except ClickFlareError:
            pass          # 名字查不到不该让整个功能失败,上面会显示「查不到名字」
    return out


def tracking_domain_id_for(host_or_url: str) -> str:
    """按域名反查 tracking_domain_id。**查不到就报错,绝不随便挑一个。**"""
    raw = str(host_or_url or "").strip().lower()
    if "://" in raw:
        raw = urlparse(raw).hostname or ""
    raw = raw.strip("./")
    if not raw:
        raise ClickFlareError("没给追踪域名,认不出用哪个 tracking domain。")
    for d in list_domains():
        if str(d.get("domain") or "").lower() == raw:
            return str(d["id"])
    raise ClickFlareError(
        f"ClickFlare 里没有「{raw}」这个追踪域名。请确认 CTA 地址用的域名"
        "确实是这个账号里配好的追踪域名 —— 域名对不上的话,追踪是接不起来的。")


# ---------------------------------------------------------------- 新建(只新增)

def create_landing(name: str, url: str, workspace_id: str,
                   tracking_domain_id: str = "", cta_count: int = 1,
                   notes: str = "") -> dict:
    """在 ClickFlare 里建一个 Lander 记录。

    **只新增,不动任何在跑的东西** —— 建好的 Lander 要等到被挂进某个 campaign 的
    flow 里才会承接流量,而那一步是单独的写操作、要走保险箱。

    `cta_count`:这个落地页上有几个**不同的** Offer 出口。我们生成的 A/B 页面
    所有 CTA 都指向同一个 `/cf/click/1`,所以是 1(账号里 938 个落地页有 919 个是 1)。
    """
    name = str(name or "").strip()
    url = str(url or "").strip()
    if not name:
        raise ClickFlareError("落地页名字不能为空 —— 以后在 ClickFlare 里全靠它认。")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ClickFlareError(f"落地页地址必须是完整的 http(s) 地址,收到的是「{url[:60]}」。")
    if not _OBJECT_ID.match(str(workspace_id or "").lower()):
        raise ClickFlareError(
            "workspace_id 不对。这个账号有 22 个 workspace,**不能替用户挑** —— "
            "正常做法是从要投放的那条 campaign 上读它的 workspace_id。")
    if not isinstance(cta_count, int) or not 1 <= cta_count <= 20:
        raise ClickFlareError("cta_count 要是 1~20 的整数(我们的 A/B 页面通常是 1)。")

    body = {"workspace_id": str(workspace_id).lower(), "name": name[:200], "url": url,
            "cta_count": cta_count, "is_prelander": False, "notes": str(notes or "")[:500],
            "tags": []}
    if tracking_domain_id:
        if not _OBJECT_ID.match(str(tracking_domain_id).lower()):
            raise ClickFlareError("tracking_domain_id 格式不对(应该是 24 位十六进制)。")
        body["tracking_info"] = {"tracking_domain_id": str(tracking_domain_id).lower()}
    created = _api("POST", "/api/landings", json_body=body)
    ident = created.get("_id") if isinstance(created, dict) else None
    if not ident:
        raise ClickFlareError(f"建好了但没拿到 id,平台返回:{str(created)[:200]}")
    return {"id": str(ident), "name": created.get("name"), "url": created.get("url"),
            "cta_count": created.get("cta_count")}


def delete_landing(landing_id: str) -> bool:
    """删掉一个 Lander。给自动化测试收拾自己造的东西用,别拿它删用户的。"""
    if not _OBJECT_ID.match(str(landing_id or "").lower()):
        raise ClickFlareError("landing_id 格式不对。")
    _api("DELETE", f"/api/landings/{str(landing_id).lower()}")
    return True


# ---------------------------------------------------------------- 改 flow(会立刻影响投放)

def get_flow(flow_id: str) -> dict:
    if not _OBJECT_ID.match(str(flow_id or "").lower()):
        raise ClickFlareError("flow_id 格式不对(应该是 24 位十六进制)。")
    return _api("GET", f"/api/flows/{str(flow_id).lower()}")


def flow_fingerprint(flow_doc: dict) -> str:
    """给 flow 的「内容」算个指纹,用来判断登记之后有没有被别人改过。

    只取 PUT 会覆盖的那两块(`flow` / `paths`)—— `updated_at` 这类字段
    不该参与比较,否则平台自己刷一下时间戳就误判成「被人改过」。
    """
    import hashlib
    body = _flow_put_body(flow_doc)
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _flow_put_body(flow_doc: dict) -> dict:
    """从 GET 回来的完整文档里取出 PUT 需要的那两块。

    **PUT 是整体替换,不是打补丁** —— `paths` 必须整块搬过去(里面除了
    `defaultPaths` 还可能有 `rulePaths` 规则分流),只 PUT 一半等于把另一半清空。
    """
    flow = dict(flow_doc.get("flow") or {})
    keep = {k: flow[k] for k in ("workspace_id", "name", "transition", "notes") if k in flow}
    if not keep.get("name") or not keep.get("transition"):
        raise ClickFlareError("这条 flow 缺 name 或 transition,不敢往回写 —— "
                              "PUT 是整体替换,漏字段会把它清空。")
    return {"flow": keep, "paths": copy.deepcopy(flow_doc.get("paths") or {})}


def _iter_paths(paths_obj: dict):
    """遍历 flow 里所有的 path,返回 (分组名, 序号, path 本身)。"""
    for group_name, group in (paths_obj or {}).items():
        if not isinstance(group, dict):
            continue
        for idx, path in enumerate(group.get("paths") or []):
            if isinstance(path, dict):
                yield group_name, idx, path


def plan_lander_swap(campaign: str, landers: list[dict], path_name: str = "") -> dict:
    """算出「把这条 campaign 的落地页换成这几个」之后 flow 应该长什么样。

    **只算,不写。** 返回里带着 before/after、警告,以及执行时要 PUT 的完整 body
    和当前 flow 的指纹 —— 执行前会拿指纹再比一次,期间被人改过就停下。

    `landers`:`[{"id": ..., "weight": 50}, ...]`,权重加起来必须是 100。
    **`offers` 一个字都不动。**
    """
    total = sum(int(x.get("weight") or 0) for x in landers)
    if len(landers) < 1:
        raise ClickFlareError("至少要给一个落地页。")
    if total != 100:
        raise ClickFlareError(f"权重加起来要正好 100,现在是 {total}。")
    ids = [str(x.get("id") or "").lower() for x in landers]
    if len(set(ids)) != len(ids):
        raise ClickFlareError("同一个落地页被放了两次 —— 那不是 A/B,是把流量分给同一页。")
    for ident in ids:
        if not _OBJECT_ID.match(ident):
            raise ClickFlareError(f"落地页 id 格式不对:{ident[:30]}")
    known = {str(x.get("id")) for x in list_landings(limit=100000)["landings"]}
    missing = [i for i in ids if i not in known]
    if missing:
        raise ClickFlareError(f"ClickFlare 里找不到这些落地页:{missing} —— "
                              "先用 create_clickflare_landers 建好再挂。")

    campaign_doc = get_campaign(campaign)
    flow_id = _flow_id_of(campaign_doc)
    # campaign 里内嵌的 flow 可能是精简过的,改之前一律按 id 重新取一份完整的
    plan = plan_flow_lander_swap(get_flow(flow_id), ids, landers, path_name)
    plan["campaign"] = {"id": campaign_doc.get("_id"), "名字": campaign_doc.get("name")}
    return plan


def plan_flow_lander_swap(flow_doc: dict, ids: list[str], landers: list[dict],
                          path_name: str = "") -> dict:
    """`plan_lander_swap` 的核心:只跟 flow 打交道,不碰 campaign。

    单独拆出来是为了**能脱离真实投放测试** —— 拿一条临时 flow 就能验
    「改哪一条 path」「兄弟 path 会不会被整体替换清掉」这些要命的地方。
    """
    flow_id = str(flow_doc.get("_id") or "")
    candidates = [(g, i, p) for g, i, p in _iter_paths(flow_doc.get("paths") or {})
                  if p.get("enabled", True)]
    if path_name:
        candidates = [c for c in candidates if str(c[2].get("name") or "") == path_name]
    if not candidates:
        raise ClickFlareError("这条 flow 里没有启用中的 path" +
                              (f"叫「{path_name}」" if path_name else "") + ",没法挂落地页。")
    if len(candidates) > 1:
        names = "、".join(f"{g}/{p.get('name')}" for g, _, p in candidates)
        raise ClickFlareError(
            f"这条 flow 有 {len(candidates)} 条启用中的 path({names}),"
            "**不能替用户挑**要改哪一条。请把 path 的名字告诉我再来。")
    group_name, idx, current = candidates[0]

    body = _flow_put_body(flow_doc)
    target = body["paths"][group_name]["paths"][idx]
    old_kind = target.get("destination")
    old_block = dict(target.get(old_kind) or {})
    offers = copy.deepcopy(old_block.get("offers") or [])
    warnings = []
    if old_kind == "offers_only":
        warnings.append(
            "⚠️ 这条 campaign 现在是**直接跳 offer、不经过落地页**的。"
            "挂上落地页等于改变整条漏斗的形状,转化率会明显变化(不一定变好),"
            "而且和历史数据不可比。确认前请务必跟用户说清楚。")
    elif old_kind != "landers_offers":
        raise ClickFlareError(
            f"这条 path 的类型是 `{old_kind}`,不是「落地页 + offer」也不是「只有 offer」,"
            "换落地页的逻辑不适用。请到 ClickFlare 后台手工处理。")
    if not offers:
        raise ClickFlareError("这条 path 上一个 offer 都没有 —— 挂了落地页流量也无处可去。")

    before_landers = copy.deepcopy(old_block.get("landers") or [])
    target.pop(old_kind, None)
    target["destination"] = "landers_offers"
    target["landers_offers"] = {
        "landers": [{"id": i, "weight": int(x.get("weight"))} for i, x in zip(ids, landers)],
        "offers": offers,
    }
    if before_landers:
        warnings.append(
            "⚠️ 这条 campaign 的历史数据会**横跨两批落地页** —— 换之后再看这条的"
            "转化率,前后是两个不同的页面。要干净的对比,得另建一条 campaign 跑新页面。")

    names = _name_lookup()
    return {
        "flow_id": flow_id,
        "path": {"分组": group_name, "序号": idx, "名字": current.get("name")},
        "换之前": {"类型": old_kind,
                 "落地页": [_row(x, names["landings"]) for x in before_landers] or "(没有落地页,直接跳 offer)",
                 "offer": [_row(x, names["offers"]) for x in offers]},
        "换之后": {"类型": "landers_offers",
                 "落地页": [_row(x, names["landings"]) for x in target["landers_offers"]["landers"]],
                 "offer": [_row(x, names["offers"]) for x in offers]},
        "offer有没有动": "没动" if offers == (old_block.get("offers") or []) else "❗动了(这是 bug)",
        "warnings": warnings,
        "put_body": body,
        "fingerprint": flow_fingerprint(flow_doc),
    }


def apply_flow(flow_id: str, put_body: dict, expect_fingerprint: str = "") -> dict:
    """把算好的 flow 写回去。**这一下会立刻改变正在花钱的投放。**

    写之前拿指纹再比一次:登记之后如果有人在 ClickFlare 后台动过这条 flow,
    我们手里这份 body 是照着旧状态算的,写回去等于把他的改动**静默抹掉**
    (PUT 是整体替换)。所以对不上就停,不写。
    """
    if not _OBJECT_ID.match(str(flow_id or "").lower()):
        raise ClickFlareError("flow_id 格式不对。")
    if expect_fingerprint:
        now = flow_fingerprint(get_flow(flow_id))
        if now != expect_fingerprint:
            raise ClickFlareError(
                "没有改。这条 flow 在登记之后被改过了(可能有人在 ClickFlare 后台动过)。"
                "写回去会把那个改动整个覆盖掉 —— 请重新看一遍现状再登记。")
    _api("PUT", f"/api/flows/{str(flow_id).lower()}", json_body=put_body)
    after = get_flow(str(flow_id).lower())
    landed = []
    for _, _, path in _iter_paths(after.get("paths") or {}):
        block = path.get(path.get("destination")) or {}
        landed.extend(block.get("landers") or [])
    return {"done": True, "flow_id": flow_id,
            "写完回查到的落地页": landed or "(一个都没有 —— 不对劲,请到后台核对)"}
