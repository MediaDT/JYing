"""Cloudflare Pages Direct Upload：按域名管理并发布 A/B 落地页。

Cloudflare 凭证来自服务器 .env；域名与 Pages 项目不写死，而是在每次发布时选择。
同一域名始终复用同一个项目，并保留已经发布的实验目录。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
SITES_DIR = DATA_DIR / "cloudflare_sites"
MAPPING_FILE = DATA_DIR / "cloudflare_sites.json"
WRANGLER = ROOT / "node_modules" / ".bin" / "wrangler"
# 上传超时**必须小于前端的 180 秒**。超过的话:用户看到"等太久了",而后端还在传 ——
# 发布是**不可逆的外部写操作**,他很可能再确认一次,于是发布两遍。
# (和大脑那边 BRAIN_TIMEOUT_S 是同一条教训,但这里后果更重。)
UPLOAD_TIMEOUT_S = 120
API = "https://api.cloudflare.com/client/v4"
_lock = threading.Lock()


class CloudflarePagesError(Exception):
    pass


def _now_beijing() -> str:
    """记录时间统一用北京时间 —— 用户说的"哪天发的"是他那边的日期。"""
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def _env(name: str) -> str:
    """现读 .env，使用户补好 Cloudflare 配置后不必重启服务。"""
    path = ROOT / ".env"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    except Exception:
        pass
    return os.getenv(name, "").strip()


def configured() -> bool:
    return bool(_env("CLOUDFLARE_ACCOUNT_ID") and _env("CLOUDFLARE_API_TOKEN"))


def _credentials() -> tuple[str, str]:
    account, token = _env("CLOUDFLARE_ACCOUNT_ID"), _env("CLOUDFLARE_API_TOKEN")
    if not account or not token:
        raise CloudflarePagesError(
            "还没配置 CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN。"
            "Token 需要 Cloudflare Pages Edit 权限；自动绑定域名还需要 Zone Read / DNS Edit。"
        )
    return account, token


def _api(method: str, path: str, *, params: dict | None = None,
         body: dict | None = None, timeout: float = 30) -> dict:
    account, token = _credentials()
    path = path.replace("{account}", account)
    try:
        with httpx.Client(timeout=timeout, trust_env=False,
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "application/json"}) as client:
            response = client.request(method, API + path, params=params, json=body)
    except httpx.TimeoutException as e:
        raise CloudflarePagesError("Cloudflare API 请求超时") from e
    except httpx.HTTPError as e:
        raise CloudflarePagesError(f"连接 Cloudflare 失败：{str(e)[:160]}") from e
    try:
        data = response.json()
    except Exception as e:
        raise CloudflarePagesError(f"Cloudflare 返回了无法解析的内容（HTTP {response.status_code}）") from e
    if not data.get("success"):
        errors = "; ".join(str(x.get("message") or x) for x in data.get("errors") or [])
        raise CloudflarePagesError(errors or f"Cloudflare 请求失败（HTTP {response.status_code}）")
    return data


def _read_mappings() -> dict:
    try:
        data = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_mappings(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MAPPING_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(MAPPING_FILE)


def normalize_domain(domain: str) -> str:
    raw = str(domain or "").strip().lower()
    if "://" in raw:
        raw = urlparse(raw).hostname or ""
    raw = raw.strip(". /")
    if (not raw or len(raw) > 253 or ".." in raw
            or not re.fullmatch(r"[a-z0-9.-]+", raw)
            or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                   for label in raw.split("."))
            or "." not in raw):
        raise CloudflarePagesError("域名格式不正确，请填写 hostname，例如 lp.example.com，不要带路径")
    return raw


def normalize_slug(slug: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(slug or "").lower()).strip("-")
    if not value:
        raise CloudflarePagesError("实验名称不能为空，例如 roof-260902")
    return value[:60].rstrip("-")


def project_name_for_domain(domain: str) -> str:
    domain = normalize_domain(domain)
    prefix = re.sub(r"[^a-z0-9-]+", "-", _env("CLOUDFLARE_PAGES_PROJECT_PREFIX") or "landing")
    base = re.sub(r"[^a-z0-9-]+", "-", domain.replace(".", "-"))
    digest = hashlib.sha256(domain.encode()).hexdigest()[:6]
    return f"{prefix[:12].strip('-') or 'landing'}-{base[:38].strip('-')}-{digest}"[:58].strip("-")


def _list_zones() -> list[dict]:
    account, _ = _credentials()
    first = _api("GET", "/zones", params={"account.id": account, "per_page": 100, "page": 1})
    rows = list(first.get("result") or [])
    pages = min(20, int((first.get("result_info") or {}).get("total_pages") or 1))
    for page in range(2, pages + 1):
        more = _api("GET", "/zones", params={"account.id": account,
                                               "per_page": 100, "page": page})
        rows.extend(more.get("result") or [])
    return rows


def list_resources(query: str = "", limit: int = 50) -> dict:
    """列出账号下可选择的 Zone、Pages 项目和本系统的域名映射（只读）。"""
    all_zones = _list_zones()
    zones_total = len(all_zones)
    projects_data = _api("GET", "/accounts/{account}/pages/projects")
    needle = str(query or "").strip().lower()
    if needle:
        all_zones = [x for x in all_zones if needle in str(x.get("name") or "").lower()]
    limit = max(1, min(int(limit or 50), 100))
    zones = [{"name": x.get("name"), "status": x.get("status"), "id": x.get("id")}
             for x in all_zones[:limit]]
    projects = [{"name": x.get("name"), "subdomain": x.get("subdomain"),
                 "created_on": x.get("created_on")}
                for x in projects_data.get("result") or []]
    return {"configured": True, "zones": zones, "zones_total": zones_total,
            "zones_returned": len(zones), "projects": projects,
            "domain_project_mappings": _read_mappings(),
            "note": "发布时选择域名；新域名自动创建项目，已映射域名自动复用项目。"}


def owned_zone(domain: str) -> str:
    return _owned_zone_record(domain)["name"]


def _owned_zone_record(domain: str) -> dict:
    domain = normalize_domain(domain)
    hits = [z for z in _list_zones()
            if domain == str(z.get("name") or "").lower()
            or domain.endswith("." + str(z.get("name") or "").lower())]
    if not hits:
        raise CloudflarePagesError("这个域名不在当前 Cloudflare 账号的 Zone 中，不能自动绑定")
    return max(hits, key=lambda z: len(str(z.get("name") or "")))


# 会「接管这个主机名」的记录类型。TXT/MX 之类不影响网页服务,不算冲突。
_SERVING_TYPES = ("A", "AAAA", "CNAME")


def domain_conflict(domain: str) -> dict:
    """这个域名上是不是已经有别的东西在跑?

    **实测踩到过**:某个根域名上有一条已代理的 A 记录,打开是一个**在投的落地页**
    (`<title>Window LP1</title>`)。把它绑给 Pages 项目,Cloudflare 会接管这个主机名 ——
    那个页面**当场下线**,而它可能正承接着广告流量,你要过一阵才发现转化归零。

    已经绑在**我们自己这个项目**上的不算冲突(那是重复发布同一个域名,正常)。
    """
    domain = normalize_domain(domain)
    zone = _owned_zone_record(domain)
    records = _api("GET", f"/zones/{zone['id']}/dns_records",
                   params={"name": domain, "per_page": 100}).get("result") or []
    project = str((_read_mappings().get(domain) or {}).get("project")
                  or project_name_for_domain(domain))

    def _points_at_us(r: dict) -> bool:
        """CNAME 已经指向**我们这个项目**的 pages.dev —— 那不是冲突,是有人先手动配好了。

        不排除这种记录的话,一个完全合理的操作(用户自己先在 Cloudflare 后台把
        CNAME 建好)反而会被护栏拦住,还告诉他"这个域名已经被占用了" —— 莫名其妙。
        指向**别的** pages 项目仍然算冲突,那确实会抢走别人的站点。
        """
        if str(r.get("type") or "").upper() != "CNAME":
            return False
        return str(r.get("content") or "").strip().lower().rstrip(".") == f"{project}.pages.dev"

    blocking = [{"type": r.get("type"), "content": str(r.get("content"))[:80],
                 "proxied": bool(r.get("proxied"))}
                for r in records
                if str(r.get("type") or "").upper() in _SERVING_TYPES and not _points_at_us(r)]
    already_ours = False
    if blocking and _project_exists(project):   # 这里只关心"域名绑没绑",空项目也算
        bound = _api("GET", f"/accounts/{{account}}/pages/projects/{project}/domains")
        already_ours = any(str(x.get("name") or "").lower() == domain
                           for x in bound.get("result") or [])
    return {"domain": domain, "zone": zone["name"], "records": blocking,
            "already_ours": already_ours,
            "conflict": bool(blocking) and not already_ours}


def _guard_domain(domain: str) -> None:
    """目标域名已经在跑别的东西 → **直接拒绝**,不给"确认一下就覆盖"的口子。

    这和「覆盖旧实验」是两回事:那个的影响范围是自己的实验,这个是**抢走一个
    正在服务的主机名**,可能把别人在投的页面弄下线。真要这么干,应该由人去
    Cloudflare 后台先把那条记录删掉 —— 那是个需要看清楚才做得出的动作。
    """
    info = domain_conflict(domain)
    if not info["conflict"]:
        return
    what = "；".join(f"{r['type']} → {r['content']}" + ("（已代理）" if r["proxied"] else "")
                    for r in info["records"])
    raise CloudflarePagesError(
        f"停止发布：{info['domain']} 上已经有 DNS 记录在服务（{what}）。"
        "把它绑给 Pages 项目会**接管这个主机名，现有页面当场下线** —— "
        "如果它正在承接广告流量，你要过一阵才会发现转化归零。"
        f"请改用一个没被占用的子域名（例如 lp.{info['zone']}）；"
        "确实要用这个域名的话，请先自己到 Cloudflare 后台删掉那条记录再来发布。")


# ClickFlare 的 CTA Click URL **格式是固定的**:https://<追踪域名>/cf/click/<数字>
# 只有域名会变(用户自己的追踪子域名),路径形状不变。
# 有了这个我们才挡得住一类很贵的错误:**把三个地址搞混**。
#   · Cloudflare 页面 URL      → 填进 ClickFlare 的 Lander
#   · CTA Click URL (/cf/click/N) → 放在页面按钮的 href 上   ← 这里要的是它
#   · Campaign Tracking URL    → 填进 NewsBreak 的 clickThroughUrl
# 粘错了页面看起来**完全正常**,要等数据不对劲才发现 —— 那时钱已经花了。
_CTA_PATH = re.compile(r"^/cf/click/\d+/?$")

# 脚本里写死了追踪域名(实测:同一追踪域名下,不管建几个 Lander、路径怎么排,
# ClickFlare 给的脚本都是**逐字节相同的同一份**;两段脚本之间唯一会变的就是这个域名)。
# 而且脚本会把页面上 CTA 链接的域名**改写成它自己的** —— 实测:
#   href  https://trk.a.com/cf/click/1
#   → 变成 clickflare.l="https://track.b.com/cf/click/1"
# 所以脚本和 CTA 必须来自同一套。对不上的话点击被送到另一个追踪器,
# 而页面一切正常、按钮照点、跳转照跳,要等数据对不上才发现 —— 静默且昂贵。
_SCRIPT_HOST = re.compile(r"https://([a-z0-9][a-z0-9.\-]{0,252}[a-z0-9])", re.I)


def script_hosts(tracking_script: str) -> list[str]:
    """脚本里出现过的 https 主机名(去重排序)。

    注意脚本里的 `http(?:s?)://` 是**正则源码**不是网址,不含字面 `https://`,
    所以不会被误抓。实测两份真脚本各只提取到 1 个,就是它的追踪域名。
    """
    found = _SCRIPT_HOST.findall(str(tracking_script or ""))
    return sorted({h.lower().rstrip(".") for h in found})


def tracking_domain_of(tracking_script: str) -> str:
    """脚本对应的追踪域名;认不出就返回空字符串,**不猜**。

    只有恰好提取到 1 个主机名时才算认出来。0 个 = 格式变了;
    多于 1 个 = 分不清哪个是追踪域名 —— 两种都不该拿去当索引。
    """
    hosts = script_hosts(tracking_script)
    return hosts[0] if len(hosts) == 1 else ""


def validate_clickflare(cta_url: str, tracking_script: str) -> tuple[str, str]:
    url = str(cta_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise CloudflarePagesError("ClickFlare CTA Click URL 必须是完整 HTTPS 地址")
    if not _CTA_PATH.match(parsed.path or ""):
        hint = ""
        if "cpid=" in (parsed.query or "") or not (parsed.path or "").strip("/"):
            hint = ("这看起来像 **Campaign Tracking URL** —— 那个是最后填进 NewsBreak 广告里的,"
                    "不是放在页面按钮上的。")
        raise CloudflarePagesError(
            f"这不像 ClickFlare 的 CTA Click URL。它的路径固定是 /cf/click/<数字>,"
            f"整体形如 https://trk.你的域名.com/cf/click/1,而你给的路径是 "
            f"「{parsed.path or '(空)'}」。{hint}"
            "请到 ClickFlare 后台复制 **CTA Click URL**(落地页上的按钮用的那个)。")
    script = str(tracking_script or "").strip()
    if not script or len(script) > 30000 or "<script" not in script.lower() or "</script>" not in script.lower():
        raise CloudflarePagesError("请提供 ClickFlare 后台给出的完整 Lander Tracking Script")
    if re.search(r"</(?:body|html)\s*>", script, re.I):
        raise CloudflarePagesError("Tracking Script 里不能包含 </body> 或 </html>")
    # 追踪域名对不上就拒绝 —— 见上面 _SCRIPT_HOST 那段的实测说明。
    # **只在有正面证据时拦**:提取不到主机名(ClickFlare 改了格式)时放行,
    # 否则一次改版就会把所有发布堵死。
    hosts = script_hosts(script)
    cta_host = (parsed.hostname or "").lower()
    if hosts and cta_host not in hosts:
        raise CloudflarePagesError(
            f"CTA 地址和 Tracking Script 不是同一套。脚本里写死的追踪域名是 "
            f"「{'、'.join(hosts)}」,而 CTA 用的是「{cta_host}」。"
            "脚本会把页面上 CTA 链接的域名**改写成它自己的**,所以这样发出去,"
            "点击会被送到另一个追踪器 —— 页面一切正常、按钮照点,你看不出来,"
            "要等数据对不上才发现。请在 ClickFlare 里用**同一个追踪域名**"
            "生成脚本和 CTA Click URL,两者配套再发。")
    return url, script


def inject_tracking(source: str, cta_url: str, tracking_script: str) -> tuple[str, int]:
    """替换代码层占位符；绝不让模型改写用户提供的 URL 或脚本。"""
    cta_url, tracking_script = validate_clickflare(cta_url, tracking_script)
    marker = "[[CLICKFLARE_CTA_URL]]"
    count = source.count(marker)
    if count < 1:
        raise CloudflarePagesError(
            "这个页面不是可发布模板（缺少 ClickFlare CTA 占位符）。请在落地页工作室重新生成 A/B 页面。"
        )
    result = source.replace(marker, cta_url)
    script_marker = "[[CLICKFLARE_LANDER_SCRIPT]]"
    comment_marker = "<!--[[CLICKFLARE_LANDER_SCRIPT]]-->"
    if comment_marker in result:
        result = result.replace(comment_marker, tracking_script)
    elif script_marker in result:
        result = result.replace(script_marker, tracking_script)
    elif re.search(r"</body\s*>", result, re.I):
        result = re.sub(r"</body\s*>", tracking_script + "\n</body>", result,
                        count=1, flags=re.I)
    else:
        result += "\n" + tracking_script
    return result, count


def _project_info(name: str) -> dict | None:
    """项目对象;不存在返回 None。"""
    try:
        return _api("GET", f"/accounts/{{account}}/pages/projects/{name}").get("result") or {}
    except CloudflarePagesError as e:
        if "does not exist" in str(e).lower() or "not found" in str(e).lower():
            return None
        raise


def _project_exists(name: str) -> bool:
    return _project_info(name) is not None


def _project_has_content(name: str) -> bool:
    """项目上**真的部署过东西**吗?

    只判"项目存在"是不够的:一个刚建好、从没部署过的空项目**没有任何内容可丢**,
    把它当成"线上有东西会被删"会白白拦住第一次发布 ——
    而"先手动把项目建好"恰恰是个很自然的操作。
    """
    info = _project_info(name)
    return bool(info and info.get("latest_deployment"))


def _ensure_project(name: str) -> bool:
    if _project_exists(name):
        return False
    _api("POST", "/accounts/{account}/pages/projects",
         body={"name": name, "production_branch": "main"})
    return True


def _ensure_domain(project: str, domain: str) -> dict:
    current = _api("GET", f"/accounts/{{account}}/pages/projects/{project}/domains")
    hit = next((x for x in current.get("result") or []
                if str(x.get("name") or "").lower() == domain), None)
    if hit:
        return hit
    result = _api("POST", f"/accounts/{{account}}/pages/projects/{project}/domains",
                  body={"name": domain})
    return result.get("result") or {"name": domain, "status": "pending"}


def _ensure_dns(domain: str, project: str) -> dict:
    """让这个主机名真的解析得到 —— 建一条**代理的 CNAME** 指向 Pages 项目。

    **绑定自定义域名 ≠ 这个域名能访问。** `POST .../pages/projects/x/domains`
    只是把名字登记到项目上,**不会替你建 DNS 记录**(走 API 这条路不会;后台点的时候会)。
    实测踩到:发布"成功"了、结果里还给了 A/B 地址,而 Cloudflare 那边是
    `verification_data.error_message = "CNAME record not set"`、证书 pending,
    浏览器打开是 `ERR_SSL_PROTOCOL_ERROR` —— **页面根本没人能访问,我们却报了成功**。
    买来的流量落在这上面就是全丢。

    只在**目标主机名上一条服务型记录都没有**时才建 —— 有记录说明那儿已经有东西在跑,
    绝不碰(和 `domain_conflict()` 是同一条规矩:别抢占一个已经在服务的名字)。
    """
    zone = _owned_zone_record(domain)
    zone_id = str(zone.get("id") or "")
    target = f"{project}.pages.dev"
    existing = _api("GET", f"/zones/{zone_id}/dns_records", params={"name": domain})
    rows = [r for r in (existing.get("result") or [])
            if str(r.get("type") or "").upper() in _SERVING_TYPES]
    for r in rows:
        if (str(r.get("type") or "").upper() == "CNAME"
                and str(r.get("content") or "").lower().rstrip(".") == target.lower()):
            return {"action": "已存在", "type": "CNAME", "content": target}
    if rows:
        # 有别的记录在这个名字上 —— 不是我们的,坚决不动
        return {"action": "没动",
                "⚠️": ("%s 上已经有 %s 记录(%s),不是指向本项目的。**没有改它** —— "
                       "那上面可能有别的东西在跑。请到 Cloudflare 后台确认后再处理。"
                       % (domain, rows[0].get("type"), str(rows[0].get("content"))[:60]))}
    created = _api("POST", f"/zones/{zone_id}/dns_records",
                   body={"type": "CNAME", "name": domain, "content": target,
                         "proxied": True, "ttl": 1,
                         "comment": "landing A/B (auto)"})
    return {"action": "已创建", "type": "CNAME", "content": target,
            "id": (created.get("result") or {}).get("id")}


def replace_risk(domain: str, slug: str) -> dict:
    """这次发布会不会把线上已有的实验整站替换掉?

    `wrangler pages deploy <目录>` 是**整站替换**,而那个目录在本地 `data/` 里 ——
    `data/` 不进 git。一旦它丢了(换机器、重装、没备份),项目名还能靠域名 hash 认回来,
    但本地没有任何历史实验目录 → 这一次部署就会把线上**所有旧实验删掉**,
    而 ClickFlare 里那些 Lander 还指着老地址 —— 买来的流量落在 404 上,钱照花。

    判据不依赖本地映射文件(它可能一起丢了):
      Cloudflare 上项目**已经存在**,而本地**除了这次这个实验之外一个目录都没有**
      → 我们其实不知道线上有什么,不能闭着眼睛整站覆盖。
    """
    domain = normalize_domain(domain)
    slug = normalize_slug(slug)
    project = str((_read_mappings().get(domain) or {}).get("project")
                  or project_name_for_domain(domain))
    # 判据是「项目上**部署过东西**」,不是「项目存在」—— 空项目没有内容可丢
    exists = _project_has_content(project)
    site_dir = SITES_DIR / project
    others = sorted(d.name for d in site_dir.iterdir()
                    if d.is_dir() and d.name != slug) if site_dir.is_dir() else []
    known = [str(r.get("slug")) for r in ((_read_mappings().get(domain) or {}).get("releases") or [])]
    return {
        "project": project,
        "project_has_content": exists,
        "local_slugs": others,
        "known_releases": sorted(set(known)),
        # 项目是新建的 → 线上本来就没东西,不存在覆盖问题
        "risky": bool(exists and not others),
        "missing_locally": sorted(set(known) - set(others) - {slug}),
    }


def _guard_replace(domain: str, slug: str, allow_replace: bool) -> dict:
    risk = replace_risk(domain, slug)
    if risk["risky"] and not allow_replace:
        lost = ("已知的旧实验:" + "、".join(risk["missing_locally"])
                if risk["missing_locally"] else "无法列出线上有哪些旧实验(本地记录也丢了)")
        raise CloudflarePagesError(
            f"停止发布,以免误删线上页面。Cloudflare 上项目 {risk['project']} 已经存在,"
            f"但本地 data/ 里没有任何历史实验目录 —— 而发布是**整站替换**,"
            f"这一次上传会把线上现有的页面全部删掉。{lost}。"
            "ClickFlare 里的 Lander 若还指着它们,流量就会落到 404 上。"
            "请先恢复 data/ 目录的备份;确实要整站重来的话,明确说「覆盖发布」再登记一次。")
    return risk


def _wrangler_deploy(directory: Path, project: str, slug: str) -> str:
    if not WRANGLER.exists():
        raise CloudflarePagesError("服务器还没安装 Wrangler。请在项目目录执行 npm install 后再发布。")
    account, token = _credentials()
    env = dict(os.environ)
    env.update({"CLOUDFLARE_ACCOUNT_ID": account, "CLOUDFLARE_API_TOKEN": token})
    cmd = [str(WRANGLER), "pages", "deploy", str(directory),
           "--project-name", project, "--branch", "main", "--commit-dirty=true",
           "--commit-message", f"Publish landing A/B {slug}"]
    try:
        done = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                              timeout=UPLOAD_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as e:
        raise CloudflarePagesError(
            f"Cloudflare Pages 上传超过 {UPLOAD_TIMEOUT_S} 秒，已停止等待。"
            "**先别急着重新确认** —— 请到 Cloudflare Pages 后台看看这次部署是不是已经成功了，"
            "重复确认会再发布一遍。") from e
    output = (done.stdout or "") + "\n" + (done.stderr or "")
    if done.returncode:
        # Wrangler 输出不应包含 token，但仍只返回末尾且做一次主动遮盖。
        safe = output.replace(token, "[REDACTED]")[-1200:]
        raise CloudflarePagesError("Cloudflare Pages 发布失败：" + safe.strip())
    urls = re.findall(r"https://[a-zA-Z0-9._-]+\.pages\.dev", output)
    return urls[-1] if urls else f"https://{project}.pages.dev"


_IMG_REF = re.compile(r"""\bsrc\s*=\s*["\']img/([A-Za-z0-9._-]+)["\']""", re.I)


def _copy_images(source_html: Path, html: str, dest_dir: Path) -> list[str]:
    """把页面**真正引用到的**配图从生成目录复制进发布目录。

    只复制引用到的那几张,不是整个图库目录 —— 发布是上传一个目录,
    多传的每一张都是白花的时间,而且会把这个人别的实验用过的图一起公开出去。
    源图找不到就跳过并如实返回(调用方会把它并进结果里)——
    **不能因为一张图挡住整次发布**,但也绝不能不吭声。
    """
    src_dir = source_html.parent / "img"
    missing: list[str] = []
    names = set(_IMG_REF.findall(html))
    if not names:
        return missing
    out = dest_dir / "img"
    out.mkdir(parents=True, exist_ok=True)
    for name in sorted(names):
        one = (src_dir / name).resolve()
        if one.parent != src_dir.resolve() or not one.is_file():
            missing.append(name)
            continue
        shutil.copy2(one, out / name)
    return missing


def publish_ab(domain: str, slug: str, variant_a: Path, variant_b: Path,
               cta_url: str, tracking_script: str, user_id: str = "",
               allow_replace: bool = False, tracking_script_b: str = "") -> dict:
    """创建/复用项目，发布 A/B 两页并绑定域名。调用方必须先取得用户确认。

    tracking_script_b:B 版单独的追踪脚本。**留空 = 两版用同一段。**

    为什么要留这个口子:追踪器的 lander 脚本有两种设计 ——
    通用一段(靠 Lander URL 区分)或每个 Lander 一段(脚本里带 lander id)。
    如果是后者而我们两版注入同一段,**B 版会上报成 A 版,A/B 数据全废**,
    而且页面一切正常、看不出任何异常。与其赌一个不确定的前提,不如两种都支持。
    """
    domain, slug = normalize_domain(domain), normalize_slug(slug)
    owned_zone(domain)
    _guard_domain(domain)      # 别把一个正在跑的域名抢过来
    for path in (variant_a, variant_b):
        if not path.is_file():
            raise CloudflarePagesError(f"找不到待发布页面：{path.name}")
    script_b = str(tracking_script_b or "").strip() or tracking_script
    html_a, count_a = inject_tracking(variant_a.read_text(encoding="utf-8"), cta_url, tracking_script)
    html_b, count_b = inject_tracking(variant_b.read_text(encoding="utf-8"), cta_url, script_b)

    with _lock:
        # 本地目录是"线上有什么"的唯一依据,丢了就不能闭眼整站覆盖(见 replace_risk)
        _guard_replace(domain, slug, allow_replace)
        mappings = _read_mappings()
        old = mappings.get(domain) or {}
        project = str(old.get("project") or project_name_for_domain(domain))
        created = _ensure_project(project)
        site_dir = SITES_DIR / project
        target = site_dir / slug
        (target / "a").mkdir(parents=True, exist_ok=True)
        (target / "b").mkdir(parents=True, exist_ok=True)
        (target / "a" / "index.html").write_text(html_a, encoding="utf-8")
        (target / "b" / "index.html").write_text(html_b, encoding="utf-8")
        # **配图要跟着页面一起传上去。** 页面里写的是相对路径 `img/xxx.jpg`,
        # 所以 A、B 各自目录下都要有一份自己的 img/。少了这一步,页面发上去
        # 图全是裂的 —— 而这正是「发布成功 = 打得开」那条规矩防的事:
        # 接口返回 200 不等于用户看到的是一个完好的页面。
        lost = (_copy_images(variant_a, html_a, target / "a")
                + _copy_images(variant_b, html_b, target / "b"))
        # 根路径放一个中性静态页,**不跳转、也不列出实验清单**。
        # 跳转有两个坏处:①裸访问域名会算到 A 版头上;②平台/审核抓根域名时
        # 看到的是一个跳转页。列清单则等于把在跑的实验公开给同行看。
        # A/B 的准确地址在发布结果里给用户,也存在 data/cloudflare_sites.json 里。
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "index.html").write_text(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex">'
            f'<title>{domain}</title></head>'
            '<body style="font:16px/1.6 system-ui;margin:0;display:flex;'
            'align-items:center;justify-content:center;height:100vh;color:#334">'
            f'<main><h1 style="font-size:20px;margin:0 0 6px">{domain}</h1>'
            '<p style="margin:0;color:#889">This site is online.</p></main>'
            '</body></html>', encoding="utf-8")
        pages_url = _wrangler_deploy(site_dir, project, slug)
        domain_state = _ensure_domain(project, domain)
        # 绑完还要让它**真的解析得到**,否则证书签不出来、页面打不开(见 _ensure_dns)
        try:
            dns_state = _ensure_dns(domain, project)
        except Exception as e:
            dns_state = {"action": "失败", "⚠️": "DNS 记录没建成(%s)。"
                                                "在 Cloudflare 后台给 %s 加一条代理的 CNAME "
                                                "指向 %s.pages.dev 即可。"
                                                % (str(e)[:120], domain, project)}
        releases = list(old.get("releases") or [])
        releases.append({"slug": slug, "a": f"https://{domain}/{slug}/a/",
                         "b": f"https://{domain}/{slug}/b/", "user_id": user_id,
                         # 记上时间:以后要查"这个实验哪天发的"总得有个依据
                         "at": _now_beijing()})
        mappings[domain] = {"project": project, "pages_url": pages_url,
                            "latest_slug": slug, "releases": releases[-50:]}
        _write_mappings(mappings)

    return {
        "done": True,
        **({"⚠️请立刻告诉用户": "有 %d 张配图没能一起传上去(见「⚠️配图丢了」),"
                              "线上页面那几个位置是裂图。**别说成发布完全成功。**"
                              % len(set(lost))} if lost else {}),
        "created": {
            "pages_project": project,
            "project_action": "新建" if created else "复用",
            "domain": domain,
            "domain_status": domain_state.get("status") or "active",
            "DNS记录": dns_state,
            "pages_dev_url": pages_url,
            "variant_a_url": f"https://{domain}/{slug}/a/",
            "variant_b_url": f"https://{domain}/{slug}/b/",
            "clickflare_lander_a_url": f"https://{domain}/{slug}/a/?cpid={{campaign_id}}",
            "clickflare_lander_b_url": f"https://{domain}/{slug}/b/?cpid={{campaign_id}}",
            "cta_links_injected": {"A": count_a, "B": count_b},
            "追踪脚本": "A/B 各用各的" if script_b != tracking_script else "两版共用同一段",
            # 少一张图不挡发布(整次发布回滚代价更大),但**绝不能不吭声** ——
            # 「发布成功」的定义是「打得开而且是完好的」,不是「接口返回 200」。
            **({"⚠️配图丢了": sorted(set(lost))} if lost else {}),
        },
        "note": (
            ("⏳ 域名还在签发证书(状态 " + str(domain_state.get("status")) + ")，"
             "**刚建好 DNS 记录的话通常要几分钟**,这期间打开会看到 "
             "`ERR_SSL_PROTOCOL_ERROR` 或证书错误,那是正常的。"
             "**证书没签好之前先别把地址填进 ClickFlare**；"
             "先用 " + pages_url + " 自查页面内容。"
             + (" ⚠️ " + str(dns_state.get("⚠️")) if dns_state.get("⚠️") else "")
             if str(domain_state.get("status") or "").lower() != "active" else
             "域名已生效，可以直接使用。")
            + " 接下来在 ClickFlare 建两个 Lander，50/50 分流；"
            "NewsBreak 使用同一个 Campaign Tracking URL。"
            "**A/B 两版共用同一个 CTA Click URL** —— 这在「同一个 Campaign、同一路径、"
            "同一个 Offer」的前提下是对的;若要让 A/B 指向不同 Offer，现在的流程做不到，"
            "需要分成两次发布。"),
    }
