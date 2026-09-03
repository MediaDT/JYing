"""
账号系统 —— 登录、会话、以及"每个账号自己的聊天记录"。

安全要点(都是不能省的):
  1. **密码绝不明文存**:用 PBKDF2 加盐哈希(Python 自带,不用装东西)。
     即使 users.json 泄露,也反推不出原密码;
  2. **会话用随机长令牌**,存服务器;浏览器只拿一个 httponly cookie
     (httponly = 网页里的 JS 读不到它,防止被脚本偷走);
  3. **比密码用 compare_digest**:防止通过"猜多久报错"来试密码;
  4. 每个账号的聊天记录**各存各的文件**,互相看不到。

数据放在 data/ 目录:
  data/users.json          账号(用户名 / 盐 / 哈希,没有明文)
  data/sessions.json       登录会话(令牌 → 用户,重启不掉线)
  data/chats/<uid>.json    每个账号自己的聊天记录
"""

import hashlib
import json
import secrets
import threading
import time
import uuid
from pathlib import Path

_DIR = Path(__file__).with_name("data")
_CHATS = _DIR / "chats"
_USERS_FILE = _DIR / "users.json"
_SESSIONS_FILE = _DIR / "sessions.json"
_CREDS_FILE = _DIR / "creds.json"     # 每个人自己的投放平台凭据

SESSION_DAYS = 30           # 登录后多久要重新登录
_lock = threading.Lock()    # 多个请求同时写文件时上锁


def _ensure_dirs() -> None:
    _CHATS.mkdir(parents=True, exist_ok=True)


