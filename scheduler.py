"""
定时任务模块 —— 到点自动开启/暂停广告(行话叫 dayparting,分时段投放)。

设计要点(都是为了"到点自动花钱"这件事的安全性):
  1. 任务存盘(scheduled_tasks.json),服务器重启不丢;
  2. **错过的任务不偷偷补跑** —— 服务器关了几小时再开,不会突然开始烧钱,
     只标记成"错过"并留日志,等用户自己决定;
  3. 每次执行都写日志 + 记录结果,用户随时可查;
  4. 时间一律按「北京时间」理解(用户说几点就是他那边的几点),
     但对外展示时同时给出美东时间,方便和 NewsBreak 后台对照。

时间表示:任务里存的是"北京时间的墙上时钟"(如 09:00),
执行判断时换算成 UTC 比较,避免夏令时/时区混乱。
"""

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
US_EAST = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

_TASKS_FILE = Path(__file__).with_name("scheduled_tasks.json")

# 错过多久就不再补跑(分钟)。超过这个时间的任务只标记"错过",绝不自动执行——
# 免得服务器停机一晚,第二天一开机把广告全打开、无人看管地烧钱。
MISS_TOLERANCE_MIN = 15

_lock = threading.Lock()        # 存盘/改任务时上锁,避免定时线程和网页请求打架
_worker_started = False


# ============ 存取 ============

def load_tasks() -> list[dict]:
    try:
        if _TASKS_FILE.exists():
            data = json.loads(_TASKS_FILE.read_text())
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[schedule] 读取任务文件失败: {e}", flush=True)
    return []


def save_tasks(tasks: list[dict]) -> None:
    try:
        _TASKS_FILE.write_text(json.dumps(tasks, ensure_ascii=False, indent=1))
    except Exception as e:
        print(f"[schedule] 保存任务文件失败: {e}", flush=True)


# ============ 时间计算 ============

def now_beijing() -> datetime:
    return datetime.now(BEIJING)


def both_times(dt_beijing: datetime) -> str:
    """同一时刻的两种说法,避免用户和平台后台对不上。"""
    return (f"{dt_beijing.strftime('%Y-%m-%d %H:%M')}(北京)"
            f" / {dt_beijing.astimezone(US_EAST).strftime('%m-%d %H:%M')}(美东)")


def parse_when(kind: str, when: str) -> tuple[datetime | None, str]:
    """把用户说的时间解析成"下一次该执行的北京时间"。

    kind="once":  when = "YYYY-MM-DD HH:MM"(北京时间)
    kind="daily": when = "HH:MM"(每天这个点,北京时间)
    返回 (下次执行时刻, 错误说明);解析失败则时刻为 None。
    """
    now = now_beijing()
    try:
        if kind == "once":
            dt = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING)
            if dt <= now:
                return None, f"这个时间已经过去了({both_times(dt)}),请给一个将来的时间"
            return dt, ""
        if kind == "daily":
            hh, mm = when.strip().split(":")
            hh, mm = int(hh), int(mm)
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                return None, "时间要在 00:00 ~ 23:59 之间"
            dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)      # 今天这个点过了,从明天开始
            return dt, ""
    except Exception:
        pass
    return None, ('时间格式不对。一次性任务用 "2026-08-06 09:00",'
                  '每天重复用 "09:00"(都按北京时间)')


# ============ 增删查 ============

def add_task(task: dict) -> dict:
    """登记一个定时任务。task 需含:kind/when/level/object_id/name/status。"""
    next_at, err = parse_when(task["kind"], task["when"])
    if not next_at:
        return {"error": err}
    with _lock:
        tasks = load_tasks()
        # 同一个人 + 同一个对象 + 同一个动作 + 同一个时间点 = 同一个任务,不重复登记。
        # **`user_id` 这一项不能少**:少了的话 A 和 B 想定同一件事会被归并成一条,
        # B 拿到的是 A 的 task_id —— 然后 `list_tasks` 里看不见它、`cancel_task`
        # 说「是别的账号登记的」,他被卡在一个自己完全无法理解的死胡同里。
        # 和保险箱 `_find_duplicate` 少了 user_id 是同一个洞。
        for t in tasks:
            if (t["object_id"] == task["object_id"] and t["status"] == task["status"]
                    and t["kind"] == task["kind"] and t["when"] == task["when"]
                    and str(t.get("user_id") or "") == str(task.get("user_id") or "")
                    and t.get("state") == "active"):
                return {"task_id": t["task_id"], "duplicate": True,
                        "note": f"这个定时任务已经存在(编号 {t['task_id']}),无需重复添加"}
        import uuid
        task_id = uuid.uuid4().hex[:6]
        tasks.append({
            **task,
            "task_id": task_id,
            "state": "active",
            "next_at": next_at.isoformat(),
            "last_result": "",
            "created_at": now_beijing().isoformat(),
        })
        save_tasks(tasks)
    verb = "开启" if task["status"] == "ON" else "暂停"
    when_desc = ("每天 " + task["when"]) if task["kind"] == "daily" else both_times(next_at)
    print(f"[schedule] 新增任务 {task_id}: {when_desc} {verb} {task.get('name') or task['object_id']}", flush=True)
    return {"task_id": task_id, "next_run": both_times(next_at), "when_desc": when_desc}


