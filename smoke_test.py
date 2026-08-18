"""
冒烟测试 —— 改完代码跑一遍,确认核心功能没被改坏。

用法(在 my-agent 目录下):
    ./venv/bin/python smoke_test.py

它会做什么:
  ✅ 查询类:真的连 NewsBreak 查数据(只读,不改任何东西)
  ✅ 护栏类:用假 id 走一遍两阶段确认流程(不会真的改动广告)
  ✅ 纯逻辑:素材类型判断、校验规则等(不联网)
  ❌ 不会:创建广告、改状态、花钱、动你的真实投放

绿色 PASS 全过 = 可以放心提交。
"""

import sys
import traceback

PASSED, FAILED = [], []


def check(name: str, fn):
    """跑一个检查项,不管成败都继续往下跑,最后统一汇总。"""
    try:
        result = fn()
        if result is True or result is None:
            PASSED.append(name)
            print(f"  ✅ {name}")
        else:
            FAILED.append((name, str(result)))
            print(f"  ❌ {name} → {result}")
    except Exception as e:
        FAILED.append((name, f"{type(e).__name__}: {e}"))
        print(f"  ❌ {name} → {type(e).__name__}: {e}")
        if "-v" in sys.argv:
            traceback.print_exc()


# ============ 1. 纯逻辑检查(不联网,最快) ============

