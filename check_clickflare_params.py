#!/usr/bin/env python
"""
ClickFlare 接口体检 —— 把我们依赖的每一条事实拿真 key 打一遍。

    ./venv/bin/python check_clickflare_params.py

**为什么需要它**:ClickFlare 的接口是**实测出来的,不是文档给的**。
公开文档没有接口清单;网上还有页面说认证头是 `api-token`(错的,实测是 `api-key`);
官方 swagger 里写着 `GET /api/campaigns`,实际调用返回 404。
把结论写死在注释里只会慢慢过期,不如留一个随时能重跑的体检。

**全程只发 GET,一次写操作都不做** —— 体检不该改用户账号里的任何东西。

判据不是"有没有报错",而是"和我们记录的事实对不对得上"。
接口改了、字段改名了,这里会红,别等到发布落地页时才发现。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

BASE = "https://public-api.clickflare.io"
AUTH_HEADER = "api-key"
TIMEOUT = 20.0

# 我们真正依赖的接口。改了任何一个,落地页那条链就会断。
REQUIRED = {
    "/api/workspaces": "工作区(建落地页要传 workspace_id)",
    "/api/campaigns/list": "campaign 列表",
    "/api/landings": "落地页(注意叫 landings 不是 landers)",
    "/api/offers": "offer",
    "/api/domains": "追踪域名",
    "/api/traffic-sources": "流量源",
}
# 建落地页要传的字段。少一个都建不成。
LANDING_FIELDS = {"workspace_id", "name", "url", "cta_count", "is_prelander",
                  "notes", "tracking_info", "tags"}


def _key() -> str:
    """现读 .env —— 和项目其它地方一样,别只信进程启动时的环境变量。"""
    path = Path(__file__).parent / ".env"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("CLICKFLARE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _get(client: httpx.Client, path: str, key: str, header: str = AUTH_HEADER):
    return client.get(BASE + path, headers={header: key, "Accept": "application/json"})


def main() -> int:
    key = _key()
    if not key:
        print("❌ .env 里没有 CLICKFLARE_API_KEY。")
        print("   到 ClickFlare 后台 Settings → Security → Generate API Key 生成一把,")
        print("   然后写进 .env 的 CLICKFLARE_API_KEY=(不要提交 git)。")
        return 1
    print(f"入口 {BASE}   key 长度 {len(key)}\n")

    bad: list[str] = []
    with httpx.Client(timeout=TIMEOUT) as c:

        # ① 认证头。判据很妙:用对了头,路径不对时返回 404「Path not found」;
        #    用错了头,一律 401「not authorized」——卡在认证根本走不到路由。
        print("① 认证头")
        probe = "/api/campaigns"          # 存在于 swagger、但实际调用是 404 的路径
        right = _get(c, probe, key)
        wrong = _get(c, probe, key, header="Authorization")
        if right.status_code == 404 and wrong.status_code == 401:
            print(f"   ✅ 认证头仍是 `{AUTH_HEADER}`(它 404、别的 401)")
        elif right.status_code == 401:
            bad.append(f"`{AUTH_HEADER}` 头被拒(401)——key 失效了,或者平台换了认证方式")
            print(f"   ❌ `{AUTH_HEADER}` 返回 401,key 可能已失效")
            return _finish(bad)
        else:
            print(f"   ⚠️ 判据变了:{AUTH_HEADER}→{right.status_code}, Authorization→{wrong.status_code}")
            print("      (不一定是坏事,但说明平台改了行为,下面的结论要重新看)")

        # ② 接口清单。文档没有,但 swagger 自己吐得出来。
        print("\n② 接口清单 /api/swagger.json")
        spec = {}
        r = _get(c, "/api/swagger.json", key)
        if r.status_code == 200:
            try:
                spec = r.json()
            except json.JSONDecodeError:
                spec = {}
        if spec.get("paths"):
            print(f"   ✅ 拿到 {len(spec['paths'])} 个接口")
        else:
            bad.append("拿不到 swagger.json —— 以后接口变了会没处对照")
            print(f"   ❌ 拿不到(HTTP {r.status_code})")

        # ③ 我们依赖的接口还在不在
        print("\n③ 依赖的接口")
        width = max(len(p) for p in REQUIRED)
        for path, what in REQUIRED.items():
            r = _get(c, path, key)
            if r.status_code == 200:
                try:
                    data = r.json()
                    n = len(data) if isinstance(data, list) else "?"
                except json.JSONDecodeError:
                    n = "?"
                print(f"   ✅ {path:<{width}}  {n} 条   ({what})")
            else:
                bad.append(f"{path} 返回 {r.status_code}({what})")
                print(f"   ❌ {path:<{width}}  {r.status_code}   ({what})")

        # ④ 建落地页要传的字段有没有变
        print("\n④ POST /api/landings 的字段")
        props = _post_body_props(spec, "/api/landings")
        if props is None:
            print("   ⚠️ swagger 里读不到,跳过")
        else:
            missing = LANDING_FIELDS - props
            extra = props - LANDING_FIELDS
            if missing:
                bad.append(f"建落地页缺了字段:{sorted(missing)}")
                print(f"   ❌ 少了:{sorted(missing)}")
            else:
                print(f"   ✅ {len(LANDING_FIELDS)} 个字段都在")
            if extra:
                print(f"   ℹ️ 平台新增了字段(不影响现有功能):{sorted(extra)}")

        # ⑤ 权重结构。A/B 分流全靠它,改名了我们就写不进去。
        print("\n⑤ flow 里的 A/B 权重结构")
        shape = _find_landers_path(c, key)
        if shape is None:
            print("   ⚠️ 账号里暂时没有「落地页+offer」类型的 path,查不了")
            print("      (不算失败:等有一条这种 campaign 时再跑一次)")
        elif shape is False:
            bad.append("找到了 landers_offers 的 path,但里面没有 landers[].weight —— 分流写不进去")
            print("   ❌ 结构变了:landers_offers 里找不到带 weight 的 landers")
        else:
            print(f"   ✅ destination=landers_offers,landers[{{id,weight}}] 还在")
            print(f"      (实测取到一条,landers {shape[0]} 个 / offers {shape[1]} 个)")

        # ⑥ swagger 说有、实际未必能用 —— 这条本身就是要守的结论
        print("\n⑥ swagger 和实际对不上的地方(已知,守着别悄悄变)")
        for path, expect in (("/api/campaigns", 404), ("/api/offers/list", 500)):
            r = _get(c, path, key)
            note = "仍然对不上(和记录一致)" if r.status_code == expect else "**变了**"
            print(f"   {'✅' if r.status_code == expect else 'ℹ️'} GET {path:<20} {r.status_code}  {note}")

    return _finish(bad)


def _post_body_props(spec: dict, path: str) -> set | None:
    try:
        schema = (spec["paths"][path]["post"]["requestBody"]["content"]
                  ["application/json"]["schema"])
    except (KeyError, TypeError):
        return None
    if "$ref" in schema:
        node = spec
        for seg in schema["$ref"].lstrip("#/").split("/"):
            node = node.get(seg, {})
        schema = node
    props = schema.get("properties")
    return set(props) if isinstance(props, dict) else None


def _find_landers_path(client: httpx.Client, key: str, scan: int = 30):
    """在账号里找一条真的用了落地页的 path,确认权重结构没变。

    返回 (落地页数, offer 数);找不到这种 path 返回 None;结构变了返回 False。
    """
    r = _get(client, "/api/campaigns/list", key)
    if r.status_code != 200:
        return None
    try:
        campaigns = r.json()
    except json.JSONDecodeError:
        return None
    for cam in campaigns[:scan]:
        flow_id = cam.get("flow_id")
        if not flow_id:
            continue
        fr = _get(client, f"/api/flows/{flow_id}", key)
        if fr.status_code != 200:
            continue
        try:
            flow = fr.json()
        except json.JSONDecodeError:
            continue
        for group in (flow.get("paths") or {}).values():
            if not isinstance(group, dict):
                continue
            for path in group.get("paths") or []:
                if path.get("destination") != "landers_offers":
                    continue
                block = path.get("landers_offers") or {}
                landers = block.get("landers")
                if not isinstance(landers, list) or not landers:
                    return False
                if "weight" not in landers[0] or "id" not in landers[0]:
                    return False
                return len(landers), len(block.get("offers") or [])
    return None


def _finish(bad: list[str]) -> int:
    print()
    if bad:
        print("❌ 下面这些和我们记录的事实对不上,**改代码前先看清楚**:")
        for b in bad:
            print(f"     · {b}")
        print("\n对照 CLAUDE.md 第六之十八节;确认平台真改了就顺手把那一节改掉。")
        return 1
    print("✅ 全部对得上,ClickFlare 那条链的前提都还成立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