def _mine(task: dict, user_id: str) -> bool:
    """这条任务归不归当前这个人管?

    判据和 `confirm_action` 的归属检查**故意保持一致**(宽松那一版):
      · 不在用户上下文(`user_id` 为空:看表线程 / 命令行 / 测试)→ 全都算;
      · 任务上没有归属(按人隔离之前登记的老任务)→ 也算,否则老任务谁都管不了;
      · 两边都有且不相等 → **不是他的**。
    """
    owner = str(task.get("user_id") or "")
    return (not user_id) or (not owner) or owner == user_id


def list_tasks(user_id: str = "") -> list[dict]:
    """列出任务(含已结束的),给用户看的字段都换算好了。

    **`user_id` 非空就只列他自己的。** 原来是无条件列全部 —— 而任务里带着
    对象名(`gutter-0805` 这类),等于把别人在投什么、什么时候开关**直接摊给了他**。
    定时任务本来就是按人存的(第六之四节),漏的只是读这一头。
    """
    out = []
    for t in load_tasks():
        if not _mine(t, user_id):
            continue
        next_at = datetime.fromisoformat(t["next_at"]) if t.get("next_at") else None
        out.append({
            "task_id": t["task_id"],
            "动作": ("开启" if t["status"] == "ON" else "暂停") + " " + t["level"],
            "对象": t.get("name") or t["object_id"],
            "对象id": t["object_id"],
            "时间": ("每天 " + t["when"]) if t["kind"] == "daily" else t["when"] + "(北京)",
            "下次执行": both_times(next_at) if next_at and t["state"] == "active" else "-",
            "状态": {"active": "生效中", "done": "已完成", "cancelled": "已取消",
                     "missed": "已错过(未执行)", "failed": "上次失败"}.get(t["state"], t["state"]),
            "上次结果": t.get("last_result") or "-",
        })
    return out


def cancel_task(task_id: str, user_id: str = "") -> dict:
    """取消一个定时任务。**别人的任务碰不得**(判据见 `_mine`)。

    原来一个 id 就能取消任何人的任务,而且取消完只回一句「cancelled: True」——
    取消错了对方毫无察觉,到点广告没开,几天后才发现。所以现在:
      ①查归属;②**把取消掉的是什么原样报出来**,让用户当场看得出取错没取错。
    """
    with _lock:
        tasks = load_tasks()
        for t in tasks:
            if t["task_id"] == task_id:
                if not _mine(t, user_id):
                    return {"cancelled": False,
                            "error": f"任务 {task_id} 是别的账号登记的,不能取消。"}
                if t["state"] != "active":
                    return {"cancelled": False, "note": f"任务 {task_id} 当前状态是「{t['state']}」,无需取消"}
                t["state"] = "cancelled"
                save_tasks(tasks)
                print(f"[schedule] 取消任务 {task_id}", flush=True)
                verb = "开启" if t["status"] == "ON" else "暂停"
                when = ("每天 " + t["when"]) if t["kind"] == "daily" else t["when"] + "(北京)"
                return {"cancelled": True, "task_id": task_id,
                        "取消掉的是": f"{when} {verb} {t['level']}「{t.get('name') or t['object_id']}」",
                        "note": "**请把「取消掉的是」原样念给用户核对一遍** —— "
                                "取消错了他不会有任何察觉,到点广告没开,几天后才发现。"}
    return {"cancelled": False, "error": f"找不到任务 {task_id}"}


# ============ 后台执行 ============

# 最近执行过的定时任务(供聊天时主动汇报给用户),**每人各留最近 5 条**。
#
# 原来这是一串**纯字符串**、全局一份,而每条消息里都带着计划名
# (`⏰ 定时任务 07b614:开启 campaign「paa-0814」→ ✅ 成功`),
# 又被 `_system_prompt_now` 无过滤地拼进**每个人**的提示词,后面还跟着一句
# 「若用户还不知道,主动告知一句」—— 于是 A 在投什么、什么时候开关,
# B 一登录就被 AI 主动念给他听。现在每条记下是谁的,读的时候按人筛。
RECENT_RUNS_PER_USER = 5
recent_runs: list[dict] = []