def test_pure_logic():
    import newsbreak_client as nb
    import platforms as plat

    print("\n【1】纯逻辑(不联网)")

    # 平台注册表:没写对接代码的平台绝不能标成能用,否则用户点进去踩空
    def t_platform_registry():
        rows = plat.public_list()
        if not rows:
            return "平台列表是空的"
        bad = []
        for p in rows:
            if p["status"] not in ("ready", "coming"):
                bad.append(f"{p['id']} 状态怪:{p['status']}")
            if p["status"] == "coming" and p["bound"]:
                bad.append(f"{p['id']} 还没对接却标成已绑定")
            for k in ("id", "name", "icon", "desc_zh", "desc_en"):
                if not p.get(k):
                    bad.append(f"{p['id']} 缺字段 {k}")
        if not plat.is_ready(plat.DEFAULT_ID):
            bad.append(f"默认平台 {plat.DEFAULT_ID} 不是 ready")
        if plat.get("查无此平台") is not None:
            bad.append("不存在的平台 id 居然查到了东西")
        return bad or True

    check("平台注册表自洽(未对接的不会假装能用)", t_platform_registry)

    # 递给 Gemini 的工具定义必须是合法 schema。Gemini 从函数签名自动生成,
    # 数组参数只写 `list` 会生成不出 items → 每次请求都 400,整条 Gemini 路径全废。
    # 这条不联网,纯查签名,但能挡住那类低级错误。
    def t_tool_schema_sane():
        import agent_server as srv
        import inspect
        import typing
        bad = []
        for fn in srv.NEWSBREAK_TOOLS:
            for pname, param in inspect.signature(fn).parameters.items():
                ann = param.annotation
                if ann is inspect.Parameter.empty:
                    bad.append(f"{fn.__name__}.{pname} 没写类型标注")
                    continue
                # 把 X | None 拆开,只看真正的类型
                args = [a for a in typing.get_args(ann) if a is not type(None)]
                real = args[0] if args else ann
                if real is list:
                    bad.append(f"{fn.__name__}.{pname} 标成了裸 list —— "
                               f"Gemini 生成不出 items,会 400。改成 list[dict] 之类")
                if real is dict:
                    bad.append(f"{fn.__name__}.{pname} 标成了裸 dict,同理会缺 properties")
        return bad or True

    check("递给 Gemini 的工具签名不会生成非法 schema", t_tool_schema_sane)

    # 素材查找:质量评分是纯算术,不联网,先把它测死。
    # 用户要的是"没有好素材时也能看出质量如何",所以差图必须被判成"不建议"而不是藏起来。
    def t_creative_quality():
        import creative_search as cs
        bad = []
        cases = [
            ((1200, 628), "推荐", "正好是平台建议尺寸"),
            ((1920, 1005), "推荐", "大图且比例接近 1.91:1"),
            ((400, 210), "不建议", "分辨率低于底线"),
            ((1200, 1600), "不建议", "竖图,信息流里会被裁掉大半"),
            ((1000, 1000), "可用", "正方形,能用但不是最优"),
            ((0, 0), "未知", "图库没给尺寸"),
        ]
        for (w, h), want, why in cases:
            got = cs._quality(w, h)["verdict"]
            if got != want:
                bad.append(f"{w}×{h}({why})判成「{got}」,应为「{want}」")
        # 必须说得出理由,不能只给个结论 —— 用户是小白,要看懂差在哪
        if not cs._quality(400, 210)["reasons"]:
            bad.append("判成不建议却说不出理由")
        return bad or True

    # 工具要在三个地方同时登记:Gemini 清单、OpenAI 的 JSON schema、OpenAI 的名字→函数表。
    # 少登记一处的表现是"某条大脑路径用不了这个工具",而且只在切到那条路径时才发作,
    # 平时测不出来。这条把三处对齐守死。
    def t_tools_registered_everywhere():
        import agent_server as srv
        gem = {fn.__name__ for fn in srv.NEWSBREAK_TOOLS}
        oai_fn = set(srv.OPENAI_TOOL_FUNCS)
        oai_schema = {t["function"]["name"] for t in srv.OPENAI_TOOL_SCHEMAS}
        bad = []
        if gem - oai_schema:
            bad.append(f"Gemini 有但 OpenAI schema 里没有:{sorted(gem - oai_schema)}")
        if oai_schema - gem:
            bad.append(f"OpenAI schema 有但 Gemini 清单里没有:{sorted(oai_schema - gem)}")
        if gem != oai_fn:
            bad.append(f"Gemini 清单和 OpenAI 函数表对不上:{sorted(gem ^ oai_fn)}")
        for name in ("search_stock_creatives", "use_stock_creative"):
            if name not in gem:
                bad.append(f"{name} 没登记")
            if name not in srv._TOOL_LABELS:
                bad.append(f"{name} 没有进度播报文案,用户会看到默认的「正在查数据…」")
        return bad or True

    # 防幻觉:AI 不能自己拼一个图片地址让系统去下载。
    # 这是本项目反复防的行为(见 CLAUDE.md「AI 幻觉执行」),必须守在代码层而不是提示词里。
    def t_stock_url_must_come_from_search():
        import agent_server as srv
        r = srv.use_stock_creative("https://evil.example.com/anything.jpg")
        if "error" not in r:
            return "编造的素材地址居然被接受了 —— 防幻觉这道闸没关上"
        return True if "搜索结果" in r["error"] else f"拦是拦了,但话说得不清楚:{r['error'][:60]}"

    # 素材来源的规矩改了(从"绝不许上网找"改成"只许从授权图库找"),
    # 中英两版提示词必须同步 —— 英文那版漏改过不止一次(见坑表)。
    def t_creative_rules_in_prompt():
        import agent_server as srv
        bad = []
        for lang, must in (("zh", ["search_stock_creatives", "use_stock_creative",
                                   "编素材链接", "许可证"]),
                           ("en", ["search_stock_creatives", "use_stock_creative",
                                   "inventing asset URLs", "license"])):
            p = srv._system_prompt_now(lang)
            for m in must:
                if m not in p:
                    bad.append(f"{lang} 提示词里缺「{m}」")
        return bad or True

    check("素材质量评分(差图要判成不建议,不能藏)", t_creative_quality)
    check("工具在 Gemini/OpenAI 三处都登记了", t_tools_registered_everywhere)
    check("编造的素材地址会被挡下(防幻觉)", t_stock_url_must_come_from_search)
    check("素材来源新规矩中英提示词都同步了", t_creative_rules_in_prompt)


    # 每人绑自己的 token:A 绑过之后 B 绝不能蹭到。这是安全边界,不能退化。
    def t_per_user_isolation():
        import accounts as acc
        import newsbreak_client as nb
        bad = []
        acc.set_creds("_smoke_A", "newsbreak", token="tok-A")
        try:
            if acc.get_creds("_smoke_B"):
                bad.append("B 居然读到了 A 的凭据")
            if not plat.is_bound("newsbreak", "_smoke_A"):
                bad.append("A 绑了却说没绑")
            if plat.is_bound("newsbreak", "_smoke_B"):
                bad.append("B 没绑却说绑了")
            if plat.is_bound("newsbreak"):
                bad.append("不传 user_id 时居然算已绑定")

            old = nb.CURRENT_CREDS.get()
            try:
                nb.CURRENT_CREDS.set({"token": "tok-A"})
                if nb._token() != "tok-A":
                    bad.append("用户上下文里没拿到他自己的 token")
                nb.CURRENT_CREDS.set({})          # 登录了但没绑
                try:
                    nb._token()
                    bad.append("没绑的人居然拿到了 token(偷偷回落 .env 了)")
                except nb.NewsBreakError:
                    pass
            finally:
                nb.CURRENT_CREDS.set(old)
        finally:
            acc.clear_creds("_smoke_A")
        return bad or True

    check("每人一份 token,互相蹭不到", t_per_user_isolation)

    def t_creative_type():
        cases = [
            ("a.png", "image/png", "IMAGE"),
            ("a.gif", "image/gif", "GIF"),
            ("a.mp4", "video/mp4", "VIDEO"),
            ("没有后缀", "video/mp4", "VIDEO"),   # MIME 优先于后缀
            ("a.gif", "", "GIF"),                # 没 MIME 时看后缀
            ("a.mkv", "", "VIDEO"),
        ]
        bad = [f"{f}/{m}→{nb.creative_type_of(f, m)}(应为{w})"
               for f, m, w in cases if nb.creative_type_of(f, m) != w]
        return "; ".join(bad) or True

    def t_report_level_guard():
        try:
            nb.get_report(level="不存在的层级")
            return "非法 level 居然没报错"
        except nb.NewsBreakError:
            return True

    def t_status_guard():
        try:
            nb.update_status("campaign", "1", "MAYBE")
            return "非法 status 居然没报错"
        except nb.NewsBreakError:
            return True

    check("素材类型判断(MIME 优先、后缀兜底)", t_creative_type)
    check("报表层级非法值会被拦", t_report_level_guard)
    check("开关状态非法值会被拦", t_status_guard)