def _read(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception as e:
        print(f"[accounts] 读取 {path.name} 失败: {e}", flush=True)
    return default


def _write(path: Path, data) -> None:
    _ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(path)        # 先写临时文件再改名:中途断电也不会写出半个坏文件


# ============ 每个人自己的投放平台凭据 ============
#
# 结构:{user_id: {platform: {"token": "...", "account_id": "..."}}}
#
# 为什么按人存:token 一旦全局共享,A 绑好之后 B 登录进来就直接操作 A 的广告账户了。
# 团队共管同一批广告时那样也能用,但"谁都能动谁的账户"不是能默认的事。

def get_creds(user_id: str, platform: str = "newsbreak") -> dict:
    """读某人在某个平台的凭据。没有就返回空字典。"""
    if not user_id:
        return {}
    all_creds = _read(_CREDS_FILE, {})
    return dict((all_creds.get(user_id) or {}).get(platform) or {})


def set_creds(user_id: str, platform: str = "newsbreak", **fields) -> None:
    """写某人在某个平台的凭据(只覆盖传进来的字段,其余保留)。"""
    if not user_id:
        return
    with _lock:
        all_creds = _read(_CREDS_FILE, {})
        rec = all_creds.setdefault(user_id, {}).setdefault(platform, {})
        rec.update(fields)
        _write(_CREDS_FILE, all_creds)
        # 里面是真 token,只让文件属主读写
        try:
            _CREDS_FILE.chmod(0o600)
        except OSError:
            pass


def clear_creds(user_id: str, platform: str = "newsbreak") -> None:
    """解绑:把某人某个平台的凭据删掉。"""
    if not user_id:
        return
    with _lock:
        all_creds = _read(_CREDS_FILE, {})
        if user_id in all_creds:
            all_creds[user_id].pop(platform, None)
            _write(_CREDS_FILE, all_creds)


# ============ 密码 ============

def _hash_password(password: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256,20 万轮。慢是故意的——让暴力破解代价高。"""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def _valid_username(name: str) -> str:
    name = (name or "").strip()
    if len(name) < 2 or len(name) > 32:
        return "用户名长度要在 2~32 个字符之间"
    if any(c in name for c in "/\\:*?\"<>| \t\n"):
        return "用户名不能含空格和这些符号: / \\ : * ? \" < > |"
    return ""


# ============ 账号 ============

def list_users() -> dict:
    return _read(_USERS_FILE, {})


def user_count() -> int:
    return len(list_users())


def create_user(username: str, password: str) -> dict:
    """注册一个账号。返回 {"error": ...} 或 {"id": ..., "username": ...}"""
    err = _valid_username(username)
    if err:
        return {"error": err}
    if len(password or "") < 6:
        return {"error": "密码至少 6 位"}

    with _lock:
        users = list_users()
        if username.lower() in {u.lower() for u in users}:
            return {"error": f"用户名「{username}」已被占用,换一个"}
        salt = secrets.token_hex(16)
        uid = uuid.uuid4().hex[:12]
        users[username] = {
            "id": uid,
            "salt": salt,
            "hash": _hash_password(password, salt),
            "created_at": time.time(),
        }
        _write(_USERS_FILE, users)
    print(f"[accounts] 新账号: {username}", flush=True)
    return {"id": uid, "username": username}


def verify_user(username: str, password: str) -> dict:
    """校验账号密码。对就返回用户信息,不对返回 {"error": ...}"""
    users = list_users()
    # 用户名不区分大小写地找
    hit = next((u for u in users if u.lower() == (username or "").strip().lower()), None)
    if not hit:
        # 故意和"密码错"给同样的提示:不告诉攻击者哪个用户名存在
        return {"error": "用户名或密码不对"}
    rec = users[hit]
    calc = _hash_password(password or "", rec["salt"])
    if not secrets.compare_digest(calc, rec["hash"]):
        return {"error": "用户名或密码不对"}
    return {"id": rec["id"], "username": hit}


def change_password(username: str, old: str, new: str) -> dict:
    if len(new or "") < 6:
        return {"error": "新密码至少 6 位"}
    check = verify_user(username, old)
    if check.get("error"):
        return {"error": "原密码不对"}
    with _lock:
        users = list_users()
        salt = secrets.token_hex(16)
        users[username]["salt"] = salt
        users[username]["hash"] = _hash_password(new, salt)
        _write(_USERS_FILE, users)
    return {"ok": True}


# ============ 会话(登录状态)============

def _load_sessions() -> dict:
    sessions = _read(_SESSIONS_FILE, {})
    now = time.time()
    alive = {t: s for t, s in sessions.items() if s.get("expires", 0) > now}
    if len(alive) != len(sessions):
        _write(_SESSIONS_FILE, alive)      # 顺手清掉过期的
    return alive


def create_session(user: dict) -> str:
    """登录成功后发一张"门票"(随机长令牌)。"""
    token = secrets.token_urlsafe(32)
    with _lock:
        sessions = _load_sessions()
        sessions[token] = {
            "user_id": user["id"],
            "username": user["username"],
            "expires": time.time() + SESSION_DAYS * 86400,
        }
        _write(_SESSIONS_FILE, sessions)
    return token


def session_user(token: str) -> dict | None:
    """凭门票查是谁。过期或伪造的返回 None。"""
    if not token:
        return None
    s = _load_sessions().get(token)
    if not s:
        return None
    return {"id": s["user_id"], "username": s["username"]}


def destroy_session(token: str) -> None:
    if not token:
        return
    with _lock:
        sessions = _load_sessions()
        if sessions.pop(token, None) is not None:
            _write(_SESSIONS_FILE, sessions)


# ============ 每个账号自己的聊天记录 ============

MAX_CONVS = 50            # 每个账号最多留 50 段对话
MAX_MSGS = 400            # 每段对话最多 400 条消息


def _chat_file(user_id: str) -> Path:
    safe = "".join(c for c in user_id if c.isalnum())   # 防路径穿越
    return _CHATS / f"{safe}.json"


def load_chats(user_id: str) -> list:
    data = _read(_chat_file(user_id), [])
    return data if isinstance(data, list) else []


def save_chats(user_id: str, conversations: list) -> dict:
    """存这个账号的聊天记录。会做上限裁剪,防止文件无限长大。"""
    if not isinstance(conversations, list):
        return {"error": "格式不对,应该是一个列表"}
    trimmed = []
    for conv in conversations[:MAX_CONVS]:
        if not isinstance(conv, dict):
            continue
        msgs = conv.get("messages") or []
        trimmed.append({
            "id": str(conv.get("id") or uuid.uuid4().hex[:10]),
            "title": str(conv.get("title") or "")[:80],
            "mode": conv.get("mode") if conv.get("mode") in {"campaign", "creative", "landing"} else "campaign",
            "messages": msgs[-MAX_MSGS:] if isinstance(msgs, list) else [],
            "updatedAt": conv.get("updatedAt") or 0,
        })
    with _lock:
        _write(_chat_file(user_id), trimmed)
    return {"ok": True, "count": len(trimmed)}