def _note_run(task: dict, msg: str) -> None:
    """记一条执行结果,并按人裁到上限。"""
    recent_runs.append({"user_id": str(task.get("user_id") or ""), "msg": msg})


def _trim_runs() -> None:
    """**每人各留最近 5 条**,不是全局留 5 条 —— 全局留的话,A 的任务多跑几次
    就把 B 的执行结果挤没了,B 再也看不到自己那条到底跑没跑。
    (和素材登记表「200 条上限是每人一份」是同一条。)
    """
    kept: dict[str, int] = {}
    out = []
    for item in reversed(recent_runs):
        who = item.get("user_id") or ""
        if kept.get(who, 0) >= RECENT_RUNS_PER_USER:
            continue
        kept[who] = kept.get(who, 0) + 1
        out.append(item)
    out.reverse()
    recent_runs[:] = out


def runs_for(user_id: str = "") -> list[str]:
    """这个人自己的定时任务执行结果。判据和 `_mine` 同一把尺子:
    不在用户上下文 → 全给;记录没有归属(老的)→ 也给;否则只给他自己的。
    """
    return [x["msg"] for x in recent_runs if _mine(x, user_id)]


def _run_due_tasks(execute_fn) -> None:
    """检查并执行到点的任务。execute_fn(level, object_id, status) 由主程序注入。"""
    now = now_beijing()
    with _lock:
        tasks = load_tasks()
        changed = False

        for t in tasks:
            if t.get("state") != "active" or not t.get("next_at"):
                continue
            next_at = datetime.fromisoformat(t["next_at"])
            if now < next_at:
                continue

            late_min = (now - next_at).total_seconds() / 60
            verb = "开启" if t["status"] == "ON" else "暂停"
            label = f"{verb} {t['level']}「{t.get('name') or t['object_id']}」"

            # 错过太久:不补跑,只记账。宁可不动,也不能无人看管地开始花钱。
            if late_min > MISS_TOLERANCE_MIN:
                msg = f"⏰ 定时任务 {t['task_id']}({label})原定 {both_times(next_at)}," \
                      f"但服务当时没运行(迟了 {int(late_min)} 分钟),**没有执行**"
                print(f"[schedule] {msg}", flush=True)
                _note_run(t, msg)
                if t["kind"] == "daily":
                    nxt, _ = parse_when("daily", t["when"])   # 跳到下一次
                    t["next_at"] = nxt.isoformat() if nxt else ""
                else:
                    t["state"] = "missed"
                changed = True
                continue

            # 到点了,执行
            try:
                # 带上「当初是谁登记的」—— 按人隔离之后,要用他自己的凭据去执行;
                # 以及「当初选了要一起改哪些对象」—— 开启广告要三层一起开,
                # 只翻 campaign 一层的话到点了广告照样不投。
                result = execute_fn(t["level"], t["object_id"], t["status"],
                                    t.get("user_id", ""), t.get("targets"))
                ok = not (isinstance(result, dict) and result.get("error"))
                detail = str(result.get("error"))[:150] if not ok else "成功"
            except Exception as e:
                ok, detail = False, str(e)[:150]

            t["last_result"] = f"{now.strftime('%m-%d %H:%M')} {'成功' if ok else '失败:' + detail}"
            msg = f"⏰ 定时任务 {t['task_id']}:{label} → {'✅ 成功' if ok else '❌ 失败(' + detail + ')'}"
            print(f"[schedule] {msg}", flush=True)
            _note_run(t, msg)

            if t["kind"] == "daily":
                # 每天重复的任务:失败一次不停掉,排下一次继续试(结果已记在 last_result 里)
                nxt, _ = parse_when("daily", t["when"])
                t["next_at"] = nxt.isoformat() if nxt else ""
            else:
                t["state"] = "done" if ok else "failed"
            changed = True

        if changed:
            save_tasks(tasks)
    _trim_runs()              # 每人各留最近 5 条


def start_worker(execute_fn) -> None:
    """启动后台看表线程(每 30 秒查一次有没有到点的任务)。"""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    def loop():
        print("[schedule] 定时任务看表线程已启动(每 30 秒检查一次)", flush=True)
        while True:
            try:
                _run_due_tasks(execute_fn)
            except Exception as e:
                print(f"[schedule] 检查任务时出错(不影响下一轮): {e}", flush=True)
            time.sleep(30)

    threading.Thread(target=loop, daemon=True, name="scheduler").start()