# ============ 2. 建广告的校验规则(不联网,不会真建) ============

def test_validation():
    import agent_server as srv

    print("\n【2】建广告校验规则(只登记不执行)")

    base = dict(
        ad_account_id="123", keyword="test",
        landing_url="https://example.com", budget_dollars=20,
        headline="Test Headline", description="Test description here",
        asset_url="https://cdn.example.com/a.png",
    )

    def rejects(label, **override):
        def run():
            r = srv.propose_create_campaign(**{**base, **override})
            if "error" not in r:
                srv.cancel_action(r.get("action_id", ""))   # 误放行了,清理掉
                return "居然被放行了"
            return True
        check(label, run)

    rejects("预算低于 $10 被拒", budget_dollars=5)
    rejects("落地页不是链接被拒", landing_url="不是链接")
    rejects("素材地址无效被拒", asset_url="")
    rejects("关键词为空被拒", keyword="  ")
    rejects("预算类型非法被拒", budget_type="WEEKLY")

    # 预算可以不传(向导用默认值),但**必须如实标出来是系统定的**,
    # 否则用户不知道有东西是替他决定的 —— 那就成了偷偷替用户花钱
    def t_default_budget():
        no_budget = {k: v for k, v in base.items() if k != "budget_dollars"}
        no_budget["keyword"] = "smokedefault"
        srv._REQUEST_SEQ += 1
        r = srv.propose_create_campaign(**no_budget)
        if "error" in r:
            return f"不传预算居然被拒:{r['error']}"
        bad = []
        try:
            shown = r["pending"].get("日预算", "")
            if f"${srv.DEFAULT_BUDGET_DOLLARS:g}" not in shown:
                bad.append(f"没用默认预算:{shown}")
            if "默认" not in shown:
                bad.append(f"没标出这是默认值:{shown}")
            if not any("预算" in x for x in r.get("defaults_used", [])):
                bad.append("defaults_used 里没列出预算")
            if "默认值" not in r.get("note", ""):
                bad.append("note 没要求 AI 向用户说明默认值")
        finally:
            srv.cancel_action(r["action_id"])
        return bad or True

    def t_user_budget_wins():
        mine = {**base, "keyword": "smokemine", "budget_dollars": 50}
        srv._REQUEST_SEQ += 1
        r = srv.propose_create_campaign(**mine)
        if "error" in r:
            return f"用户指定预算被拒:{r['error']}"
        bad = []
        try:
            if "$50" not in r["pending"].get("日预算", ""):
                bad.append(f"没用用户给的 50:{r['pending'].get('日预算')}")
            if any("预算" in x for x in r.get("defaults_used", [])):
                bad.append("用户自己定的预算被当成了默认值")
        finally:
            srv.cancel_action(r["action_id"])
        return bad or True

    check("不传预算 → 用默认值,并如实标注是系统定的", t_default_budget)
    check("用户指定预算 → 照用,且不算默认值", t_user_budget_wins)

    # 命名规范:计划 NB-类型-年月日-NN,组 年月日-类型-NNN,广告 AD-年月日-类型-NNN。
    # 序号要从平台已有的名字往下排,不能每次都从 01 开始撞名。
    # 日期必须带年份(6 位),否则明年同月同日的序号会接着今年往下排。
    def t_naming_convention():
        bad = []
        if srv._type_word("roof") != "Roof":
            bad.append(f"类型词没规范化:{srv._type_word('roof')}")
        if srv._type_word("ROOF") != "Roof":
            bad.append("全大写没转成首字母大写")
        if srv._type_word("gutter修缮") != "Gutter":
            bad.append("非英文字符没剔掉")
        if srv._type_word("") != "Ad":
            bad.append("类型为空时没有兜底")

        got = srv._campaign_name("Roof", "260817", [])
        if got != "NB-Roof-260817-01":
            bad.append(f"首支计划名不对:{got}")
        got = srv._campaign_name("Roof", "260817",
                                 ["NB-Roof-260817-01", "NB-Roof-260817-02",
                                  "NB-Gutter-260817-05", "杂名"])
        if got != "NB-Roof-260817-03":
            bad.append(f"序号没接着已有的往下排:{got}(应为 03)")
        got = srv._campaign_name("Roof", "260817", ["nb-roof-260817-07"])   # 大小写不敏感
        if got != "NB-Roof-260817-08":
            bad.append(f"大小写不同的同名没算进去:{got}")
        # 年份要真的隔开:去年同月同日的计划不能算进今年的序号
        got = srv._campaign_name("Roof", "260817", ["NB-Roof-250817-09"])
        if got != "NB-Roof-260817-01":
            bad.append(f"去年的同月同日被算进来了:{got}(应为 01)")

        sn, an = srv._child_names("Roof", "260817")
        if sn != "260817-Roof-001":
            bad.append(f"广告组命名不对:{sn}")
        if an != "AD-260817-Roof-001":
            bad.append(f"广告命名不对:{an}(广告要带 AD- 前缀)")
        if sn == an:
            bad.append("广告组和广告同名 —— 按名字搜就分不出是哪一层")
        return bad or True

    def t_naming_end_to_end():
        """真走一遍登记,确认三个名字都按规范生成(不执行,只登记后撤掉)。"""
        srv._REQUEST_SEQ += 1
        r = srv.propose_create_campaign(
            ad_account_id=srv._default_ad_account_id(), keyword="smoketype",
            landing_url="https://example.com/x", headline="Smoke Headline",
            description="Smoke description here.", asset_url="https://cdn.example.com/a.png")
        if "error" in r:
            return f"登记失败:{r['error']}"
        bad = []
        try:
            import re as _re
            import scheduler as _sched
            ymd = _sched.now_beijing().strftime("%y%m%d")    # 命名按北京时间,不是 UTC
            p = r.get("pending") or {}
            if not _re.fullmatch(rf"NB-Smoketype-{ymd}-\d{{2}}", p.get("计划名", "")):
                bad.append(f"计划名不合规范:{p.get('计划名')}")
            if p.get("广告组名") != f"{ymd}-Smoketype-001":
                bad.append(f"广告组名不合规范:{p.get('广告组名')}")
            if p.get("广告名") != f"AD-{ymd}-Smoketype-001":
                bad.append(f"广告名不合规范:{p.get('广告名')}")
        finally:
            srv.cancel_action(r.get("action_id", ""))
        return bad or True

    def t_naming_uses_beijing_time():
        """命名的日期必须走北京时间,不能是 UTC。

        直接比"名字里的日期 == 今天"是守不住的:北京和 UTC 一天里有 16 小时是同一天,
        测试多半在那 16 小时里跑,用 UTC 也能过。所以这里把时钟换掉——
        换成一个北京和 UTC **必然不同天**的时刻(北京 03:00 = UTC 前一天 19:00),
        名字跟着北京走才算对。
        """
        from datetime import datetime
        real = srv.sched.now_beijing
        srv.sched.now_beijing = lambda: datetime(2030, 1, 2, 3, 0, tzinfo=srv.sched.BEIJING)
        srv._REQUEST_SEQ += 1
        r = None
        try:
            r = srv.propose_create_campaign(
                ad_account_id=srv._default_ad_account_id(), keyword="tzcheck",
                landing_url="https://example.com/x", headline="TZ Headline",
                description="TZ description here.", asset_url="https://cdn.example.com/a.png")
            if "error" in r:
                return f"登记失败:{r['error']}"
            got = (r.get("pending") or {}).get("计划名", "")
            if "300102" not in got:
                return (f"计划名用的不是北京时间:{got}"
                        f"(北京已是 2030-01-02,应含 300102;含 300101 说明还在用 UTC)")
            return True
        finally:
            srv.sched.now_beijing = real
            if r:
                srv.cancel_action(r.get("action_id", ""))

    def t_known_types():
        """已知类型要能从落地页认出来;认不出的必须留给用户定,不能瞎编。
        清单只在代码里存一份,提示词是注入的 —— 这条同时守住"两边不同步"。"""
        bad = []
        cases = {
            "https://x.com/roofing-repair": "Roof",
            "https://x.com/gutter-guards": "Gutter",
            "https://x.com/window-replacement": "Window",
            "https://x.com/bathroom-remodel": "Bathroom",
            "https://x.com/solar-panels": "",          # 对不上 → 空,交给用户定
        }
        for url, want in cases.items():
            got = srv.match_known_type(url)
            if got != want:
                bad.append(f"{url} → {got!r}(应为 {want!r})")
        # 清单必须真的注入进了提示词,否则改了代码 AI 还按老的来
        for lang in ("zh", "en"):
            p = srv._system_prompt_now(lang)
            for t in srv.KNOWN_AD_TYPES:
                if t not in p:
                    bad.append(f"{lang} 提示词里没有类型 {t}")
        return bad or True

    check("命名规范(类型词/序号往下排/三层格式)", t_naming_convention)
    check("已知类型能认出来,认不出的留给用户定", t_known_types)
    check("真走一遍登记,三个名字都按规范生成", t_naming_end_to_end)
    check("命名的日期走北京时间,不是 UTC", t_naming_uses_beijing_time)


