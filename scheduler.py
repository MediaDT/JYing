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
        # 同一个对象 + 同一个动作 + 同一个时间点 = 同一个任务,不重复登记
        for t in tasks:
            if (t["object_id"] == task["object_id"] and t["status"] == task["status"]
                    and t["kind"] == task["kind"] and t["when"] == task["when"]
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


def list_tasks() -> list[dict]:
    """列出所有任务(含已结束的),给用户看的字段都换算好了。"""
    out = []
    for t in load_tasks():
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


def cancel_task(task_id: str) -> dict:
    with _lock:
        tasks = load_tasks()
        for t in tasks:
            if t["task_id"] == task_id:
                if t["state"] != "active":
                    return {"cancelled": False, "note": f"任务 {task_id} 当前状态是「{t['state']}」,无需取消"}
                t["state"] = "cancelled"
                save_tasks(tasks)
                print(f"[schedule] 取消任务 {task_id}", flush=True)
                return {"cancelled": True, "task_id": task_id}
    return {"cancelled": False, "error": f"找不到任务 {task_id}"}


# ============ 后台执行 ============

# 最近执行过的定时任务(供聊天时主动汇报给用户),只留最近 5 条
recent_runs: list[str] = []


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
                recent_runs.append(msg)
                if t["kind"] == "daily":
                    nxt, _ = parse_when("daily", t["when"])   # 跳到下一次
                    t["next_at"] = nxt.isoformat() if nxt else ""
                else:
                    t["state"] = "missed"
                changed = True
                continue

            # 到点了,执行
            try:
                result = execute_fn(t["level"], t["object_id"], t["status"])
                ok = not (isinstance(result, dict) and result.get("error"))
                detail = str(result.get("error"))[:150] if not ok else "成功"
            except Exception as e:
                ok, detail = False, str(e)[:150]

            t["last_result"] = f"{now.strftime('%m-%d %H:%M')} {'成功' if ok else '失败:' + detail}"
            msg = f"⏰ 定时任务 {t['task_id']}:{label} → {'✅ 成功' if ok else '❌ 失败(' + detail + ')'}"
            print(f"[schedule] {msg}", flush=True)
            recent_runs.append(msg)

            if t["kind"] == "daily":
                # 每天重复的任务:失败一次不停掉,排下一次继续试(结果已记在 last_result 里)
                nxt, _ = parse_when("daily", t["when"])
                t["next_at"] = nxt.isoformat() if nxt else ""
            else:
                t["state"] = "done" if ok else "failed"
            changed = True

        if changed:
            save_tasks(tasks)
    del recent_runs[:-5]      # 只留最近 5 条


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
