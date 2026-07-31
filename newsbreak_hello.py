"""
第一个 NewsBreak 实验脚本:带上钥匙,问 NewsBreak「我有哪些组织(organizations)」。

这是最简单的一次 API 调用,用来验证「带 Access-Token 喊 NewsBreak 窗口」这条路能走通。
写法照着 qx-ad-bot 的真实规矩来(只学不抄它的文件):
  - 窗口地址: https://business.newsbreak.com/business-api/v1
  - 钥匙放在请求头: Access-Token: <你的钥匙>
  - 回话里带 code 字段, code == 0 才算成功, 数据在 data.list 里

运行方法(在 my-agent 目录下):
    ./venv/bin/python newsbreak_hello.py
"""

from pathlib import Path

import httpx  # 发网络请求的工具, venv 里已经装好了

# NewsBreak 的「服务窗口」地址(和 qx-ad-bot 里用的一致)
NEWSBREAK_API_BASE = "https://business.newsbreak.com/business-api/v1"


def load_token() -> str:
    """从同目录的 .env 文件里读出钥匙。读不到就友好提示、正常退出。"""
    env_path = Path(__file__).with_name(".env")
    token = ""
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            # 跳过空行和以 # 开头的注释行
            if not line or line.startswith("#"):
                continue
            if line.startswith("NEWSBREAK_ACCESS_TOKEN="):
                token = line.split("=", 1)[1].strip()

    if not token:
        print("❌ 还没填钥匙。请打开 .env 文件,把 NEWSBREAK_ACCESS_TOKEN= 后面填上你的 access token。")
        print("   (在 NewsBreak Ad Manager 后台 Resources → API Access Tokens 里生成)")
        raise SystemExit(1)  # 正常退出,不是程序崩溃
    return token


def main() -> None:
    token = load_token()

    # 把钥匙别在请求头上,再对着「查组织」这个窗口喊一嗓子
    headers = {
        "Access-Token": token,
        "Content-Type": "application/json",
    }
    url = f"{NEWSBREAK_API_BASE}/org/admin-orgs"

    print(f"正在请求: {url} ...")
    with httpx.Client(timeout=30.0) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()  # 如果是 4xx/5xx 这种网络层错误, 这里会直接报出来
        payload = response.json()    # 把回话解析成 Python 字典

    # NewsBreak 的约定: code == 0 才算成功
    if payload.get("code") != 0:
        reason = payload.get("errMsg") or payload.get("message") or payload
        print(f"❌ NewsBreak 返回了错误: {reason}")
        raise SystemExit(1)

    orgs = (payload.get("data") or {}).get("list") or []
    print(f"✅ 成功!你名下有 {len(orgs)} 个组织:\n")
    for org in orgs:
        # 不同字段名都试一下, 拿到啥显示啥(真实数据字段名以 NewsBreak 返回为准)
        org_id = org.get("id") or org.get("orgId")
        name = org.get("name") or org.get("orgName") or "(未命名)"
        print(f"  - {name}  (id={org_id})")


if __name__ == "__main__":
    main()