# ============ 3. 写操作护栏(用假 id,不会真改) ============

def test_guardrail():
    import agent_server as srv

    print("\n【3】写操作护栏(假 id,不会动真广告)")

    srv._REQUEST_SEQ = 10_000
    r1 = srv.propose_status_change("campaign", "0", "OFF", name="冒烟测试用")
    aid = r1.get("action_id", "")

    def t_registered():
        return True if aid else f"登记失败: {r1}"

    def t_dedupe():
        r2 = srv.propose_status_change("campaign", "0", "OFF", name="冒烟测试用")
        return True if r2.get("action_id") == aid else "同样内容居然登记出了新编号(查重失效)"

    def t_fuse():
        r = srv.confirm_action(aid)
        return True if "保险丝" in str(r.get("error", "")) else f"保险丝没拦住: {r}"

    def t_listed():
        ids = [a["action_id"] for a in srv.list_pending_actions().get("pending_actions", [])]
        return True if aid in ids else "保险箱里查不到刚登记的待办"

    def t_prompt_injected():
        return True if aid in srv._system_prompt_now() else "保险箱现状没注入提示词(AI 会忘编号)"

    def t_stamp_liar():
        reply = srv._finalize("好消息!已成功创建!")["reply"]
        return True if "幻觉" in reply else "谎报没有被拆穿"

    def t_stamp_real():
        srv._EXECUTED_THIS_REQUEST.append({"id": "xxx", "ok": True, "detail": "{}"})
        reply = srv._finalize("搞定")["reply"]
        srv._EXECUTED_THIS_REQUEST.clear()
        return True if "系统核验" in reply else "真执行没有盖钢印"

    def t_cancel():
        return True if srv.cancel_action(aid).get("cancelled") else "取消失败"

    check("能登记待办并拿到编号", t_registered)
    check("相同内容不会重复登记", t_dedupe)
    check("同一条消息里执行会被保险丝拦住", t_fuse)
    check("保险箱能查到待办", t_listed)
    check("保险箱现状会注入提示词", t_prompt_injected)
    check("AI 谎报会被拆穿(⚠️ 标记)", t_stamp_liar)
    check("真执行会盖钢印(🔒 标记)", t_stamp_real)
    check("能取消待办", t_cancel)

    # 开启广告必须三层一起开 —— 只开 campaign 等于没开,用户会白等几天。
    # 这条是领域知识,代码层要守住,不能只靠提示词。
    def t_turn_on_needs_children():
        srv._REQUEST_SEQ += 1
        r = srv.propose_status_change("campaign", "999", "ON", name="只开一层")
        try:
            if "warning" not in r:
                return "只开 campaign 一层居然没警告"
            if "跑不起来" not in r["warning"]:
                return f"警告说得不清楚:{r['warning']}"
        finally:
            srv.cancel_action(r.get("action_id", ""))
        return True

    def t_batch_targets():
        srv._REQUEST_SEQ += 1
        r = srv.propose_status_change(
            "campaign", "c1", "ON", name="计划",
            extra_targets=[{"level": "ad_set", "id": "s1", "name": "组"},
                           {"level": "ad", "id": "a1", "name": "广告"},
                           {"level": "ad_set", "id": "s1", "name": "组"}])   # 重复的
        bad = []
        try:
            act = srv.PENDING_ACTIONS.get(r.get("action_id"), {})
            tg = act.get("targets", [])
            if len(tg) != 3:
                bad.append(f"应登记 3 个对象(重复的要去掉),实际 {len(tg)}")
            if not isinstance(r.get("pending"), list) or len(r["pending"]) != 3:
                bad.append("复述清单没把三条都列出来")
            if "warning" in r:
                bad.append("带了子对象却还在警告只开一层")
        finally:
            srv.cancel_action(r.get("action_id", ""))
        return bad or True

    def t_bad_target_rejected():
        srv._REQUEST_SEQ += 1
        r = srv.propose_status_change("campaign", "c9", "ON", name="x",
                                      extra_targets=[{"level": "怪层级", "id": "1"}])
        if "error" not in r:
            srv.cancel_action(r.get("action_id", ""))
            return "非法 level 居然被放行"
        return True

    def t_pause_no_warning():
        srv._REQUEST_SEQ += 1
        r = srv.propose_status_change("campaign", "888", "OFF", name="暂停一层")
        try:
            return True if "warning" not in r else "暂停单独一层不该警告(关计划底下自然都停)"
        finally:
            srv.cancel_action(r.get("action_id", ""))

    check("只开 campaign 一层会明确警告跑不起来", t_turn_on_needs_children)
    check("能一次登记多个对象(自动去重)", t_batch_targets)
    check("extra_targets 里的非法对象被拦", t_bad_target_rejected)
    check("暂停只关 campaign 不报警告", t_pause_no_warning)

    # 定时开启同样要三层一起开 —— 而且更要紧:到点没人盯着,
    # 只翻了一层的话第二天才发现一条都没跑
    def t_schedule_needs_children():
        srv._REQUEST_SEQ += 1
        r = srv.propose_schedule("daily", "09:00", "campaign", "777", "ON", name="只定一层")
        try:
            if "warning" not in r:
                return "定时只开 campaign 一层居然没警告"
            if "没人盯着" not in r["warning"]:
                return f"警告没说清风险:{r['warning']}"
        finally:
            srv.cancel_action(r.get("action_id", ""))
        return True

    def t_schedule_batch():
        srv._REQUEST_SEQ += 1
        r = srv.propose_schedule("daily", "09:30", "campaign", "c2", "ON", name="计划",
                                 extra_targets=[{"level": "ad_set", "id": "s2", "name": "组"},
                                                {"level": "ad", "id": "a2", "name": "广告"}])
        bad = []
        try:
            act = srv.PENDING_ACTIONS.get(r.get("action_id"), {})
            if len(act.get("targets", [])) != 3:
                bad.append(f"应登记 3 个对象,实际 {len(act.get('targets', []))}")
            if "warning" in r:
                bad.append("带了子对象却还在警告")
            if "组" not in r.get("pending", ""):
                bad.append("复述里没列出要一起改的对象")
        finally:
            srv.cancel_action(r.get("action_id", ""))
        return bad or True

    def t_old_task_still_runs():
        """老任务存档里没有 targets 字段 —— 不能因此就跑不了(兼容性)。"""
        calls = []
        real = srv.nb.update_status
        srv.nb.update_status = lambda lv, oid, st: calls.append((lv, oid, st)) or {"ok": True}
        try:
            r = srv._scheduled_execute("campaign", "old-1", "ON")          # 老格式:没有 targets
            if not r.get("ok"):
                return f"老任务执行失败:{r}"
            if calls != [("campaign", "old-1", "ON")]:
                return f"老任务改的对象不对:{calls}"
            calls.clear()
            srv._scheduled_execute("campaign", "c3", "ON", "", [           # 新格式:三层
                {"level": "campaign", "id": "c3", "name": "计划"},
                {"level": "ad_set", "id": "s3", "name": "组"},
                {"level": "ad", "id": "a3", "name": "广告"}])
            if len(calls) != 3:
                return f"新任务应改 3 个对象,实际 {len(calls)}:{calls}"
        finally:
            srv.nb.update_status = real
        return True

    check("定时只开 campaign 一层会警告", t_schedule_needs_children)
    check("定时任务能一次开三层", t_schedule_batch)
    check("老定时任务(没 targets)仍能执行", t_old_task_still_runs)


