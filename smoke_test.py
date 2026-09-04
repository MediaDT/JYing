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


def _read_file(path: str) -> str:
    import pathlib
    return pathlib.Path(path).read_text()


def _no_comments(src: str) -> str:
    """读源码做判断前先剥注释 —— 注释里正当地写着这些名字,直接搜会误命中(踩过多次)。"""
    return "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))


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
        for name in ("search_stock_creatives", "search_competitor_ads", "use_found_creative",
                     "decompose_creative", "summarize_creative_patterns"):
            if name not in gem:
                bad.append(f"{name} 没登记")
            if name not in srv._TOOL_LABELS:
                bad.append(f"{name} 没有进度播报文案,用户会看到默认的「正在查数据…」")
        return bad or True

    # 防幻觉:AI 不能自己拼一个图片地址让系统去下载。
    # 这是本项目反复防的行为(见 CLAUDE.md「AI 幻觉执行」),必须守在代码层而不是提示词里。
    def t_stock_url_must_come_from_search():
        import agent_server as srv
        r = srv.use_found_creative("https://evil.example.com/anything.jpg")
        if "error" not in r:
            return "编造的素材地址居然被接受了 —— 防幻觉这道闸没关上"
        return True if "搜索结果" in r["error"] else f"拦是拦了,但话说得不清楚:{r['error'][:60]}"

    # 素材来源的规矩改了(从"绝不许上网找"改成"只许从授权图库找"),
    # 中英两版提示词必须同步 —— 英文那版漏改过不止一次(见坑表)。
    def t_creative_rules_in_prompt():
        import agent_server as srv
        bad = []
        for lang, must in (("zh", ["search_stock_creatives", "search_competitor_ads", "use_found_creative",
                                   "decompose_creative", "summarize_creative_patterns",
                                   "编素材链接", "许可证"]),
                           ("en", ["search_stock_creatives", "search_competitor_ads",
                                   "decompose_creative", "summarize_creative_patterns",
                                   "use_found_creative", "inventing asset URLs", "license"])):
            p = srv._system_prompt_now(lang)
            for m in must:
                if m not in p:
                    bad.append(f"{lang} 提示词里缺「{m}」")
        return bad or True

    check("素材质量评分(差图要判成不建议,不能藏)", t_creative_quality)
    check("工具在 Gemini/OpenAI 三处都登记了", t_tools_registered_everywhere)
    check("编造的素材地址会被挡下(防幻觉)", t_stock_url_must_come_from_search)
    check("素材来源新规矩中英提示词都同步了", t_creative_rules_in_prompt)

    # 竞品广告查询(OpenAdLibrary)。这家是正规 API key,不会像上一家那样几小时过期,
    # 但**参数真相和文档对不上**,下面几条守的就是实测出来的那些值。
    def t_competitor_params_verified():
        import openadlibrary_client as oal
        bad = []
        # 实测:sort 只有 placements / oldest 真的有效,别的(包括整个 sortBy 参数)
        # 都是**静默忽略** —— 不报错、也不排序,比直接报错更难发现。
        # 暴露一个用不了的排序选项比不暴露更糟,所以这里守住只留验证过的。
        if set(oal.SORTS) != {"placements", "oldest"}:
            bad.append(f"排序选项被改了:{sorted(oal.SORTS)}(实测只有 placements/oldest 有效)")
        # 实测 minDaysRunning 稳定返回 503,是平台服务端的问题。
        # 哪天有人看文档觉得"这参数好用"又加回来,这条会提醒他。
        # 查的是"有没有真的发给平台",不是"有没有提到" —— 注释和文档里提它是应该的
        # (要写明为什么不用),所以先把 docstring 剥掉再查。
        import inspect
        src = inspect.getsource(oal.search)
        body = src.split('"""')[2] if src.count('"""') >= 2 else src
        # 注释里也会正当地提到它(要写清为什么不用),同样得剥掉再查 ——
        # 只剥 docstring 不够,踩过一次了
        body = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))
        for banned in ("minDaysRunning", "lastSeenFrom", "lastSeenTo"):
            if banned in body:
                bad.append(f"又把 {banned} 发给平台了 —— 实测它稳定 503,要自己在本地算/筛")
        # 投放天数是自己算的,算错就整条功能没意义
        if oal._days_running("2026-01-01T00:00:00Z", "2026-06-30T00:00:00Z") != 180:
            bad.append("投放天数算错了")
        if oal._days_running("", "") is not None:
            bad.append("缺日期时没有如实返回 None")
        return bad or True

    def t_competitor_normalize():
        """字段映射是实测确认的。两个点最容易坏:
        ①imageUrl 是相对路径,不拼域名就是死链;②竞品素材必须带风险标记。"""
        import openadlibrary_client as oal
        fake = {"id": "x1", "headline": "Roof Ad", "body": "Free estimate today",
                "imageUrl": "/api/public/assets/abc.webp", "adNetwork": "Taboola",
                "trafficSource": "news.example.com", "advertiserName": "SomeCo",
                "landingDomain": "someco.com", "placements": 17, "isActive": True,
                "firstSeenAt": "2026-01-01T00:00:00Z", "lastSeenAt": "2026-06-30T00:00:00Z"}
        n = oal._normalize(fake)
        bad = []
        if not n["image_url"].startswith("https://"):
            bad.append(f"相对路径没拼成完整地址:{n['image_url']}")
        if "⚠️" not in n["license"]:
            bad.append("竞品素材没有被标出风险,会被当成和图库图一样安全")
        if n["投放天数"] != 180:
            bad.append(f"投放天数算错:{n['投放天数']}")
        if n["版位数"] != 17 or n["还在投"] is not True:
            bad.append(f"版位数/在投状态没取对:{n['版位数']} {n['还在投']}")
        # 平台是 leak-safe 设计,约六成广告没有广告主和落地页。没有就留空,不许编
        empty = oal._normalize({"headline": "x", "imageUrl": "/a.webp"})
        if empty["落地页域名"] != "":
            bad.append("平台没给落地页时居然编了一个")
        return bad or True

    def t_landing_search_is_lightweight():
        """落地页搜索不能再回到 50 条×3 页；全文索引会因此稳定超时/503。"""
        import openadlibrary_client as oal
        seen = {}
        original = oal.raw_search
        oal._LANDING_SEARCH_CACHE.clear()
        def fake(**params):
            seen.update(params)
            return {"total": 1, "data": [{
                "id": "a", "headline": "Roof repair nearby", "imageUrl": "/a.webp",
                "landingDomain": "roof.example", "placements": 9, "geos": ["US"],
                "isActive": True, "firstSeenAt": "2026-01-01T00:00:00Z",
                "lastSeenAt": "2026-02-01T00:00:00Z"}]}
        try:
            oal.raw_search = fake
            result = oal.search_landing_candidates("roof repair", count=8, country="US")
        finally:
            oal.raw_search = original
            oal._LANDING_SEARCH_CACHE.clear()
        bad = []
        if seen.get("scope") != "adtext" or seen.get("page") != 1:
            bad.append(f"没有限定 adtext/单页:{seen}")
        if int(seen.get("pageSize") or 99) > 16:
            bad.append(f"候选池又开太大:{seen.get('pageSize')}")
        if not result.get("results"):
            bad.append("轻量搜索没有保留相关结果")
        return bad or True

    def t_landing_search_retries_503():
        """精确全文搜索 503 时应自动缩小查询，不应把第三方错误直接丢给聊天。"""
        import openadlibrary_client as oal
        calls = []
        original = oal.raw_search
        oal._LANDING_SEARCH_CACHE.clear()
        def fake(**params):
            calls.append(dict(params))
            if len(calls) == 1:
                raise oal.OpenAdLibraryError("OpenAdLibrary 暂时不可用(HTTP 503)")
            return {"total": 1, "data": [{
                "id": "b", "headline": "Roof repair estimate", "imageUrl": "/b.webp",
                "landingDomain": "retry.example", "placements": 7, "geos": ["US"],
                "isActive": True, "firstSeenAt": "2026-01-01T00:00:00Z",
                "lastSeenAt": "2026-02-01T00:00:00Z"}]}
        try:
            oal.raw_search = fake
            result = oal.search_landing_candidates("roof repair", count=8, country="US")
        finally:
            oal.raw_search = original
            oal._LANDING_SEARCH_CACHE.clear()
        bad = []
        if len(calls) != 2:
            bad.append(f"503 后没有且只重试一次:{calls}")
        if len(calls) > 1 and (calls[1].get("search") != "roof" or calls[1].get("pageSize") != 4):
            bad.append(f"降级查询没有缩小:{calls[1]}")
        if not result.get("results") or "自动降级" not in result.get("数据状态", ""):
            bad.append(f"重试成功后没有返回候选/状态:{result}")
        return bad or True

    def t_landing_search_has_last_resort():
        """常规和主词查询都 503 时，还有最小精确查询这一级兜底。"""
        import openadlibrary_client as oal
        calls = []
        original = oal.raw_search
        oal._LANDING_SEARCH_CACHE.clear()
        def fake(**params):
            calls.append(dict(params))
            if len(calls) < 3:
                raise oal.OpenAdLibraryError("OpenAdLibrary 暂时不可用(HTTP 503)")
            return {"total": 1, "data": [{
                "id": "c", "headline": "Roof repair quote", "imageUrl": "/c.webp",
                "landingDomain": "last.example", "placements": 4, "geos": ["US"]}]}
        try:
            oal.raw_search = fake
            result = oal.search_landing_candidates("roof repair", count=8, country="US")
        finally:
            oal.raw_search = original
            oal._LANDING_SEARCH_CACHE.clear()
        bad = []
        if len(calls) != 3 or calls[-1].get("search") != "roof repair" or calls[-1].get("pageSize") != 1:
            bad.append(f"最后一级不是最小精确查询:{calls}")
        if not result.get("results") or "最小精确查询" not in result.get("数据状态", ""):
            bad.append(f"最后一级成功后未正常返回:{result}")
        return bad or True

    check("竞品 API 参数仍是实测可用口径", t_competitor_params_verified)
    check("竞品字段映射没有把域名/素材编错", t_competitor_normalize)
    check("落地页搜索限制为 adtext 单页小候选池", t_landing_search_is_lightweight)
    check("落地页搜索遇到 503 会缩小查询重试", t_landing_search_retries_503)
    check("落地页搜索连续 503 仍有最小查询兜底", t_landing_search_has_last_resort)

    def t_cloudflare_landing_template():
        """CTA 与追踪脚本必须由代码精确注入，不能让模型猜地址或改脚本。"""
        import cloudflare_pages as cfp
        source = ('<html><body><a href="[[CLICKFLARE_CTA_URL]]">Go</a>'
                  '<a href="[[CLICKFLARE_CTA_URL]]">Again</a>'
                  '<!--[[CLICKFLARE_LANDER_SCRIPT]]--></body></html>')
        script = '<script src="https://track.example/cf/lander.js"></script>'
        out, count = cfp.inject_tracking(source, "https://track.example/cf/click/1", script)
        bad = []
        if count != 2 or out.count("https://track.example/cf/click/1") != 2:
            bad.append("没有把所有 CTA 精确替换")
        if script not in out or "[[CLICKFLARE" in out or "<!--<script" in out:
            bad.append("追踪脚本没有正确替换注释占位符")
        try:
            cfp.inject_tracking("<html></html>", "https://track.example/cf/click/1", script)
            bad.append("缺少 CTA 占位符的旧页面居然允许发布")
        except cfp.CloudflarePagesError:
            pass
        return bad or True

    def t_cloudflare_domain_projects_are_dynamic():
        """项目名按域名稳定生成；换域名就换项目，而不是读一个固定 LANDING_DOMAIN。"""
        import cloudflare_pages as cfp
        first = cfp.project_name_for_domain("roof.example.com")
        same = cfp.project_name_for_domain("https://roof.example.com/")
        other = cfp.project_name_for_domain("solar.example.com")
        bad = []
        if first != same:
            bad.append("同一域名没有复用同一个项目名")
        if first == other:
            bad.append("不同域名生成了同一个项目名")
        if len(first) > 58 or not first.startswith("landing-"):
            bad.append(f"Pages 项目名不合法:{first}")
        return bad or True

    def t_cloudflare_publish_confirmation_gate():
        """发布会创建项目/改域名，必须隔一条用户消息确认，且脚本不能回显。"""
        import uuid
        import agent_server as srv
        import cloudflare_pages as cfp
        uid = "smoke-cloudflare"
        suffix = uuid.uuid4().hex[:8]
        names = [f"{suffix}-a.html", f"{suffix}-b.html"]
        # A/B 两版必须真的不一样(两版逐字节相同的话根本不是 A/B,已被代码层拦住)
        source = ('<html><body><h1>{h}</h1><a href="[[CLICKFLARE_CTA_URL]]">Go</a>'
                  '<!--[[CLICKFLARE_LANDER_SCRIPT]]--></body></html>')
        sources = [source.format(h="Save on energy"), source.format(h="Done in one day")]
        paths = [srv.lp.GENERATED_DIR / name for name in names]
        original_list, original_publish, original_zone = cfp.list_resources, cfp.publish_ab, cfp.owned_zone
        original_risk, original_conflict = cfp.replace_risk, cfp.domain_conflict
        # 这条测试会走到 cfs.remember() —— 不改指向的话它会写进**用户真实的**脚本库,
        # 跑一次留一条 smoke-cloudflare 的垃圾在那儿(实测留下了)。
        import clickflare_scripts as cfs
        import tempfile as _tf
        from pathlib import Path as _P
        original_store = cfs.STORE
        _tmpdir = _tf.TemporaryDirectory()
        cfs.STORE = _P(_tmpdir.name) / "scripts.json"
        user_token = srv.CURRENT_USER_ID.set(uid)
        mode_token = srv.CURRENT_CHAT_MODE.set("landing")
        seq_token = srv.CURRENT_SEQ.set(81001)
        aid = ""
        try:
            for path, body in zip(paths, sources):
                path.write_text(body, encoding="utf-8")
            srv._LANDING_PAGES_BY_USER[uid] = [{"file": names[0]}, {"file": names[1]}]
            cfp.list_resources = lambda *args, **kwargs: {
                "zones": [{"name": "example.com"}], "projects": [],
                "domain_project_mappings": {}}
            cfp.owned_zone = lambda domain: "example.com"
            # 这条测的是"二次确认"这道关,不联网查覆盖风险和域名占用(各有专门的测试)
            cfp.replace_risk = lambda d, s2: {"project": "landing-x", "project_has_content": False,
                                              "local_slugs": [], "known_releases": [],
                                              "risky": False, "missing_locally": []}
            cfp.domain_conflict = lambda d: {"domain": d, "zone": "example.com", "records": [],
                                             "already_ours": False, "conflict": False}
            cfp.publish_ab = lambda *args, **kwargs: {
                "done": True, "created": {"variant_a_url": "https://lp.example.com/test/a/",
                                             "variant_b_url": "https://lp.example.com/test/b/"}}
            proposed = srv.propose_publish_landing_pages(
                "lp.example.com", "test", "https://track.example/cf/click/1",
                '<script src="https://track.example/lander.js"></script>')
            aid = proposed.get("action_id", "")
            same_turn = srv.confirm_action(aid)
            pending = srv.list_pending_actions()
            srv.CURRENT_SEQ.set(81002)
            executed = srv.confirm_action(aid)
            bad = []
            # **提案里的 A/B 地址必须自带"现在打不开"** —— 原来键名是 "A版"/"B版",
            # 模型照着渲染成「A版 正式网址」,用户当场点过去拿到
            # ERR_NAME_NOT_RESOLVED(DNS 记录要等确认发布那一刻才建),以为出错了。
            # 把话写进键名里,模型就没法把它说成"正式网址"。线上实测踩到过。
            keys = " ".join((proposed.get("pending") or {}).keys())
            if "A版" in keys and "打不开" not in keys:
                bad.append("提案里的 A/B 地址没写明现在打不开,用户会去点")
            if "打不开" not in str(proposed.get("note") or ""):
                bad.append("note 没要求 AI 主动说明这两个地址现在打不开")
            if not aid or "保险丝" not in same_turn.get("error", ""):
                bad.append("同一条消息内发布没有被保险丝拦截")
            if "https://track.example/lander.js" in str(pending):
                bad.append("待办列表回显了完整 Tracking Script")
            if not executed.get("done") or aid in srv.PENDING_ACTIONS:
                bad.append(f"下一条确认没有执行成功:{executed}")
            return bad or True
        finally:
            cfp.list_resources, cfp.publish_ab, cfp.owned_zone = original_list, original_publish, original_zone
            cfp.replace_risk, cfp.domain_conflict = original_risk, original_conflict
            cfs.STORE = original_store
            _tmpdir.cleanup()
            if aid:
                srv.PENDING_ACTIONS.pop(aid, None)
                srv._save_actions()
            srv._LANDING_PAGES_BY_USER.pop(uid, None)
            for path in paths:
                path.unlink(missing_ok=True)
            srv.CURRENT_SEQ.reset(seq_token)
            srv.CURRENT_CHAT_MODE.reset(mode_token)
            srv.CURRENT_USER_ID.reset(user_token)

    check("Cloudflare 发布模板精确注入 ClickFlare", t_cloudflare_landing_template)
    check("Cloudflare Pages 项目按域名动态生成和复用", t_cloudflare_domain_projects_are_dynamic)
    check("Cloudflare A/B 发布经过二次确认保护", t_cloudflare_publish_confirmation_gate)

    # ===== 整站替换的护栏 =====
    # `wrangler pages deploy <目录>` 是**整站替换**,而那个目录在本地 data/ 里,
    # data/ 不进 git。它一丢(换机器/重装/没备份),项目名还能靠域名 hash 认回来,
    # 但本地没有历史实验目录 → 这一次部署会把线上所有旧实验删掉,
    # 而 ClickFlare 里的 Lander 还指着老地址 —— 买来的流量落到 404 上,钱照花。
    def t_replace_guard():
        import shutil
        import tempfile
        from pathlib import Path as _P
        import cloudflare_pages as cfp

        tmp = _P(tempfile.mkdtemp(prefix="lpguard-"))
        keep = (cfp.SITES_DIR, cfp.MAPPING_FILE, cfp._project_exists,
                cfp._ensure_project, cfp._ensure_domain, cfp._wrangler_deploy,
                cfp.owned_zone, cfp._api, cfp._guard_domain, cfp._project_has_content)
        try:
            cfp.SITES_DIR, cfp.MAPPING_FILE = tmp / "sites", tmp / "map.json"
            made, uploaded = set(), []
            cfp._api = lambda *a, **k: {"success": True, "result": []}
            cfp.owned_zone = lambda d: "example.com"
            cfp._guard_domain = lambda d: None      # 这条测的是覆盖风险,不是域名占用
            cfp._project_exists = lambda n: n in made
            cfp._project_has_content = lambda n: n in made and bool(uploaded)
            cfp._ensure_project = lambda n: (n not in made) and (made.add(n) or True)
            cfp._ensure_domain = lambda p, d: {"name": d, "status": "active"}
            cfp._wrangler_deploy = lambda d, p, s2: (
                uploaded.append(sorted(str(x.relative_to(d)) for x in _P(d).rglob("*.html")))
                or "https://x.pages.dev")

            page = ('<html><body><a href="[[CLICKFLARE_CTA_URL]]">go</a>'
                    '<!--[[CLICKFLARE_LANDER_SCRIPT]]--></body></html>')
            fa, fb = tmp / "a.html", tmp / "b.html"
            fa.write_text(page); fb.write_text(page)
            cta = "https://t.example.com/cf/click/1"
            scr = '<script src="https://t.example.com/l.js"></script>'

            cfp.publish_ab("lp.example.com", "one", fa, fb, cta, scr)
            cfp.publish_ab("lp.example.com", "two", fa, fb, cta, scr)
            if len([x for x in uploaded[-1] if "/" in x]) != 4:
                return f"同域名第二次发布把第一个实验弄丢了:{uploaded[-1]}"

            # 模拟 data/ 丢失:目录和映射都没了,但 Cloudflare 上项目还在
            shutil.rmtree(cfp.SITES_DIR)
            cfp.MAPPING_FILE.unlink()
            try:
                cfp.publish_ab("lp.example.com", "three", fa, fb, cta, scr)
                return "本地历史丢了还照发不误 —— 线上旧实验会被静默删掉"
            except cfp.CloudflarePagesError as e:
                if "整站替换" not in str(e):
                    return f"拦住了但没说清原因:{str(e)[:80]}"

            # 用户明确同意「覆盖发布」时要放行
            cfp.publish_ab("lp.example.com", "three", fa, fb, cta, scr, allow_replace=True)

            # 根路径:不许自动跳转、也不许把实验清单公开
            root = (cfp.SITES_DIR / next(iter(made)) / "index.html").read_text()
            if "http-equiv" in root.lower():
                return "根路径还在自动跳转(裸访问会算到 A 版头上,审核也只看到跳转页)"
            if "three" in root:
                return "根路径把在跑的实验清单公开了"
            return None
        finally:
            (cfp.SITES_DIR, cfp.MAPPING_FILE, cfp._project_exists, cfp._ensure_project,
             cfp._ensure_domain, cfp._wrangler_deploy, cfp.owned_zone, cfp._api,
             cfp._guard_domain, cfp._project_has_content) = keep
            shutil.rmtree(tmp, ignore_errors=True)
    check("本地历史丢失时拒绝整站覆盖(除非用户明确同意)", t_replace_guard)

    # 发布是**不可逆的外部写操作**:后端还在传、前端已经说"等太久了",
    # 用户很可能再确认一次 → 发布两遍。所以上传超时必须小于前端的 180 秒。
    def t_upload_timeout():
        import cloudflare_pages as cfp
        if cfp.UPLOAD_TIMEOUT_S >= 180:
            return f"上传超时 {cfp.UPLOAD_TIMEOUT_S}s 不小于前端的 180s,用户会以为失败而重复确认"
        src = _no_comments(_read_file("cloudflare_pages.py"))
        if "timeout=UPLOAD_TIMEOUT_S" not in src:
            return "常量定义了但没真的用上"
        return None
    check("发布上传超时小于前端等待上限", t_upload_timeout)

    # 提案阶段就要把覆盖风险查出来,和占位符检查一个道理:
    # 不能等用户点了头、真要上传时才发现"这一下会删掉线上的旧页面"
    def t_risk_checked_at_propose():
        import inspect
        import agent_server as srv
        src = _no_comments(inspect.getsource(srv.propose_publish_landing_pages))
        if "replace_risk" not in src:
            return "提案阶段没查覆盖风险"
        if "allow_replace" not in src:
            return "提案没有把用户的覆盖意图带下去"
        i_risk, i_reg = src.find("replace_risk"), src.find("PENDING_ACTIONS[action_id]")
        if i_reg >= 0 and i_risk > i_reg:
            return "先登记了待办才查风险,顺序反了"
        return None
    check("提案阶段就查出整站覆盖风险", t_risk_checked_at_propose)

    # 目标域名上已经有东西在跑时,绑定会**接管这个主机名**,现有页面当场下线。
    # 实测踩到:某根域名上有一条已代理的 A 记录,打开是一个在投的落地页;
    # 追踪子域名(CNAME 到 ClickFlare)更不能碰 —— 绑了等于把追踪打断。
    def t_domain_conflict_guard():
        import cloudflare_pages as cfp
        keep = (cfp._list_zones, cfp._api, cfp._project_exists, cfp._read_mappings)
        try:
            cfp._list_zones = lambda: [{"id": "z1", "name": "example.com"}]
            cfp._project_exists = lambda n: False
            cfp._read_mappings = lambda: {}
            records = {
                "example.com": [{"type": "A", "content": "1.2.3.4", "proxied": True}],
                "trk.example.com": [{"type": "CNAME", "content": "cname.tracker.com"}],
                "lp.example.com": [],
                "txt.example.com": [{"type": "TXT", "content": "v=spf1"}],
            }
            cfp._api = lambda m, path, **kw: {
                "success": True,
                "result": records.get((kw.get("params") or {}).get("name"), [])}

            # 用户自己先在后台把 CNAME 配好指向**本项目**,是个完全合理的操作 ——
            # 不能反过来告诉他"这个域名被占用了"。指向别的项目才算冲突。
            ours = cfp.project_name_for_domain("mine.example.com")
            records["mine.example.com"] = [{"type": "CNAME", "content": f"{ours}.pages.dev",
                                            "proxied": True}]
            records["other.example.com"] = [{"type": "CNAME", "content": "someone-else.pages.dev",
                                             "proxied": True}]
            for host, want in (("example.com", True), ("trk.example.com", True),
                               ("lp.example.com", False), ("txt.example.com", False),
                               ("mine.example.com", False), ("other.example.com", True)):
                got = cfp.domain_conflict(host)["conflict"]
                if got != want:
                    return (f"{host}: 期望 conflict={want} 实际 {got}"
                            + ("（TXT 不影响网页服务,不该算冲突）" if host.startswith("txt") else ""))
            try:
                cfp._guard_domain("example.com")
                return "根域名上有在跑的记录,竟然放行了 —— 会把现有页面弄下线"
            except cfp.CloudflarePagesError as e:
                if "下线" not in str(e) or "lp.example.com" not in str(e):
                    return f"拦住了但没说清后果/没给替代方案:{str(e)[:90]}"
            cfp._guard_domain("lp.example.com")      # 空着的子域名要放行
            return None
        finally:
            (cfp._list_zones, cfp._api, cfp._project_exists, cfp._read_mappings) = keep
    check("不抢占已经在服务的域名(会把现有页面弄下线)", t_domain_conflict_guard)

    # 「先把 Pages 项目手动建好」是个很自然的操作。空项目**没有任何内容可丢**,
    # 把它当成"线上有东西会被删"会白白拦住第一次发布。
    def t_empty_project_not_risky():
        import cloudflare_pages as cfp
        keep = (cfp._api, cfp._read_mappings)
        try:
            cfp._read_mappings = lambda: {}
            for latest, want_risky, label in ((None, False, "空项目(从没部署过)"),
                                              ({"id": "d1"}, True, "部署过东西的项目")):
                cfp._api = lambda m, p, **kw: {"success": True, "result": {"latest_deployment": latest}}
                got = cfp.replace_risk("lp.example.com", "exp1")["risky"]
                if got != want_risky:
                    return f"{label}: 期望 risky={want_risky} 实际 {got}"
            return None
        finally:
            (cfp._api, cfp._read_mappings) = keep
    check("空项目不算「会删掉线上内容」", t_empty_project_not_risky)

    # 三个地址最容易搞混(CLAUDE.md 里专门记着)。ClickFlare 的 CTA Click URL
    # **路径固定是 /cf/click/<数字>**,只有域名会变 —— 有了这个形状才挡得住误粘。
    # 粘错了页面看起来完全正常,要等数据不对劲才发现,那时钱已经花了。
    def t_cta_url_shape():
        import cloudflare_pages as cfp
        from urllib.parse import urlparse
        # 脚本里写死的追踪域名必然和它自己的 CTA 域名一致(实测过),
        # 所以这里按 URL 现造配套的脚本 —— 用固定域名的假脚本是不真实的数据。
        def script_for(u):
            host = urlparse(u).hostname or "t.example"
            return f'<script src="https://{host}/cf/lander.js"></script>'
        good = ["https://trk.example.com/cf/click/1",      # 标准
                "https://trk.example.com/cf/click/2",      # 第二个 offer
                "https://t.other.io/cf/click/1",           # 别的子域名前缀
                "https://trk.example.com/cf/click/1/"]     # 结尾多个斜杠
        bad = [("https://trk.example.com/abc?cpid=x", "Campaign Tracking URL"),
               ("https://trk.example.com/", "域名首页"),
               ("https://lp.example.com/exp/a/", "落地页地址")]
        for u in good:
            try:
                cfp.validate_clickflare(u, script_for(u))
            except Exception as e:
                return f"合法地址被拦了:{u} → {str(e)[:60]}"
        for u, label in bad:
            try:
                cfp.validate_clickflare(u, script_for(u))
                return f"误粘的{label}竟然通过了:{u}"
            except cfp.CloudflarePagesError as e:
                if "/cf/click/" not in str(e):
                    return f"拦住{label}但没说清正确格式"
        # 误粘 Campaign URL 时要点名说破,别让用户对着"格式不对"发呆
        try:
            cfp.validate_clickflare("https://trk.example.com/abc?cpid=x",
                                    script_for("https://trk.example.com/x"))
        except cfp.CloudflarePagesError as e:
            if "Campaign Tracking URL" not in str(e):
                return "没认出这是误粘了 Campaign Tracking URL"
        return None
    check("CTA 地址的形状要对(挡住三个地址搞混)", t_cta_url_shape)

    # 脚本里写死了追踪域名,而且它会把页面上 CTA 链接的域名**改写成自己的**
    # (实测:href 从 trk.a.com/cf/click/1 变成 track.b.com/cf/click/1)。
    # 所以两者对不上就等于把点击送去另一个追踪器,而页面一切正常、看不出来。
    def t_tracking_domain_match():
        import cloudflare_pages as cfp
        cta = "https://trk.mine.com/cf/click/1"
        ok = '<script>var c="https://trk.mine.com";</script>'
        bad = '<script>var c="https://track.other.com";</script>'
        try:
            cfp.validate_clickflare(cta, ok)
        except Exception as e:
            return f"配套的脚本被误拦:{str(e)[:70]}"
        try:
            cfp.validate_clickflare(cta, bad)
            return "追踪域名对不上竟然放行了 —— 点击会被送到另一个追踪器"
        except cfp.CloudflarePagesError as e:
            if "trk.mine.com" not in str(e) or "track.other.com" not in str(e):
                return "拦住了但没点名是哪两个域名对不上"
        # **只在有正面证据时拦**:提取不到主机名(平台改了格式)必须放行,
        # 否则 ClickFlare 一次改版就会把所有发布堵死。
        try:
            cfp.validate_clickflare(cta, "<script>console.log(1)</script>")
        except Exception as e:
            return f"认不出域名时不该拦:{str(e)[:70]}"
        if cfp.tracking_domain_of('<script>var a="https://x.com",b="https://y.com";</script>'):
            return "两个域名时应该认不出(返回空),不许挑一个当追踪域名"
        return None
    check("CTA 和追踪脚本必须同一个追踪域名", t_tracking_domain_match)

    # 实测:同一追踪域名下所有 Lander 的脚本**逐字节相同**(脚本里没有 lander id,
    # 靠上报 lpurl 区分),所以每个追踪域名只需用户贴一次。
    def t_script_registry():
        import clickflare_scripts as cfs
        import tempfile
        from pathlib import Path as _P
        real = cfs.STORE
        with tempfile.TemporaryDirectory() as d:
            cfs.STORE = _P(d) / "s.json"      # 别碰用户真实的脚本库
            try:
                s1 = '<script>var c="https://trk.one.com";/*a*/</script>'
                s2 = '<script>var c="https://trk.two.com";/*b*/</script>'
                r = cfs.remember("userA", s1)
                if not r.get("stored") or r.get("domain") != "trk.one.com":
                    return f"没按脚本自己的追踪域名存:{r}"
                got = cfs.lookup("userA", "trk.one.com")
                if not got or got["script"] != s1:
                    return "存进去和取出来的不是同一段"
                # 按人隔离:B 不该读到 A 存的东西(坑表:缓存键忘了带用户 = 数据串号)
                if cfs.lookup("userB", "trk.one.com") is not None:
                    return "另一个账号读到了别人存的脚本"
                cfs.remember("userB", s2)
                if cfs.lookup("userA", "trk.two.com") is not None:
                    return "A 读到了 B 存的脚本"
                # 清单里**绝不能**出现脚本原文
                text = str(cfs.listing("userA"))
                if s1 in text or "<script" in text:
                    return "清单把脚本原文回显出来了"
                if "trk.one.com" not in text:
                    return "清单没列出存过的追踪域名"
                # 认不出唯一域名的脚本不存,但要说清原因
                bad = cfs.remember("userA", '<script>var a="https://p.com",b="https://q.com";</script>')
                if bad.get("stored") or not bad.get("reason"):
                    return "认不出域名时不该存,而且要说明原因"
                # 重贴同样的脚本要**刷新时间** —— 否则过期提醒叫你去做的事做不掉它
                import clickflare_scripts as _c
                _real_now = _c._now          # 存下来还原,别 del(那会把函数整个删掉)
                _c._now = lambda: "2099-01-01 00:00"
                try:
                    refreshed = cfs.remember("userA", s1).get("saved_at")
                finally:
                    _c._now = _real_now
                if refreshed != "2099-01-01 00:00":
                    return "重贴同样的脚本没有刷新 saved_at,过期提醒永远消不掉"
                if not cfs.forget("userA", "trk.one.com") or cfs.lookup("userA", "trk.one.com"):
                    return "删不掉"
                # 存满之后再存一个:不许崩、新的要进得去、被丢掉的必须是最久没用的
                for i in range(cfs.MAX_DOMAINS):
                    dom = f"trk{i:02d}.full.com"
                    cfs.remember("userC", f'<script>var c="https://{dom}";</script>')
                    cfs.touch("userC", dom)
                try:
                    r = cfs.remember("userC", '<script>var c="https://trk-new.full.com";</script>')
                except Exception as e:
                    return f"存满之后再存一个直接崩了:{type(e).__name__} {e}"
                names = [x["追踪域名"] for x in cfs.listing("userC")]
                if not r.get("stored") or "trk-new.full.com" not in names:
                    return "存满之后新域名进不去(而且用户的脚本就这么丢了)"
                if len(names) > cfs.MAX_DOMAINS:
                    return "超过上限了"
                if "trk00.full.com" in names:
                    return "淘汰顺序反了:最久没用的还在,新来的反被丢掉"
            finally:
                cfs.STORE = real
        return None
    check("脚本库按人存、按追踪域名索引、不回显原文", t_script_registry)

    # 脚本不能出现在聊天和待办清单里。tracking_script_b 是后加的,
    # 加的时候漏了脱敏 —— 这条守着两个字段都被挡住。
    def t_pending_hides_scripts():
        import agent_server as srv
        aid = "__smoke_mask__"
        srv.PENDING_ACTIONS[aid] = {
            "type": "publish_landing_pages", "domain": "lp.example.com", "slug": "x",
            "cta_url": "https://trk.example.com/cf/click/1",
            "tracking_script": "<script>SECRET_A</script>",
            "tracking_script_b": "<script>SECRET_B</script>", "seq": 1}
        try:
            token = srv.CURRENT_CHAT_MODE.set("landing")
            try:
                text = str(srv.list_pending_actions())
            finally:
                srv.CURRENT_CHAT_MODE.reset(token)
            for secret in ("SECRET_A", "SECRET_B"):
                if secret in text:
                    return f"待办清单把脚本原文回显了({secret})"
            if "[已保存" not in text:
                return "没有标出脚本已保存"
        finally:
            srv.PENDING_ACTIONS.pop(aid, None)
        return None
    check("待办清单不回显 A/B 两段追踪脚本", t_pending_hides_scripts)

    # A/B 两版一模一样 = 花一周的钱跑同一个页面,分出来的「胜者」只是噪音,
    # 而且**从外面完全看不出来**(两个网址不同、两页都正常、数据照常上报)。
    def t_ab_must_differ():
        import agent_server as srv, landing_lab as lp, cloudflare_pages as cfp
        import clickflare_scripts as cfs, tempfile, inspect
        from pathlib import Path as _P
        sig = inspect.signature(srv.propose_publish_landing_pages)
        if sig.parameters["allow_identical"].default is not False:
            return "A/A 的口子默认必须是关着的"
        page = ('<!doctype html><html><head><title>{t}</title></head><body>'
                '<a href="[[CLICKFLARE_CTA_URL]]">Go</a>'
                '<!--[[CLICKFLARE_LANDER_SCRIPT]]--></body></html>')
        script = '<script>var c="https://trk.ab-test.com";</script>'
        cta = "https://trk.ab-test.com/cf/click/1"
        keep = (cfs.STORE, lp.GENERATED_DIR, cfp.MAPPING_FILE, cfp.SITES_DIR)
        with tempfile.TemporaryDirectory() as d:
            d = _P(d)
            cfs.STORE, cfp.MAPPING_FILE, cfp.SITES_DIR = d/"s.json", d/"m.json", d/"sites"
            lp.GENERATED_DIR = d/"gen"; lp.GENERATED_DIR.mkdir()
            tok = srv.CURRENT_USER_ID.set("__abtest__")
            try:
                # save_pages 一次固定只出两版(A/B 的设计),第三版要再调一次
                f = lp.save_pages([{"name": "A", "html": page.format(t="X")},
                                   {"name": "B", "html": page.format(t="X")}])
                f += lp.save_pages([{"name": "C", "html": page.format(t="Y")}])
                # 内容相同的两个文件
                r = srv.propose_publish_landing_pages("lp.example.com", "s1", cta, script,
                                                      f[0]["file"], f[1]["file"])
                if not r.get("error") or "噪音" not in r["error"]:
                    return f"两版内容一样竟然没拦:{str(r)[:120]}"
                # 同一个文件当 A 又当 B
                r = srv.propose_publish_landing_pages("lp.example.com", "s2", cta, script,
                                                      f[0]["file"], f[0]["file"])
                if not r.get("error"):
                    return "同一个文件当 A 又当 B 竟然没拦"
                # 两版真的不同 → 不该被这条拦住(拦住就成了画地为牢)
                r = srv.propose_publish_landing_pages("lp.example.com", "s3", cta, script,
                                                      f[0]["file"], f[2]["file"])
                if r.get("error") and "噪音" in r["error"]:
                    return "两版明明不同却被误拦"
                if r.get("action_id"):
                    srv.PENDING_ACTIONS.pop(r["action_id"], None); srv._save_actions()
            finally:
                srv.CURRENT_USER_ID.reset(tok)
                cfs.STORE, lp.GENERATED_DIR, cfp.MAPPING_FILE, cfp.SITES_DIR = keep
                srv._LANDING_PAGES_BY_USER.pop("__abtest__", None)
        return None
    check("A/B 两版内容不许一模一样(A/A 要用户明说)", t_ab_must_differ)

    # 保险箱是全进程共享的一份。confirm_action 早就查了归属,cancel_action 一直没查 ——
    # B 能把 A 登记好的待办删掉,A 那边只会看到「找不到待办」。
    def t_cancel_checks_owner():
        import agent_server as srv
        aid = "__smoke_owner__"
        srv.PENDING_ACTIONS[aid] = {"type": "status_change", "level": "campaign",
                                    "object_id": "x", "status": "ON",
                                    "user_id": "owner-a", "seq": 1}
        try:
            tok = srv.CURRENT_USER_ID.set("someone-b")
            try:
                r = srv.cancel_action(aid)
            finally:
                srv.CURRENT_USER_ID.reset(tok)
            if r.get("cancelled") or aid not in srv.PENDING_ACTIONS:
                return "别的账号把这个待办取消掉了"
            if "另一个账号" not in str(r.get("error", "")):
                return "拦住了但没说清原因"
            tok = srv.CURRENT_USER_ID.set("owner-a")
            try:
                if not srv.cancel_action(aid).get("cancelled"):
                    return "本人反而取消不了"
            finally:
                srv.CURRENT_USER_ID.reset(tok)
        finally:
            srv.PENDING_ACTIONS.pop(aid, None)
        return None
    check("待办只能由登记它的账号取消", t_cancel_checks_owner)

    # 测试跑完不许在用户真实的脚本库里留垃圾(实测留过 smoke-cloudflare 一条)
    def t_no_test_junk_in_store():
        import json, clickflare_scripts as cfs
        if not cfs.STORE.exists():
            return None
        try:
            data = json.loads(cfs.STORE.read_text(encoding="utf-8"))
        except Exception:
            return None
        junk = [k for k in data if str(k).startswith(("smoke-", "__", "userA", "userB", "userC"))]
        return f"测试往真实脚本库里写了垃圾:{junk}" if junk else None
    check("测试不许污染用户真实的脚本库", t_no_test_junk_in_store)

    # ClickFlare:接口全靠实测(公开文档没有接口清单),细节见 CLAUDE.md 第六之十八节。
    # 这几条不联网,守的是「代码里写死的那些前提别被人改坏」。
    def t_clickflare_client_basics():
        import clickflare_client as cfc
        # ① 从 Campaign Tracking URL 抠 campaign id —— 用户手里有的就是这条链接,
        #    比让模型按名字猜是哪个 campaign 可靠得多。
        cid = "a1b2c3d4e5f60718293a4b5c"
        cases = [(f"https://trk.x.com/abc?cpid={cid}&s1=y", cid),
                 (f"https://trk.x.com/abc?campaign_id={cid}", cid),
                 (cid, cid), (cid.upper(), cid)]
        for raw, want in cases:
            try:
                got = cfc.campaign_id_from(raw)
            except Exception as e:
                return f"认不出 campaign:{raw[:40]} → {e}"
            if got != want:
                return f"抠错了:{raw[:40]} → {got}"
        for bad, why in [("https://lp.example.com/exp/a/", "落地页地址"),
                         ("https://trk.x.com/cf/click/1", "CTA 地址"),
                         ("", "空的"), ("随便一句话", "不是链接")]:
            try:
                cfc.campaign_id_from(bad)
                return f"把{why}当成 campaign 了:{bad}"
            except cfc.ClickFlareError:
                pass
        # ② 建落地页的校验必须**在联网之前**就拦住 —— 这里没有网也应该报错
        bad_args = [({"name": "x", "url": "https://a.com/", "workspace_id": "nope"}, "workspace"),
                    ({"name": "x", "url": "ftp://a", "workspace_id": cid}, "地址协议"),
                    ({"name": "  ", "url": "https://a.com/", "workspace_id": cid}, "空名字"),
                    ({"name": "x", "url": "https://a.com/", "workspace_id": cid,
                      "cta_count": 0}, "cta_count")]
        for kwargs, why in bad_args:
            try:
                cfc.create_landing(**kwargs)
                return f"{why}不合法却没拦住"
            except cfc.ClickFlareError:
                pass
            except Exception as e:
                return f"{why}报的不是 ClickFlareError,而是 {type(e).__name__}(说明是联网后才失败的)"
        return None
    check("ClickFlare:追踪链接能定位 campaign、建落地页的校验在联网前", t_clickflare_client_basics)

    # 加工具最容易漏的就是「只注册了一半」——Gemini 一份、OpenAI 两份、
    # 模式白名单一份、进度文案一份,漏哪份都是某条路径上悄悄用不了。
    def t_clickflare_tools_registered():
        import agent_server as srv, inspect
        names = ["list_clickflare_campaigns", "describe_clickflare_campaign",
                 "create_clickflare_landers"]
        bad = []
        for n in names:
            if not any(f.__name__ == n for f in srv.NEWSBREAK_TOOLS):
                bad.append(f"{n} 不在 Gemini 工具清单里")
            if n not in srv.OPENAI_TOOL_FUNCS:
                bad.append(f"{n} 不在 OPENAI_TOOL_FUNCS 里")
            if not any(t["function"]["name"] == n for t in srv.OPENAI_TOOL_SCHEMAS):
                bad.append(f"{n} 没有 OpenAI schema")
            if n not in srv.LANDING_TOOL_NAMES:
                bad.append(f"{n} 落地页模式里用不了")
            if n not in srv._TOOL_LABELS:
                bad.append(f"{n} 没有进度文案(流式时用户看不到在干嘛)")
            # Gemini 从签名生成 schema,裸 list/dict 会让**整条 Gemini 路径**都 400
            f = srv.OPENAI_TOOL_FUNCS.get(n)
            if f:
                for k, prm in inspect.signature(f).parameters.items():
                    if prm.annotation in (list, dict):
                        bad.append(f"{n} 的参数 {k} 标了裸 {prm.annotation.__name__}")
        return bad or True
    check("ClickFlare 三个工具五处都注册了", t_clickflare_tools_registered)

    # 建 Lander 之前那道「A/B 不能是同一个地址」的检查,必须排在联网调用之前:
    # 没网/没配 key 的环境里它也该直接拦住,而不是先去打接口。
    def t_clickflare_ab_check_is_free():
        import agent_server as srv, clickflare_client as cfc
        called = []
        real = cfc.describe_campaign
        cfc.describe_campaign = lambda *a, **k: called.append(1) or {}
        try:
            r = srv.create_clickflare_landers("anything", "https://a.com/x/", "https://a.com/x/",
                                              "https://trk.x.com/cf/click/1")
        finally:
            cfc.describe_campaign = real
        if not r.get("error") or "同一个" not in r["error"]:
            return f"A/B 同址没被拦:{str(r)[:120]}"
        if called:
            return "拦住了,但已经先去联网了 —— 免费的检查要排在联网之前"
        return None
    check("A/B 同址的检查排在联网之前", t_clickflare_ab_check_is_free)

    # 换落地页会**立刻改变正在花钱的投放**,是全项目风险最高的写操作。
    # 这几条守的是算新 flow 那一步:PUT 是整体替换,算错一点就是线上出事。
    def t_flow_lander_swap_plan():
        import clickflare_client as cfc
        L1, L2, OF = "1" * 24, "2" * 24, "9" * 24

        def mkflow(paths):
            return {"_id": "f" * 24, "flow": {"name": "f", "transition": "302",
                                              "workspace_id": "w" * 24},
                    "paths": paths, "updated_at": "x"}

        def path(name, dest="landers_offers", landers=(L1,), enabled=True):
            d = {"name": name, "destination": dest, "enabled": enabled,
                 "transition": "302", "weight": 100}
            d[dest] = ({"landers": [{"id": i, "weight": 100 // max(1, len(landers))} for i in landers],
                        "offers": [{"id": OF, "weight": 100}]} if dest == "landers_offers"
                       else {"offers": [{"id": OF, "weight": 100}]})
            return d

        real_names = cfc._name_lookup
        cfc._name_lookup = lambda: {"landings": {}, "offers": {}}
        try:
            want = [{"id": L1, "weight": 50}, {"id": L2, "weight": 50}]
            # ① 多条启用中的 path → 必须拒绝,不许替用户挑
            two = mkflow({"defaultPaths": {"paths": [path("a"), path("b")]}})
            try:
                cfc.plan_flow_lander_swap(two, [L1, L2], want)
                return "有两条启用的 path 却自己挑了一条"
            except cfc.ClickFlareError as e:
                if "不能替用户挑" not in str(e):
                    return f"拦住了但理由不对:{str(e)[:60]}"
            # ② 指名之后要改对那一条,**兄弟 path 必须原样留着**(PUT 是整体替换)
            plan = cfc.plan_flow_lander_swap(two, [L1, L2], want, path_name="b")
            got = plan["put_body"]["paths"]["defaultPaths"]["paths"]
            if len(got) != 2:
                return "兄弟 path 被整体替换弄丢了"
            if got[0]["landers_offers"]["landers"] != [{"id": L1, "weight": 100}]:
                return "改错了 path —— 动到了 keep 的那条"
            if [x["id"] for x in got[1]["landers_offers"]["landers"]] != [L1, L2]:
                return "指定的那条没被改成 A/B"
            if plan["offer有没有动"] != "没动":
                return "offer 被动了"
            if got[1]["landers_offers"]["offers"] != [{"id": OF, "weight": 100}]:
                return "offer 内容变了"
            # ③ 只有一条 path 时可以不指名
            one = mkflow({"defaultPaths": {"paths": [path("only")]}})
            if not cfc.plan_flow_lander_swap(one, [L1, L2], want)["put_body"]:
                return "只有一条 path 时反而算不出来"
            # ④ 原来是「直接跳 offer」的,要明确警告漏斗形状变了
            direct = mkflow({"defaultPaths": {"paths": [path("d", dest="offers_only")]}})
            warn = cfc.plan_flow_lander_swap(direct, [L1, L2], want)["warnings"]
            if not any("漏斗" in w for w in warn):
                return "从『直接跳 offer』改成挂落地页,没有警告漏斗形状变了"
            # ⑤ 一个 offer 都没有 → 挂了落地页流量也无处可去
            empty = mkflow({"defaultPaths": {"paths": [{
                "name": "x", "destination": "landers_offers", "enabled": True,
                "landers_offers": {"landers": [], "offers": []}}]}})
            try:
                cfc.plan_flow_lander_swap(empty, [L1, L2], want)
                return "一个 offer 都没有也照算"
            except cfc.ClickFlareError:
                pass
            # ⑥ 只给 flow_id、没有内嵌 flow 的 campaign 也要认得(新建的就是这样)
            if cfc._flow_id_of({"flow_id": "c" * 24}) != "c" * 24:
                return "campaign 只有 flow_id 时认不出来"
            if cfc._flow_id_of({"flow": {"_id": "d" * 24}}) != "d" * 24:
                return "内嵌 flow 时认不出来"
        finally:
            cfc._name_lookup = real_names
        return None
    check("换落地页:只改指定那条 path,兄弟 path 和 offer 都不动", t_flow_lander_swap_plan)

    # 三处闸门原来各写死一个 "publish_landing_pages" 字符串,加一种待办漏一处
    # 就是「登记得了但看不见」或「确认时被拒」——而且直接调函数测不出来,
    # 必须走真正的工具分发层 _tool_call。
    def t_studio_action_gates():
        import agent_server as srv
        aid = "__smoke_gate__"
        srv.PENDING_ACTIONS[aid] = {
            "type": "swap_campaign_landers", "campaign_name": "X", "flow_id": "a" * 24,
            "put_body": {"flow": {"transition": "302"}}, "fingerprint": "f",
            "user_id": "", "seq": 1}
        # **这条测试会真的走到 confirm_action -> apply_flow**(保险丝拦不住:
        # CURRENT_SEQ 默认 0,和待办里的 seq=1 不相等)。不挡住的话,冒烟跑一次
        # 就往用户真实的 ClickFlare 账号发一次请求 —— 测试不许碰真实数据。
        import clickflare_client as cfc
        real_apply = cfc.apply_flow
        cfc.apply_flow = lambda *a, **k: {"done": True, "已经生效": True, "flow_id": "test"}
        try:
            for want in srv.LANDING_ACTION_TYPES:
                if want not in ("publish_landing_pages", "swap_campaign_landers"):
                    return f"落地页待办类型清单里多了没预期的:{want}"
            m = srv.CURRENT_CHAT_MODE.set("landing")
            try:
                listed = str(srv.list_pending_actions())
                if aid not in listed:
                    return "落地页工作室看不见自己登记的换页待办"
                if '"transition"' in listed:
                    return "待办清单把整坨 put_body 回显出来了"
                if "[已算好" not in listed:
                    return "put_body 没有脱敏标记"
                r = srv._tool_call("confirm_action", {"action_id": aid})
                if "不能确认" in str(r.get("error", "")):
                    return "落地页工作室确认自己的换页待办被拒了"
            finally:
                srv.CURRENT_CHAT_MODE.reset(m)
            m = srv.CURRENT_CHAT_MODE.set("creative")
            try:
                r = srv._tool_call("confirm_action", {"action_id": aid})
                if "不能确认" not in str(r.get("error", "")):
                    return "素材工作室居然能确认换落地页的待办"
            finally:
                srv.CURRENT_CHAT_MODE.reset(m)
        finally:
            cfc.apply_flow = real_apply
            srv.PENDING_ACTIONS.pop(aid, None)
            srv._save_actions()
        return None
    check("换页待办:落地页工作室看得见也确认得了,素材工作室碰不到", t_studio_action_gates)

    def t_swap_tool_registered():
        import agent_server as srv, inspect
        n = "propose_swap_campaign_landers"
        bad = []
        if not any(f.__name__ == n for f in srv.NEWSBREAK_TOOLS):
            bad.append("不在 Gemini 工具清单")
        if n not in srv.OPENAI_TOOL_FUNCS:
            bad.append("不在 OPENAI_TOOL_FUNCS")
        if not any(t["function"]["name"] == n for t in srv.OPENAI_TOOL_SCHEMAS):
            bad.append("没有 OpenAI schema")
        if n not in srv.LANDING_TOOL_NAMES:
            bad.append("落地页模式里用不了")
        if n not in srv._TOOL_LABELS:
            bad.append("没有进度文案")
        for k, prm in inspect.signature(srv.OPENAI_TOOL_FUNCS.get(n, lambda: None)).parameters.items():
            if prm.annotation in (list, dict):
                bad.append(f"参数 {k} 标了裸 {prm.annotation.__name__}")
        # 换落地页必须两阶段:propose 只登记,execute 单独一个函数
        if not hasattr(srv, "_execute_swap_campaign_landers"):
            bad.append("没有单独的执行函数(说明没走保险箱)")
        return bad or True
    check("换落地页的工具五处都注册了、且走两阶段", t_swap_tool_registered)

    # 追踪脚本和 CTA 地址原来要用户手工去 ClickFlare 后台复制,两样都可能粘错,
    # 而粘错了页面看起来完全正常。现在从平台接口现取 —— 实测「模板 + 域名」拼出来的
    # 和用户手工复制的真脚本 md5 完全相同。
    def t_clickflare_script_from_api():
        import clickflare_client as cfc
        MARK = "{{{__TRACKING_DOMAIN__}}}"
        calls = []
        real = cfc._api

        def fake(method, path, **kw):
            calls.append(path)
            if path == "/api/scripts/direct":
                return {"script": 'var c="' + MARK + '";'}
            if path == "/api/scripts/links":
                return {"click": MARK + "/cf/click", "conversion": MARK + "/cf/cv"}
            raise AssertionError("不该调 " + path)

        cfc._api = fake
        try:
            out = cfc.lander_script("trk.mine.com")
            if out != '<script>var c="https://trk.mine.com";</script>':
                return f"脚本拼错了:{out[:80]}"
            if "/api/scripts/direct" not in calls:
                return "没有去平台现取模板(抄进代码里就会静默发老版本)"
            # 传完整地址也要认得(用户手里常常是一整条 URL)
            if cfc.lander_script("https://trk.mine.com/cf/click/1") != out:
                return "传完整地址时没抽出主机名"
            url = cfc.click_url("trk.mine.com")
            if url != "https://trk.mine.com/cf/click/1":
                return f"CTA 地址拼错了:{url}"
            if cfc.click_url("trk.mine.com", 2) != "https://trk.mine.com/cf/click/2":
                return "CTA 序号没生效"
            if "/api/scripts/links" not in calls:
                return "CTA 路径是写死的,没从平台取"
            # 平台改了格式(占位符没了)→ 必须明确报错,绝不许发一段拼错的脚本出去
            cfc._api = lambda m, pth, **k: {"script": "no placeholder here"}
            try:
                cfc.lander_script("trk.mine.com")
                return "模板里没有占位符也照发 —— 那会发出一段坏脚本"
            except cfc.ClickFlareError:
                pass
            # 域名不合法要拦
            cfc._api = fake
            for bad in ("", "不是域名", "localhost"):
                try:
                    cfc.lander_script(bad)
                    return f"「{bad}」这种域名也放行了"
                except cfc.ClickFlareError:
                    pass
        finally:
            cfc._api = real
        return None
    check("追踪脚本和 CTA 地址从平台接口现取(不写死、不让用户贴)", t_clickflare_script_from_api)

    # 脚本原文绝不能出现在聊天里(和待办清单同一条规矩)
    def t_publish_kit_hides_script():
        import agent_server as srv, clickflare_client as cfc, clickflare_scripts as cfs
        import tempfile
        from pathlib import Path as _P
        real_kit, real_store = cfc.publish_kit, cfs.STORE
        cfc.publish_kit = lambda campaign, cta_index=1: {
            "campaign": {"id": "c" * 24, "名字": "X"},
            "追踪域名": "trk.mine.com",
            "CTA_Click_URL": "https://trk.mine.com/cf/click/1",
            "Campaign_Tracking_URL": "https://trk.mine.com/cf/r/xxx",
            "tracking_script": "<script>SECRET_SCRIPT_BODY</script>"}
        with tempfile.TemporaryDirectory() as d:
            cfs.STORE = _P(d) / "s.json"
            tok = srv.CURRENT_USER_ID.set("__kit__")
            try:
                out = str(srv.clickflare_publish_kit("c" * 24))
            finally:
                srv.CURRENT_USER_ID.reset(tok)
                cfc.publish_kit, cfs.STORE = real_kit, real_store
        if "SECRET_SCRIPT_BODY" in out or "<script" in out:
            return "把脚本原文吐进聊天了"
        if "cf/click/1" not in out:
            return "CTA 地址没给出来(那用户还是得自己去后台找)"
        return None
    check("publish_kit 给地址但不回显脚本原文", t_publish_kit_hides_script)

    # 只给相对路径的话,模型会自己配 host —— 线上实测编出了 localhost:3000,
    # 用户点开是他本机另一个项目的 404,而且完全看不出问题在哪。
    def t_preview_url_is_absolute():
        import agent_server as srv
        tok = srv.CURRENT_BASE_URL.set("http://localhost:18100/")
        try:
            got = srv._absolute_previews([{"preview_url": "/landing-pages/x.html"}])
        finally:
            srv.CURRENT_BASE_URL.reset(tok)
        if got[0]["preview_url"] != "http://localhost:18100/landing-pages/x.html":
            return f"没补成完整地址:{got[0]['preview_url']}"
        # 拿不到站点地址时(命令行/测试)保持原样,不瞎拼
        plain = srv._absolute_previews([{"preview_url": "/landing-pages/x.html"}])
        if plain[0]["preview_url"] != "/landing-pages/x.html":
            return "没有站点地址时不该自己拼一个"
        # 真有这个路由(不然给了地址也是 404)
        routes = [getattr(r, "path", "") for r in srv.app.routes]
        if "/landing-pages/{filename}" not in routes:
            return "预览路由不存在 —— 给了地址也打不开"
        return None
    check("落地页预览给的是完整地址,而且路由真的在", t_preview_url_is_absolute)

    def t_kit_tool_registered():
        import agent_server as srv
        n = "clickflare_publish_kit"
        bad = []
        if not any(f.__name__ == n for f in srv.NEWSBREAK_TOOLS):
            bad.append("不在 Gemini 工具清单")
        if n not in srv.OPENAI_TOOL_FUNCS:
            bad.append("不在 OPENAI_TOOL_FUNCS")
        if not any(t["function"]["name"] == n for t in srv.OPENAI_TOOL_SCHEMAS):
            bad.append("没有 OpenAI schema")
        if n not in srv.LANDING_TOOL_NAMES:
            bad.append("落地页模式里用不了")
        if n not in srv._TOOL_LABELS:
            bad.append("没有进度文案")
        return bad or True
    check("publish_kit 工具五处都注册了", t_kit_tool_registered)

    # 用户说「只要一个」就该只出一个 —— 多的那版是白花的模型钱,还逼他在两个里挑。
    def t_landing_variant_count():
        import landing_lab as lp, agent_server as srv, inspect, tempfile
        from pathlib import Path as _P
        pages = [{"name": f"v{i}", "html": f"<html><body>{i}"
                  "<a href='[[CLICKFLARE_CTA_URL]]'>go</a></body></html>"} for i in range(3)]
        real = lp.GENERATED_DIR
        with tempfile.TemporaryDirectory() as d:
            lp.GENERATED_DIR = _P(d)
            try:
                if len(lp.save_pages(pages, limit=1)) != 1:
                    return "要 1 版却不止 1 个"
                if len(lp.save_pages(pages, limit=2)) != 2:
                    return "要 2 版却不是 2 个"
                if len(lp.save_pages(pages, limit=9)) != 2:
                    return "上限没守住(最多 2 版)"
            finally:
                lp.GENERATED_DIR = real
        # 参数要一路通到工具和 schema,不然模型根本没法传
        if "n_variants" not in inspect.signature(srv.summarize_landing_page_patterns).parameters:
            return "工具没有 n_variants 参数,用户说只要一个也传不下去"
        if "n_variants" not in inspect.signature(lp.summarize).parameters:
            return "summarize 没有 n_variants"
        sch = [t for t in srv.OPENAI_TOOL_SCHEMAS
               if t["function"]["name"] == "summarize_landing_page_patterns"]
        if not sch or "n_variants" not in sch[0]["function"]["parameters"]["properties"]:
            return "OpenAI schema 里没有 n_variants"
        # 只要一版时,提示词要明确说别多给
        src = lp.summarize.__doc__ or ""
        body = inspect.getsource(lp.summarize)
        if "只要 1 个页面" not in body:
            return "只要一版时提示词没写清「别多给」"
        return None
    check("落地页要几版由用户说了算", t_landing_variant_count)

    # 线上实测:助手编了个假 CTA 地址,还许诺「先不放追踪代码就发布」——
    # 而缺 CTA 占位符的页面代码层直接拒绝,答应了也做不到。
    def t_landing_prompt_no_false_promise():
        import agent_server as srv
        m = srv.CURRENT_CHAT_MODE.set("landing")   # 模式来自 contextvar,不是参数
        try:
            zh = srv._system_prompt_now("zh")
            en = srv._system_prompt_now("en")
        finally:
            srv.CURRENT_CHAT_MODE.reset(m)
        bad = []
        if "clickflare_publish_kit" not in zh:
            bad.append("中文提示词没说先调 clickflare_publish_kit(还会去问用户要)")
        if "clickflare_publish_kit" not in en:
            bad.append("英文提示词没说先调 clickflare_publish_kit")
        if "先不放追踪代码" not in zh:
            bad.append("中文提示词没禁止「先不放追踪代码就发布」这种承诺")
        if "without tracking" not in en.lower():
            bad.append("英文提示词没禁止无追踪发布")
        if "n_variants=1" not in zh or "n_variants=1" not in en:
            bad.append("提示词没说「用户只要一个就传 1」")
        if "preview_url" not in zh or "preview_url" not in en:
            bad.append("提示词没要求把预览地址列给用户")
        return bad or True
    check("落地页提示词:不问用户要追踪信息、不许承诺无追踪发布", t_landing_prompt_no_false_promise)

    # 看图这条路以前写死 Gemini,于是把大脑切成 openai 之后它还在偷偷用 Gemini ——
    # Gemini 一 503,拆素材和拆落地页截图就整个坏掉,而用户以为早就不用它了。
    def t_vision_follows_brain():
        import creative_lab as lab, agent_server as srv
        calls = []
        real_g, real_o = lab._vision_gemini, lab._vision_openai
        real_env, real_shrink = srv._read_env_value, lab.shrink
        lab._vision_gemini = lambda *a: (calls.append("gemini"), {"ok": "g"})[1]
        lab._vision_openai = lambda *a: (calls.append("openai"), {"ok": "o"})[1]
        lab.shrink = lambda data, mime="": (b"x", "image/jpeg")   # 别真去解码图片
        try:
            for brain, want in (("openai", ["openai"]), ("auto", ["gemini"]), ("", ["gemini"])):
                calls.clear()
                srv._read_env_value = lambda k, b=brain: b if k == "BRAIN" else real_env(k)
                lab._ask_vision(b"fake", "image/jpeg", "p")
                if calls != want:
                    return f"BRAIN={brain or '(空)'} 时走的是 {calls},应该是 {want}"
            # openai 那条挂了也**绝不许**偷偷回落 Gemini —— 用户明说不用它
            calls.clear()
            srv._read_env_value = lambda k: "openai" if k == "BRAIN" else real_env(k)
            def boom(*a):
                calls.append("openai"); raise RuntimeError("挂了")
            lab._vision_openai = boom
            out = lab._ask_vision(b"fake", "image/jpeg", "p")
            if "gemini" in calls:
                return "BRAIN=openai 时 openai 挂了竟然偷偷用了 Gemini"
            if not out.get("error"):
                return "全挂了却没报错"
        finally:
            lab._vision_gemini, lab._vision_openai = real_g, real_o
            srv._read_env_value, lab.shrink = real_env, real_shrink
        return None
    check("看图跟着 BRAIN 走(设了 openai 就一次都不碰 Gemini)", t_vision_follows_brain)

    # 超时的顺序不变量:**后端必须比前端先失败**。
    # 反过来的话,用户看到「等太久了,这条没发出去」而后端还在正常干活 ——
    # 他会以为发失败了再发一遍,白烧一次额度,而且拿不到真实原因。
    # (和 UPLOAD_TIMEOUT_S 必须小于前端 180 秒是同一条。)
    def t_timeout_ordering():
        import agent_server as srv, re, inspect
        from pathlib import Path as _P
        html = _P("static/index.html").read_text(encoding="utf-8")
        m = re.search(r"IDLE_TIMEOUT_MS\s*=\s*(\d+)", html)
        if not m:
            return "前端找不到 IDLE_TIMEOUT_MS"
        idle_s = int(m.group(1)) / 1000
        if srv.GEN_TIMEOUT_S >= idle_s:
            return (f"后端长文超时 {srv.GEN_TIMEOUT_S}s 不小于前端 {idle_s}s —— "
                    "用户会先看到「等太久了」,而后端还在跑")
        if srv.BRAIN_TIMEOUT_S >= idle_s:
            return f"聊天超时 {srv.BRAIN_TIMEOUT_S}s 不小于前端 {idle_s}s"
        import cloudflare_pages as cfp
        if cfp.UPLOAD_TIMEOUT_S >= idle_s:
            return f"上传超时 {cfp.UPLOAD_TIMEOUT_S}s 不小于前端 {idle_s}s"
        # 长文生成本来就慢(实测 gpt-5.5 出一整页 61 秒),不能还用聊天那个短超时
        src = _no_comments(inspect.getsource(srv._plain_completion))
        if "GEN_TIMEOUT_S" not in src:
            return "长文生成还在用聊天的短超时,一整页落地页会被掐掉"
        if "timeout=BRAIN_TIMEOUT_S" in src:
            return "长文生成里还留着 BRAIN_TIMEOUT_S"
        return None
    check("超时顺序:后端一定比前端先失败", t_timeout_ordering)

    # 前端的判据必须是「多久没动静」,不是「一共等了多久」——
    # 一次正常但耗时的生成会被总时长判成"卡死"掐掉。
    def t_frontend_idle_not_total():
        from pathlib import Path as _P
        import re
        html = _P("static/index.html").read_text(encoding="utf-8")
        # 剥掉注释再找,免得命中说明文字(坑表:index() 会命中注释)
        body = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
        body = re.sub(r"^\s*//.*$", "", body, flags=re.M)
        if "bump()" not in body:
            return "没有重置计时器的 bump()"
        # 读到数据之后必须重新计时
        m = re.search(r"if \(done\) break;\s*(.*?)buf \+= decoder", body, re.S)
        if not m or "bump()" not in m.group(1):
            return "读到 SSE 数据之后没有重新计时 —— 长回复还是会被掐"
        if re.search(r"setTimeout\(\(\) => controller\.abort\(\), 180000\)", body):
            return "还留着写死 180 秒总时长的老写法"
        return None
    check("前端超时看的是「多久没动静」", t_frontend_idle_not_total)

    # 追踪器的 lander 脚本有两种设计:通用一段(靠 Lander URL 区分)、
    # 或每个 Lander 一段(脚本里带 lander id)。**如果是后者而我们两版注入同一段,
    # B 版会上报成 A 版,A/B 数据全废,而且页面一切正常、看不出任何异常。**
    # 与其赌一个不确定的前提,不如两种都支持。
    def t_per_variant_script():
        import shutil
        import tempfile
        from pathlib import Path as _P
        import cloudflare_pages as cfp
        tmp = _P(tempfile.mkdtemp(prefix="lpscript-"))
        keep = (cfp.SITES_DIR, cfp.MAPPING_FILE, cfp._api, cfp.owned_zone, cfp._guard_domain,
                cfp._project_exists, cfp._project_has_content, cfp._ensure_project,
                cfp._ensure_domain, cfp._wrangler_deploy)
        try:
            cfp.SITES_DIR, cfp.MAPPING_FILE = tmp / "s", tmp / "m.json"
            cfp._api = lambda *a, **k: {"success": True, "result": []}
            cfp.owned_zone = lambda d: "example.com"
            cfp._guard_domain = lambda d: None
            cfp._project_exists = lambda n: False
            cfp._project_has_content = lambda n: False
            cfp._ensure_project = lambda n: True
            cfp._ensure_domain = lambda p, d: {"name": d, "status": "active"}
            cfp._wrangler_deploy = lambda d, p, s2: "https://x.pages.dev"
            page = ('<html><body><a href="[[CLICKFLARE_CTA_URL]]">go</a>'
                    '<!--[[CLICKFLARE_LANDER_SCRIPT]]--></body></html>')
            fa, fb = tmp / "a.html", tmp / "b.html"
            fa.write_text(page); fb.write_text(page)
            cta = "https://trk.example.com/cf/click/1"
            sa = '<script src="https://trk.example.com/l.js?id=AAA"></script>'
            sb = '<script src="https://trk.example.com/l.js?id=BBB"></script>'

            r = cfp.publish_ab("lp.example.com", "same", fa, fb, cta, sa)
            proj = r["created"]["pages_project"]
            read = lambda slug, v: (cfp.SITES_DIR / proj / slug / v / "index.html").read_text()
            if "id=AAA" not in read("same", "a") or "id=AAA" not in read("same", "b"):
                return "只给一段时,两版都该用它"

            cfp.publish_ab("lp.example.com", "diff", fa, fb, cta, sa, tracking_script_b=sb)
            if "id=AAA" not in read("diff", "a") or "id=BBB" in read("diff", "a"):
                return "A 版注入的不是 A 的脚本"
            if "id=BBB" not in read("diff", "b") or "id=AAA" in read("diff", "b"):
                return "B 版注入的不是 B 的脚本 —— B 会上报成 A,A/B 数据全废"
            return None
        finally:
            (cfp.SITES_DIR, cfp.MAPPING_FILE, cfp._api, cfp.owned_zone, cfp._guard_domain,
             cfp._project_exists, cfp._project_has_content, cfp._ensure_project,
             cfp._ensure_domain, cfp._wrangler_deploy) = keep
            shutil.rmtree(tmp, ignore_errors=True)
    check("A/B 可以各用各的追踪脚本(留空则共用)", t_per_variant_script)

    def t_conflict_checked_at_propose():
        import inspect
        import agent_server as srv
        src = _no_comments(inspect.getsource(srv.propose_publish_landing_pages))
        if "domain_conflict" not in src:
            return "提案阶段没查域名占用"
        i_chk, i_reg = src.find("domain_conflict"), src.find("PENDING_ACTIONS[action_id]")
        if i_reg >= 0 and i_chk > i_reg:
            return "先登记了待办才查域名占用,顺序反了"
        return None
    check("提案阶段就查域名有没有被占用", t_conflict_checked_at_propose)

    # 生成那一步就要判断"这页能不能发布"。实测模型生成过整页零个 CTA 占位符、
    # CTA 是 onclick="alert('Thank you!')" 的假按钮 —— 那种页面走到发布会被拒,
    # 但用户已经把整条链走完才知道白做。提示词管不住模型,只能代码层查。
    def t_generated_pages_checked():
        import landing_lab as lp
        made = []
        try:
            ok = {"命名": "ok", "html": '<html><body><a href="[[CLICKFLARE_CTA_URL]]">go</a></body></html>'}
            bad = {"命名": "bad", "html": '<html><body><button onclick="alert(1)">go</button></body></html>'}
            rows = lp.save_pages([ok, bad])
            made = [lp.GENERATED_DIR / r["file"] for r in rows]
            good, junk = rows[0], rows[1]
            if not good.get("可发布"):
                return f"合格的页面被判成不能发布:{good.get('⚠️问题')}"
            if junk.get("可发布"):
                return "没有 CTA 占位符的页面竟然算可发布 —— 用户会一路走到发布才发现白做"
            if not any("CTA 占位符" in x for x in junk.get("⚠️问题") or []):
                return "没说清缺的是 CTA 占位符"
            if not any("假 CTA" in x for x in junk.get("⚠️问题") or []):
                return "alert 假按钮没被点出来"
            # 脚本占位符位置固定,可以确定性补上;CTA 不能猜(猜错就是把追踪挂错元素)
            for path in made:
                if "<!--[[CLICKFLARE_LANDER_SCRIPT]]-->" not in path.read_text():
                    return "脚本占位符没有自动补上"
            return None
        finally:
            for f in made:
                f.unlink(missing_ok=True)
    check("生成时就判断页面能不能发布(缺 CTA 占位符要明说)", t_generated_pages_checked)

    def t_summarize_reports_unpublishable():
        import inspect
        import agent_server as srv
        src = _no_comments(inspect.getsource(srv.summarize_landing_page_patterns))
        if "不能发布的版本" not in src:
            return "归纳结果没有把不可发布的版本列出来"
        if "可发布" not in src:
            return "note 里没要求 AI 把问题讲给用户"
        return None
    check("不能发布的版本要主动告诉用户", t_summarize_reports_unpublishable)


    # 竞品 key 按人存。换平台之后 key 不会过期了,但"每人各贴各的"这条仍然要守。


    # 创意拆解(P0)。以下几条都不联网,纯逻辑,但守的都是真出过问题的地方。

    # 文案查重:实测发现模型会把竞品主标题原样吐回来(提示词里已经写了"不许照抄"也拦不住)。
    # 文案抄袭不像品牌名那样一眼能看出来,用户很可能直接拿去投,所以必须代码层查。
    def t_copy_detection():
        import creative_lab as lab
        src = [{"文字层": [{"内容": "Call an Expert Contractor Now"},
                           {"内容": "Flat Roof Specialists - Waterproofing & Durability"}]}]
        cases = [
            ("Call an Expert Contractor Now",          True,  "原样照抄"),
            ("Call an Expert Roofing Contractor Now",  True,  "只加一个词"),
            ("Your Roof Deserves a Real Professional", False, "同角度但自己写的"),
            ("Flat Roof Waterproofing That Lasts",     False, "学细分品类,换了用词"),
        ]
        bad = []
        for text, want, why in cases:
            out = {"方案": [{"主标题": text}]}
            lab._flag_copied(out, src)
            if bool(out.get("有照抄嫌疑")) != want:
                bad.append(f"「{text[:36]}」({why})判成"
                           f"{'抄袭' if out.get('有照抄嫌疑') else '原创'},判错了")
        return bad or True

    # 模型爱把 JSON 包在 ```json 围栏里,也可能前后带客套话。剥不干净就整个功能失效。
    def t_json_extraction():
        import creative_lab as lab
        bad = []
        cases = {
            '```json\n{"a": 1}\n```': 1,
            '好的,结果如下:\n{"a": 1}\n希望有帮助': 1,
            '{"a": 1}': 1,
            '```\n{"a": 1}\n```': 1,
        }
        for raw, want in cases.items():
            got = lab._json_from(raw)
            if got.get("a") != want:
                bad.append(f"{raw[:26]!r} 没剥干净:{got}")
        # 真解析不了时要如实报错,不能悄悄返回空 dict 让上游以为成功了
        r = lab._json_from("完全不是 JSON")
        if "error" not in r:
            bad.append("解析失败时没有报错")
        return bad or True

    # 原图能到 6000×4000(10MB),直接喂模型又慢又贵。缩图这步不能坏。
    def t_image_shrink():
        import io
        import creative_lab as lab
        try:
            from PIL import Image
        except ImportError:
            return "Pillow 没装,creative_lab 缩图会失效(requirements.txt 里要有)"
        buf = io.BytesIO()
        Image.new("RGB", (4000, 3000), (120, 90, 60)).save(buf, "JPEG")
        big = buf.getvalue()
        small, mime = lab.shrink(big, "image/jpeg")
        if len(small) >= len(big):
            return f"没缩小:{len(big)} → {len(small)}"
        w, h = Image.open(io.BytesIO(small)).size
        if max(w, h) > lab.MAX_EDGE:
            return f"缩完还是超过 {lab.MAX_EDGE}:{w}×{h}"
        if mime != "image/jpeg":
            return f"类型不对:{mime}"
        # 坏数据不能把整条链炸掉,原样返回即可
        if lab.shrink(b"not an image", "image/png")[0] != b"not an image":
            return "遇到坏图没有原样返回"
        return True

    # 拆解只能拆搜索结果里出现过的素材 —— 和 use_found_creative 一个道理,防 AI 编地址
    def t_decompose_guards():
        import agent_server as srv
        bad = []
        r = srv.decompose_creative("https://evil.example.com/x.jpg")
        if "error" not in r or "搜索结果" not in r["error"]:
            bad.append(f"编造的素材地址没被挡:{r}")
        # 归纳至少要 2 条,1 条归纳不出"共同点"
        old = dict(srv._models())
        try:
            srv._models().clear()
            srv._models()["only-one"] = {"版式": "x"}
            r = srv.summarize_creative_patterns()
            if "error" not in r or "2 条" not in r["error"]:
                bad.append(f"只有 1 条时没拒绝归纳:{r}")
        finally:
            srv._models().clear()
            srv._models().update(old)
        return bad or True

    # 合规是这个模块的立身之本:产出里不许有竞品品牌、不许照抄具体承诺和原句。
    # 这三条必须在提示词里,而且要在**最前面**(放末尾会被前面一大段内容带跑,
    # 和 SYSTEM_PROMPT_EN 那条教训一样)。
    def t_compliance_rules_present():
        import creative_lab as lab
        p = lab._summary_prompt([{"竞品标识": ["X"]}], "我方品牌", "https://x.com", 3)
        bad = []
        for must in ("绝对要求", "竞品的品牌名", "具体承诺", "自己重新写的", "不许编造"):
            if must not in p:
                bad.append(f"归纳提示词里缺「{must}」")
        if p.index("绝对要求") > 40:
            bad.append("合规要求没有放在提示词最前面")
        d = lab._DECOMPOSE_PROMPT
        # 查的是**意思**不是某句原话 —— 措辞改过好几轮,写死原话的话
        # 改一次文案就误报一次。这三条底线本身不能丢:
        if "竞品标识" not in d:
            bad.append("拆解提示词里没让它列出竞品品牌(后面要靠它把别人的品牌剔干净)")
        if not any(k in d for k in ("不许编造", "绝不许编", "绝对不要编", "不要编")):
            bad.append("拆解提示词里没有「不许编」这条底线")
        if not any(k in d for k in ("原样抄", "原样照抄", "原样列出")):
            bad.append("拆解提示词里没要求原样照抄(钩子原话和品牌名不能被改写)")
        return bad or True

    check("文案查重能认出照抄的主标题", t_copy_detection)
    check("模型返回的 JSON 围栏能剥干净", t_json_extraction)
    check("大图会先缩小再喂给模型", t_image_shrink)
    check("拆解/归纳的两道门槛守住了", t_decompose_guards)
    check("创意方案的合规约束在提示词最前面", t_compliance_rules_present)




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
        srv._new_turn()          # 模拟「下一条用户消息」
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
        srv._new_turn()          # 模拟「下一条用户消息」
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
        srv._new_turn()          # 模拟「下一条用户消息」
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
        srv._new_turn()          # 模拟「下一条用户消息」
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

def test_creative_render():
    """生成广告图:AI 画无字底图 + 代码叠字。

    叠字那半是纯本地的,不花钱也不联网,可以完整测;
    真生图要花钱,所以这里只测提示词规则、尺寸约定和报错翻译。
    """
    import io

    import agent_server as srv
    import creative_render as cr
    from PIL import Image

    print("\n【6】生成广告图(无字底图 + 代码叠字)")

    # 底图的硬规矩必须写死在代码里。少一条就是"画出来的图自带文字或 logo" ——
    # 那种图没法安全叠字,还可能有版权问题。
    def t_rules():
        pr = cr.build_prompt("a roof")
        missing = [k for k in ("NO TEXT", "no letters", "NO logos") if k not in pr]
        return f"底图提示词少了:{missing}" if missing else None
    check("底图提示词写死了「不许有字、不许有 logo」", t_rules)

    # **默认必须出干净的图**:NewsBreak 的 headline/description/callToAction 是和
    # assetUrl 并列的独立字段,平台自己渲染。把标题烧进图里是重复,画个假按钮
    # 更会和平台的真按钮并排出现。实测跑得最好的竞品广告(3245 版位)图上一个字都没有。
    def t_clean_default():
        import inspect
        if inspect.signature(cr.render).parameters["overlay"].default is not False:
            return "render 默认还在叠字 —— 平台已经渲染文字了,图上再来一遍是重复"
        src = "\n".join(ln for ln in inspect.getsource(srv._execute_make_creatives).splitlines()
                         if not ln.strip().startswith("#"))
        if "overlay=True" in src:
            return "生图时强行叠了字"
        if "Learn More" in src:
            return "还在往图上画假的行动按钮 —— 平台会渲染真按钮,两个会并排出现"
        return None
    check("默认出干净的图(文字交给平台字段,不画假按钮)", t_clean_default)

    # 竞品 key **全公司一份,配在 .env 里**,不再做"每人贴一份"。
    # key 不过期、5000 次/天,查的又是公开的竞品库,没有"谁的数据"之分。
    def t_oal_single_key():
        import pathlib

        import openadlibrary_client as _oal
        if hasattr(_oal, "CURRENT_CREDS"):
            return "还留着按人取凭据的口子 —— 已经改成全公司一份了"
        for path in ("/api/competitor", "/api/competitor/token"):
            if f'"{path}"' in pathlib.Path("agent_server.py").read_text():
                return f"{path} 接口还在,前端已经没有对应界面了"
        if "spy" in pathlib.Path("static/index.html").read_text():
            return "前端还残留 spy 相关代码"
        return None
    check("竞品 key 是全公司一份(没有残留的按人贴 key)", t_oal_single_key)

    # geoCountry 有两种坏法且会交替出现:多数时候 503,偶尔 200 但返回 0 条。
    # 只针对 503 降级会被第二种骗过去(实测骗到了),所以干脆一律本地筛。
    def t_geo_local():
        import inspect

        import openadlibrary_client as _oal
        body = "\n".join(ln for ln in inspect.getsource(_oal.search).splitlines()
                          if not ln.strip().startswith("#"))
        if "geoCountry" in body:
            return "又把 geoCountry 发给平台了 —— 它会 503,还会静默返回 0 条"
        if 'x.get("geos")' not in body:
            return "没有在本地按 geos 筛国家,country 参数等于没用"
        if '"geos"' not in inspect.getsource(_oal._normalize):
            return "_normalize 没把 geos 带出来,本地筛国家等于没筛"
        return None
    check("国家一律本地筛(不发已知会坏的 geoCountry)", t_geo_local)

    # 品类缓存**不能跨用户串**。原来大家不指定账户时都落在同一个 "_default" 键上,
    # B 登录后会直接读到 A 的品类 —— 和第六之四节"每人绑各自的账号"是同一条底线。
    def t_cats_isolated():
        srv._MY_CATS_CACHE.clear()
        srv.CURRENT_USER_ID.set("smoke-A")
        a_key = [k for k in ("smoke-A::_default",)][0]
        srv._MY_CATS_CACHE[a_key] = {"at": 9e9, "val": {"品类": ["__a_only__"]}}
        srv.CURRENT_USER_ID.set("smoke-B")
        got = srv._account_categories().get("品类") or []
        srv._MY_CATS_CACHE.clear()
        srv.CURRENT_USER_ID.set(None)
        if "__a_only__" in got:
            return "B 读到了 A 的品类缓存 —— 缓存键没带用户"
        return None
    check("账户品类缓存不会串到别人身上", t_cats_isolated)

    # **话术必须跟着行为一起改。** 这次把默认从"叠字"改成"出干净图"时,
    # 六处描述(中英提示词、两个 docstring、提议话术、OpenAI schema)全都留在原地,
    # 而五套测试当时是全绿的 —— 助手会照着旧话术跟用户说"文字是代码排上去的",
    # 实际图上根本没字。项目规矩第 6 条说的就是这个。
    def t_copy_matches_behavior():
        import inspect
        if inspect.signature(cr.render).parameters["overlay"].default is not False:
            return None      # 哪天默认改回叠字了,这条自然不适用
        # 关键词按"意思"列全一点:上次就是换了个说法(「代码把文案精确排上去」)
        # 从 summarize 那步漏出去的 —— 而 summarize 原本根本没在检查名单里。
        stale = ("代码精确排版", "代码把标题", "代码把文案", "文案精确排", "精确排上去",
                 "composited\n     by code", "代码叠字", "把标题排上", "把文字排上")
        hits = []
        for text, where in ((srv.SYSTEM_PROMPT, "中文提示词"),
                            (srv.SYSTEM_PROMPT_EN, "英文提示词"),
                            (inspect.getdoc(srv.propose_make_creatives) or "", "propose docstring"),
                            (inspect.getdoc(srv._execute_make_creatives) or "", "执行 docstring"),
                            (inspect.getsource(srv.propose_make_creatives), "提议话术/schema"),
                            (inspect.getsource(srv.summarize_creative_patterns), "归纳话术"),
                            (inspect.getsource(srv._execute_make_creatives), "执行话术")):
            for kw in stale:
                if kw.replace("\n     ", " ") in text.replace("\n", " "):
                    hits.append(f"{where} 还在说「{kw.strip()}」")
        return "默认已经不叠字了,但这些地方还在描述旧行为:" + ";".join(hits) if hits else None
    check("对用户的说法和代码实际行为一致(没有残留的旧话术)", t_copy_matches_behavior)

    # 报价给用户看的是哪几版,真做出来的就必须是哪几版。
    # 只存编号的话:用户还没点头就又归纳了一次 → 方案列表整个换掉 →
    # 同样的编号指到别的方案 → **花着钱做出他没同意过的东西**。
    def t_plan_snapshot():
        srv._new_turn()
        srv._plans().clear()
        srv._plans().extend([{"命名": "planA", "主标题": "A", "画面怎么拍": "aaa"},
                             {"命名": "planB", "主标题": "B", "画面怎么拍": "bbb"}])
        r = srv.propose_make_creatives(variants="1")
        aid = r.get("action_id", "")
        if not aid:
            return f"登记失败:{r}"
        try:
            snap = srv.PENDING_ACTIONS[aid].get("plans")
            if not isinstance(snap, list) or not snap:
                return "待办里没有方案快照,只有编号 —— 中途重新归纳过就会做错东西"
            if snap[0].get("命名") != "planA":
                return f"快照存错了:{snap[0].get('命名')}"
            # 模拟"用户还没确认,又重新归纳了一次"
            srv._plans().clear()
            srv._plans().extend([{"命名": "换成别的了", "主标题": "X", "画面怎么拍": "xxx"}])
            still = srv.PENDING_ACTIONS[aid]["plans"][0].get("命名")
            if still != "planA":
                return f"重新归纳之后待办跟着变了:{still}"
            return True
        finally:
            srv.cancel_action(aid)
            srv._plans().clear()
    check("待办里存的是方案快照(报价什么就做什么)", t_plan_snapshot)

    # 上一版生成成功后 local 指着它的文件;这一版若在生图**之前**就失败,
    # 报错会说"图已存在 <上一版的文件>" —— 用户以为这版的钱也花了,其实没花
    def t_local_not_leaked():
        import inspect
        src = "\n".join(ln for ln in inspect.getsource(srv._execute_make_creatives).splitlines()
                         if not ln.strip().startswith("#"))
        if "local = None" not in src:
            return "循环里没有把 local 清空,失败时会报出上一版的文件路径"
        return None
    check("失败时不会报出上一版的文件路径", t_local_not_leaked)

    # 拆解要回答的是「它为什么跑得动」,不是「它长什么样」——
    # 老板要的是亮点在哪、怎么发挥,不是一份摄影笔记。
    def t_why_it_works():
        import creative_lab as lab
        need = ("为什么有展示", "为什么被点", "为什么有转化", "亮点",
                "如何发挥这个亮点", "可迁移的公式")
        miss = [k for k in need if k not in lab._DECOMPOSE_PROMPT]
        if miss:
            return f"拆解没问这些:{miss} —— 那就只是在描述长相,不是在分析为什么跑得动"
        # 平台不给点击率/转化率,只有版位数和投放天数。让模型编数字是最危险的。
        if "绝不许写出" not in lab._DECOMPOSE_PROMPT:
            return "没禁止编造点击率/转化率 —— 平台根本不给这些数"
        import inspect
        if "perf" not in inspect.signature(lab.decompose).parameters:
            return "拆解时没把投放实绩喂进去,模型只看一张图答不了「为什么跑得动」"
        if "版位数" not in inspect.getsource(srv.decompose_creative):
            return "工具没把版位数传给拆解"
        pr = lab._summary_prompt([], "", "", 3)
        for k in ("为什么这批能跑起来", "最该学的亮点", "亮点用在哪"):
            if k not in pr:
                return f"归纳里少了「{k}」"
        return None
    check("拆解回答的是「为什么跑得动」而不是「长什么样」", t_why_it_works)

    # 「现在什么广告跑得好」是**开放问题**,不该被账户已有的品类框住。
    # 上一版为了防 AI 编关键词,一律按账户品类查 —— 结果开放问题也被框回
    # roof/gutter/window,用户看不到别的机会。防幻觉不能变成画地为牢。
    def t_open_question():
        import inspect

        import openadlibrary_client as _oal
        if not hasattr(_oal, "market_scan"):
            return "没有全市场扫描,开放问题只能靠编一个关键词回答"
        for name in ("VERTICALS", "HOOKS"):
            if len(getattr(_oal, name, [])) < 5:
                return f"{name} 分桶太少,汇总不出有用的排行"
        # 提示词必须把两种问法分开,否则模型还是会拿账户品类去框
        for kw in ("开放探索", "品类明确", "native_market_scan"):
            if kw not in srv.SYSTEM_PROMPT:
                return f"中文提示词里没写清「{kw}」,开放问题还是会被框回账户品类"
        if "native_market_scan" not in srv.SYSTEM_PROMPT_EN:
            return "英文提示词没提全市场扫描,英文模式下还是会被框住"
        # 品类对不上时只是**提示**,不是拦截 —— 用户自己说要看别的品类很正常
        src = inspect.getsource(srv.search_competitor_ads)
        if "⚠️关键词提醒" in src:
            return "品类对不上还在当成警告拦一道,用户想看新方向会被反复追问"
        if "ℹ️品类提示" not in src:
            return "连提示都没有了,AI 自己编关键词时没人拦"
        for tbl, name in (([f.__name__ for f in srv.NEWSBREAK_TOOLS], "Gemini 工具表"),
                          (list(srv.OPENAI_TOOL_FUNCS), "OpenAI 函数表"),
                          ([t["function"]["name"] for t in srv.OPENAI_TOOL_SCHEMAS], "OpenAI schema")):
            if "native_market_scan" not in tbl:
                return f"native_market_scan 没注册进{name}"
        return None
    check("开放问题扫全市场,不被账户品类框住", t_open_question)

    # 素材池要跨平台:在 NewsBreak 上投,该学的是**所有原生平台**上的同品类广告,
    # 不是只看 NewsBreak 自己的。同类型平台之间创意套路通用,池子大得多。
    def t_cross_platform():
        import ad_platform_kinds as _apk
        if _apk.kind_of("NewsBreak") != "native":
            return "没把 NewsBreak 判成原生广告平台"
        if _apk.kind_of("Meta") != "walled" or _apk.kind_of("The Trade Desk") != "dsp":
            return "大媒体/DSP 分类不对"
        if _apk.kind_of("某某平台"):
            return "认不出的平台居然猜了一个类型 —— 应当明说认不出并问用户"
        if len(_apk.peers("NewsBreak")) < 4:
            return "同类平台列得太少,素材池等于没扩大"
        import inspect
        if "素材来自这些原生平台" not in inspect.getsource(srv.search_competitor_ads):
            return "查竞品时没报出素材来自哪些平台,用户看不到覆盖面"
        for tbl, name in ((["f.__name__" for f in []], ""),):
            pass
        for tbl, name in (([f.__name__ for f in srv.NEWSBREAK_TOOLS], "Gemini 工具表"),
                          (list(srv.OPENAI_TOOL_FUNCS), "OpenAI 函数表"),
                          ([t["function"]["name"] for t in srv.OPENAI_TOOL_SCHEMAS], "OpenAI schema")):
            if "platform_kind" not in tbl:
                return f"platform_kind 没注册进{name}"
        return None
    check("跨平台取材(按平台类型决定素材池)", t_cross_platform)

    # 老板要的那条链:拆解 → **专业关键词** → 出图。关键在于生图提示词要用
    # **从素材里真拆出来的关键词**,不是代码里写死的模板 —— 否则拆解等于白做。
    def t_keywords():
        import creative_lab as lab
        if len(lab.KEYWORD_DIMENSIONS) < 9:
            return f"拆解角度只有 {len(lab.KEYWORD_DIMENSIONS)} 个,不够拼一条完整的生图提示词"
        keys = [en for en, _zh, _t in lab.KEYWORD_DIMENSIONS]
        # 键名必须是英文:一开始键用中文、值里写英文名,模型直接把值里的英文词
        # 当成了键,返回来中英混着一串,按中文名取全是空。
        if any(not k.isascii() for k in keys):
            return f"关键词的键必须是英文(模型会把值里的英文词当成键):{keys}"
        if cr._KW_ORDER != keys:
            return f"生图侧的键和拆解侧对不上:{cr._KW_ORDER} vs {keys}"
        for want in ("shot", "lens", "light", "treatment"):
            if want not in keys:
                return f"少了「{want}」这个角度 —— 它直接决定出图长什么样"
        if "生图关键词" not in lab._DECOMPOSE_PROMPT:
            return "拆解提示词里没让模型吐关键词"
        if "生图关键词" not in lab._summary_prompt([], "", "", 3):
            return "归纳提示词里没让每版方案带上关键词"
        return None
    check("拆解能出九个角度的专业生图关键词", t_keywords)

    def t_keywords_drive_prompt():
        kw = {"subject": "a roofer", "action": "installing gutters",
              "shot": "low-angle", "lens": "35mm f/2.8",
              "light": "overcast daylight", "treatment": "documentary"}
        prompt = cr.build_prompt("", 0, kw)
        for v in kw.values():
            if v not in prompt:
                return f"关键词「{v}」没进提示词"
        # 关键词里已经指定了镜头,就不该再塞模板里的镜头 —— 两句话会打架
        if any(v in prompt for v in cr.VARIETY):
            return "关键词已给了镜头,却还塞了模板镜头,两者会冲突"
        # 没关键词时要能兜底,不能直接不出图
        if len(cr.build_prompt("a roof on a house", 1)) < 100:
            return "没有关键词时兜底失效"
        import inspect
        if "keywords=" not in inspect.getsource(srv._execute_make_creatives):
            return "生成时没把关键词传下去,拆解出来的东西没用上"
        return None
    check("生图提示词由关键词驱动(不是写死的模板)", t_keywords_drive_prompt)

    # 三版要靠**镜头语言**拉开差距。第一版三张用同一套模板 + 相近提示词,
    # 出来几乎一模一样,做 A/B 时变量只有文案,画面等于没变。
    def t_variety():
        if len(cr.VARIETY) < 3:
            return f"只有 {len(cr.VARIETY)} 种镜头,不够三版各不相同"
        shots = {cr.build_prompt("gutter cleaning", i) for i in range(3)}
        if len(shots) != 3:
            return "三版的提示词有重复 —— 出来的画面会长得一样"
        import inspect
        src = inspect.getsource(srv._execute_make_creatives)
        if "variant=" not in src:
            return "生图时没把版次传下去,三张还是同一种镜头"
        return None
    check("三版用不同镜头,画面真的不一样", t_variety)

    # 实测踩过:先生成 1024×1024 方图再裁成 1200×628,会把画面下半部分的正主
    # (屋顶样品)整个裁掉,只剩虚化背景 —— 等于广告主体没了。
    def t_gen_size():
        w, h = (int(x) for x in cr.GEN_SIZE.split("x"))
        if w <= h:
            return f"生图尺寸不是横版({cr.GEN_SIZE}),裁成广告位时会切掉主体"
        if w / h > cr.AD_SIZE[0] / cr.AD_SIZE[1] + 0.35:
            return f"生图比例({w}/{h})比广告位宽太多,左右会被切掉不少"
        return None
    check("生图时就按横版出,不先方后裁", t_gen_size)

    def t_compose():
        buf = io.BytesIO()
        Image.new("RGB", (1536, 1024), (90, 130, 170)).save(buf, "PNG")
        out = cr.compose(buf.getvalue(),
                         "A Headline That Is Quite Long And Will Wrap Onto Lines",
                         "A description line that also wraps around here.",
                         "Get a Free Quote")
        im = Image.open(io.BytesIO(out))
        if im.size != cr.AD_SIZE:
            return f"成品尺寸是 {im.size},应该是 {cr.AD_SIZE}"
        if im.format != "JPEG":
            return f"成品格式是 {im.format},平台要 JPEG"
        return None
    check("超长文案也能叠成正确尺寸的成品图", t_compose)

    # 余额不足 ≠ 钥匙无效。混着说会让人跑去反复检查钥匙,而真正要做的是充值。
    # (和坑表「400 和 401/403 不能混报」是同一条教训)
    def t_errors():
        class R:
            def __init__(self, c):
                self.status_code, self.text = c, "{}"
            def json(self):
                return {"error": {"message": "boom"}}
        e402, e401, e503 = (cr._gen_error(R(c)) for c in (402, 401, 503))
        if "余额" not in e402 or "充值" not in e402:
            return f"402 没说清是余额问题:{e402[:60]}"
        if "钥匙" in e402:
            return f"402 误报成了钥匙问题:{e402[:60]}"
        if "钥匙" not in e401:
            return f"401 没说是钥匙问题:{e401[:60]}"
        if "平台" not in e503:
            return f"5xx 没说清是平台自己的问题:{e503[:60]}"
        return None
    check("生图报错分得清余额/钥匙/平台故障", t_errors)

    # 只能照着**真归纳出来的**方案做图,不许 AI 现编文案去渲染
    # (和 use_found_creative 只认搜索结果里的地址是同一个思路)
    def t_needs_plan():
        srv._plans().clear()
        return None if srv.propose_make_creatives().get("error") else "没方案时居然让做图了"
    check("没归纳过方案就不许生图(防AI现编)", t_needs_plan)

    def t_propose():
        # 待办是落盘持久化的,之前跑测试造的还在 → 查重会直接命中,
        # 拿不到"新登记"那条分支。所以先清场,跑完也把自己造的收走,
        # 别把测试垃圾留在用户的保险箱里。
        for k in [k for k, v in srv.PENDING_ACTIONS.items()
                  if v.get("type") == "make_creatives"]:
            srv.PENDING_ACTIONS.pop(k, None)
        srv._plans().clear()
        srv._plans().extend(
            {"命名": f"方案{i}", "主标题": f"Headline {i}", "描述": "Desc.",
             "画面怎么拍": "a roof", "CTA": "Learn More"} for i in (1, 2, 3))
        r = srv.propose_make_creatives()
        if not r.get("action_id"):
            return f"没登记出待办:{r}"
        if "$" not in str(r.get("预估花费")):
            return "确认前没告诉用户大概花多少钱"
        if len(r.get("要做的图") or []) != 3:
            return "留空时应该三版全做"
        r2 = srv.propose_make_creatives(variants="1,3")
        if [x["第几版"] for x in r2.get("要做的图") or []] != [1, 3]:
            return "指定 variants='1,3' 没只挑这两版"
        if srv.propose_make_creatives(variants="1,3").get("action_id") != r2["action_id"]:
            return "同样的一单被重复登记了(查重没生效)"
        # 生图要花钱,同样必须守保险丝:登记和执行不能在同一条用户消息里
        if "保险丝" not in str(srv.confirm_action(r2["action_id"]).get("error")):
            return "生图没走保险丝,AI 可以自问自答直接花钱"
        for k in [k for k, v in srv.PENDING_ACTIONS.items()
                  if v.get("type") == "make_creatives"]:
            srv.PENDING_ACTIONS.pop(k, None)
        srv._save_actions()
        srv._plans().clear()
        return None
    check("生图走确认关卡:先报价、能挑版、查重、保险丝", t_propose)

    # 生图很贵($0.20/张),所以**能失败的免费步骤必须排在花钱之前**。
    # 血泪:第一版把文件名放在 cr.render() 之后算,图已生成、钱已付,却卡在起名上,
    # 那一张的钱白花了。这条只能靠读源码守 —— 真跑一次要花钱。
    def t_cheap_first():
        import inspect
        # 注意:注释里也写着 cr.render(),直接 index 会先命中注释 —— 要按**真正的调用点**找
        src = "\n".join(ln for ln in inspect.getsource(srv._execute_make_creatives).splitlines()
                         if not ln.strip().startswith("#"))
        i_render, i_name = src.index("img = cr.render("), src.index("fname =")
        if i_name > i_render:
            return "文件名在生图之后才算 —— 起名失败会让已付费的图白扔"
        if src.index("check_balance") > i_render:
            return "余额检查排在生图之后,等于没查"
        if src.index("local.write_bytes") > src.index("upload_asset"):
            return "本地留档排在上传之后 —— 上传失败就等于钱付了图没了"
        return None
    check("花钱之前先做完所有免费又可能失败的步骤", t_cheap_first)

    # 存进媒体库时 mediaName 是必填的,不给就 400(实测踩过,那一张的钱也白花了)
    def t_media_name():
        import inspect
        src = inspect.getsource(srv._execute_make_creatives)
        if "media_name=" not in src:
            return "上传时没传 mediaName,存媒体库会报 400"
        return None
    check("上传素材带上必填的 mediaName", t_media_name)

    # 成本必须是实测值。按公开单价算出来的 $0.05 实际差了 4 倍,
    # 报低了会让用户以为很便宜,批量生成时才发现烧了不少。
    def t_cost():
        c = cr.COST_PER_IMAGE_USD
        if not (0.10 <= c <= 0.40):
            return f"单张成本 ${c} 不在实测区间(2026-08-20 实测 $0.203)"
        return None
    check("单张成本是实测校正过的值", t_cost)

    # 查竞品的关键词**不许 AI 自己编**。实测:用户只问了句"同行都在跑什么广告",
    # AI 编了个 roof,而账户实际投的是 gutter/window —— 查回来全是别的行业的广告。
    # 和命名那条「类型词对不上必须问用户」是同一条规矩,同样要守在代码里。
    def t_keyword_guard():
        import inspect
        src = inspect.getsource(srv.search_competitor_ads)
        if "_account_categories" not in src:
            return "查竞品时没有比对账户实际在投的品类,AI 编的关键词没人拦"
        if "ℹ️品类提示" not in src:
            return "关键词对不上时没有给出提示字段"
        for tbl, name in (([f.__name__ for f in srv.NEWSBREAK_TOOLS], "Gemini 工具表"),
                          (list(srv.OPENAI_TOOL_FUNCS), "OpenAI 函数表"),
                          ([t["function"]["name"] for t in srv.OPENAI_TOOL_SCHEMAS], "OpenAI schema")):
            if "my_ad_categories" not in tbl:
                return f"my_ad_categories 没注册进{name}"
        return None
    check("查竞品的关键词不许 AI 自己编(代码层比对账户品类)", t_keyword_guard)

    # 提示词里也要写明白,两种语言都要 —— 只写中文的话英文模式下模型会照旧自己编
    def t_keyword_prompt():
        zh, en = srv.SYSTEM_PROMPT, srv.SYSTEM_PROMPT_EN
        if "my_ad_categories" not in zh:
            return "中文提示词没说「先看账户在投什么,别自己编关键词」"
        if "my_ad_categories" not in en:
            return "英文提示词没说这条 —— 英文模式下模型会照旧自己编关键词"
        return None
    check("双语提示词都写了「关键词先看账户、别自己编」", t_keyword_prompt)

    # 站点图标:四个页面都要有,而且**未登录时也得能取到** ——
    # 图标是在登录页就要显示的,被登录门拦住的话标签页还是那个默认小地球。
    def t_favicon():
        import pathlib as _pl
        missing = [f for f in ("login.html", "platforms.html", "index.html", "dashboard.html")
                   if "favicon.svg" not in (_pl.Path("static") / f).read_text()]
        if missing:
            return f"这些页面没有站点图标:{missing}"
        for f in ("favicon.svg", "favicon-32.png", "apple-touch-icon.png"):
            if not (_pl.Path("static") / f).exists():
                return f"图标文件 {f} 不存在(页面引用了但文件没有 = 还是默认图标)"
        return None
    check("四个页面都有站点图标,文件也在", t_favicon)

    # 三处工具表漏注册一处,就是"某条大脑路径上这个工具不存在"
    def t_registered():
        tables = {
            "Gemini 工具表": [f.__name__ for f in srv.NEWSBREAK_TOOLS],
            "OpenAI 函数表": list(srv.OPENAI_TOOL_FUNCS),
            "OpenAI schema": [t["function"]["name"] for t in srv.OPENAI_TOOL_SCHEMAS],
        }
        miss = [k for k, v in tables.items() if "propose_make_creatives" not in v]
        return f"这些表里漏了 propose_make_creatives:{miss}" if miss else None
    check("生图工具三处工具表都注册了", t_registered)


def test_guardrail():
    import agent_server as srv

    print("\n【3】写操作护栏(假 id,不会动真广告)")

    srv._new_turn()
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
        srv._executed().append({"id": "xxx", "ok": True, "detail": "{}"})
        reply = srv._finalize("搞定")["reply"]
        srv._executed().clear()
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
        srv._new_turn()          # 模拟「下一条用户消息」
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
        srv._new_turn()          # 模拟「下一条用户消息」
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
        srv._new_turn()          # 模拟「下一条用户消息」
        r = srv.propose_status_change("campaign", "c9", "ON", name="x",
                                      extra_targets=[{"level": "怪层级", "id": "1"}])
        if "error" not in r:
            srv.cancel_action(r.get("action_id", ""))
            return "非法 level 居然被放行"
        return True

    def t_pause_no_warning():
        srv._new_turn()          # 模拟「下一条用户消息」
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
        srv._new_turn()          # 模拟「下一条用户消息」
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
        srv._new_turn()          # 模拟「下一条用户消息」
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


# ============ 3.4 并发:两段对话同时提问时,护栏不能串 ============


def test_concurrent_turns():
    print("\n【3.4】两段对话同时提问:保险丝和执行台账不许串")
    import contextvars
    import agent_server as srv

    def in_own_request(fn):
        """在一份**独立的上下文**里跑,模拟另一个请求 —— FastAPI 就是这么隔离的。"""
        return contextvars.copy_context().run(fn)

    def seq_is_per_request():
        # 保险丝比的是"登记时的号"和"这一轮的号"。号要是全局的,
        # B 一来把号推高,A 同一条消息里再 confirm 就拦不住了 → 两阶段确认失效
        before = srv._seq()
        a_seq = in_own_request(lambda: (srv._new_turn(), srv._seq())[1])
        b_seq = in_own_request(lambda: (srv._new_turn(), srv._seq())[1])
        if a_seq == b_seq:
            return f"两个请求拿到同一个号({a_seq}),保险丝会误判"
        if srv._seq() != before:
            return "请求里开的新一轮泄漏回了外面 —— 说明根本没隔离"
        return True
    check("每个请求有自己的序号", seq_is_per_request)

    def fuse_still_bites_after_another_request():
        # 真场景:A 登记 → B 插进来发了条消息 → A 同一条消息里想直接执行。
        # 必须照样被拦住。
        box = {}

        def a_register():
            srv._new_turn()
            r = srv.propose_status_change("campaign", "0", "OFF", name="并发冒烟-A")
            box["aid"] = r.get("action_id", "")
            box["ctx"] = None
            return r

        ctx_a = contextvars.copy_context()
        ctx_a.run(a_register)
        in_own_request(lambda: (srv._new_turn(), None)[1])     # B 发了一条消息,全局发号器 +1
        err = ctx_a.run(lambda: srv.confirm_action(box["aid"])).get("error", "")
        srv.cancel_action(box["aid"])
        if "保险丝" not in str(err):
            return f"别的请求插进来之后,保险丝就拦不住了:{err}"
        return True
    check("别的对话插队也拦得住自问自答", fuse_still_bites_after_another_request)

    def executed_ledger_is_per_request():
        # 台账要是全局的:B 开个头就把 A 的执行记录清空 → A 的回复盖不上 🔒,
        # 甚至被误判成谎报当众自我拆穿;反过来 B 会盖上 A 的记录,声称做了没做的事
        # 顺序要按真实交错来:A 执行完 → **B 插进来开新一轮** → A 才收尾。
        # 台账要是全局的,B 那一下就把 A 的记录清空了。
        ctx_a = contextvars.copy_context()
        ctx_a.run(lambda: (srv._new_turn(), srv._executed().append({"id": "A1", "ok": True, "detail": "{}"})))
        reply_b = in_own_request(lambda: (srv._new_turn(), srv._finalize("搞定")["reply"])[1])
        reply_a = ctx_a.run(lambda: srv._finalize("搞定")["reply"])
        if "系统核验" not in reply_a:
            return "真执行的那一轮没盖上核验章"
        if "系统核验" in reply_b or "A1" in reply_b:
            return "另一个请求的回复盖上了别人的执行记录(会声称做了没做的事)"
        return True
    check("执行台账每个请求一份", executed_ledger_is_per_request)


# ============ 3.39 磁盘不能被慢慢吃掉 ============


def test_disk_growth():
    print("\n【3.39】本地留档有上限")
    import agent_server as srv

    def generated_is_capped():
        # 这些图已经传进平台素材库了,本地这份只是"上传失败时别把钱花的东西弄丢"的保险。
        # 不设上限的话,长驻服务跑几个月磁盘就被它吃掉了(每张几百 KB,只增不减)。
        d = srv._GENERATED_DIR
        made = []
        try:
            import os
            for i in range(srv._KEEP_GENERATED + 5):
                f = d / f"smoketest-{i:04d}.jpg"
                f.write_bytes(b"x")
                os.utime(f, (1000 + i, 1000 + i))     # 造出先后顺序
                made.append(f)
            srv._prune_generated()
            left = {f.name for f in d.glob("smoketest-*.jpg")}
            if len(list(d.glob("*.jpg"))) > srv._KEEP_GENERATED:
                return "超过上限了还没清"
            if f"smoketest-0000.jpg" in left:
                return "删的不是最旧的"
            if f"smoketest-{srv._KEEP_GENERATED + 4:04d}.jpg" not in left:
                return "最新的反而被删了"
            return True
        finally:
            for f in made:
                f.unlink(missing_ok=True)     # 测试自己造的垃圾自己收走
    check("生图的本地留档只留最近若干张", generated_is_capped)

    def prune_is_called():
        import inspect
        src = "\n".join(ln for ln in inspect.getsource(srv._execute_make_creatives).splitlines()
                         if not ln.strip().startswith("#"))
        if "_prune_generated()" not in src:
            return "落盘之后没调清理,上限等于没设"
        return True
    check("落盘之后真的会调清理", prune_is_called)


# ============ 3.40 CSS 变量名不许编 ============


def test_css_vars():
    print("\n【3.40】CSS 变量:用到的必须真的定义过")
    import pathlib
    import re

    # 血泪:日期弹层写了 `background: var(--surface-0)`,而这个变量**根本不存在**
    # (真名是 --surface-1)。CSS 遇到取不到值的变量**不报错**,那条声明直接作废 →
    # 弹层没有底色,浮在图表上是"透视"的,后面的数字全透出来。
    # 这类错误浏览器不吭声、node --check 也管不着,只能这样扫。
    def scan(f):
        src = pathlib.Path(f).read_text()
        css = "\n".join(re.findall(r"<style>([\s\S]*?)</style>", src))
        defined = set(re.findall(r"(--[a-zA-Z0-9-]+)\s*:", css))
        used = set(re.findall(r"var\((--[a-zA-Z0-9-]+)", src))
        return sorted(used - defined)

    bad = {}
    for f in ("static/dashboard.html", "static/index.html",
              "static/platforms.html", "static/login.html"):
        miss = scan(f)
        if miss:
            bad[f] = miss
    check("四个页面用到的 CSS 变量都定义过",
          lambda: True if not bad else f"用了没定义的变量(会静默失效):{bad}")

    def popup_is_opaque():
        # 浮层压在图表上面,底色必须是实色,而且必须是真存在的变量
        src = pathlib.Path("static/dashboard.html").read_text()
        m = re.search(r"\.date-pop\s*\{([^}]*)\}", src)
        if not m:
            return "找不到 .date-pop 的样式"
        body = re.sub(r"/\*[\s\S]*?\*/", "", m.group(1))
        bg = re.search(r"background:\s*([^;]+);", body)
        if not bg:
            return "日期弹层没有设底色 —— 浮在图表上会透视"
        if "transparent" in bg.group(1):
            return f"日期弹层底色是透明的:{bg.group(1).strip()}"
        return True
    check("日期弹层有实色底(不会透出后面的图表)", popup_is_opaque)


# ============ 3.41 大屏的时间范围(含自定义起止日期) ============


def test_dash_range():
    print("\n【3.41】大屏时间范围")
    import agent_server as srv
    import scheduler as sched

    def beijing_not_utc():
        # 用 UTC 的话,北京时间 00:00~08:00 这 8 小时里 UTC 还停在昨天,
        # 大屏会少一天数据(和建广告命名那条是同一个坑)
        _, end, _ = srv._resolve_range(7, "", "")
        if end != sched.now_beijing().date():
            return f"结尾不是北京时间的今天:{end}"
        return True
    check("按北京时间算今天", beijing_not_utc)

    def custom_wins():
        a, b, n = srv._resolve_range(30, "2026-08-01", "2026-08-10")
        return True if (str(a), str(b), n) == ("2026-08-01", "2026-08-10", 10) else f"{a}~{b} {n}天"
    check("给了起止日期就按它算", custom_wins)

    def swapped_is_fixed():
        # 用户把起止填反了,替他调过来就行,不用报错烦他
        a, b, _ = srv._resolve_range(30, "2026-08-10", "2026-08-01")
        return True if str(a) < str(b) else "填反了没被纠正"
    check("起止填反了自动纠正", swapped_is_fixed)

    def future_end_clamped():
        _, b, _ = srv._resolve_range(30, "2026-08-01", "2099-01-01")
        return True if b == sched.now_beijing().date() else f"没截到今天:{b}"
    check("结束日期在未来会截到今天", future_end_clamped)

    def bad_input_says_why():
        # 直接把这些甩给平台的话,用户看到的是一句英文的 Invalid parameters
        for args, kw in ((("2026/08/01", "2026-08-10"), "格式"),
                         (("2025-01-01", "2026-08-10"), "180")):
            try:
                srv._resolve_range(30, *args)
                return f"{kw} 这种情况没被拦住"
            except ValueError as e:
                if kw not in str(e):
                    return f"报错没说清原因:{e}"
        return True
    check("非法范围拦住并说人话", bad_input_says_why)

    def analyze_follows_dashboard():
        # 诊断必须和大屏看的是同一段时间,否则 AI 说的和用户看到的对不上
        import inspect
        if "start" not in inspect.getsource(srv.AnalyzeIn):
            return "AnalyzeIn 不接受自定义范围"
        if "body.start" not in inspect.getsource(srv.analyze_data):
            return "诊断没把自定义范围传下去"
        return True
    check("AI 诊断跟着大屏的时间范围走", analyze_follows_dashboard)


# ============ 3.42 模型空回复:要重试、要说清原因 ============


def test_empty_reply():
    print("\n【3.42】模型一个字都没返回时的处理")
    import inspect
    import agent_server as srv

    def once_returns_finish_reason():
        # 上游只有 finish_reason 能说明"为什么空"(截断?被安全策略拦了?)。
        # 原来完全没读它,于是那种情况只能甩一句"没有返回文字",日志里也查不到线索
        src = inspect.getsource(srv._openai_once)
        if "finish_reason" not in src:
            return "_openai_once 没把 finish_reason 带回来"
        if src.count("return") < 2:
            return "返回值没改全"
        return True
    check("拿得到 finish_reason(否则查不出为什么空)", once_returns_finish_reason)

    def retries_once():
        # 实测同样的问题连发三次都正常 —— 空回复是偶发,重试一次基本就好了。
        # 但只重试**一次**:真坏了的话反复重试只是白烧额度
        for fn, name in ((srv.ask_openai, "ask_openai"), (srv._gemini_loop, "_gemini_loop")):
            src = "\n".join(ln for ln in inspect.getsource(fn).splitlines()
                             if not ln.strip().startswith("#"))
            if "retried_empty" not in src:
                return f"{name} 空回复时不会重试"
        return True
    check("空回复会自动重试一次(两条大脑路径都要)", retries_once)

    def message_is_actionable():
        # "(ChatGPT 没有返回文字)"是死胡同:用户不知道是自己的问题还是系统坏了,
        # 也不知道下一步该干嘛
        for lang in ("zh", "en"):
            m = srv._empty_reply_msg("length", lang)
            if "finish_reason" in m and lang == "zh":
                return "中文版把技术字段直接甩给用户了"
            if len(m) < 30:
                return f"{lang} 的说明太短,等于没说"
        zh = srv._empty_reply_msg("content_filter", "zh")
        if "安全策略" not in zh:
            return "没把 content_filter 翻译成人话"
        en = srv._empty_reply_msg("", "en")
        if not en.startswith("\u26a0"):
            return "英文版没走英文分支"
        return True
    check("给用户的说明是人话、双语、说得出下一步", message_is_actionable)

    def no_dead_end_left():
        # 老的死胡同话术不许残留
        src = _src_no_comments("agent_server.py")
        for bad in ("(ChatGPT 没有返回文字)", "(Gemini 没有返回文字)"):
            if bad in src:
                return f"还留着死胡同话术:{bad}"
        return True
    check("旧的「没有返回文字」话术已清干净", no_dead_end_left)


def _src_no_comments(path: str) -> str:
    import pathlib
    return "\n".join(ln for ln in pathlib.Path(path).read_text().splitlines()
                      if not ln.strip().startswith("#"))


# ============ 3.45 按人隔离:别人的东西不能串过来 ============


# ============ 3.43 「别再烧下去了」闸门 ============

def test_loop_guard():
    """模型在工具循环里原地打转时,要能自己停下来,而不是转满十轮把钱烧完。

    **必须真跑一遍 `ask_openai`**,不能只搜源码里有没有那几个名字 ——
    闸门写对了但没接进循环,搜源码照样全绿(踩过:linkifyBubble 那条)。
    """
    import contextvars
    import inspect
    import os
    import agent_server as srv

    print("\n【3.43】绕住了就停下(别一直烧钱)")

    # 没钥匙时 openai.OpenAI() 构造就会抛,和闸门无关 —— 补一把假的,跑完还原
    _had = os.environ.get("OPENAI_API_KEY")
    os.environ.setdefault("OPENAI_API_KEY", "sk-smoke-test-not-a-real-key")

    class Msg:
        def __init__(self, content=""):
            self.content = content

    def run_with_stub(reply_fn):
        """把 `_openai_once` 换成假的,数一数它被调了几次。"""
        calls = []

        def fake_once(client, model, msgs, streaming):
            calls.append(model)
            return reply_fn(len(calls))

        real = srv._openai_once
        srv._openai_once = fake_once
        ctx = contextvars.copy_context()          # 闸片是 contextvar,一轮一份

        def go():
            srv.CURRENT_GUARD.set(srv.LoopGuard())
            return srv.ask_openai([srv.ChatMessage(role="user", content="查一下")], "zh")
        try:
            return ctx.run(go), calls
        finally:
            srv._openai_once = real

    def tc(name, args_json, idx=0):
        return {"id": "c%d" % idx, "name": name, "arguments": args_json}

    # ---- ① 同一个工具 + 同一份参数反复调 → 停 ----
    def stops_on_repeat():
        out, calls = run_with_stub(
            lambda n: ("", [tc("__fake_tool__", '{"search": "roof"}', n)], "tool_calls", (0, 0)))
        if "停下来" not in out:
            return "没有停,或者停了但没说人话:%r" % out[:120]
        if len(calls) >= srv.MAX_TOOL_ROUNDS:
            return "一直转到轮数上限才停(%d 轮),重复检测没起作用" % len(calls)
        if len(calls) != srv.TOOL_REPEAT_LIMIT:
            return "应该在第 %d 次同样的调用就停,实际问了 %d 轮" % (
                srv.TOOL_REPEAT_LIMIT, len(calls))
        return True
    check("同一个工具+同一份参数调到第 3 次 → 立刻停", stops_on_repeat)

    # ---- ② 参数不一样就不算打转(别误伤正常的多步查询) ----
    def different_args_not_flagged():
        out, calls = run_with_stub(
            lambda n: ("", [tc("__fake_tool__", '{"page": %d}' % n, n)], "tool_calls", (0, 0)))
        if len(calls) != srv.MAX_TOOL_ROUNDS:
            return "参数每次都不同却提前停了(只跑了 %d 轮)" % len(calls)
        if "停下来" not in out:
            return "转满轮数也没停:%r" % out[:120]
        return True
    check("参数不同 = 正常的多步查询,不误判成打转", different_args_not_flagged)

    # ---- ③ 花费到顶 → 停(而且要比轮数上限先踩到) ----
    def stops_on_budget():
        big = srv.TURN_TOKEN_BUDGET // 3 + 1        # 三轮就超
        out, calls = run_with_stub(
            lambda n: ("", [tc("__fake_tool__", '{"page": %d}' % n, n)], "tool_calls",
                       (big // 2, big // 2)))
        if len(calls) >= srv.MAX_TOOL_ROUNDS:
            return "花费闸门没生效,一直转到轮数上限"
        if "额度" not in out:
            return "停是停了,但没说是花费到顶:%r" % out[:120]
        if "万 token" not in out:
            return "没有如实报出花了多少"
        return True
    check("这条消息的花费到顶 → 停,并报出花了多少", stops_on_budget)

    # ---- ④ 上游不给用量时,绝不假装在管钱 ----
    def no_usage_no_fake_number():
        g = srv.LoopGuard()
        g.rounds = 3
        if g.usage_seen:
            return "什么都没记就说自己拿到用量了"
        say = g.spent()
        if "查不到" not in say:
            return "上游没给用量,却报了一个数出来:%r" % say
        # 花费闸门此时必须失效(否则 tokens 恒为 0,永远不触发,或者更糟:瞎估)
        g.tokens_in = 10 ** 9
        if g.before_round() == "tokens":
            return "没拿到真实用量却拿估算数去踩闸门"
        return True
    check("拿不到用量时如实说查不到,不编数字也不假装在管钱", no_usage_no_fake_number)

    # ---- ⑤ 两条大脑路径都要装(只修一边等于没修) ----
    def both_brains_guarded():
        for fn, name in ((srv.ask_openai, "ask_openai"), (srv._gemini_loop, "_gemini_loop")):
            src = _no_comments(inspect.getsource(fn))
            for needle in ("before_round(", "note_call(", "guard.message("):
                if needle not in src:
                    return "%s 里没有 %s" % (name, needle)
            if "range(10)" in src:
                return "%s 还留着写死的 range(10)" % name
        return True
    check("Gemini 和 ofox 两条路都装了闸门", both_brains_guarded)

    # ---- ⑥ 用量在 choices 为空的那个 chunk 上,必须抢在 continue 之前读 ----
    def usage_read_before_continue():
        src = _no_comments(inspect.getsource(srv._openai_once))
        i_usage = max(src.find("took(chunk)"), src.find("chunk.usage"))
        i_skip = src.find("if not chunk.choices")
        if i_usage < 0:
            return "流式路径根本没读用量"
        if i_skip < 0 or i_usage > i_skip:
            return "读用量排在 `if not chunk.choices: continue` 后面 —— 永远读不到"
        return True
    check("流式的用量抢在 `choices 为空就跳过` 之前读", usage_read_before_continue)

    # ---- ⑦ 闸片按请求隔离(不能所有人共用一份) ----
    def guard_is_per_request():
        # 两个坑:
        # ① **要留着对象本身比,不能比 id()** —— 第一个闸片在这里已经没人引用了,
        #    回收之后第二个很可能分到同一个地址,id 相等,测试就误报;
        # ② **要用 `Context()` 而不是 `copy_context()`** —— 后者会把外面已经
        #    设过的值一起继承过来,于是两份都是同一个,被上一个测试的残留骗过去。
        seen = []
        for _ in range(2):
            contextvars.Context().run(
                lambda: (srv._new_turn("campaign"), seen.append(srv._guard())))
        if seen[0] is seen[1]:
            return "两个请求拿到的是同一份闸片,A 的轮数会算到 B 头上"
        seen[0].rounds = 7
        if seen[1].rounds != 0:
            return "两份闸片的状态串了"
        if contextvars.Context().run(srv.CURRENT_GUARD.get) is not None:
            return "闸片用了可变对象当 ContextVar 的默认值(所有请求会共用一份)"
        return True
    check("闸片按请求隔离,不是全局一份", guard_is_per_request)

    # ---- ⑧ 停下来那句话要能让人知道下一步干嘛 ----
    def message_is_actionable():
        for lang in ("zh", "en"):
            g = srv.LoopGuard()
            g.rounds, g.stop = 5, "rounds"
            g.add_usage(6800, 8000)
            m = g.message(lang)
            if len(m) < 80:
                return "%s 版太短,等于只说了句'已中止'" % lang
            if "token" not in m.lower():
                return "%s 版没告诉用户花了多少" % lang
        zh = srv.LoopGuard()
        zh.rounds, zh.stop = 5, "rounds"
        if "你可以" not in zh.message("zh"):
            return "中文版没给下一步该怎么办"
        return True
    check("停下来时说清楚:发生了什么 / 花了多少 / 下一步", message_is_actionable)

    if _had is None:
        os.environ.pop("OPENAI_API_KEY", None)


# ============ 3.44 编造的落地页预览链接要当场拆穿 ============

def test_fake_preview_link():
    """线上实测:模型整轮**没调任何生成工具**,却照着
    `<8位十六进制>-version-a---<英文名>.html` 这个形状编了一个链接给用户。
    点开只有一句 `{"error":"落地页预览不存在或已过期"}` —— 用户完全看不出是编的,
    只会以为"这个功能坏了"。提示词里早写着"不许编地址",拦不住。
    """
    import agent_server as srv
    import landing_lab as lp

    print("\n【3.44】编造的落地页预览链接")

    made_up = "deadbeef-version-a---this-file-never-existed.html"
    real = lp.recent_previews(1)

    def stamps_fake_link():
        out = srv._finalize("两版做好了:\n· /landing-pages/" + made_up, "zh")["reply"]
        if "系统核验" not in out:
            return "编的链接没被拆穿"
        if made_up not in out:
            return "没点名是哪个链接编的"
        if "是编的" not in out:
            return "话说得太含糊,用户看不出这是幻觉"
        return True
    check("回复里给了不存在的预览链接 → 当场拆穿", stamps_fake_link)

    def real_link_not_flagged():
        # 误伤比漏判更糟:真链接被打上"这是编的",用户就再也不信这个提示了
        if not real:
            return None                     # 盘上没有预览,这条跳过
        out = srv._finalize("看这里 /landing-pages/" + real[0]["file"], "zh")["reply"]
        if "系统核验" in out:
            return "真实存在的链接被误判成编造的"
        return True
    check("真实存在的链接不会被误伤", real_link_not_flagged)

    def independent_of_pending_box():
        # 老的 ⚠️ 拆穿章只在"保险箱里还有待办"时才触发。编链接和待办**毫无关系**,
        # 挂在那条分支下面的话,保险箱一空就什么都抓不到了。
        import inspect
        src = _no_comments(inspect.getsource(srv._finalize))
        if src.count("_fake_preview_note(") < 3:
            return "只在部分分支上查了链接,另外的分支漏掉"
        saved = dict(srv.PENDING_ACTIONS)
        srv.PENDING_ACTIONS.clear()          # 保险箱空着,老的拆穿章不会触发
        try:
            out = srv._finalize("好了:/landing-pages/" + made_up, "zh")["reply"]
        finally:
            srv.PENDING_ACTIONS.update(saved)
        if made_up not in out or "系统核验" not in out:
            return "保险箱空着时抓不到编造的链接"
        return True
    check("保险箱空着也照样抓(和待办无关)", independent_of_pending_box)

    def four_o_four_is_helpful():
        # 甩一句 JSON 等于把用户扔在死胡同里:他不知道是编的还是过期的,也不知道下一步
        r = srv.landing_page_preview(made_up)
        if r.status_code != 404:
            return "状态码不是 404"
        body = r.body.decode()
        if "error" in body[:40] and "<" not in body[:40]:
            return "还在甩 JSON,没给人话"
        if "编出来" not in body:
            return "没告诉用户最可能的原因是助手编了地址"
        for row in lp.recent_previews(3):
            if row["file"] not in body:
                return "没把盘上真实存在的预览列出来给他点"
        return True
    check("404 页面说人话,并列出真实存在的预览", four_o_four_is_helpful)

    def path_traversal_still_blocked():
        # 加了兜底清单别顺手把目录穿越也放开了
        if lp.preview_exists("../../.env"):
            return "preview_exists 放行了目录穿越"
        if lp.preview_exists("x.html/../../etc/passwd"):
            return "preview_exists 放行了目录穿越"
        if lp.preview_exists("notes.txt"):
            return "非 .html 也算存在"
        return True
    check("回查函数挡得住目录穿越", path_traversal_still_blocked)

    def prompt_forbids_inventing():
        # 提示词不是防线(它拦不住,这次就是证明),但两种语言都得写上 ——
        # 少写一边,那个语言下模型连"这事不许干"都不知道
        import contextvars
        for lang, needle in (("zh", "一个 /landing-pages/... 链接都不许写出来"),
                             ("en", "NEVER write a /landing-pages/... link")):
            ctx = contextvars.Context()
            sp = ctx.run(lambda: (srv.CURRENT_CHAT_MODE.set("landing"),
                                  srv._system_prompt_now(lang))[1])
            if needle not in sp:
                return "%s 的落地页提示词里没写「不许自己编预览链接」" % lang
        return True
    check("中英提示词都写了不许自己编预览链接", prompt_forbids_inventing)


# ============ 3.45 执行时要把校验再做一遍(老待办身上没做过) ============

def test_execute_time_recheck():
    """待办会在保险箱里躺很久(重启也不丢),而校验是后来才加的。

    **实测**:彩排时发现保险箱里正躺着一条「加检查之前」登记的假 A/B ——
    A、B 是同一个文件。它的两个 sha256 都能对上,于是一路放行,
    发出去两个网址跑同一个页面,钱照花、数据照上报,而从外面完全看不出来。
    这是「两阶段流程里,校验只做在前一阶段」的通病。
    """
    import hashlib
    import agent_server as srv
    import landing_lab as lp

    print("\n【3.45】执行时把校验再做一遍")

    same = sorted(lp.GENERATED_DIR.glob("*.html"))
    if not same:
        check("(跳过:盘上没有生成好的落地页)", lambda: True)
        return
    one = same[0]
    sha = hashlib.sha256(one.read_bytes()).hexdigest()

    def base_action(**kw):
        return {"type": "publish_landing_pages", "domain": "example.com", "slug": "t",
                "cta_url": "https://trk.example.com/cf/click/1", "tracking_script": "<script></script>",
                "variant_a_file": one.name, "variant_b_file": one.name,
                "variant_a_sha256": sha, "variant_b_sha256": sha, **kw}

    def refuses_identical_ab():
        r = srv._execute_publish_landing_pages(base_action())
        if "error" not in r:
            return "两版一模一样的老待办被放行了"
        if "完全相同" not in r["error"]:
            return "拦是拦了,但没说清为什么:%r" % r["error"][:80]
        return True
    check("老待办的 A/B 是同一个文件 → 执行时拦住", refuses_identical_ab)

    def allow_identical_still_works():
        # A/A 测试是真实用法,用户明说了就得放行 —— 别把口子一起焊死
        real = srv.cfp.publish_ab
        srv.cfp.publish_ab = lambda *a, **k: {"done": True, "detail": "stub"}
        try:
            r = srv._execute_publish_landing_pages(base_action(allow_identical=True))
        finally:
            srv.cfp.publish_ab = real
        if r.get("error"):
            return "明说了要做 A/A 还是被拦:%r" % str(r["error"])[:80]
        return True
    check("用户明说要做 A/A 测试的仍然放行", allow_identical_still_works)

    def missing_file_is_plain_language():
        # 文件被清理掉的老待办:必须是人话,而且**绝不能**走到发布那一步。
        # **要走 confirm_action**,不能直接调 _execute_ —— 兜住异常、翻成人话的那层
        # 就在 confirm_action 里,绕过它测出来的是一个裸 ValueError(第一版就这么错了)。
        aid = "smoke-missing-file"
        srv.PENDING_ACTIONS[aid] = base_action(
            variant_a_file="never-existed-xyz.html", user_id="", seq=-1)
        try:
            r = srv.confirm_action(aid)
        finally:
            srv.PENDING_ACTIONS.pop(aid, None)
            srv._save_actions()
        if "error" not in r:
            return "文件不存在却没报错"
        if "找不到" not in str(r["error"]):
            return "报错不是人话:%r" % str(r["error"])[:80]
        return True
    check("引用了已被清理文件的老待办 → 说人话,不发布", missing_file_is_plain_language)

    def swap_refuses_identical_landers():
        r = srv._execute_swap_campaign_landers(
            {"type": "swap_campaign_landers", "flow_id": "f" * 24, "put_body": {},
             "fingerprint": "x", "expect_landers": ["abc123", "ABC123"]})
        if "error" not in r:
            return "两个 Lander 是同一个,却放行了"
        if "同一个" not in r["error"]:
            return "拦是拦了,但没说清为什么:%r" % str(r["error"])[:80]
        return True
    check("换页待办的两个 Lander 是同一个 → 执行时拦住", swap_refuses_identical_landers)


# ============ 3.46 从投放链接认 campaign,绝不许按名字猜 ============

def test_campaign_id_from_link():
    """**实测踩过的真事**:用户把投放链接
    `https://trk.…/cf/r/6a0fc07a5357ae0012e9b844?CALLBACK_PARAM=…` 贴进来,
    而代码只认查询参数 `cpid=` —— 抠不出来,模型于是改成按名字搜,
    把 `NewsBreak_333_Windows_20260522` 搜成了 `fb小苏苏-system1 - window replacement`。
    差一点就把买来的流量换到别人的计划上。

    真相是:**Campaign Tracking URL 的 id 在路径里**(`/cf/r/<24位id>`),
    实测账号里 50 条 campaign 的 `url` 字段全是这个形状;`?cpid=` 是**落地页**地址的形式。
    """
    import contextvars
    import inspect
    import agent_server as srv
    import clickflare_client as cfc

    print("\n【3.46】从投放链接认 campaign")

    ID = "6a0fc07a5357ae0012e9b844"

    def reads_path_form():
        # 平台给的真实 Campaign Tracking URL
        u = ("https://trk.example.com/cf/r/" + ID +
             "?CALLBACK_PARAM=__CALLBACK_PARAM__&OS=__OS__&CAMPAIGN_ID=__CAMPAIGN_ID__")
        got = cfc.campaign_id_from(u)
        if got != ID:
            return "投放链接里的 id 没认出来:%r" % got
        return True
    check("投放链接 /cf/r/<id> 里的 campaign id 认得出", reads_path_form)

    def placeholder_not_mistaken_for_id():
        # 这条链接的查询里有 `CAMPAIGN_ID=__CAMPAIGN_ID__` —— 是**字面占位符**,
        # 不是 id。要是把它当 id 用,查出来就是一片空
        u = "https://trk.example.com/cf/r/" + ID + "?CAMPAIGN_ID=__CAMPAIGN_ID__"
        if cfc.campaign_id_from(u) != ID:
            return "被查询参数里的占位符带偏了"
        return True
    check("不会把 CAMPAIGN_ID=__CAMPAIGN_ID__ 占位符当成 id", placeholder_not_mistaken_for_id)

    def cpid_form_still_works():
        # 落地页地址那种形式不能因为加了新形状就失效
        if cfc.campaign_id_from("https://lp.example.com/w/a/?cpid=" + ID) != ID:
            return "?cpid= 这种老形式认不出来了"
        if cfc.campaign_id_from(ID) != ID:
            return "直接给 24 位 id 也认不出来"
        return True
    check("?cpid= 和裸 id 两种老写法照常认", cpid_form_still_works)

    def wrong_links_are_refused():
        for name, u in (("CTA Click URL", "https://trk.example.com/cf/click/1"),
                        ("落地页地址", "https://lp.example.com/window-260904/a/")):
            try:
                got = cfc.campaign_id_from(u)
                return "%s 竟然认出了 %r" % (name, got)
            except Exception as e:
                # 报错里必须明确禁止改用名字 —— 模型上次就是这么绕过去的
                if "不许" not in str(e) and "绝不" not in str(e):
                    return "%s 的报错没禁止改用名字去猜:%r" % (name, str(e)[:80])
        return True
    check("认不出时报错,并明确禁止改用名字去猜", wrong_links_are_refused)

    def proposal_shows_the_link():
        # 光给名字用户核对不了(账号里 50 条、名字高度相似),
        # 而他手里正好有那条链接 —— 必须原样摆出来给他对。
        # **要真调一次看返回**:只在源码里搜字符串是假通过 ——
        # 那句话在 note 里也出现,把 pending 里那一行删掉照样绿(第一版就这么错了)。
        link = "https://trk.example.com/cf/r/" + ID
        real = cfc.plan_lander_swap
        cfc.plan_lander_swap = lambda *a, **k: {
            "flow_id": "f" * 24, "path": {"名字": "New path"},
            "换之前": {"落地页": []}, "换之后": {"落地页": [{"id": "x", "weight": 50}]},
            "offer有没有动": "没动", "put_body": {}, "fingerprint": "fp",
            "campaign": {"id": ID, "名字": "NewsBreak_333_Windows", "这条计划的投放链接": link}}
        aid = ""
        try:
            out = srv.propose_swap_campaign_landers(link, "a" * 24, "b" * 24)
            aid = out.get("action_id", "")
            pend = out.get("pending") or {}
            if link not in str(pend):
                return "待办里没有把这条计划的投放链接摆出来给用户核对"
            if "投放链接" not in str(out.get("note") or ""):
                return "note 里没要求 AI 把链接贴给用户比对"
        finally:
            cfc.plan_lander_swap = real
            if aid:
                srv.PENDING_ACTIONS.pop(aid, None)
                srv._save_actions()
        return True
    check("换页待办里回显投放链接给用户比对", proposal_shows_the_link)

    def prompt_forbids_name_search():
        for lang, needle in (("zh", "绝不许改用 list_clickflare_campaigns 按名字搜"),
                             ("en", "NEVER fall back to searching campaigns by name")):
            sp = contextvars.Context().run(
                lambda: (srv.CURRENT_CHAT_MODE.set("landing"), srv._system_prompt_now(lang))[1])
            if needle not in sp:
                return "%s 提示词里没禁止「抠不出 id 就按名字搜」" % lang
        return True
    check("中英提示词都禁止「抠不出 id 就按名字搜」", prompt_forbids_name_search)


def test_per_user_isolation():
    print("\n【3.45】按人隔离(缓存 / 方案 / 超时)")
    import agent_server as srv

    def plans_are_per_user():
        # 生图是花钱的($0.20/张)。方案要是全局一份,B 一归纳就把 A 的冲掉,
        # A 接着生图就照着 B 的方案画 —— 花了钱还画错东西
        tok = srv.CURRENT_USER_ID.set("userA")
        try:
            srv._plans().clear()
            srv._plans().append({"主标题": "A 的方案"})
        finally:
            srv.CURRENT_USER_ID.reset(tok)
        tok = srv.CURRENT_USER_ID.set("userB")
        try:
            b_sees = list(srv._plans())
        finally:
            srv.CURRENT_USER_ID.reset(tok)
        tok = srv.CURRENT_USER_ID.set("userA")
        try:
            a_still = list(srv._plans())
        finally:
            srv.CURRENT_USER_ID.reset(tok)
        if b_sees:
            return f"B 看到了 A 的方案:{b_sees}"
        if not a_still:
            return "A 自己的方案反而没了"
        return True
    check("创意方案按人分开(生图照着自己的画)", plans_are_per_user)

    def cats_cache_keyed_by_user():
        # 踩过:键写成 ad_account_id or "_default",网页上的人一般不指定账户,
        # 于是所有人都落在同一个桶里,B 登录后直接读到 A 的品类
        import inspect
        src = inspect.getsource(srv._account_categories)
        if "CURRENT_USER_ID" not in src:
            return "品类缓存的键没带上是谁"
        return True
    check("账户品类缓存的键带了用户", cats_cache_keyed_by_user)

    def brain_client_has_timeout():
        # SDK 默认 600 秒 + 重试 2 次 = 最坏 30 分钟,而前端 180 秒就放弃了:
        # 用户看到"等太久了",后端还在跑(中转商可能照样计费)
        if srv.BRAIN_TIMEOUT_S >= 180:
            return f"大脑超时 {srv.BRAIN_TIMEOUT_S}s 不小于前端的 180s,后端会白跑"
        worst = srv.BRAIN_TIMEOUT_S * (srv.BRAIN_RETRIES + 1)
        if worst > 180:
            return f"算上重试最坏 {worst}s,超过前端的 180s"
        return True
    check("大脑客户端超时小于前端等待上限", brain_client_has_timeout)


# ============ 3.5 大脑接力:超时、切换、冷却(纯逻辑,不联网) ============


def test_brain_relay():
    print("\n【3.5】大脑接力(超时 / 切换 / 冷却)")
    import httpx
    import agent_server as srv
    from google.genai import errors as genai_errors

    def _api_err(code):
        """造一个指定状态码的 genai APIError,不联网。"""
        class _R:
            status_code = code
            headers = {}
            def json(self):
                return {"error": {"code": code, "message": "x"}}
            text = "x"
        return genai_errors.APIError(code, _R().json(), _R())

    def gemini_has_timeout():
        # 没有超时的话,对方不回音就干等到 TCP 自己放弃 —— 用户看到的是"很慢"
        c = srv._gemini_client()
        ms = getattr(getattr(c, "_api_client", None), "_http_options", None)
        ms = getattr(ms, "timeout", None)
        if not ms:
            return "Gemini 客户端没设超时"
        return True
    check("Gemini 客户端带超时", gemini_has_timeout)

    def timeout_triggers_fallback():
        # 血泪:原来只捕 genai APIError,而超时抛的是 httpx.ReadTimeout →
        # 明明配了 ofox 也不切,直接把错甩给用户
        if not srv._should_fallback(httpx.ReadTimeout("x")):
            return "超时没被算进接力条件"
        return True
    check("超时也会切备用通道", timeout_triggers_fallback)

    def key_error_never_falls_back():
        # 401/403 绝不接力:切了只会把配置错误盖过去,用户永远看不到真实原因
        if srv._should_fallback(_api_err(403)):
            return "403 竟然会接力,会掩盖钥匙问题"
        if not srv._should_fallback(_api_err(503)):
            return "503 应该接力"
        return True
    check("钥匙问题(403)绝不接力,503 才接力", key_error_never_falls_back)

    def cooldown_only_for_slow_fails():
        # 冷却是为了"别让用户再白等一次",所以只对**干等型**失败生效;
        # 429 是秒回的,压五分钟没道理
        if srv._slow_fail(_api_err(429)):
            return "429 秒回,不该进冷却"
        if not srv._slow_fail(_api_err(504)):
            return "504 干等型,应该进冷却"
        if not srv._slow_fail(httpx.ReadTimeout("x")):
            return "网络超时应该进冷却"
        return True
    check("只有「干等型」失败才进冷却", cooldown_only_for_slow_fails)

    def cooldown_after_fallback_works():
        # **顺序**:必须等备用通道真的返回了才记冷却。备用通道也坏的话
        # 跳过 Gemini = 一个能用的都不剩(实测撞上过:Gemini 504 + ofox 402)
        import pathlib as _pl
        # 注释里也会正当地写到这些名字,直接 find 会先命中注释(踩过两次)
        src = "\n".join(ln for ln in _pl.Path("agent_server.py").read_text().splitlines()
                        if not ln.strip().startswith("#"))
        seg = src[src.index("def _route_brain"):]
        seg = seg[:seg.index("class _NoBrainKey")]
        i_call, i_mark = seg.find("ask_openai(req.messages"), seg.find("_mark_gemini_down(")
        if i_call < 0 or i_mark < 0:
            return "没找到接力那段代码"
        if i_mark < i_call:
            return "先记了冷却才去调备用通道 —— 备用通道也坏时会把 Gemini 也跳过"
        return True
    check("备用通道成功之后才记冷却(顺序)", cooldown_after_fallback_works)

    def balance_402_is_its_own_message():
        # 402 是"要充值",和"稍后再试"是两回事。混着报会让人跑去改配置
        import openai as _oa
        class _Resp:
            status_code = 402
            headers = {}
            request = None
            def json(self):
                return {"error": {"message": "Insufficient credits. Current balance: $-0.09"}}
        e = _oa.APIStatusError("402", response=_Resp(), body=None)
        code, msg = srv._brain_error(e)
        if code != 402 or "充值" not in msg:
            return f"402 没被单独翻译:{code} / {msg[:60]}"
        return True
    check("余额不足(402)单独说清要充值", balance_402_is_its_own_message)


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

    def t_icon_public():
        r = client.get("/static/favicon.svg")
        if r.status_code != 200:
            return f"未登录取不到站点图标(HTTP {r.status_code})—— 登录页的标签页会是默认图标"
        return None
    check("未登录也能取到站点图标", t_icon_public)

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
    test_concurrent_turns()
    test_disk_growth()
    test_css_vars()
    test_dash_range()
    test_empty_reply()
    test_loop_guard()
    test_fake_preview_link()
    test_execute_time_recheck()
    test_campaign_id_from_link()
    test_per_user_isolation()
    test_brain_relay()
    test_newsbreak_readonly()
    test_creative_render()
    test_http()

    print("\n" + "=" * 60)
    print(f"结果:{len(PASSED)} 通过 / {len(FAILED)} 失败")
    if FAILED:
        print("\n失败项:")
        for name, why in FAILED:
            print(f"  ❌ {name}\n     {why}")
        sys.exit(1)
    print("✅ 全部通过,可以放心提交")
