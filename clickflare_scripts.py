"""ClickFlare Lander Tracking Script 脚本库（按人存）。

**为什么能这么做**（2026-09-03 拿两份真脚本实测出来的）：
同一个追踪域名下，不管建几个 Lander、路径怎么排，ClickFlare 给出的
Tracking Script 都是**逐字节相同的同一份文件**——脚本里根本没有 lander id，
它靠上报 `lpurl`（页面自己的网址）区分是哪个落地页。
两段脚本之间唯一会变的，只有里面写死的那个追踪域名：

    A 版 2718 字节 / C 版 2704 字节，差 14 = 两个域名的长度差；
    把 C 里的域名换回 A 的域名后，md5 完全相同。

所以每个追踪域名只需要用户贴一次，以后同域名的发布直接复用。

**为什么不做成「把脚本抄进代码里当模板，只换域名」**：那等于**我们自己生成追踪代码**。
ClickFlare 哪天改版（加参数、换上报格式），我们会照旧发老版本，
数据静默出错而页面一切正常——正是这个项目反复踩的那类故障。
这里存的是用户给的**真脚本原文**，一个字都不改，只是省掉重复粘贴。

**按人存**：脚本属于某个人的 ClickFlare 工作区，会带出他的 campaign 归属，
不是公共资源（和 OPENADLIBRARY_API_KEY 那种全公司一份的东西不同）。
键里必须带 user_id——见 CLAUDE.md 坑表「缓存键忘了带用户 = 数据串号」。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

import cloudflare_pages as cfp
import scheduler

ROOT = Path(__file__).parent
STORE = ROOT / "data" / "clickflare_scripts.json"
_TIME_FMT = "%Y-%m-%d %H:%M"
# 超过这么久就提醒用户回 ClickFlare 核对一次。
# 脚本本身不会过期，但平台可能改版——存着的那份不会自己更新。
STALE_DAYS = 180
# 每人最多存这么多个追踪域名，防止文件无限长大（和 _ASSET_TYPES 上限同理）。
MAX_DOMAINS = 40
_lock = threading.Lock()


def _key(user_id: str) -> str:
    """按人隔离；不在用户上下文（命令行 / 测试）时落到 _local。"""
    return str(user_id or "").strip() or "_local"


def _read() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STORE)
    try:
        os.chmod(STORE, 0o600)      # 和 data/creds.json 一样，别让同机器上别的用户读到
    except OSError:
        pass


def _now() -> str:
    return scheduler.now_beijing().strftime(_TIME_FMT)


def age_days(saved_at: str) -> int | None:
    """存了多少天；时间戳读不出来就返回 None（不猜）。"""
    try:
        then = datetime.strptime(str(saved_at or ""), _TIME_FMT)
    except (TypeError, ValueError):
        return None
    return max(0, (scheduler.now_beijing().replace(tzinfo=None) - then).days)


def _stale_note(saved_at: str) -> str:
    days = age_days(saved_at)
    if days is None or days < STALE_DAYS:
        return ""
    return (f"⚠️ 这段脚本是 {days} 天前存的。ClickFlare 若改过版，"
            "这里存的就是旧代码——建议回后台复制一份新的覆盖掉。")


def remember(user_id: str, tracking_script: str) -> dict:
    """按脚本**自己的**追踪域名存起来。

    索引用脚本里提取出来的域名，不用调用方传进来的——这样存进去的东西
    天然和它的键一致，以后按域名取出来的必然是对的那一段。
    认不出域名时不存（`stored: False`），照常可以本次直接用。
    """
    script = str(tracking_script or "").strip()
    if not script or "<script" not in script.lower() or "</script>" not in script.lower():
        return {"stored": False, "reason": "不是完整的 <script> 片段，没有存。"}
    domain = cfp.tracking_domain_of(script)
    if not domain:
        hosts = cfp.script_hosts(script)
        return {"stored": False, "domain": "",
                "reason": ("脚本里认不出唯一的追踪域名"
                           + (f"（找到 {len(hosts)} 个：{'、'.join(hosts)}）" if hosts else "（一个都没找到）")
                           + "，这次直接用，但没有存进脚本库——下次还要再贴一遍。")}
    with _lock:
        data = _read()
        mine = dict(data.get(_key(user_id)) or {})
        old = mine.get(domain) or {}
        same = str(old.get("script") or "") == script
        # `saved_at` 的意思是「最后一次确认这段脚本是当前版本」,所以**每次贴都刷新**,
        # 哪怕内容一模一样。原来「内容相同就保留旧时间」会让过期提醒**永远消不掉**:
        # 提醒叫你回后台复制一份新的,而平台没改版时复制回来的就是同一段。
        saved_at = _now()
        mine[domain] = {"script": script, "saved_at": saved_at,
                        "last_used_at": old.get("last_used_at") or ""}
        if len(mine) > MAX_DOMAINS:
            # 丢最久没用过的。两个坑(都是实测撞出来的):
            # ① **刚存进来的这条绝不能算进候选** —— 它 `last_used_at` 是空的,
            #    排序会把它排在最前面 → 刚存就被淘汰,接着读它直接 KeyError,
            #    而且落盘的已经是「没有这条」的版本,重贴多少次都是同样的错;
            # ② 没用过的条目要拿**存入时间**当"最近使用",否则空串排最前,
            #    反而先淘汰新来的、把真正最老的留下来(淘汰顺序整个是反的)。
            others = [(k, v) for k, v in mine.items() if k != domain]
            others.sort(key=lambda kv: (kv[1].get("last_used_at") or kv[1].get("saved_at") or "",
                                        kv[1].get("saved_at") or ""))
            for dead, _ in others[:len(mine) - MAX_DOMAINS]:
                mine.pop(dead, None)
        data[_key(user_id)] = mine
        _write(data)
    return {"stored": True, "domain": domain,
            "action": "已存在脚本库里（内容相同）" if same and old else
                      ("已更新脚本库里这个域名的脚本" if old else "已存进脚本库"),
            "saved_at": saved_at}


def lookup(user_id: str, domain: str) -> dict | None:
    """取某个追踪域名存过的脚本；没有就返回 None。只读，不写。"""
    domain = str(domain or "").strip().lower()
    entry = (_read().get(_key(user_id)) or {}).get(domain)
    if not isinstance(entry, dict) or not entry.get("script"):
        return None
    return {"domain": domain, "script": entry["script"],
            "saved_at": entry.get("saved_at") or "",
            "stale_note": _stale_note(entry.get("saved_at") or "")}


def touch(user_id: str, domain: str) -> None:
    """记一次「这个域名的脚本又被用了」，方便用户看哪些还在用。"""
    domain = str(domain or "").strip().lower()
    with _lock:
        data = _read()
        mine = data.get(_key(user_id)) or {}
        if domain in mine:
            mine[domain]["last_used_at"] = _now()
            data[_key(user_id)] = mine
            _write(data)


def listing(user_id: str) -> list[dict]:
    """这个人存过哪些追踪域名（**不回显脚本原文**）。"""
    mine = _read().get(_key(user_id)) or {}
    out = []
    for domain, entry in sorted(mine.items()):
        if not isinstance(entry, dict):
            continue
        saved = entry.get("saved_at") or ""
        out.append({"追踪域名": domain, "存于": saved,
                    "最近使用": entry.get("last_used_at") or "还没用过",
                    **({"提醒": _stale_note(saved)} if _stale_note(saved) else {})})
    return out


def forget(user_id: str, domain: str) -> bool:
    domain = str(domain or "").strip().lower()
    with _lock:
        data = _read()
        mine = data.get(_key(user_id)) or {}
        if domain not in mine:
            return False
        mine.pop(domain)
        data[_key(user_id)] = mine
        _write(data)
    return True