# ============ 4. 真连 NewsBreak(只读) ============

def test_newsbreak_readonly():
    import agent_server as srv
    import newsbreak_client as nb

    print("\n【4】连 NewsBreak 查真数据(只读)")

    def t_orgs():
        return True if nb.list_organizations() else "查不到任何组织"

    def t_accounts_flat():
        accts = srv._all_ad_accounts()
        if not accts:
            return "查不到任何广告账户"
        a = accts[0]
        if not a.get("id") or a["id"] == a.get("group_id"):
            return f"账户 id 可能拿成了分组 id: {a}"
        return True

    def t_campaigns():
        acct = srv._default_ad_account_id()
        return True if "items" in nb.list_campaigns(acct) else "计划列表结构不对"

    def t_events():
        acct = srv._default_ad_account_id()
        return True if isinstance(nb.list_events(acct), list) else "转化事件列表结构不对"

    def t_report_fields():
        r = nb.get_report("campaign", start_date="2026-04-01")   # 保持在 180 天上限内
        if not r.get("rows"):
            return True   # 没数据也算通过(可能确实没投放)
        row = r["rows"][0]
        need = {"name", "id", "cost", "revenue", "roas", "clicks", "ctr", "conversions"}
        missing = need - set(row)
        return f"报表缺字段: {missing}" if missing else True

    check("能查到组织", t_orgs)
    check("账户列表已拍平(拿到真账户 id)", t_accounts_flat)
    check("能查广告计划列表", t_campaigns)
    check("能查转化事件列表", t_events)

    # 推荐素材:必须来自这个账户自己投过的广告(有数据背书、版权干净),
    # 而且要能直接拿去建广告 —— 所以 asset_url 得是平台域名下的真实地址
    def t_recommend_creatives():
        r = srv.recommend_creatives()
        if "error" in r:
            return f"推荐失败:{r['error']}"
        items = r.get("creatives", [])
        if not items:
            return True      # 账户没投过广告也算正常,如实返回空即可
        bad = []
        for c in items[:5]:
            u = c.get("asset_url", "")
            if not u.startswith("http"):
                bad.append(f"素材地址不像真的:{u[:40]}")
            if "particlenews.com" not in u and "newsbreak" not in u:
                bad.append(f"素材不是平台域名下的(可能是编的):{u[:60]}")
            if c.get("type") not in ("IMAGE", "VIDEO", "GIF"):
                bad.append(f"素材类型怪:{c.get('type')}")
            if u and srv._ASSET_TYPES.get(u) != c.get("type"):
                bad.append("类型没登记进 _ASSET_TYPES,复用时会判错图片/视频")
        if "不要编" not in r.get("note", ""):
            bad.append("note 没提醒 AI 别编造效果数据")
        return bad or True

    check("推荐素材来自本账户历史广告(地址真实、类型已登记)", t_recommend_creatives)

    # 素材查找的底线:**搜回来的每一张都必须允许商用**。
    # 这条不是形式主义 —— 实测过 Openverse 不加 license 过滤时,搜 roof 的
    # 第一条就是 by-nc-sa(NC = 禁止商用),拿去投广告就是侵权。
    # 所以过滤必须写死在请求里,这条测试守着它别哪天被人改掉。
    def t_stock_search_commercial_only():
        import creative_search as cs
        r = cs.search("roof repair", count=6)
        if r.get("error"):
            return f"搜索失败:{r['error']}"
        items = r.get("results", [])
        if not items:
            return True      # 图库当时没货也算正常,如实返回空即可
        bad = []
        SAFE = ("CC0", "PDM", "Pexels", "Pixabay")     # 这几种可商用且不要求署名
        for c in items:
            lic = c.get("license", "")
            if not lic.startswith(SAFE):
                bad.append(f"许可证不在可商用白名单里:{lic[:50]}")
            # NC = NonCommercial,ND = NoDerivatives,两种都不能用来投广告
            head = lic.split("(")[0].upper()
            if "NC" in head or "ND" in head:
                bad.append(f"混进了禁止商用/禁止改编的素材:{lic[:50]}")
            if not c.get("source_page"):
                bad.append("素材没有出处链接,没法查证")
            if not c.get("image_url", "").startswith("http"):
                bad.append(f"素材地址不像真的:{c.get('image_url', '')[:40]}")
            if c.get("quality", {}).get("verdict") not in ("推荐", "可用", "不建议", "未知"):
                bad.append(f"质量评价怪:{c.get('quality')}")
        if "许可证" not in r.get("note", "") if isinstance(r.get("note"), str) else False:
            bad.append("note 没要求 AI 把许可证讲给用户听")
        return bad or True

    check("图库素材全部可商用(带许可证和出处)", t_stock_search_commercial_only)

    def t_report_span_guard():
        try:
            nb.get_report("campaign", start_date="2025-01-01", end_date="2026-07-31")
            return "超过 180 天居然没被拦"
        except nb.NewsBreakError as e:
            return True if "180" in str(e) else f"拦是拦了,但提示不对: {e}"

    check("报表字段齐全(含名字/收入/ROAS)", t_report_fields)
    check("报表跨度超 180 天会被友好拦截", t_report_span_guard)


