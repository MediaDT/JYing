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
        old = dict(srv._CREATIVE_MODELS)
        try:
            srv._CREATIVE_MODELS.clear()
            srv._CREATIVE_MODELS["only-one"] = {"版式": "x"}
            r = srv.summarize_creative_patterns()
            if "error" not in r or "2 条" not in r["error"]:
                bad.append(f"只有 1 条时没拒绝归纳:{r}")
        finally:
            srv._CREATIVE_MODELS.clear()
            srv._CREATIVE_MODELS.update(old)
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
        stale = ("代码精确排版", "代码把标题", "composited\n     by code", "代码叠字")
        hits = []
        for text, where in ((srv.SYSTEM_PROMPT, "中文提示词"),
                            (srv.SYSTEM_PROMPT_EN, "英文提示词"),
                            (inspect.getdoc(srv.propose_make_creatives) or "", "propose docstring"),
                            (inspect.getdoc(srv._execute_make_creatives) or "", "执行 docstring"),
                            (inspect.getsource(srv.propose_make_creatives), "提议话术/schema")):
            for kw in stale:
                if kw.replace("\n     ", " ") in text.replace("\n", " "):
                    hits.append(f"{where} 还在说「{kw.strip()}」")
        return "默认已经不叠字了,但这些地方还在描述旧行为:" + ";".join(hits) if hits else None
    check("对用户的说法和代码实际行为一致(没有残留的旧话术)", t_copy_matches_behavior)

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
        srv._CREATIVE_PLANS.clear()
        return None if srv.propose_make_creatives().get("error") else "没方案时居然让做图了"
    check("没归纳过方案就不许生图(防AI现编)", t_needs_plan)

    def t_propose():
        # 待办是落盘持久化的,之前跑测试造的还在 → 查重会直接命中,
        # 拿不到"新登记"那条分支。所以先清场,跑完也把自己造的收走,
        # 别把测试垃圾留在用户的保险箱里。
        for k in [k for k, v in srv.PENDING_ACTIONS.items()
                  if v.get("type") == "make_creatives"]:
            srv.PENDING_ACTIONS.pop(k, None)
        srv._CREATIVE_PLANS.clear()
        srv._CREATIVE_PLANS.extend(
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
        srv._CREATIVE_PLANS.clear()
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
