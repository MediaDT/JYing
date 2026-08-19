#!/usr/bin/env python
"""
竞品平台参数体检 —— 逐个试探哪些查询参数是真的生效的。

    ./venv/bin/python check_competitor_params.py

**为什么需要它**:OpenAdLibrary 有一批参数传了**不报错、也不生效**
(`sortBy`、`sort` 的多数值、`mediaType`),返回和不传一模一样 ——
比"传错就报错"难发现得多。而且平台会改:今天 503 的参数,明天可能修好了。

与其把结论写死在注释里慢慢过期,不如留一个能随时重跑的体检。
接了新平台、或者怀疑某个参数不对劲时,先跑这个。

判据是 **`total` 变没变**,不是看头几条 —— 踩过这个坑:`dateFrom` 会改变 total
(说明生效了),但前 5 条恰好没被筛掉,只看头几条会误判成"被忽略"。
"""

import sys

import openadlibrary_client as oal


def main() -> int:
    if not oal.is_configured():
        print("❌ 还没配置 OPENADLIBRARY_API_KEY")
        print(oal.HOWTO)
        return 1
    ok, msg = oal.validate()
    if not ok:
        print(f"❌ key 不可用:{msg}")
        return 1

    kw = sys.argv[1] if len(sys.argv) > 1 else "roof repair"
    print(f"用关键词「{kw}」体检中(每项一次请求,共十来次)…\n")
    r = oal.param_check(kw)
    print(f"基准结果数:{r['基准total']}\n")

    width = max(len(k) for k in r["结果"])
    ignored = []
    for name, verdict in r["结果"].items():
        print(f"  {name:<{width}}  {verdict}")
        if verdict.startswith("⚠️"):
            ignored.append(name)

    print()
    if ignored:
        print("⚠️ 下面这些参数传了没用、但也不报错,**别在代码里依赖它们**:")
        for n in ignored:
            print(f"     · {n}")
    print("\n对照 CLAUDE.md 第六之十二节的参数表;对不上就说明平台改了,顺手把文档改掉。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