# ============ 5. HTTP 接口 ============

def test_http():
    from fastapi.testclient import TestClient
    import agent_server as srv

    print("\n【5】HTTP 接口")
    client = TestClient(srv.app)

    # 未登录时:页面应跳登录页、接口应 401 —— 这是登录门在起作用
    check("未登录访问首页会跳登录页",
          lambda: True if client.get("/", follow_redirects=False).status_code == 302
          else "没跳登录页,登录门可能失效了")
    check("未登录访问接口被拦",
          lambda: True if client.get("/api/dashboard", follow_redirects=False).status_code in (302, 401)
          else "接口没拦住")
    # 登录后直接敲根域名,要先去选平台 —— 不然用户根本没机会选,
    # 拿到的是写死默认平台的聊天页。用真账号的会话验,不污染数据。
    def t_root_goes_to_platforms():
        import accounts as acc
        users = acc.list_users()
        if not users:
            return True                      # 一个账号都没有,跳过
        uid = list(users.values())[0]["id"]
        tok = acc.create_session({"id": uid, "username": list(users)[0]})
        cli = TestClient(srv.app)
        cli.cookies.set(srv.SESSION_COOKIE, tok)
        try:
            r1 = cli.get("/", follow_redirects=False)
            r2 = cli.get("/?platform=newsbreak", follow_redirects=False)
            bad = []
            if r1.status_code != 302 or "/platforms" not in r1.headers.get("location", ""):
                bad.append(f"直接访问 / 应跳 /platforms,实际 {r1.status_code} {r1.headers.get('location')}")
            if r2.status_code != 200:
                bad.append(f"带了 ?platform= 应直接进聊天,实际 {r2.status_code}")
            return bad or True
        finally:
            acc.destroy_session(tok)

    check("登录后直接访问 / 会先去选平台", t_root_goes_to_platforms)

    check("未登录访问平台选择页会跳登录",
          lambda: True if client.get("/platforms", follow_redirects=False).status_code == 302
          else "平台页没设防")
    check("登录页本身可访问", lambda: True if client.get("/login").status_code == 200 else "登录页打不开")
    # 登录 cookie 的 secure 标志:https 下必须有(防明文抓包),http 下必须没有
    # (写死 True 会让本地 http://localhost 开发时浏览器直接不存 cookie,登不进去)
    def t_cookie_secure():
        from starlette.requests import Request
        from fastapi.responses import JSONResponse

        def fake_req(scheme):
            return Request({"type": "http", "scheme": scheme, "method": "GET",
                            "path": "/", "query_string": b"", "headers": [],
                            "server": ("testserver", 443 if scheme == "https" else 80)})

        bad = []
        for scheme, want in (("https", True), ("http", False)):
            resp = JSONResponse(content={})
            srv._set_session_cookie(resp, "dummy-token", fake_req(scheme))
            raw = resp.headers.get("set-cookie", "").lower()
            has = "secure" in raw
            if has != want:
                bad.append(f"{scheme} 下 secure={has}(应为 {want})")
            if "httponly" not in raw:
                bad.append(f"{scheme} 下丢了 httponly")
        return bad or True

    check("登录 cookie:https 加 secure、http 不加", t_cookie_secure)
    # 邀请码填中文/emoji 不能把服务搞成 500 —— compare_digest 不吃非 ASCII 字符串
    def t_invite_non_ascii():
        bad = []
        for bogus in ("错的码", "🔑", "wrong", ""):
            r = client.post("/api/register",
                            json={"username": "_smoke_probe", "password": "whatever12345",
                                  "invite": bogus})
            if r.status_code == 500:
                bad.append(f"邀请码「{bogus}」把服务搞崩了(500)")
            elif r.status_code == 200:
                bad.append(f"邀请码「{bogus}」居然放行了")
        return bad or True

    if srv._read_env_value("APP_PASSWORD"):
        check("邀请码填中文/emoji 也只是被拒,不会 500", t_invite_non_ascii)

    # 流式接口:必须是 SSE,而且要带上"别缓冲"的头,否则放在 nginx 后面
    # 会被攒成一坨再发,用户看到的还是转半天圈然后一次蹦出来。
    # 直接调函数(绕开登录门),并把大脑换成假的(不烧 AI 额度)。
    def t_stream_sse():
        orig = srv._route_brain
        srv._route_brain = lambda req: "冒烟测试的回答"
        try:
            resp = srv.chat_stream(srv.ChatRequest(
                messages=[srv.ChatMessage(role="user", content="hi")], lang="zh"))
            bad = []
            if resp.media_type != "text/event-stream":
                bad.append(f"不是 SSE:{resp.media_type}")
            if resp.headers.get("x-accel-buffering") != "no":
                bad.append("少了 X-Accel-Buffering: no(nginx 会把流缓冲住)")
            # StreamingResponse 会把同步生成器包成异步迭代器,这里手动收干
            import asyncio

            async def drain():
                out = []
                async for piece in resp.body_iterator:
                    out.append(piece if isinstance(piece, str) else piece.decode("utf-8"))
                return "".join(out)

            body = asyncio.new_event_loop().run_until_complete(drain())
            if "data: " not in body:
                bad.append("响应里没有 SSE 事件")
            if '"type": "done"' not in body.replace('"type":"done"', '"type": "done"'):
                bad.append(f"没有 done 事件:{body[:120]}")
            if "冒烟测试的回答" not in body:
                bad.append("done 里没带上回复正文")
            return bad or True
        finally:
            srv._route_brain = orig

    check("流式接口是 SSE、禁用代理缓冲、能吐出 done", t_stream_sse)

    check("Markdown 渲染库在位",
          lambda: True if client.get("/static/marked.min.js").status_code == 200 else "marked.min.js 丢了")

    # 登录后再验证页面和"接口文档不可访问"
    import accounts as acc
    had_users = acc.user_count() > 0
    if not had_users:
        client.post("/api/register", json={"username": "smoketest", "password": "smoke12345"})
    else:
        print("     (已有真实账号,跳过登录相关的页面检查)")

    if not had_users:
        check("登录后首页能打开",
              lambda: True if client.get("/").status_code == 200 else "首页打不开")
        # 文档必须"拿不到" —— 关掉时是 404,登录门先拦则是 302/401,都算安全
        for path in ("/docs", "/openapi.json"):
            check(f"接口文档不可访问({path})",
                  lambda p=path: True if client.get(p, follow_redirects=False).status_code in (404, 302, 401)
                  else f"{p} 还能打开")
        check("每个账号的聊天记录是独立的",
              lambda: True if client.get("/api/chats").json().get("conversations") == [] else "新账号不该有记录")
        check("登录后平台选择页能打开",
              lambda: True if client.get("/platforms").status_code == 200 else "平台页打不开")
        # 清理测试账号,不留痕
        import shutil, pathlib
        users = acc.list_users()
        if list(users) == ["smoketest"]:
            shutil.rmtree(pathlib.Path(__file__).with_name("data"), ignore_errors=True)


if __name__ == "__main__":
    print("=" * 60)
    print("广告投放小助手 · 冒烟测试(只读,不会改动真实广告)")
    print("=" * 60)

    test_pure_logic()
    test_validation()
    test_guardrail()
    test_newsbreak_readonly()
    test_http()

    print("\n" + "=" * 60)
    print(f"结果:{len(PASSED)} 通过 / {len(FAILED)} 失败")
    if FAILED:
        print("\n失败项:")
        for name, why in FAILED:
            print(f"  ❌ {name}\n     {why}")
        sys.exit(1)
    print("✅ 全部通过,可以放心提交")
