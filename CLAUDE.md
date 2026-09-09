# my-agent · NewsBreak 对话式广告投放助手

> 本文件是项目的「记忆文件」:目标、架构、进度、决策与坑,全在这里。
> Claude Code 每次会话会自动读取本文件;人也可以当项目手册看。
> **有重大进展或决策变化时,请同步更新本文件。**

## 一、这是什么项目

用「聊天」的方式管理 NewsBreak 平台的广告投放:查数据、看报表、开关投放,
以后还能对话式建广告。开发者 Cole 是编程新手,本项目边做边学。

**产品铁律:把使用者当成彻底的投放小白** —— 一步只问一件事;每次要信息都带
"大白话解释 + 例子 + 推荐默认值";多步流程报进度(第N步/共M步);用户懵了给选项。
(已写入 agent_server.py 的 SYSTEM_PROMPT,改提示词时不许丢。)

## 二、合作规矩(不可违反)

1. **`~/workspace/qx-ad-bot` 只许读、绝对不许改**——它是参考样板(生产级),不是本项目;
2. 动手前先用一两句话说明要做什么;
3. 解释用大白话,Cole 需要边做边学;
4. `.env` 里是真实密钥:不外传、不提交 git、不写进本文件;
   **本文件已推到 GitHub(MediaDT/JYing),所以公司名、org id、广告账户 id、
   真实 campaign/ad id 一律不写进来** —— 要用现查(见第九节「账户事实」那条命令);
5. **改完代码必跑五套测试全绿才提交 git**:`./venv/bin/python smoke_test.py`(270)、`node frontend_test.js`(101)、`node dashboard_test.js`(28)、`node platform_test.js`(43)、`node stream_test.js`(19)
   (项目已纳入版本管理,改坏了可以 `git diff` / 回滚);
6. **别只看注释和文档下结论**——本项目已多次出现"注释/CLAUDE.md 说的和代码实际行为不一致"
   (docstring 还写着"只读客户端"、BRAIN 实际值等)。以代码和实测为准,发现不一致顺手改掉。

## 三、架构(七层,全部打通)

```
① 交互层  static/favicon.svg     站点图标(气泡+柱状,四页共用;PNG 兜底老 Safari)
① 交互层  static/login.html      登录/注册(账号密码,记录跟人走)
          static/platforms.html  平台选择页(登录后先选平台:已绑定/未绑定/敬请期待)
          static/index.html      聊天页(气泡/表格渲染/快捷提问/输入法修复/中英切换/记录存盘)
          static/dashboard.html  数据大屏(KPI卡/趋势图/柱状图/AI诊断/明细表,同样双语)
② 大脑层  agent_server.py     三级火箭:Gemini flash → flash-lite → OpenAI通道
                             `.env` 里 BRAIN 可指定主力:auto(默认)/openai/claude
③ 工具层  agent_server.py     39个工具:16查询(含转化事件、素材推荐、图库查找、竞品广告、创意拆解、投放树、全市场扫描、平台类型) + 1报表 + 7写操作护栏 + 3定时 + ClickFlare/落地页那批
④ 客户端  newsbreak_client.py NewsBreak API 封装(读+写)
          creative_search.py   授权图库素材搜索(第六之十一节)
          openadlibrary_client.py 竞品广告查询(第六之十二节)
          creative_lab.py      创意拆解与方案(第六之十三节,多模态看图)
          creative_render.py   把方案做成广告图(第六之十五节,无字底图+代码叠字)
          cloudflare_pages.py  落地页 A/B 发布(第六之十七节)
          clickflare_scripts.py 追踪脚本库,每个追踪域名只贴一次(第六之十七节)
          clickflare_client.py ClickFlare 追踪器 API(第六之十八节)
⑤ 安全层  agent_server.py     写操作"保险箱+保险丝"(第七节)+ AuthMiddleware 登录门
                             + LoopGuard「绕住了就停下」闸门(第六之十九节,省钱)
                             + 工作室闸门被拦时给「移交单」而不是死话(第六之二十节)
          accounts.py         账号/加盐哈希密码/会话/每人的聊天记录(第六之三节)
⑥ 平台层  platforms.py        投放平台注册表(NewsBreak 已通;Nextdoor/Meta 标 coming)
⑦ 配置层  .env                APP_PASSWORD(注册邀请码)+ BRAIN;每人的平台 token 在 data/creds.json
                             + 四把钥匙:GEMINI / OPENAI / ANTHROPIC / NEWSBREAK
```

其他文件:`start.sh` 一键启动;`README.md` 面向使用者的指南(给 Cole 和团队看);
五套测试:`smoke_test.py` 后端冒烟(270)+ `frontend_test.js` 多会话(101)+ `dashboard_test.js` 大屏绘图(28)+ `platform_test.js` 多平台(43)+ `stream_test.js` 流式(19),改完都要跑;`requirements.txt` + `.gitignore` 让项目可独立搬家
(**`.gitignore` 已排除 `.env`、`data/`(每个人的聊天记录、平台凭据、追踪脚本库、发布产物)、`pending_actions.json`、`scheduled_tasks.json` —— 后两个是运行时状态,跟机器走,别进 git**);`chat.py`、`newsbreak_hello.py` 是学习期的小练习。

## 四、怎么运行

```bash
cd ~/workspace/my-agent && ./start.sh     # 端口 18100(可用 PORT= 改),uvicorn --reload 热重载
```
- 浏览器访问需在 VSCode「端口」面板转发 18100(服务器重启后桥若断:删掉重加);
- 排障口诀:拒绝连接=没桥;一直转圈=桥断了或服务死了(跑 start.sh);页面报错=找代码问题。

## 五、NewsBreak API 速查(源自 qx-ad-bot 生产代码,已实测)

- Base URL: `https://business.newsbreak.com/business-api/v1`
- 认证:请求头 `Access-Token: <token>`(不是 Authorization)
- 响应约定:`code == 0` 才算成功,错误在 `errMsg`;数据在 `data`(列表常在 `data.list` / `data.rows`)
- 列表接口:`GET /{campaign|ad-set|ad}/getList?adAccountId=&pageNo=&pageSize=[&search=]`
- 组织/账户:`GET /org/admin-orgs`;`GET /ad-account/getGroupsByOrgIds?orgIds=`
- 报表:`POST /reports/getIntegratedReport`,body 含 dateRange=FIXED/startDate/endDate/
  dimensions/metrics;**金额单位是"分",百分比是"万分点"(188=1.88%)**
  - `dimensions=[CAMPAIGN|AD_SET|AD]`:跨度上限 **180 天**
  - **`dimensions=["DATE"]`(按天出数,画趋势图用):跨度上限约 31 天** —— 32 天就报
    `Invalid parameters(HTTP 400)`,和 180 天那条是两个不同的限制,别混。
    行里 id 和 name 都叫 `date`;实测 30 天 ✅ / 32 天 ❌ / 45 天 ❌
  - 两个层级的取数封装在 `newsbreak_client.get_report_raw()` / `get_daily_raw()`
    (返回**纯数字**给图表用;给 AI 看的格式化字符串仍走 `get_report()`)
- 写操作:`PUT /{campaign|ad-set|ad}/updateStatus/{id}`,body `{"status":"ON"|"OFF"}`
- 建广告三层套路(**已实测跑通**,配方来自 qx-ad-bot 前端向导 NewsBreakCampaignWizard,
  比其后端 publish_campaign_to_newsbreak 老流程更准):
  ① campaign:name/objective=WEB_CONVERSION/status
  ② ad set:campaignId/name/budgetType/budget≥$10(分)/startTime/endTime/
     **bidType=MAX_CONVERSION(自动出价,勿用 CPC+deliveryRate 老配方)**/
     **trackingId=转化事件id(必填!缺了报 illegal event tracking;
     事件列表 GET /event/getList/{accountId},自动选优先 submit_form)**/
     platforms=["APP_AND_WEB_UNLIMITED"]/targeting=九维度全 {"positive":["all"]}
  ③ 传素材 `POST /ad/uploadAssets`(multipart)拿 assetUrl →
     ad:adSetId/name/status/creative{type=IMAGE|VIDEO|GIF/headline≤90/
     **description 3~90字符(超了报错,实测)**/callToAction/brandName≤40/assetUrl/clickThroughUrl}
  新建的 ad 会带 onlineStatus=PENDING(平台审核中),属正常

## 六、数据大屏(static/dashboard.html + /api/dashboard、/api/analyze)

- **时间范围两种给法**:`days=N`(近 N 天,顶栏四个快捷键)或 `start`/`end` 自定义起止日期(顶栏「自定义」)。
  统一走 `_resolve_range()`:**按北京时间算今天**(用 UTC 的话北京 00:00~08:00 会少一天);
  起止填反了替他调过来、结束日期在未来截到今天、格式错和跨度超 180 天**拦住并说人话**
  (直接甩给平台的话用户看到的是一句英文 `Invalid parameters`)。
  **`/api/analyze` 必须跟着同一个范围走**,否则 AI 诊断的是另一段时间,和用户看到的对不上。
  **前端也要把「反了」换过来,不能只让后端悄悄换**(2026-09-09 线上实测):
  后端 `_resolve_range()` 一直会替他调过来,可自定义弹层里那两个输入框**还留着反的那一对**——
  于是标题写着「02-02 ~ 03-17」、框里写着「开始 03-17 / 结束 02-02」,**两组数字自相矛盾**。
  现在 `fixDateOrder()` 在**点日历那一刻**就换,并说一句「已经替你换过来」(用中性色,不标红——
  事情已经办好了),顺带把焦点跟到日期挪过去的那个框;点「确定」时再兜一道,
  换过来之后**先不关弹层**,让他看见那句说明。
- `GET /api/dashboard?days=N`:一次返回 KPI 总计 + **上一等长周期(算环比)** + 按天趋势
  + campaign/ad_set/ad 三层明细。`days` 夹在 1~180;趋势自动缩到 ≤31 天并回 `trend_capped=true`
  让前端如实说明"趋势只显示最近 31 天"。
- `POST /api/analyze`:把上面的数据塞进提示词让 AI 做诊断,固定输出四段
  (一句话结论/看点/问题/建议怎么做),要求**点名到具体广告 + 附数据 + 可执行**,
  并明确"数据不足就直说,别硬编结论"。走和聊天同一套大脑接力,支持 lang。
- **比率必须由总量重算**(`_sum_kpi`):CTR/CVR/CPC 等不能把各行比率平均——
  小花费行和大花费行等权会算出错误的 CTR。
- 图表按 dataviz 规范做,几条硬规矩:**不用双轴**(花费和点击量级差太远,改成一次看一个指标)、
  单个 headline 数字用**数字卡**不用一根柱、量级比较用**单色渐深**、配色跑过
  `validate_palette.js`(浅/深两套都过 CVD 检查,浅色 aqua 对比度不足由表格视图补偿)。
- 双语:大屏自带 I18N 表,和聊天页**共用 localStorage 键 `adbot-lang`**;
  切换后已生成的 AI 诊断不改写(那是历史结果),只提示用户可重新分析。

## 六之二、多平台架构(platforms.py + /platforms 页)

登录后先落在 `/platforms` 挑平台,选中才进聊天页 `/?platform=<id>`。

- **直接敲根路径 `/` 会 302 到 `/platforms`**(判据:URL 里有没有 `platform` 参数)。
  不这么做的话,直接访问 `/` 拿到的是写死默认平台的聊天页,用户根本没机会选 ——
  以后接了 Nextdoor / Meta 更明显。冒烟测试守着这条。

- **注册表 `platforms.py`**:每个平台一条,`status` 只有两种值——
  `ready`(有对接代码,能用)/ `coming`(还没接,只显示「敬请期待」且按钮禁用)。
  **铁律:没有真正对接代码的平台绝不许标 ready** —— 用户满怀期待点进去发现什么都干不了,
  比不列出来更糟。冒烟测试里有一条专门查这个。
- `bound` 是个**函数**,签名是 `bound(user_id)` —— **每人绑自己的**,
  判断依据是 `data/creds.json` 里这个人有没有存过 token,**不看 `.env`**。
- `GET /api/platforms` 吐 `public_list()`(去掉函数、带上当前绑定状态)给两个前端页用。
- **聊天页不再写死 NewsBreak**:`PLATFORM` / `PLATFORM_NAME` / `PLATFORM_BOUND` 三个
  **var** 变量(必须 var,见第八节的提升坑)在 I18N 之前声明,副标题文案用 `{p}` 占位符
  由 `applyLang()` 替换;`loadPlatform()` 拉到真实名字后再 `applyLang()` 一次。
- **没绑账号**:聊天区贴一条 ⚠️ 提示(带平台名 + 绑定方法 + 「现在去绑定」按钮直通 🔗 弹窗),
  并**停用输入框和发送键**——别让用户白打一段字才发现发不出去。
  平台页点「先绑定账号」会带 `&bind=1` 进来。**注意它只是「意图」**:真没绑才弹引导+锁输入;
  已经绑好的只是想改,直接开账户弹窗、什么都不锁,并把 `bind=1` 从地址里去掉(见坑表)。
  **快捷提问按钮也要一起停**,而且闸门要设在 `send()` 里 —— 那几个按钮是直接
  调 `send(q)` 的,只 disable 输入框和发送键完全拦不住(线上实测:点一下照样发出去,
  烧掉一次额度再拿回一条报错,还在左栏留下一段以按钮文字命名的空对话)。
  **存完 token 会自动解锁**(`refreshPlatformBinding()` 回查 `/api/platforms`,
  以后端 `bound()` 为准,不是"存成功就直接放开"),不用用户手动刷新。
- 平台选择记在 localStorage `adbot-platform`;顶栏 🔄 按钮随时回平台页换一个。

**接新平台(比如 Nextdoor)要做三件事**:①写 `nextdoor_client.py`(照 `newsbreak_client.py`);
②在 `agent_server` 里给它加工具函数;③把注册表里的 `status` 改成 `ready` 并实现 `bound()`。

## 六之三、登录与账号(accounts.py + /login 页)

**为什么要有**:以前聊天记录存在浏览器 localStorage 里,换台电脑就什么都没了;
而且 localStorage 是按「协议+域名+**端口**」隔离的,换个端口记录就"凭空消失"。
现在记录跟着账号存在服务器上。

- **存哪儿**(全在 `data/`,已被 `.gitignore` 排除):
  `data/users.json` 账号(用户名 / 盐 / 哈希,**没有明文密码**)、
  `data/sessions.json` 登录会话(重启不掉线)、`data/chats/<uid>.json` 每人自己的聊天记录。
- **密码**:PBKDF2-HMAC-SHA256、20 万轮、每人一把随机盐。users.json 泄露也反推不出原密码。
  比对用 `secrets.compare_digest`(定时安全,防止用响应快慢猜密码)。
- **会话**:随机长令牌存服务器,浏览器只拿一个 **httponly** cookie(网页里的 JS 读不到,
  防被脚本偷走);`SESSION_DAYS = 30` 天后要重新登录。
- **`APP_PASSWORD` 现在的角色变成"邀请码"**:`.env` 里填了它,注册时就必须填对才能开号
  (防止端口暴露后被陌生人注册);留空 = 谁都能注册。
  **一个账号都没有时**,登录页会自动切到注册态并提示"先注册第一个"。
- **门禁 `AuthMiddleware`**:没登录访问页面 → **302 跳 `/login`**;访问 `/api/*` → **401**。
  放行白名单 `_PUBLIC_PATHS` + `/static/`。**注意这条改变了探活方式**,见第八节。
- 上限(`save_chats` 里裁剪,防文件无限长大):对话取**列表前** `MAX_CONVS = 50` 段
  —— **前端 `static/index.html` 里的同名常量必须一致**:前端是按它裁完之后把**整份**列表 PUT 上来的,
  前端小一截就等于每次打开页面都在悄悄删掉服务器上的旧对话(前端曾是 30,已对齐)。
  —— 注意是按前端给的顺序切,不是按时间排序后再切;前端把最近更新的排在前面,才等效于"丢最旧的",
  哪天改了前端排序,这里就会误删。每段只留**最后** `MAX_MSGS = 400` 条消息。
- **NewsBreak token 仍是全局的**(在 `.env` 里),所有登录用户操作同一个广告账户 ——
  登录解决的是"记录跟人走",不是"数据隔离"。要每人管自己的账户,见第六之二节 `bound()` 那段。

## 六之四、每人绑自己的平台账号(按人隔离)

**为什么**:token 原来是全局一份存在 `.env` 里,A 绑好之后 B 登录进来直接就能操作 A 的广告账户。
现在每人一份。

- **存哪儿**:`data/creds.json`,结构 `{user_id: {platform: {token, account_id}}}`,
  写入后 `chmod 600`。`data/` 已被 `.gitignore` 排除。
- **怎么一路带下去**:工具函数散落在很深的调用链里,逐层传参不现实,所以用
  **`contextvars`** —— `newsbreak_client.CURRENT_CREDS` 在 `AuthMiddleware` 里设一次
  (必须设在 `call_next` **之前**,下游才读得到;FastAPI 把同步接口丢进线程池时会复制上下文)。
  同理还有 `agent_server.CURRENT_USER_ID`,给 AI 的工具函数用(比如登记定时任务要记下是谁定的)。
- **取值约定(关键)**:`CURRENT_CREDS` 为 `None` = 不在用户上下文(定时任务线程 / 命令行 / 测试)
  → 回落 `.env`;为 `dict` = 在用户上下文,**只认这个人的**,他没绑就报错,
  **绝不偷偷回落 `.env`** —— 一旦回落,B 就会用上公用 token,等于没隔离。
- **定时任务**:登记时把 `user_id` 存进任务里,看表线程执行时用**当初那个人**的凭据
  (`execute_fn(level, object_id, status, user_id)`)。
- **`_account_state()` 也要按人读**:以前读 `.env`,导致没绑账号的人也能看到公用 token 的
  掩码 —— 那是别人的东西,不该露给他。
- `.env` 里的 `NEWSBREAK_ACCESS_TOKEN` 现在只给「不在用户上下文」的场景兜底
  (冒烟测试、命令行脚本)。**网页上的用户一律各绑各的。**

## 六之五、流式回复(SSE)

`POST /api/chat/stream`,事件类型:`status`(正在查什么)/ `delta`(新的一小段字)/
`reset`(前面吐的字作废)/ `done`(带**盖过钢印**的完整回复)/ `error`。

- **两个接口共用同一套逻辑**:`_route_brain()` 选大脑、`_brain_error()` 翻译报错。
  非流式的 `/api/chat` 保留着当兜底 —— 流式一旦被中间的代理缓冲住,前端会自动退回它。
- **`X-Accel-Buffering: no` 这个响应头不能少**:nginx 默认会把流攒成一坨再发,
  用户看到的还是"转半天圈然后一次蹦出来",流式等于白做。加了这个头就不用改 nginx 配置。
- **大脑函数是同步阻塞的**,所以丢进线程跑、用 `queue` 把事件递出来;
  线程要 `contextvars.copy_context()`,否则当前用户的凭据带不进去。
- **`reset` 是干嘛的**:模型可能先说句开场白再去调工具,那段不是答案,要让前端作废。
- **前端过程中只显示纯文本**,`done` 到了才整体渲染 Markdown ——
  表格写到一半是断的,边收边渲染会显示成一片乱码,看着像出错了。
- **两条大脑路径现在都是手动挡**:Gemini 也关掉了 SDK 的自动工具调用
  (`AutomaticFunctionCallingConfig(disable=True)`),自己跑工具循环。
  原因见第八节坑表那两条 —— 自动挡配流式是坏的,而且自动挡也没法播报进度。

## 六之六、线上部署(宝塔面板,2026-08-14 首次上线)

跑在一台 **Debian 13 / Python 3.13** 的服务器上(**在欧洲,不是国内** —— 所以 Gemini 通、
pip 用官方源比国内镜像快)。代码在 `/www/wwwroot/<域名>/`,域名见宝塔站点列表。

**四条硬约束**(踩了都是隐蔽故障):

1. **进程数必须是 1**。多进程会让定时任务**执行多次**,而且写操作的保险丝
   (`_REQUEST_SEQ` 在进程内存里)会失灵 —— 在 A 进程登记的待办,请求转到 B 进程
   确认时会说"找不到"。要扛并发只能加机器,不能加 worker。
2. **绑 `127.0.0.1` 不是 `0.0.0.0`**,让 nginx 反代进来,端口不暴露公网。
3. **不加 `--reload`**,那是开发用的。所以**改完代码必须手动重启 Supervisor**。
4. **nginx 必须加这几行**,少一行就是一类故障:
   ```nginx
   client_max_body_size 100m;   # 默认只让传 1MB → 上传素材报 413
   proxy_read_timeout 300s;     # 默认 60s → AI 回复到一半被掐断
   proxy_send_timeout 300s;
   proxy_set_header X-Forwarded-Proto $scheme;   # 少了 cookie 的 secure 标志不会生效
   ```
   (SSE 不用改 nginx —— 响应头里已经带了 `X-Accel-Buffering: no`。)

**部署时踩过的**:改目录名会**废掉 venv**(脚本首行写死绝对路径,报 `bad interpreter`),
要 `rm -rf venv` 重建;Debian 默认不带 `venv` 模块,先 `apt install python3-venv`;
宝塔建的目录属主是 `www`,而 git 拉的文件是 root 的,所以 Supervisor 的启动用户填 `root`
(测试期够用,正式投产再统一 chown 给 `www`)。

**部署前先跑 `check_brain.py`**:它拿真钥匙各发一次最小请求。
"网络能连上"和"API 真能用"是两回事 —— Gemini 会按请求来源地区拒绝服务,
curl 测出来是通的,实际一句话都回不了。

## 六之七、默认值的规矩(建广告向导)

**原则:能替用户定的就别问,但替他定了必须告诉他,而且他要能改。**
问得太多,小白会在第 3 步就放弃;默默替他定又等于偷偷替他花钱。两头都不行。

- **默认值写在代码里**(`DEFAULT_BUDGET_DOLLARS` 等常量,`agent_server.py`),
  **别把数字散写进提示词** —— 散了就会改一处忘一处。
- `propose_create_campaign` 的 `budget_dollars` **可以不传**,不传就用默认;
  但返回里会:①在金额后面标「(系统默认值)」;②把这些项列进 **`defaults_used`**;
  ③在 `note` 里要求 AI 向用户说明"这是默认值、为什么、可以改"。
- **用户一旦自己给了值,就不再算默认** —— `defaults_used` 里不会出现它。
- **平台硬下限照拦**:默认值是"替他选一个合理的",不是"绕过校验"。低于 $10 仍然报错。
- 提示词里每个默认值都配了**说得出口的理由**(比如日预算 $20:平台最低 $10 但太低跑不出量;
  日预算比总预算好控;而且建好是暂停的,确认前一分钱不花)。改默认值时理由要一起改。

## 六之八、素材推荐(recommend_creatives)

用户没现成图片时,从**这个账户自己投过的广告**里挑效果好的素材给他复用。

- **为什么只推荐账户自己的**:①有真实投放数据背书,不是凭空说"这个好";
  ②**版权干净** —— 从网上找图投广告有法律风险,平台也可能拒审。
  提示词里明确写死了:**绝不许从网上找图、编造素材链接、或声称能生成图片**。
- 数据来源:`list_ads` 的 `creative.content.assetUrl`(平台确实返回了图片地址、
  尺寸、当时的标题/描述),再和 ad 层报表按 id 对上,拿到 CTR/转化/花费。
- 排序:**有转化的优先 → CTR 高的 → 花费多的**。同一张图被多条广告用过只推荐一次。
- **必须把类型登记进 `_ASSET_TYPES`**,否则复用时 `creative_type_of` 会按文件名瞎猜,
  把图片判成视频之类,平台直接拒收。
- 前端:回复里用 `![](url)` 插图,`.bubble img` 限了宽(原图有 4096px,不限宽会把布局撑爆)。
- 账户没历史素材时**如实说没有**,并给上传建议(1200×628 以上、清晰、别放大段文字)。

## 六之九、开启广告必须三层一起开(领域知识,最容易错)

**广告要真的跑起来,campaign / ad set / ad 三层必须都是 ON**,是"与"的关系。
只把 campaign 打开、底下还关着 = **一条广告都出不去**,而用户以为在投、白等几天。
(反过来暂停很简单:关掉 campaign,底下自然都不投,不用连坐。)

- `get_delivery_tree(campaign_id)`:看一条计划底下有哪些组和广告、各自开没开,
  并给出 `need_turn_on`(还差哪些没开,可直接塞进 extra_targets)。
  **接口不支持按父级过滤**,是把账户下的全拉回来按 `campaignId` / `adSetId` 自己筛的。
- `propose_status_change` 多了 `extra_targets`,一个待办可以带多个对象,
  执行时逐个改、逐个记账,**有失败的会点名说是哪一个**(别让用户以为全成了)。
- **只开 campaign 一层时,返回里会带 `warning`** —— 这条守在代码层,不能只靠提示词。
- 提示词规定:**广告组或广告多于一个时,必须把清单列给用户问"全开还是只开某几个"**,
  不许替他决定 —— 多开一条就是多花一份钱。只有一组一条时才自动带上。
- **定时开启(`propose_schedule`)完全同理,而且更要紧**:到点时没人盯着,
  只定了一层的话第二天才发现一条都没跑。任务存档里带 `targets`,
  看表线程执行时逐个改。**没有 `targets` 的老任务按单对象处理**(兼容),冒烟测试守着这条。

## 六之十、命名规范(按落地页类型)

落地页是做什么的,决定三层怎么命名。

**类型词从哪来**:代码里有一份**已知类型清单** `KNOWN_AD_TYPES`(现为 roof / gutter / window / bathroom)。
- 落地页能对上其中一个 → 直接用,告诉用户一声即可,不用反复确认;
- **一个都对不上 → 必须问用户「这次用什么关键词命名?」**,
  **绝不许自己编一个** —— 命名是团队约定,编出来的词会让以后按名字筛数据时对不上号。
- 清单只在代码里存一份,`_system_prompt_now()` 每轮**注入进提示词**。
  加新类型只改 `KNOWN_AD_TYPES`,别去改提示词文本(冒烟测试守着"两边同步")。

| 层级 | 格式 | 例 | 序号含义 |
|---|---|---|---|
| campaign | `NB-类型-年月日-NN` | `NB-Roof-260817-01` | 今天这个类型的第几支(两位) |
| ad set | `年月日-类型-NNN` | `260817-Roof-001` | 这支计划里的第几个组(三位) |
| ad | `AD-年月日-类型-NNN` | `AD-260817-Roof-001` | 这个组里的第几条广告(三位)—— **一个组下面放几条就排到几**,见第六之二十一节 |

- **日期按北京时间取**(`sched.now_beijing()`),用户说几号就是他那边的几号。
- **日期是 6 位年月日(YYMMDD),不是 4 位月日**。少了年份不只是"看着含糊":
  序号是**扫已有名字取最大值 +1** 算的,`0817` 明年还会再来一次 → 明年 8/17 的第一支
  会变成 `-06` 而不是 `-01`。冒烟测试里有一条专门喂去年的名字守这个。
- **广告比广告组多一个 `AD-` 前缀**。两层要是同名,`list_ad_sets` / `list_ads` 都是
  按名字搜的,用户说"开启 260817-Roof-001"就分不出指的是哪一层;大屏三层明细表和
  报表导出里也只有一列 name,同名两行没法区分。
- 序号**不是凭空数的**:`_campaign_name()` 会先拉平台上已有的计划名,
  用正则匹配同类型同日期的,取**最大序号 +1**。这样中途删过、或者别人也在建,都不会撞名;
  大小写不敏感(`nb-roof-260817-07` 也算数)。查不到平台数据时从 `01` 起,不让它建不出来。
- `_type_word()` 把类型规范化:`roof` / `ROOF` / `gutter修缮` → `Roof` / `Gutter`;
  非英文字符剔掉,空的兜底成 `Ad`。
- **广告组序号固定 `001`,广告序号从 `001` 顺排到 `00N`** —— 一个广告组里放几条广告
  由用户定(第六之二十一节)。**别再写「反正只有一条」**:那个前提在「复用已有计划」时
  就不成立,正是它让同一条计划底下建出了两个同名广告组、各带一份日预算。
- 用户想完全自己指定计划名,传 `campaign_name` 即可覆盖。

## 六之十一、素材查找(creative_search.py + 两个工具)

**规矩变了,先说清楚**:以前提示词里写死「**绝不许从网上找图**」。
现在改成「**只许从正规授权图库找,且每张都要标明许可证**」。
变的是"允许有授权的外部来源",没变的是"素材来源必须可查证" ——
仍然**绝不许**自己编素材链接、声称能生成图片、或从这两个工具之外拿图。

两个工具分工别搞混:
- `recommend_creatives` —— 查**自己账户投过的**素材,有真实投放数据背书;
- `search_stock_creatives` —— 查**外部授权图库的新素材**,账户里没合适的时候用。
  用户选定后必须调 `use_found_creative` 转存进 NewsBreak 换回 `assetUrl`,
  **图库的 image_url 不能直接当 asset_url 用**(平台只认自己域名下的素材)。

- **只返回可商用的,这条守在代码里不是提示词里**。投广告是商业用途,
  拿 NC(NonCommercial)的图去投就是侵权。三家图库的处理:
  - Openverse:请求里写死 `license=cc0,pdm`。**这个参数绝对不能省** ——
    实测不加时搜 roof 的第一条就是 `by-nc-sa`。只取 CC0/公有领域是因为
    这两种既可商用又不要求署名(CC-BY 也能商用但要署名,广告图上没地方放);
  - Pexels / Pixabay:许可证本身就允许商用且无需署名,整家都安全。
- **三家的定位**(`available_sources()` 会如实告诉用户):
  - **Pexels / Pixabay** 要一把**免费**钥匙(`.env` 里 `PEXELS_API_KEY` / `PIXABAY_API_KEY`),
    图是广告级商业摄影,**有钥匙就优先用**;
  - **Openverse** 不用钥匙、开箱即用,但聚合的是维基百科/Flickr 那类纪实照片,
    **家装维修这类品类的 CC0 存量很薄**(实测 "gutter cleaning" 只有 1 张)。当兜底可以,当主力不够。
- **质量评分 `_quality(w, h)`**:用户要的是"没有好素材时也能看出质量如何",
  所以差图也要列出来并说清差在哪,不许只挑好的报(提示词里明写了)。
  **分辨率不够和竖图是一票否决**,不能被另一项的高分抵消 ——
  原来用纯加分制,400×210 因为"宽高比正好"被判成"可用",是错的(冒烟测试抓出来的)。
- **防幻觉:`_SEARCHED_ASSETS` 登记表**。`use_found_creative` 只接受
  **搜索结果里真出现过的**地址,AI 自己拼一个地址会被代码层直接拒绝。
  这和第七节写操作护栏是同一个思路 —— 别指望提示词能管住幻觉。
- 转存走的还是 `nb.upload_asset()`,所以 409 查重降级、`_ASSET_TYPES` 类型登记
  这两条老规矩都照旧(见第六之八节)。

## 六之十二、竞品广告查询(openadlibrary_client.py)

**2026-08-19 从 Insightrackr 换成了 OpenAdLibrary**,换的理由都是硬的:

| | Insightrackr(已弃用) | **OpenAdLibrary(现用)** |
|---|---|---|
| 认证 | 浏览器登录态,**几小时过期**,要人工重贴 | **API key(`oal_` 开头),不过期** |
| 美国数据量 | 搜 roof repair 筛 US 只有 **4 条** | **12890 条**(只看在投的还有 1920 条) |
| 素材分辨率 | 200×200 到 1152×602,**全是缩略图** | **1200×800 / 1200×900 投放级原图** |
| 参数 | 靠实测猜魔法数字(`keyWordType=0,2`) | 具名参数,但**仍有陷阱**,见下 |
| 稳定性 | 遇到 504、限流 | 明确状态码(402 额度 / 429 频率) |

- **接口**:`GET https://openadlibrary.com/api/v1/ads`,认证 `Authorization: Bearer <key>`。
  覆盖的是**原生广告网络**(Yahoo / Outbrain / Taboola),正是家装线索的主战场。
- **配额**:Pro **5000 次/天** + **120 次/分钟**瞬时上限;免费号只有 2 次/天。
  超额 429、免费额度用尽 402,**两个码要分开报**(用户才知道是该等还是该升级)。

**参数真相(文档只写了参数名,没写哪些真的生效 —— 全是实测出来的)**:

| 参数 | 实测结论 |
|---|---|
| `sort=placements` | ✅ 按**版位数**降序(41/24/17/17)。平台不给曝光量,版位数是最接近"跑得动"的信号 |
| `sort=oldest` | ✅ 有效 |
| `sortBy` 整个参数 / `sort` 的其它值 | ❌ **静默忽略** —— 不报错、也不排序,比直接报错更难发现。所以 `SORTS` 里只暴露验证过的两个 |
| `sortDir` | ❌ 忽略,`placements` 恒为降序 |
| `status=active` | ⚠️ **单独用**有效(12892 → 1921);**和 `search=` 一起发必定 503**,平台原话 `That filter combination is too broad to count right now.` —— 是算总数那一步撑不住,不是服务挂了。所以查关键词时**不发它,在本地按 `isActive` 筛** |
| **`minDaysRunning`** | ❌ **稳定 503**,隔几秒重试三次都一样,是平台服务端的问题。**投放天数只能拿回来自己算** |
| `mediaType=image` | ❌ 无效 |
| **`geoCountry`** | ❌ **两种坏法交替出现**:多数时候 **503**,偶尔 **200 但返回 0 条**。只针对 503 做降级会被第二种骗过去(实测骗到了)。**国家一律拿回来自己筛**(每条都带 `geos`) |
| `dateTo` | ✅ 有效,筛的是**首投时间**(30天→2671,60天→530,**90天→0**) |
| `lastSeenFrom`/`lastSeenTo` | ❌ 和 minDaysRunning 一样稳定 503 |

**这些结论会过期,所以别只信这张表 —— 跑 `./venv/bin/python check_competitor_params.py` 现查。**
那个脚本逐个参数和基准结果比对,一眼看出哪些真生效、哪些静默忽略、哪些报错。
接新平台或怀疑某个参数不对劲时,先跑它。

**关键词绝不许 AI 自己编(`my_ad_categories` + `⚠️关键词提醒`)**:
用户问「同行都在跑什么广告」时通常**不会说品类**,而模型会顺手编一个 ——
实测编出了 `roof`,但该账户实际投的是 **gutter 和 window**,查回来全是别的行业的广告。
(原因八成是命名规范那份 `KNOWN_AD_TYPES` 每轮注入提示词,它抓了排第一个的 `roof`。)
解法两层:①新工具 `my_ad_categories` 从**计划名和广告文案**里认出账户在投什么
(实测 gutter 32 次 / window 15 次);②`search_competitor_ads` 里**代码层比对**,
对不上就在返回里挂 `⚠️关键词提醒`,提示词要求必须先跟用户确认再往下讲。
和命名那条「类型词对不上必须问用户、不许自己编」是同一条规矩。

**平台排不了的,我们自己排(`LOCAL_SORTS`)**:翻 2 页凑候选池,在本地按
版位数 / 投放天数 / 最近新上排序,并按 `min_days` 筛。效果比平台自己排的好得多 ——
实测同一个词,平台单页 `sort=placements` 最高版位 17,本地 50 条候选池能排到 **869**。

⚠️ **但翻页是拿相关性换排序,不是白拿的**:平台默认顺序**就是按相关性排的**,
实测搜 roof repair 时 page=1 有 97% 的标题真含 roof,**page=2 只剩 17%**。
所以候选池必须先过 `_relevant()` 筛一道再排序 —— 顺序反了的话,浮上来的会是
"跑得久但完全无关"的广告(实测撞上过养老金、社保那类)。冒烟测试守着这个顺序。

**数据深度要有心理预期**:实测扫 200 条,roof repair 最长投放 **58 天**,
insurance 71 天 —— 这个库的历史很浅(Insightrackr 上能看到 652 天的)。
所以**「投放天数」在这个平台上是个弱信号,「版位数」才是主信号**;
`minDaysRunning` 就算修好了也没多大用武之地。
另外首投超过 30 天的广告 `isActive` 基本都是 False,所以
`dateTo` + `status=active` 组合起来会返回 0 —— **那是对的,不是 bug**。

**响应字段(文档只标了 `200 Default Response`,全靠实测)**:
`{data: [...], total, page, pageSize, totalPages}`,每条广告:
`headline` / `body` / `imageUrl` / `adNetwork` / `trafficSource` / `placements` /
`isActive` / `firstSeenAt` / `lastSeenAt` / `geos` / `devices` /
`advertiserName`+`advertiserDomain`+`landingDomain`(**只有约四成有值**,平台是
leak-safe 设计不给落地页真实地址,没有就如实留空、不许编)。

- **`imageUrl` 是相对路径**(`/api/public/assets/xxx.webp`),必须拼上域名;
  实测**下载不需要带 key**,公开可取。
- **平台不返回宽高**,尺寸要下载后才知道。实测这批是 **1200×800**,是投放级原图 ——
  这条改变了创意自动化的可行性判断,见第六之十三节开头。
- **风险标记仍然守在代码里**:`_normalize()` 给每条的 `license` 字段写死
  「⚠️ 这是其它广告主正在投的广告素材」。不能只靠提示词让 AI 记得说。
- **key 全公司一份**,配在 `.env` 的 `OPENADLIBRARY_API_KEY`,不做按人存(见第六之十四节)。

## 六之十三、创意拆解与方案(creative_lab.py,P0)

**做什么**:把竞品的高效广告拆开看懂,再照着套路写出**我们自己的**文案和画面方案。

**这一期只出文字,不出图**,这是刻意的。完整评估见 CLAUDE.md 同级的可行性分析,
结论是"自动出图直接投"有三个过不去的坎:
①竞品平台存的是**缩略图**(实测尺寸 200×200 到 1152×602,**没有一个**达到
NewsBreak 建议的 1200×628),原图拿不到;②这类广告画面上全是文字
(`Free Estimate` / `$0 Down`),AI 生图渲染精确文字至今不可靠;
③全自动等于把版权风险批量化。所以后面几期(图库底图 + 程序化叠字 → AI 生底图)
分开做,且必须保留人工确认。

两个工具:
- `decompose_creative(image_url)` —— 看一张图 → 结构化「素材模型」
  (版式/画面主体/有无真人/文字层/主色/信任背书/文案角度/**竞品标识**)
- `summarize_creative_patterns(brand, landing_url, n_variants)` —— 多条汇总 →
  共同点(带出现次数)+ N 版我们自己的方案 + 「还缺什么」

- **多模态是新引入的能力**,项目此前从没给模型喂过图。要点:
  - **喂之前必须缩图**(`shrink()`,长边 1024)。实测原图能到 6000×4000 / 10MB,
    直接发又慢又贵,而拆广告结构根本不需要那么大(缩完 139KB)。
  - **视觉模型也要接力**(`VISION_MODELS`)。实测第一次调用就撞上 `gemini-flash-latest`
    返回 503 高负载,不接力整个功能就断了。
  - 模型爱把 JSON 包在 ```json 围栏里、前后加客套话,`_json_from()` 负责剥;
    真解析不了要**如实报错**,不能悄悄返回空 dict 让上游以为成功了。
- **`_plain_completion()`(agent_server)是新加的**:走同一套大脑接力但**不带工具**。
  为什么不复用 `ask_gemini`/`ask_openai` —— 那两个会把 20+ 个工具的 schema 一起发过去,
  模型可能中途跑去查广告数据。归纳竞品创意是纯推理,不该碰接口,带着工具既慢又贵还容易跑偏。
- **合规三条写在归纳提示词的最前面**(不是末尾 —— 放末尾会被前面一大段素材数据带跑,
  和第八节 `SYSTEM_PROMPT_EN` 那条教训一样):不许出现竞品品牌名、
  不许照抄具体承诺(「$99 起」这类,我们做不到就是虚假宣传)、
  **主标题和描述必须自己重写**。
- **文案查重守在代码层(`_flag_copied`),不能只靠提示词**。
  血泪:提示词里已经写了"不许照抄",实测第一版归纳出来的三版方案里
  **两版的主标题是竞品原句照搬**(`Call an Expert Contractor Now` 等)。
  品牌名照抄一眼能看出来,文案照抄看不出来,用户很可能直接拿去投。
  判据是和竞品某句的实词重合度 ≥70%(学句式必然共用 roof/free/now 这类词,
  整句重合到这个程度就不是"学"了),命中就在那一版上挂 `⚠️查重` 并置 `有照抄嫌疑`。
- **两道门槛**:`decompose_creative` 只接受**搜索结果里出现过的**地址
  (和 `use_found_creative` 一个道理,防 AI 编链接);
  `summarize_creative_patterns` **少于 2 条直接拒绝** —— 1 条归纳不出"共同点"。
- 视频只能看不能拆:`decompose_creative` 遇到 `media_type == "VIDEO"` 会明确说明并建议
  改看标题/投放天数/曝光量。视频的拆解重组是另一个量级,不在这一期。
- 拆解结果存在 `_CREATIVE_MODELS`(上限 60 条),归纳时一次读全部 ——
  几百行 JSON 靠工具参数传来传去不现实,也容易被截断。

## 六之十四、竞品 key 就一份(公用)

**OpenAdLibrary 的 key 全公司一份,配在服务器 `.env` 的 `OPENADLIBRARY_API_KEY`。**
不做「每人贴一份」——key **不会过期**、额度 **5000 次/天**够用,查的又是公开的竞品广告库,
没有"谁的数据"之分。给每人配一份只会多一套要维护的界面和状态,换不来任何隔离价值。
(NewsBreak 那边正相反:各人各绑、没绑就报错、**绝不回落 `.env`** —— 那关系到各自的
广告账户和钱。两条规矩故意不同,别当成不一致改掉。)

- 取值走 `_read_env_value()` **现读文件**,不是只信进程启动时的环境变量 ——
  `.env` 里后来才填上的键,运行中的进程读不到(第八节「空值配置读不到」)。
- 换 key:改 `.env` 里那一行 → 重启服务。key 不过期,所以这事很少发生。
- 工具报错时的 `note` 会告诉 AI:key 出问题**不是用户能自己解决的**,请他找管理员,
  别让他去翻设置。

> 历史留档:上一个平台(Insightrackr)是浏览器登录态、几小时就失效,当时为此做过
> 「网页上贴一下就生效」的按人存凭据界面。换成 OpenAdLibrary 后 key 不过期,
> 那套界面就成了纯粹的负担,已整体拆除(2026-08-20)。
> **会过期的凭据,换起来的成本决定功能死活;不会过期的,就别为它建界面。**

## 六之十三之一、拆解要回答「为什么跑得动」(不是「长什么样」)

**这条最容易做偏,做偏过一次。** 第一版拆解问的是"它是怎么拍的"——出来一份摄影笔记,
好看但没用。老板要的是:**它凭什么拿到展示、凭什么被点、凭什么有转化,
亮点在哪,怎么把这个亮点用到我们自己的广告上。**

拆解产出(`creative_lab._DECOMPOSE_PROMPT`):

| 字段 | 问的是 |
|---|---|
| `为什么有展示` | 铺得广、投得久靠什么(题材普适?人群宽?) |
| `为什么被点` | 钩子类型 + **钩子原话** + 为什么有效(戳中什么心理) |
| `为什么有转化` | 承诺什么、门槛多低、信任从哪来;**看不出就写"从素材看不出"** |
| **`亮点`** | 最值得偷师的**那一个**点 —— 只写一个 |
| `亮点为什么成立` | 讲机制,不许只说"吸引人" |
| **`如何发挥这个亮点`** | 换成我们的广告具体怎么做 |
| `可迁移的公式` | 抽成一句能套用的模板 |
| `生图关键词` | 见下一节 |

- **必须把投放实绩喂进去**(`decompose(perf=...)`):只看一张图答不了"为什么跑得动"。
  传的是平台真给的字段:版位数、投放天数、还在不在投、广告网络、投放媒体。
- **绝不许编数据,这条守在提示词最前面**:平台**不给**展示量、点击率、转化率,
  只给版位数和投放天数。所有结论都是从「广告主愿意持续为它花钱」倒推的,
  写出来也要保留这个口径。**出现「点击率 3.2%」这类数字就是编的。**
  提示词、工具说明、`SYSTEM_PROMPT` 三处都写了这条。
- 归纳(`_summary_prompt`)同样围绕亮点:`为什么这批能跑起来`(讲机制+证据)、
  `最该学的亮点`(只挑一个)、`可迁移的公式`,每版方案还要说清`亮点用在哪`。

## 六之十三之三、素材池要跨平台(ad_platform_kinds.py)

**在 NewsBreak 上投,该学的不是"NewsBreak 上的广告",而是所有同类型平台上的同品类广告。**
原生广告的玩法(标题当钩子、图是干净实拍、文字由平台渲染)在 Taboola / Outbrain /
Yahoo 上是通用的,跨平台的素材池大得多,规律也更可靠。

- `platform_kind(platform)` 判断三类:**原生广告平台 / 大媒体(封闭生态)/ DSP**,
  并说清各自的素材长什么样、能不能互相学。
  - 原生:NewsBreak、Taboola、Outbrain、Yahoo、MGID、Revcontent、Microsoft Audience…
  - 大媒体:Meta、Google、TikTok…(**别拿原生的套路套过去**,受众心态和版位形态都不同)
  - DSP:The Trade Desk、DV360…(图要自己承载文字,和原生正相反)
- **认不出的平台明说认不出,不许猜** —— 和「类型词对不上必须问用户」是同一条规矩。
- OpenAdLibrary 实测覆盖 5 家原生网络(Yahoo/Verizon 90、Taboola 62、Outbrain 26、
  Microsoft Audience 15、Revcontent 6,扫 200 条的分布),**本来就是跨平台的**;
  `search_competitor_ads` 会把 `素材来自这些原生平台` 报出来让用户看到覆盖面。

## 六之十三之四、开放问题要扫全市场(native_market_scan)

**两种问法要分开,走的路完全不同:**

| 用户说的 | 走哪条 |
|---|---|
| 「我要投 roof,同行怎么打」(**给了品类**) | `search_competitor_ads` 按那个词查 —— 他说什么查什么 |
| 「**现在什么广告跑得好**」「这平台适合跑什么单子」(**没给品类**) | `native_market_scan` 扫全市场 |

**踩过的坑**:为了防 AI 自己编关键词,曾改成一律按账户已有品类(gutter/window)去查。
结果开放问题也被框回去,用户问"什么在跑得好"永远只得到自己那三个品类的答案 ——
**防幻觉不能变成画地为牢**。现在:品类对不上只给 `ℹ️品类提示`(**提示,不拦截**),
并明说"用户自己说的就照查,只有你自己填的时候才确认"。

- `oal.market_scan()`:`sort=placements` **不给关键词**就是全库最靠前的一批
  (实测头部版位数 36350 / 33065 / 30415,比家装赛道高一个数量级),
  再按 `VERTICALS`(品类)和 `HOOKS`(钩子套路)两个维度汇总。
- **词典是粗分桶,不是精确分类**。命中不了的一律进「其它」并**如实报占比** ——
  假装分类完整比分错更糟。词典按真实数据校过一轮:命中率 43% → **79%**。
- **实测结论(2026-08-20,看头部 150 条)**:原生平台上**健康养生是绝对主力**
  (67 条 / 71 万版位),之后是宠物、金融投资、猎奇内容;**家装只排第 9**。
  钩子排行里「否定既有认知」「价格悬念」「身份对号入座」最吃香 ——
  **钩子比品类更有用,它能跨品类照搬**。
- 口径照旧:版位数=铺了多少个位置,**平台不给展示量和点击率**,
  工具的 `note` 里明写了"绝不许说成点击率或转化率"。

## 六之十三之二、生图关键词(拆解 → 关键词 → 出图 的中间一环)

**老板要的那条链的关键**:拆解不是为了出份报告,是为了拿到**能直接驱动生图的专业关键词**。
所以拆解的角度必须和「一条合格的生图提示词由哪几部分组成」对齐。

九个角度(`creative_lab.KEYWORD_DIMENSIONS`,顺序即提示词里的拼接顺序):

| 键 | 中文标签 | 例 |
|---|---|---|
| `subject` | 主体 | `middle-aged roofer in worn hi-vis vest` |
| `action` | 动作 | `kneeling on a driveway assembling a gutter section` |
| `environment` | 环境 | `ordinary suburban house, green lawn, parked work van` |
| `shot` | 镜头 | `over-the-shoulder medium shot` / `low-angle` |
| `lens` | 镜头参数 | `35mm f/2.8, shallow depth of field` |
| `light` | 光线 | `natural overcast daylight, soft shadows` |
| `color` | 色调 | `muted earth tones, desaturated sky` |
| `treatment` | 质感 | `candid documentary photography, unposed` |
| `technical` | 技术 | `sharp focus on subject, natural skin texture, no HDR` |

- **顺序有讲究**:模型对提示词开头的词更敏感,所以主体/动作在前、成像技术在最后。
- **为什么用英文**:生图模型是按英文摄影术语训练的。中文散文(「光线柔和一点」)它只能猜,
  `overcast diffused daylight` 是它认得的词。中文的「画面怎么拍」保留着 —— 那是给人看的。
- **键名必须是英文**。血泪:一开始键用中文、值的开头写英文名
  (`"主体": "subject —— 画面里最主要的…"`),模型直接把值里的英文词当成了键,
  返回来是 `主体 / action / environment` 混着的一串,按中文名取全是空。
  **键名和值里的内容长得像,模型就会分不清哪个是键。**
- **九项一个都不许留空**。拆解提示词里原有一条"图上没有的就留空、绝不要编",
  模型会把它套到关键词上。但镜头、光线这些是**任何照片都必然有的属性** ——
  留空的那几项,生成时就只能靠模板去猜,拆解等于白做。已在提示词里单列一条说明。
- **归纳时要参考版位数高的那几条**:那是被市场验证过跑得动的画面语言。
  同一批 N 版方案的 `shot` / `lens` **必须彼此不同**,否则 A/B 里画面这个变量等于没变。
- 生图侧 `cr.build_prompt(scene, variant, keywords)`:**有关键词优先用关键词**,
  中文 `scene` 当兜底;关键词里已经给了镜头就不再塞模板镜头(两句话会打架)。
  冒烟测试守着「键名一致」「关键词真的进了提示词」「不冲突」。

## 六之十五、把方案做成广告图(creative_render.py)

**默认出的是一张干净的实拍图,图上没有任何文字和按钮。**

**为什么不叠字**(一开始做错了,照搬了 Push 广告的做法):
① **NewsBreak 自己会渲染文字** —— creative 里 `headline` / `description` /
   `callToAction` 是和 `assetUrl` **并列的独立字段**,平台负责画在图旁边。
   把标题烧进图里,同一句话会出现两遍;
② **画假按钮最糟** —— 平台渲染真的行动按钮(实测某账户是 `Get Quote`),
   图上再画个 `Learn More`,就是一真一假两个按钮并排;
③ **真正跑得动的竞品广告都是干净实拍** —— 实测版位数最高的几条
   (3245 / 2108 / 1373)图上一个字都没有。

`compose()` 叠字的能力保留着,但要**显式 `overlay=True`**,给"平台不渲染文字"的
位置用(比如 Push 通知缩略图);那时也不画按钮。

**三版靠镜头语言拉开差距(`VARIETY`),不是靠换文案。** 第一版三张用同一套模板 +
相近提示词,出来几乎一模一样 —— 做 A/B 时变量只有文案,画面等于没变。
现在远景 / 特写 / 仰拍 / 过肩 / 黄金时刻五种轮着来,`variant=idx` 传下去。

**质量方向照着真跑得动的竞品定(`_QUALITY`),不是凭感觉**:纪实实拍、真人在真干活、
自然光、允许粗糙(旧工具、普通房子)。明确排除棚拍摆拍和图库式的完美 ——
家装卖的是"可信",太精致反而像广告。

**为什么文字不能交给 AI 画**(两条都是硬理由):
① **AI 画英文经常拼错**(`Free Estimate` 画成 `Free Estimte`),广告图上一个错字就废了整张,还可能被拒审;
② **AI 每次字号位置都不一样**,做 A/B 时分不清"效果差是因为文案不行,还是这次的字正好压在房子上"。
代码叠字是**确定性**的:同样输入永远同样输出。

- **生图时就按目标比例出,别先生成方图再裁**。实测 1024×1024 裁成 1200×628,
  把画面下半部分的正主(屋顶样品)整个裁掉,只剩虚化背景 —— 广告主体没了。
  现在 `GEN_SIZE = 1536x1024`,裁到 1200×628 只切边。冒烟测试守着"横版"这条。
- **底图规则写死在 `_BASE_RULES` 里**,不放提示词模板让 AI 拼:不许有任何文字/字母/数字/水印、
  不许有 logo 和品牌、横版 3:2。少一条就是画出来的图没法用。
  > **本文件一度还写着「上三分之一留白(给叠字)、主体放下半部分」——代码里没有这两条。**
  > 那是「默认叠字」时代的规则,改成默认出干净图之后就该去掉了(留白只对叠字有意义,
  > 不叠字时白白浪费三分之一画面)。落地页配图同理:要的是**完整构图**。
  > 2026-09-08 顺手对齐(规矩第 6 条)。
- **顶部那层深色渐变遮罩不能省**:底图长什么样事先不知道,白字压在亮天空上会糊成一片,
  只能靠遮罩兜住对比度。
- **只能照着真归纳出来的方案做图**(`_CREATIVE_PLANS`),不接受 AI 现编文案去渲染 ——
  和 `use_found_creative` 只认搜索结果里的地址是同一个思路。
- **走确认关卡**(`propose_make_creatives` → `confirm_action`):生图要花钱,
  先报「做哪几版 + 大概多少钱」请用户点头。**确认流程本身几乎不花钱**(纯文字,
  且 `BRAIN=auto` 下走 Gemini 免费额度),而**一次误生成的钱够走几百次确认** —— 所以这道关是省钱的。
- **报错必须分得清 402/401/5xx**:余额不足要充值、钥匙无效要换钥匙、5xx 是平台自己的问题。
  混着报会让人跑去反复检查钥匙,而真正要做的是充值(和「400 和 401/403 不能混报」同一条教训)。
- **成本是实测的:$0.203/张**(2026-08-20,记余额 → 生成一张 → 再记余额,两次分别 0.2028 / 0.2032)。
  一开始按 ofox 公开单价 `output_image = $0.000032/token` × 官方 token 数算出 $0.05,**实际差 4 倍**。
  **按公开单价算出来的成本一律要拿账单实测校正**,报低了会让人以为很便宜,批量生成时才发现烧了不少。
  三张一次约 **$0.61(4.4 元)**。
- **余额可以直接查**:`GET {OPENAI_BASE_URL}/user/balance` → `{"balance": 49.37, ...}`。
  **文档里没有这个接口,是试出来的**(其它 `/credits`、`/me` 之类全是 404),所以
  `check_balance()` 查不到时返回 `None` 并**放行** —— 换个中转商可能就没有,不该因此拦住用户。
  生成前会拿它和 `预估花费` 比一比,不够就直接说"要充多少",不会生到一半没钱。
- **一次生几张就要在每张之前播报一次进度**(`_emit("status", …)`)。生一张最长 240 秒,
  而前端是「多久没动静就放弃」(5 分钟)—— 两张连着画就是 8 分钟静默,前端先放弃、
  后端还在画,**钱花了、图传了,用户什么都看不到**(2026-09-09 线上实测)。
  播报既让前端的闹钟重新计时,也让用户知道在画第几张。**落地页配图那个循环同理。**
- **花钱之前,先把所有免费又可能失败的步骤做完**(算文件名、检查方案完整性、查余额)。
  踩了两次:一次卡在给文件起名(`_re` 没导入)、一次卡在上传缺 `mediaName` ——
  **两次都是图已经生成、钱已经付了才失败**,那张的钱就白花了。冒烟测试读源码守着这个顺序。
- **付过钱的图先落盘再上传**(`data/generated/`,已被 `.gitignore` 排除)。
  上传失败时把本地路径告诉用户 —— 钱花了,东西不能丢。
- **生成的图不标注「AI 生成」**(Cole 定的)。

## 六之十六、每段对话各跑各的(2026-08-26)

**一句话**:回复认**当初提问的那一段**,不认"当前打开的那一段"。

**为什么改**:原来全局只有一份 `history`,绑在 `currentId` 上,`saveHistory()` 把它
写回"当前会话"。于是"助手还在回答时切走"会让回复落到别的会话里 —— 只好把界面锁死
(切不了/新建不了/删不了)。而锁还漏了口子:只有 `switchConversation` 拦了 `busy`,
**「新对话」和「删除会话」没拦**,实测复现:
- 思考中点「新对话」→ 回复被写进刚开的空对话,**原来那段只剩提问没有答案**;
- 思考中删掉当前这段 → 回复被追加进**一段毫不相干的老对话**。

**现在的做法**:
- `send()` 开头 `const convId = currentId` 定死这一轮属于谁,全程只用
  `appendMsg(convId, role, content)` / `popMsg(convId)` 写数据 —— **`saveHistory()` 已删掉**,
  它就是那个"写给当前会话"的元凶;
- **屏幕画不画,只取决于 `convId === currentId`**。不是当前那段就只写数据,一个 DOM 都不碰;
- 进行中的状态(进度文字、已经吐出来的字)记在 `running` 这个 Map 里,**不记在 DOM 上** ——
  切回来时 `paintRunning()` 照着它重画。少了这步,用户切回去看到一段"死掉的"对话,
  会以为回复丢了然后重发一遍,白烧一次额度;
- `busy` 从全局布尔变成 `running.has(convId)`:**同一段**不许重复发,**别的段随便发**;
- 左栏小点:**橙色闪 = 这段正在生成,蓝色 = 后台跑完了你还没看**。
  没有这个点的话,"切走之后它还在跑"完全看不出来。

**四条容易犯错的地方(都有测试守着)**:
1. **收尾时不能无条件 `clearLive()`** —— 屏幕上那个「思考中」气泡可能属于**当前开着的另一段**,
   连坐抹掉的话那段看着就像卡死了。三处收尾都要先判 `convId === currentId`;
2. **后台那段出错,错误气泡画出来用户看不到** —— 存进 `convError`,切回去时补给他看。
   而且失败会把那句话从记录里撤掉,**必须把原话一起带上**(`t().lostQ`),否则它凭空消失,
   用户不知道该重发什么;
3. **切走又切回来之后才失败**:屏幕重画过,当初拿到的 `userRow` 已经"掉线",
   往它身上打「未送达」等于打给空气。用 `paintSeq`(重画计数)判断那一行还在不在;
4. **正在生成的那一段不许删**(删了回复回来又会把它建出来,看着像"删不掉")。
   别的段在跑不影响删这一段。

> 教训写进坑表了:**别用「锁住界面」来保证数据不出错**。锁是症状,
> "写给谁认错了对象"才是病。把归属定死,锁就不需要了。

## 六之十七、落地页工作室 + Cloudflare Pages A/B 发布(2026-09-03)

### 已确定的产品链路

这套功能**不接 ClickFlare API,也不经过 GitHub**。ClickFlare 仍由用户手工配置,
本项目只接收用户从 ClickFlare 复制来的精确 URL/脚本:

```text
落地页工作室找案例/拆解 → 生成原创 A/B 两版
→ 用户提供 ClickFlare CTA Click URL + Lander Tracking Script
→ 用户选择本次 Cloudflare 域名和实验 slug
→ propose_publish_landing_pages 登记发布待办
→ 用户下一条消息确认
→ Pages Direct Upload 一次发布 A/B
→ 用户把 A/B 正式 URL 手工添加为 ClickFlare 两个 Lander,设 50/50
→ 用户把 ClickFlare Campaign Tracking URL 复制回投放助手
→ NewsBreak 的 clickThroughUrl 使用 Campaign Tracking URL
```

三个地址绝不能混:

| 地址 | 放在哪里 | 作用 |
|---|---|---|
| Cloudflare 页面 URL | ClickFlare 的 Lander URL | 真正展示页面 |
| ClickFlare CTA Click URL(`/cf/click/1`) | Cloudflare 页面 CTA 的 `href` | 记录 Lander → Offer 点击 |

**CTA Click URL 的格式是固定的**(Cole 确认):`https://<追踪子域名>/cf/click/<数字>` ——
只有域名部分会变,路径形状不变。代码据此校验(`_CTA_PATH`):形状不对就拒绝,
带 `cpid=` 或路径为空时还会点名「这看起来像 Campaign Tracking URL」。
**为什么值得挡**:粘错了页面看起来**完全正常**,要等数据不对劲才发现,那时钱已经花了。
宁可偶尔误拦一个合法地址(报错很响、一句话就能放宽),也不能放过一个粘错的(静默、昂贵)。
| ClickFlare Campaign Tracking URL | NewsBreak `clickThroughUrl` | 记录广告访问并分流到 A/B |

**CTA Click URL 没有被省掉**,只是用户在做页面前就给出来,后端第一次发布时直接植入,
从而省去「先部署 → 再拿 CTA URL → 改页面 → 再部署」这一轮。若 A/B 进入同一个 Campaign、
同一路径、同一 Offer,两版可以用同一个 `/cf/click/1`;NewsBreak 也只使用一个 Campaign URL。

### Pages 项目和域名的规则

- **一个投放 hostname = 一个 Pages 项目**。同一域名再次发布复用原项目,不重复创建;
- **一个实验的 A/B 在同一项目的两个路径**:`/<slug>/a/`、`/<slug>/b/`;
- 新域名第一次确认发布时自动创建项目,项目名由
  `CLOUDFLARE_PAGES_PROJECT_PREFIX + 域名 + hash` 确定性生成;
- 域名在**每次发布时选择**,不能把唯一域名写死在 `.env`;
- `data/cloudflare_sites.json` 保存 `域名 ↔ Pages 项目 ↔ 历史 release`,
  `data/cloudflare_sites/<project>/` 保存待上传的完整静态目录。两者都在 `data/`,不进 git;
- 本地映射丢了也能靠确定性项目名认回项目。域名已被别的项目/DNS 占用时让 Cloudflare 报清楚,
  **不得偷偷覆盖现有绑定**;
- **根路径是中性静态页,不跳转**(理由见下面「实现边界和安全规则」里那条;
  2026-09-08 实测线上根路径返回的就是这张静态页);历史实验路径仍保留在同一部署目录中。

`.env` 只要求固定凭据:

```env
CLOUDFLARE_ACCOUNT_ID=...
CLOUDFLARE_API_TOKEN=...
CLOUDFLARE_PAGES_PROJECT_PREFIX=landing   # 可选,不填也默认 landing
```

**不要再要求**固定的 `CLOUDFLARE_PAGES_PROJECT` 或 `CLOUDFLARE_LANDING_DOMAIN`。
当前账号(2026-09-03 只读实测)能读到 **351 个 Zone、0 个 Pages 项目**;
Wrangler 身份验证成功。0 个项目是正常初始状态,第一次真实发布确认后才创建。
截至记录时**尚未创建真实 Pages 项目、尚未修改 DNS**。

### 实现边界和安全规则

- `cloudflare_pages.py`:列 Zone/项目、域名规范化、确定性项目名、创建/复用 Pages、
  Direct Upload、绑定域名、持久化映射;
- **给出去的预览链接,代码要能回查**(`lp.preview_exists()` + `_finalize` 的 ⚠️ 章):
  模型会照着 `<8位十六进制>-version-a---<英文名>.html` 的形状**凭空编一个**
  (实测发生过,整轮没调生成工具)。回查不通过就当众标出来并列出盘上真实存在的几个;
  预览路由的 404 也换成了说人话的页面,直接把真实预览列成可点链接。
  **这道检查和保险箱里有没有待办无关**,不能挂在老的拆穿章那条分支下面。
- `landing_lab.py`:生成的新页面必须把所有真正去 Offer 的 CTA 写成
  `href="[[CLICKFLARE_CTA_URL]]"`,并在 `</body>` 前留
  `<!--[[CLICKFLARE_LANDER_SCRIPT]]-->`;
- 发布时由 Python **逐字替换占位符**,不让模型拼 URL、改脚本。URL 必须是 HTTPS,
  Tracking Script 必须包含完整 `<script>...</script>`;脚本不在聊天/待办列表中回显;
- **生成那一步就要判断「这页能不能发布」**(`_check_publishable`)。实测模型生成过
  **整页零个 CTA 占位符**、CTA 是 `onclick="alert('Thank you!')"` 的假按钮 ——
  提示词里明写了规则也没用。那种页面走到发布会被拒,但用户已经把整条链走完才知道白做。
  脚本占位符位置固定,**可以确定性补上**;CTA 占位符**不能猜**(哪个按钮才是真正跳往 Offer 的,
  正则判断不了,猜错就是把追踪挂在错误的元素上)—— 只能如实标 `可发布: false` 并说清原因,
  提示词要求 AI **必须主动讲出来**并问要不要重新生成。
- 缺 CTA 占位符的旧页面拒绝发布,要重新生成,不能用正则猜哪个按钮算真正 CTA;
- 不自动访问 CTA/Campaign Tracking URL 做健康检查 —— 打开追踪链接会制造测试点击、污染统计;
- **绝不抢占一个已经在服务的域名。** 绑定自定义域名会让 Cloudflare **接管这个主机名**,
  上面原有的页面**当场下线**。实测踩到:某根域名上有一条已代理的 A 记录,打开是一个
  **在投的落地页**(`<title>Window LP1</title>`);追踪子域名(CNAME 到 ClickFlare)更碰不得,
  绑了等于把追踪打断。`domain_conflict()` 查目标主机名有没有 A/AAAA/CNAME 记录
  (TXT/MX 不影响网页服务,不算),已经绑在自己项目上的不算冲突。
  **这一条是硬拒绝,不给「确认一下就覆盖」的口子** —— 它和「覆盖自己的旧实验」不同,
  影响的是别人正在跑的东西;真要用,应该由人去 Cloudflare 后台先删那条记录。
- **发布是「整站替换」,本地 `data/cloudflare_sites/<project>/` 是「线上有什么」的唯一依据。**
  `wrangler pages deploy <目录>` 会用那个目录**整体覆盖**线上内容,而 `data/` 不进 git ——
  它一丢(换机器/重装/没备份),项目名还能靠域名 hash 认回来,但本地没有历史实验目录,
  这一次部署就会把线上**所有旧实验删掉**,而 ClickFlare 里的 Lander 还指着老地址,
  **买来的流量落到 404 上,钱照花**。`replace_risk()` 的判据**不依赖本地映射文件**(它可能一起丢):
  「Cloudflare 上项目已存在 + 本地除了这次这个实验之外一个目录都没有」→ 拒绝发布并说清楚。
  提案阶段就查(和占位符检查同理);用户明确说「覆盖发布」才带 `allow_replace=true` 放行。
- **上传超时 `UPLOAD_TIMEOUT_S=120` 必须小于前端的 180 秒。** 超了的话用户看到「等太久了」、
  后端还在传,而发布是**不可逆**的 —— 他很可能再确认一次,于是发布两遍。
- **根路径是中性静态页,不跳转、也不列实验清单。** 跳转会让裸访问算到 A 版头上、
  审核抓根域名时只看到一个跳转页;列清单等于把在跑的实验公开给同行。
  A/B 的准确地址在发布结果里给用户,也存在 `data/cloudflare_sites.json`(带发布时间)。
- **Lander Tracking Script 每个追踪域名只需贴一次**(`clickflare_scripts.py` 脚本库)。
  **2026-09-03 拿两份真脚本实测,把「是哪种设计」这个问题定死了**:同一个追踪域名下,
  不管建几个 Lander、路径怎么排,ClickFlare 给的脚本都是**逐字节相同的同一份**——
  脚本里根本**没有 lander id**,它靠上报 `lpurl`(页面自己的网址)区分是哪个落地页。
  两段脚本之间唯一会变的,只有里面写死的那个追踪域名:
  A 2718 字节 / B 2704 字节,差 14 = 两个域名的长度差;把域名换回去后 md5 完全相同。
  所以 `tracking_script` **可以留空**,按 CTA 的域名从脚本库自动取;
  第一次用某个追踪域名时明确报错请用户贴一次,**绝不许自己编**。
  存在 `data/clickflare_scripts.json`(chmod 600,**按 user_id 隔离** ——
  脚本会带出这个人的 campaign 归属,不是像竞品 key 那样的公共资源)。
  **为什么不做成「把脚本抄进代码里当模板,只换域名」**:那等于我们自己生成追踪代码,
  ClickFlare 哪天改版(加参数、换上报格式)就会静默发老版本,而页面一切正常。
  脚本库存的是用户给的**真脚本原文**,一个字都不改,只是省掉重复粘贴。
- **脚本是怎么上报的**(把它放进模拟环境真跑了一遍,不是读出来的):向
  `<追踪域名>/cf/tags/<cftmid>` 发一个请求,带上 `cpid`(campaign)、
  **`lpurl`=页面自己的网址(ClickFlare 就是靠它区分 A/B 的)**、
  `lp_ref`(来源)、`lpt`(页面标题)、`t`(时间戳)。
  脚本里的 `__CONTAINER_ID__` / `__CAMPAIGN_ID__` 是**字面占位符**,不是真 id ——
  它们只是兜底:真实流量从 Campaign Tracking URL 进来时,网址上带着 `?cftmid=&cpid=`。
  **所以别用「浏览器直接打开页面」验证脚本生没生效** —— 那样拿不到 id,本来就不记账,
  看着像坏了其实是对的。
- **`tracking_script_b`(A/B 各一段,留空=共用)保留着**。实测结论是「通用一段」所以现在用不上,
  但留着几乎不花成本,万一以后平台改成每 Lander 一段就用得上。
- **CTA 的域名必须和脚本里写死的追踪域名一致**,`validate_clickflare` 里硬拦。
  实测:脚本不只上报,它还会扫页面上所有 `/cf/click` 链接,把域名**改写成它自己的**——
  `href="https://trk.a.com/cf/click/1"` → `clickflare.l="https://track.b.com/cf/click/1"`。
  两个粘成不同域名不会报错,只会让点击静静流到另一个追踪器去。
  **只在有正面证据时拦**:脚本里提取不到主机名(平台改了格式)时放行,
  否则平台一次改版就把所有发布堵死。
- **A/B 两版内容一模一样 → 拒绝登记**(用户明确要做 A/A 测试时传 `allow_identical=true` 才放行)。
  两版相同 = 花一周的钱跑同一个页面,分出来的「胜者」只是噪音,而**从外面完全看不出来**:
  两个网址不同、两页都正常、数据照常上报。所以只能在代码层拦。
  留口子是因为 **A/A 测试是真实用法**(拿两个一样的页面验证分流准不准),
  但必须用户明说,不许 AI 自己带上 —— 和 `allow_replace` 一个规矩。
  **这个检查排在联网之前**:比两个 SHA-256 是免费的,`owned_zone`/`domain_conflict` 要走网络,
  免费又可能失败的步骤要先做完(和生图那条「花钱的步骤排最后」同理)。
- **A/B 共用一个 CTA Click URL**,前提是「同 Campaign、同路径、同 Offer」——
  要让 A/B 指向不同 Offer 现在做不到,得分两次发布。发布结果里已写明这条。
- 域名绑定后可能还在签证书(`domain_status != active`),结果里会提醒**先别填进 ClickFlare**,
  先用 `*.pages.dev` 自查。
- 发布是外部写操作,走和建广告相同的保险箱:
  `propose_publish_landing_pages` 只登记,下一条明确确认才允许 `confirm_action`;
  确认前对 A/B 文件记 SHA-256,文件被换过就停止发布;
- 同一个待办只允许创建它的账号执行;落地页工作室只能确认/取消发布待办,
  不能借 `confirm_action` 执行投放待办;
- Direct Upload 使用项目本地固定版本的 Wrangler(`package.json`),新机器要先 `npm install`。

落地页工作室新增工具:

- `list_cloudflare_landing_resources(query, limit)`:只读列域名/项目/映射。Zone 有 351 个,
  所以代码会自动翻页,返回聊天时最多 100 个并支持关键词筛选;
- `propose_publish_landing_pages(domain, slug, cta_url, tracking_script="", variant_a_file,
  variant_b_file, allow_replace=False, tracking_script_b="", allow_identical=False)`:校验并登记发布。
  **`tracking_script` 是可选的** —— 留空就按 CTA 的域名去脚本库取这个人存过的那段;
  没存过会明确报错请用户贴一次(顺带列出他已经存过哪些追踪域名);
- `confirm_action(action_id)`:下一轮才真正创建/复用项目、上传、绑定域名。成功结果同时给普通 A/B URL
  和 ClickFlare 可粘贴的 `?cpid={campaign_id}` Lander URL。

每次发布必须向用户收齐:本次 domain/subdomain、实验 slug、精确 CTA Click URL、
A/B 两个生成文件;**Lander Tracking Script 只在这个追踪域名第一次用时要**(以后自动复用,
见上面的脚本库)。页面发布后 ClickFlare 的手工步骤是:建两个 Lander →
同一个 Campaign Default Path 中选同一 Offer → 权重 50/50 → 复制一个 Campaign Tracking URL
回本项目投放。

## 六之十八、ClickFlare 接口(2026-09-03 实测,全部靠打接口打出来的)

**这一节的每一条都是实测,不是文档给的。** ClickFlare 的公开文档**没有接口清单**,
网上还有页面说认证头是 `api-token`(错的)。所以别改这一节的结论,
怀疑变了就跑 `./venv/bin/python check_clickflare_params.py`(只发 GET,不改账号)。

```
入口      https://public-api.clickflare.io
认证      请求头 api-key: <key>
接口清单  GET /api/swagger.json   ← 54 个接口,平台自己吐的
key       ClickFlare 后台 Settings → Security → Generate API Key
```

**怎么确定认证头的**(这个判据以后探别的平台也能用):
用对了头,路径不对时返回 **404「Path not found」**(过了认证、卡在路由);
用错了头,一律 **401「not authorized」**(根本没走到路由)。
拿一个**已知不存在的路径**去打,能拿到 404 的那个头就是对的。

### 数据模型:campaign → flow → paths

**落地页不挂在 campaign 上,挂在 flow 里。** campaign 只有一个 `flow_id`。

```json
"paths": {"defaultPaths": {"paths": [{
   "destination": "landers_offers",
   "enabled": true, "weight": 100,
   "landers_offers": {
     "landers": [{"id": "…", "weight": 100}],
     "offers":  [{"id": "…", "weight": 100}]
   }}]}}
```

**A/B 分流就是把 `landers` 写成两条各 `weight: 50`,`offers` 一个字不动。**
`GET /api/campaigns/{id}` 会把整个 flow 一起返回,不用再取一次。

### 我们要用的接口

| 动作 | 接口 |
|---|---|
| 建落地页 | `POST /api/landings` |
| 读 campaign(连 flow) | `GET /api/campaigns/{id}` |
| 改落地页和权重 | `PUT /api/flows/{id}` |
| 列表 | `/api/workspaces`、`/api/campaigns/list`、`/api/landings`、`/api/offers`、`/api/domains`、`/api/traffic-sources` |
| 报表 | `/api/report` |

`POST /api/landings` 的字段:`workspace_id` / `name` / `url` / `cta_count` /
`is_prelander` / `notes` / `tracking_info.tracking_domain_id` / `tags`。

### 四条要记住的

1. **落地页叫 `landings`,不叫 `landers`** —— `/api/landers` 各种写法全是 404,
   靠猜路径名浪费了十几次请求。**先找 `swagger.json`,别猜。**
2. **swagger 里写着的不一定能调**:`GET /api/campaigns` 实际 404(能用的是
   `/api/campaigns/list`),`/api/offers/list`、`/api/flows/list` 返回 500。
   和 OpenAdLibrary 那条「文档写了的参数不一定能用」是同一条教训,
   体检脚本第 6 项专门守着这两个已知的对不上。
3. **workspace 有 22 个,建东西时绝不许替用户挑。** 和 `_default_ad_account_id()`
   同一条规矩:多于一个就明确报错并列出来让人选 —— 宁可吵,不要建错地方。
4. **key 放 `.env` 全公司一份**(`CLICKFLARE_API_KEY`),和 Cloudflare 一样,
   不按人存 —— 它是公司共用的追踪账号,不是各人各自的广告账户。
   **但它和 Cloudflare 有个区别:改 flow 会立刻影响正在花钱的 campaign。**
   所以隔离靠的不是「每人一把 key」,而是写操作一律走保险箱(第七节)。

### 已经接进来的三个工具(第二步:只读 + 只新增)

| 工具 | 做什么 | 风险 |
|---|---|---|
| `list_clickflare_campaigns(search)` | 列 campaign | 只读 |
| `describe_clickflare_campaign(campaign)` | 看这条现在挂着哪些落地页/offer、权重多少 | 只读 |
| `create_clickflare_landers(...)` | 把已发布的 A/B 地址登记成两个 Lander | **只新增**,不承接流量 |

`campaign` 参数**直接把用户给的 Campaign Tracking URL 传进去**就行 ——
**它的真实形状是 `https://<追踪域名>/cf/r/<24位campaign_id>?…` —— id 在「路径」里,
不在 `cpid=` 里**(2026-09-04 实测,账号里 50 条 campaign 的 `url` 字段全是这个形状;
本文件之前写的 `cpid=` 是错的,害助手找错过一次计划)。
`?cpid=<id>` 是**落地页**地址的形式(发布结果里给用户粘进 ClickFlare 的那条),
`campaign_id_from()` 两种都认。顺着链接定位是**确定性**的,比让模型按名字猜靠谱得多
(账号里 50 条,名字高度相似);抠不出来就明确报错,而且**报错措辞里写死了
「绝不许改用名字去猜」** —— 见坑表那条,模型上次就是从这儿绕过去的。

**两个值是推导出来的,不问用户也不会挑错**:
`workspace_id` 从这条 campaign 自己读;`tracking_domain_id` 按 CTA 地址的域名反查。
这样 22 个 workspace / 74 个域名都不用让用户从一长串里挑。**对不上就报错,绝不随便选一个。**

**建好的 Lander 还不会承接任何流量** —— 要等它被挂进 campaign 的 flow 才算数。
那一步会**立刻改变正在花钱的投放**,属于写操作,必须走保险箱,**还没做**(第三步)。

### 追踪脚本和 CTA 地址不用再让用户贴了(2026-09-04)

**平台自己有接口给这两样**,是探 `/api/scripts/*` 时发现的:

| 接口 | 给什么 |
|---|---|
| `GET /api/scripts/direct` | **Lander Tracking Script 的模板**,追踪域名是占位符 `{{{__TRACKING_DOMAIN__}}}` |
| `GET /api/scripts/links` | click / conversion 地址的模板(`{{{__TRACKING_DOMAIN__}}}/cf/click`) |

**验证方式和当初判断「脚本是不是通用一段」时一样**:拿用户手工从后台复制的两段真脚本,
用「模板 + 各自的域名」拼一遍,**md5 完全相同,两个域名都对上**。

所以 `clickflare_publish_kit(campaign)` 一次给齐:追踪域名(来自 campaign 的 `domain_id`)、
CTA Click URL、Campaign Tracking URL(campaign 自己的 `url` 字段)、以及自动取好的脚本。
**用户一样都不用去后台复制。**

- **这不违反「不许把脚本抄进代码当模板」那条规矩** —— 模板是**每次现取**的,
  ClickFlare 改版我们自动跟上;抄进代码才会静默发老版本。
- **模板里没有占位符就明确报错**,绝不发一段拼错的脚本出去。
- 取到的脚本仍会存进脚本库(第六之十七节),当平台接口不可用时的兜底。
- 发布时 `tracking_script` 留空 → 先查脚本库 → 再从平台现取 → 都不行才请用户贴。

### 第三步:把 A/B 挂进 campaign(已完成)

`propose_swap_campaign_landers` → `confirm_action`,和建广告同一套保险箱。
**这是全项目风险最高的写操作:确认完立刻生效,当场就拿买来的流量往新页面送。**

- **`PUT /api/flows/{id}` 是整体替换,不是打补丁。** 先 GET 回来,**整块**搬 `paths`
  (里面除了 `defaultPaths` 还可能有 `rulePaths` 规则分流),只改目标那一条 path。
  只 PUT 一半等于把另一半清空 —— 和 Cloudflare Pages 那条「整站替换」同一类风险。
  冒烟测试里专门造了「两条 path,只改第二条」来守兄弟 path 不被弄丢。
- **offer 一个字都不动**,只换 `landers` 数组。代码算完会自查一遍,动了就中止。
- **多条启用中的 path 就明确报错让用户指名**,绝不替他挑(和 22 个 workspace 同一条规矩)。
- **指纹防覆盖**:登记时记下 flow 的 SHA-256(只算 `flow`+`paths`,不含 `updated_at`,
  否则平台刷个时间戳就误判)。执行前再比一次 —— 期间有人在 ClickFlare 后台动过就**停下不写**,
  因为整体替换会把他的改动静默抹掉。
- **数据会混,提案里必须讲**:同一条 campaign 换了落地页,历史转化率就横跨两批页面。
  默认是**原地替换**(用户要的就是「用在我正在跑的这条上」),执行结果里带**切换时间**
  (北京+美东),复盘以它为界。要干净的对比就另建一条 campaign —— 提案里写明了,让用户选。
- 原来是 `offers_only`(直接跳 offer、不经过落地页)的 campaign 也能挂,
  但会额外警告「这改的是整条漏斗的形状,转化率会变且和历史不可比」。

### 三处闸门要用同一份清单(`LANDING_ACTION_TYPES`)

待办清单、提示词注入、`_tool_call` 里 confirm/cancel 的模式闸门 —— 这三处原来各自
写死字符串 `"publish_landing_pages"`。加了 `swap_campaign_landers` 之后漏改任何一处,
表现都不一样:**待办登记得了却在清单里看不见** / AI 跨轮忘了编号也没处查 /
**确认时被「本工作室不能确认其它工作区的待办」拒掉**。现在统一走 `_mode_action_types()`,
**加新待办类型只改 `LANDING_ACTION_TYPES` / `CREATIVE_ACTION_TYPES` 这两行。**

## 六之十九、「绕住了就停下」闸门(2026-09-04)

**要解决的事**:模型有时会在工具循环里原地打转 —— 查一次、想一想、再用**一模一样的
条件**查一次,永远得不出结论。用户那头只看到「正在查…」转个不停,而每转一轮都是一次
完整的模型调用。原来唯一的拦法是 `for _ in range(10)`,它**只数轮数,不看内容也不看花费**:
模型第三次发出同样的请求时它一声不吭,非要等满十轮才停,那时钱早花完了,
最后还只甩一句「(工具调用轮数过多,已中止,请换个问法)」——
用户既不知道发生了什么,也不知道花了多少。

**账要算清楚才知道这有多贵**(实测,gpt-5.5):一轮 ≈ 6.8K 输入 + 7~9K 输出,
**输出占成本约 89%** —— 屏幕上只有几百字,多出来的全是模型看不见的思考 token。
一轮 ≈ $0.24,十轮 ≈ $2.4,**而且什么答案都没有**。

`LoopGuard`(`agent_server.py`)守三条,任一命中立刻停:

| 判据 | 阈值(`.env` 可调) | 为什么是这个数 |
|---|---|---|
| 同一个工具 + **完全相同的参数** 调到第 N 次 | `TOOL_REPEAT_LIMIT=3` | 2 次可能是一次正常重试(上次超时/报错),3 次就是没在往前走 |
| 工具轮数 | `MAX_TOOL_ROUNDS=10` | 沿用原值 |
| 这条消息的**输出**(含思考)token | `TURN_TOKEN_BUDGET=40000` | **只算输出,不算输入。** 一轮重推理 ≈ 7~9K 输出,40K 约在第 5 轮,赶在轮数上限(10)之前。填 0 = 不限。**绝不能算输入**:输入随对话变长而涨(整段历史每轮重发),长对话会被当场误杀 —— 见第六之二十四节 |

- **闸片按请求隔离**(`CURRENT_GUARD` contextvar,`_new_turn()` 里 set,**必须在
  起线程 / `copy_context()` 之前**)。用模块级全局的话,几段对话同时提问时
  A 的轮数会算到 B 头上 —— 和 `_REQUEST_SEQ` / `CURRENT_EXECUTED` 是同一个坑。
  **`default` 只能是 `None`**,不能直接放一个 `LoopGuard()`:那样所有请求共用一份。
- **两条大脑路径都要装**(`ask_openai` 和 `_gemini_loop`)。只装一边的话,
  换条路又是死胡同 —— 和「切了大脑还有一条路在偷偷用旧模型」同一条教训。
- **停下来那句话要能让人知道下一步干嘛**:说清「发生了什么 / 这条消息花了多少 /
  你可以怎么做」,中英双语。它是代码直出的,不过 AI。

**花费这条闸门只在真拿到用量时才生效,绝不本地估**:
- OpenAI 非流式读 `r.usage`;**流式必须传 `stream_options={"include_usage": True}`**,
  用量在**最后一个 chunk** 上,而那个 chunk 的 `choices` 是空的 ——
  **要抢在 `if not chunk.choices: continue` 之前读**,否则永远读不到(冒烟测试守着这个顺序);
- 有的中转不认 `stream_options`,碰过一次 400 就记进 `_STREAM_USAGE_OK` 不再发,
  而不是每轮撞一次墙(且只在错误里确实提到这个参数时才降级,别把别的 400 一起吞了);
- Gemini 读 `usage_metadata`,流式每个 chunk 带的是**累计值**,所以**不能累加**
  (累加会算出好几倍,闸门提前误踩)—— 但也**不能盲目覆盖**:实测最后一个 chunk
  可能只带 `prompt_token_count`、不带 `candidates`,一覆盖就把前面记到的输出抹成 0
  (第六之二十四节)。**逐字段取 `max`** 两头都对。`thoughts_token_count` 要单独加,
  而 OpenAI 的 `completion_tokens` 已经含思考,不用再加一遍;
- **拿不到输出用量时这条闸门自动失效,并如实说「查不到」** —— 本地估不出思考 token,
  那才是大头,估出来的数小得离谱,拿它当闸门等于没有闸门,还给人「已经管住了」的错觉。
  轮数和重复调用两道照常管用。
- **闸门只看输出,不看输入**(第六之二十四节):输入随对话变长而涨、又只占成本约 11%,
  算进去的话长对话会被当场误杀,而模型根本没在打转。

## 六之二十、工作室之间怎么交接(2026-09-07)

**三个工作室(投放助手 / 素材 / 落地页)的分隔要保留**,但它原来有两个死胡同,
两个都是线上实测踩到的,而且**根子是同一个:模型拿不到出路,就自己编一个理由**。

### 先说清楚一件事:分工作室省的钱比想象中少

实测每轮固定开销(工具 schema + 系统提示词):

| 模式 | 工具数 | 每轮固定 token |
|---|---|---|
| 投放助手 | 36(**全放行**) | ≈ 13,200 |
| 素材工作室 | 15 | ≈ 6,700 |
| 落地页工作室 | 19 | ≈ 9,300 |

看着省 30~49%,**但那是省在「输入」上,而输入只占成本的 11%**(输出占 89%,
见第六之十九节那笔账)。折成钱一条消息只省约 11%。
**所以真正撑着这套分隔的是安全,不是省钱** —— 讨论要不要合并工作室时,别拿成本当主要理由。

### 死胡同一:模型编造「接口限制」

用户在**投放助手**里问「账户上的素材图有哪些?」,助手答
「由于目前系统接口的限制,我暂时无法为您拉取账户里的历史素材图」。
而 campaign 模式**放行全部 36 个工具**,`recommend_creatives` 当时完全可用
(拿真凭据实测:能返回带 assetUrl / 尺寸 / CTR / 花费的历史素材)。

诱因很具体:那个工具的说明把**动作锁死在"推荐"、场景锁死在"建广告第3步"**,
而「列出账户里有哪些图」这种问法对不上号,模型于是编了个理由。解法:

- 说明改成「**列出/查看**这个账户已有的素材图」,并加一句**排他声明**
  「**这是本项目唯一能拿到账户历史素材的工具,没有别的接口**」——
  专治「我以为还有别的接口而我没有」这条编造路径;
- **两处都要改**:`BRAIN=auto` 时 Gemini 从**函数签名和 docstring** 生成 schema,
  走 ofox 时模型只看 `_oa_tool` 里那段。**只改一处等于漏一半**
  (冒烟测试把两处分开查,各自退回都会变红)。

### 死胡同二:拒绝没有出路

用户在**落地页工作室**里要图库图,闸门拦下,返回的是一句
`{"error": "当前工作室不能执行这项投放操作。请切换到投放助手。"}` ——
模型把它转述成「我没有权限」,然后让用户自己上传。用户不知道该去哪个工作室,
也不知道切过去之后刚才聊的还在不在。

现在 `_tool_call` 被拦时返回一张**代码写好的移交单**(`_handoff()`):

```json
{"handoff": {"到哪个工作室": "素材工作室", "switch_to": "creative",
             "为什么": "「从授权图库里找素材」这件事当前工作室做不了…",
             "到那边第一句可以说": "帮我从授权图库里找素材"},
 "note": "**绝不许说成「我没有权限」「接口限制」「系统不支持」**…"}
```

- **指到最近的那个工作室**:隔壁工作室有这个工具就指隔壁(离他正在做的事更近),
  两边都没有才回投放助手(它全放行,所以一定能用);
- **待办不归本工作室管**时同样给去处,不再是一句「不能确认其它工作区的待办」;
- **但「编号根本不存在」要和「没有权限」分开报** —— 混着报会让用户跑去切工作室,
  而真正该做的是查编号(和「400 和 401/403 不能混报」同一条)。

### 死胡同三:切工作室 = 静默丢上下文

顶栏那三个按钮原来直接调 `startNewConversation(mode)` —— **开一段全新的空对话**。
于是助手让用户「去素材工作室」,他切过去发现刚才聊的全没了。
**这才是前两个死胡同真正让人绝望的地方**:给了出路,走过去却什么都不剩。

现在 `switchStudio(mode)`:

- **空对话原地切**(换 `conv.mode` 就完了),不再每点一下就在左栏留一个空壳;
- **有内容就弹框问**:「带着这段过去」/「另开一段新的」/「再想想」,
  用户自己定。带过去 = 同一段对话只换视角,消息一条不丢;
- **那一段还在生成时不给「带过去」** —— 在飞的请求带的是旧视角,
  半路换挡只会让人以为切了却没切。这时只给「另开一段」并说清为什么;
- `askConfirm()` 因此多了一个可选的第三按钮(`opts.alt`,resolve 成字符串 `"alt"`),
  老调用方一个都没传,行为不变;顺带 `opts.icon === false` 可以把垃圾桶图标藏掉 ——
  切工作室不是破坏性操作,不该顶着它。

> **中间产物本来就是通的**,不用另做搬运:`_LANDING_PAGES_BY_USER` /
> `_CREATIVE_PLANS_BY_USER` / `_CREATIVE_MODELS_BY_USER` 全都按 `CURRENT_USER_ID` 存、
> 和 mode 无关。断的只是对话本身和「模型不知道自己手上有什么」。

## 六之二十一、一个广告组里放几条广告(2026-09-07)

**用户说「一个 campaign 下面一个 ad set,一个 ad set 下面两个 ad」—— 这是素材 A/B 最常规的做法**
(同一份预算、同一批人群,只换素材和文案),而代码里**压根没有「多条广告」这个概念**:
`propose_create_campaign` 的 `asset_url` 是单数,`_child_names()` 写死只出一条 `001`,
连 `ad_name` 这个参数都没有(用户明说了要叫 `-002`,**没有任何地方能接住这个值**)。

于是模型只能退回去再跑一次 `propose_create_campaign`。执行时 campaign 那一层是幂等的
(同名就复用),**而 ad set 和 ad 是无条件新建的** —— 结果:

```
NB-Window-260904-01
├─ 广告组 260904-Window-001  id …8316674  $20/天  → AD-260904-Window-001   ← 第2套素材
└─ 广告组 260904-Window-001  id …1991937  $20/天  → AD-260904-Window-001   ← 第1套素材
```

**两个广告组同名、两个广告同名、日预算从 $20 变成 $40**,而用户以为是 $20。
报表里只有一列 name,这两行他一眼分不出哪个是哪个。
(翻旧任务存档发现 **2026-08-14 的 `paa-0814` 就已经这样了** —— `targets` 里两个
`paa-0814-set1` id 不同。这个 bug 早就在发生,只是没人注意到。)

### 现在的做法

- **一次建好:`propose_create_campaign(..., extra_creatives=[{...}])`**
  第一套走原来的 `asset_url/headline/description`,第二套起放 `extra_creatives`;
  `_child_names(tw, ymd, n)` 把广告名排成 `AD-年月日-类型-001 / -002 / …`,
  **广告组还是只有一个**。待办里存的是**快照**(`ads` 列表),不是指向可变状态的引用。
- **事后追加:`propose_add_ad(ad_set_id, ...)`** —— 计划和广告组已经建好了才想加广告时用。
  序号**扫账户里同前缀的广告名取最大值 +1**(和 `_campaign_name` 同一个套路,
  按整个账户扫而不是只扫本组:同一支计划里两个组的广告前缀一样,只看本组会和兄弟组撞名)。
  落地页 / 按钮文案 / 品牌名**从这个组里已有的广告借**,不再问用户第二遍、也不给模型编的机会;
  借了什么会列进 `defaults_used` 如实告诉用户。
- **同名广告组一律拒绝**,登记时查一次(`_existing_ad_set`,免费的检查排在前面)、
  **执行时再查一次**(待办会在保险箱里躺很久,期间用户完全可能又建了一条)。
  拒绝时**给出路**:要么改用 `propose_add_ad` 加进已有的组,要么让用户给新组换个名字 ——
  和第六之二十节「拒绝必须带出路」是同一条。
- **同一个组里两条内容完全一样(素材+标题+描述)→ 拒绝登记。**
  等于花两份钱跑同一条,而且数据上分不出胜负 —— 和落地页「A/B 两版相同直接拒」同一条规矩。
- **逐条体检,一条不合格整单拒**(报错点名「第2条」),不做「跳过坏的、把好的建了」:
  用户要两条,给他一条还报成功,他要到平台后台才发现。
- 有广告没建成时,结果里带 `没建成的广告` 和「建成的广告条数 N/M」,
  提示词要求**点名说是哪几条** —— 和第六之九节「三层开关有失败的要点名」同一条。
- **老待办兼容**:保险箱是持久化的,里面可能躺着「支持多条」之前登记的单子(只有单套字段),
  `_ads_from()` 把它回落成一条广告。

> `propose_add_ad` **不进** `CREATIVE_TOOL_NAMES` / `LANDING_TOOL_NAMES` —— 它是投放助手的写操作。

### 「编号不存在」的措辞也一起改了

同一轮里模型还**编了一个待办编号** `0d51be21` 报给用户(服务器日志里从来没有这个编号),
用户回「确认」,`confirm_action` 返回的是:

```
找不到待办 0d51be21(可能已执行/已取消,或 id 有误)
```

它没有承认编错,而是**从括号里那三个「可能」里挑了个最不用担责的**,对用户说
「**系统的待办编号由于超时或刷新重置了**」,然后重新登记了一条。用户完全看不出是编的。

`_no_such_action()` 现在两层:①明说**系统根本没有「重置编号」这回事**,把那条退路堵死;
②**把真实存在的编号列出来**(只列他自己的、这个工作室管得了的)——
正确答案摆在眼前就不用编了,和落地页预览 404 页面直接列出盘上真实文件是同一个做法。
`confirm_action` / `cancel_action` / `_tool_call` 三个入口**用同一份措辞**:
话不一样的话,模型会挑最软的那句来引用。

## 六之二十二、按人隔离的「读」这一头(2026-09-07)

**上一轮只把「写」堵上了** —— `confirm_action` / `cancel_action` 查归属、待办登记带
`user_id`。对抗性审查(38 个 agent,逐条实跑复现)指出:**读这一头一个字没改**,
而且不是一处,是**五处**。教训一句话:

> **同一份共享状态,读和写必须用同一把尺子。** 只堵「动」不堵「看」,
> 等于把别人的东西连同编号一起摊开,还催着他去执行 —— 而他一执行就撞上
> 「这个待办属于另一个账号」,是个**代码亲手造出来的死胡同**。

判据统一走 `_my_action(a)`(agent_server)和 `_mine(task, uid)`(scheduler),
两者**逐字同义**,也和 `confirm_action` 那条一致:

- 不在用户上下文(命令行 / 测试 / 后台线程)→ 全都算;
- 对象上没有归属(按人隔离**之前**留下的老待办 / 老任务 / 老预览文件)→ **也算**;
  否则那些东西谁都管不了、谁都发不了。这是 confirm/cancel 早就做出的选择,保持一致。
- 两边都有且不相等 → 不是他的。

| 漏在哪 | 原来会发生什么 |
|---|---|
| `list_pending_actions` | B 一句「查一下待办」拿到 A 待办的**全部原文**:广告账户 id、落地页地址、预算、标题描述、素材地址;发布待办还带 domain / slug / **cta_url(里面就是 A 的 campaign id)**。三个工具集里都有这个工具,所以工作室里同样漏 |
| `_system_prompt_now` 的待办注入 | **比上一条更主动**:B 什么都没问,每轮 system prompt 里都躺着 A 的待办,后面还跟着「用户已确认时,直接调 confirm_action(用上面的编号)执行」 |
| `_finalize` 的 ⚠️ 拆穿章 | 连 mode 都不过滤,直接 `PENDING_ACTIONS.keys()` 全倒出来 —— 而这是**代码直出给用户看**的文案,还写着「请回复『执行待办 xxx』重试」 |
| `sched.recent_runs` | 模块级一串**纯字符串**,消息里带着计划名,无过滤拼进每个人的提示词,后面跟着「若用户还不知道,主动告知一句」 |
| `landing_lab` 的预览目录 | 所有人平铺在一个目录:404 页面把**别人的**预览列成可点链接;拿到文件名就能读别人整页;`KEEP_GENERATED=80` 全局共用,A 多生成几轮就把 B 的页面删了,而 B 的待办还指着它 |

顺带补上两处「查重没认人」:`sched.add_task` 的查重条件缺 `user_id`
(A 和 B 定同样的事被归并成一条,B 拿到 A 的 task_id,然后既看不见也取消不了),
以及各类上限要**按人算**(`RECENT_RUNS_PER_USER`、`KEEP_GENERATED` 每人一份)——
和素材登记表那条「200 条上限是每人一份」是同一条。

- `list_pending_actions` 的输出里 **`user_id` 直接不输出**,不写成「[已保存,不回显]」:
  筛过之后它必然等于当前用户,是纯噪音,白占提示词 token。
  `tracking_script` 那种是「有值但不能给你看」,两回事,别套同一套处理。
- 落地页预览改成 `data/landing_pages/<uid>/`,老文件仍平铺在根目录(当作没有归属)。
  取文件统一走 `lp.resolve_preview(filename, owner)`:先找他自己的,再找老文件,
  目录穿越一律拒绝(这个函数的返回值会被直接读出来发给用户)。

### 最要紧的那条:流式线程一条测试都没有

**网页默认走流式**(`/api/chat/stream`),而按人隔离全靠 contextvars。
大脑函数是同步阻塞的,所以丢进线程跑 —— 线程必须 `copy_context()`。

审查实测:把 `ctx.run(work)` 改成 `work`(去掉上下文拷贝),
**12 条按人隔离的测试全部照样绿** —— 因为它们都是直接调函数,一次都没走过那条线程。
而线程里 `CURRENT_USER_ID.get()` 是空串的话,这一整轮隔离**在生产主路径上完全失效**:
`_put_action` 只查 key 在不在(空串照过)、归属判据空串是假值整段短路、
`_find_duplicate` 又把两个人归并、素材和预览全落进同一个桶、
`sched.list_tasks("")` 谁都看得见谁的。

现在有一条测试**真的走 `chat_stream`**,在线程里回读
`CURRENT_USER_ID` / `nb.CURRENT_CREDS` / `CURRENT_CHAT_MODE` / `CURRENT_SEQ` / `CURRENT_GUARD`,
并且先断言**确实换了线程**(否则实现改成同步跑,这条测试会变成假绿)。

> **规矩**:凡是靠 contextvars 做隔离的项目,**每一条会起线程 / 起任务的入口**
> 都要有一条测试真的走过去,在里面回读一次。只测函数本身等于没测 ——
> 和坑表「测『新函数』不等于测『它被接上了』」是同一条,只是这次断的是线程边界。

## 六之二十三、落地页里的配图(2026-09-07)

线上实测:生成的落地页里是一个灰色占位框,写着 `[ Image: A beautiful modern house... ]`。
**模型不是偷懒,是手上没有图** —— 三层都堵着,而且每层当初都是有意的:

| 挡在哪 | 原来是什么 |
|---|---|
| 生成提示词 | 明写「**不要引用外部 JS/CSS/图片**」 |
| `summarize_landing_page_patterns` | **没有任何图片参数**,模型拿不到一个真实地址 |
| 预览路由的 CSP | `img-src data:` —— 就算写了外链,预览也拦掉 |

> 顺带分清:**CSP 只管本地预览,管不到线上。** 发布是把目录整个传到 Cloudflare,
> 我们没给线上页面设过任何响应头。真正卡住的是前两层。

### 现在这条链

```
模型只写 [[IMAGE_1]] + 英文搜索词
  → Python 去授权图库找(creative_search,只要可商用)
  → 下载、按内容 sha256 命名、存进 data/landing_pages/<uid>/img/
  → 逐字替换成相对路径 img/xxx.jpg,并打上 data-slot="n"
  → 预览:/landing-pages/img/xxx.jpg(按人隔离)
  → 发布:把**页面真正引用到的**那几张复制进 <slug>/a/img/ 和 <slug>/b/img/
```

- **模型永远不碰图片网址。** 它只写编号和「我要什么图」,找图/下载/落盘/替换
  全是 Python 的确定性动作 —— 和 CTA 追踪链接完全同一条规矩:
  **凡是绝不许编的东西,连举例都不许它写。** 它写出来的地址一定是编的,
  用户点开是一张裂图,而买来的流量已经落在上面了。
- **用相对路径 `img/xxx.jpg`,不是绝对地址。** 这样本地预览
  (`/landing-pages/xxx.html` + `/landing-pages/img/`)和线上
  (`/<slug>/a/index.html` + `/<slug>/a/img/`)**同一套写法都成立**,不用拼两次。
  所以 A、B 各自目录下都要有一份自己的 `img/`。
- **发布只复制页面真正引用到的那几张**,不是整个图库目录 ——
  多传的每一张都是白花的时间,而且会把这个人别的实验用过的图一起公开出去。
- **找不到图就把整个 `<img>` 摘掉**,不留破图:页面少一张图只是丑一点,
  留一张裂图是「买来的流量落在坏页面上」。同理,**发布时源图丢了不挡发布**
  (整次回滚代价更大),但结果里必须带 `⚠️配图丢了` 和一句「别说成发布完全成功」。
- **模型硬写的外部图片地址,代码层摘掉并报告** —— 提示词里禁了也拦不住
  (坑表里反复的那条)。外链还有别的坏处:哪天失效就是裂图,而且会把用户的
  浏览行为报给第三方。CSP 因此只放开 `img-src 'self' data:`,**不给 https:**。
- **`swap_landing_image(file, slot, query)`**:只换某一张图,别的一个字不动。
  为了换一张图重新生成整页的话,要等模型再出一整页(实测 61 秒)、再花一次钱,
  而且**文案版式全会跟着变,用户刚看顺眼的东西就没了**。靠 `data-slot` 定位。
- 一页最多 4 张(`MAX_IMAGES_PER_PAGE`),首屏那张最有用。

### 图库配不上时,用 AI 生图补(2026-09-08)

家装类在免费图库里本来就薄(实测 `metal roof installation crew` / `gutter cleaning`
在 Openverse 都是 **0 张**),所以要有一条不依赖图库的路。

**但生图要花钱($0.20/张),而「生成落地页」是免费步骤** —— 一页 3 张就是 $0.6
没经用户同意。所以**绝不能在生成那一步顺手生成**,必须走保险箱(第六之十五节)。

链路拆成两步:

```
生成落地页 → 图库先试(免费)
  → 配不上的位置留一个**隐藏占位** <img data-slot="n" data-want="英文画面描述" hidden>
  → 用户说「用 AI 把图补上」
  → propose_landing_images 报价(N 张 × $0.20,顺带查余额)
  → 用户确认 → 才真生成、填进页面
```

- **隐藏占位是关键**:`hidden` 且**没有 `src`** —— 页面上什么都不显示、浏览器不会去请求、
  `_copy_images` 也不会把它当成图去复制(它只认 `src="img/…"`)。
  现在就发布出去是干净的;以后想补图,位置和「要什么图」还在。
  **别用灰方块占位** —— 那就是模型原来干的事,用户看到的是个空框。
- **画面描述用当初写在页面上的那句**(`data-want`),不许在确认那一刻另写 ——
  否则「报价时看到的是 A、做出来的是 B」(和生图待办存快照同一条)。
- **同一页的几张用不同的 `variant`**(镜头语言轮换),否则几张长得一样。
- **落地页配图不叠字**:标题是 HTML 排的,图上再来一遍就重复了。
  `cr.render(..., overlay=False)` 正好是默认行为。
- 部分失败要**点名说哪几张**,成功的那几张钱已经花了、必须留下,
  失败的位置**继续留着隐藏占位**,好让他重生。
- 图已经配齐时明说「**这不是出错**」——否则模型会转述成「功能坏了」。

> **为什么不用「账户历史素材」当配图**:实测把账户里的图拉出来看了 ——
> 那是**完整的成品广告**:标题烧在图上、带着另一家的品牌 logo、右下角还有个假按钮。
> 贴进落地页就是「同一句话出现两遍 + 别人的牌子 + 一真一假两个按钮」。
> 而且**新账户根本没有历史** —— 这个"兜底"恰恰在最需要的时候是空的。
> 只有我们自己 `creative_render` 生的图是干净实拍(它按设计就不出文字),那条才成立。

### 图库这条路不走了,配图一律用 AI 生(2026-09-08,Cole 定的)

**2026-09-07 实测**:`.env` 里 `PEXELS_API_KEY` / `PIXABAY_API_KEY` **两行都是空的**,
所以只剩 Openverse,而它**对家装类基本没用**。图库这条路实际一张也配不上
(AI 生图那条不受影响,见上一小节)。

> **更正**:我一开始写成「从这台服务器连不上」,**那是错的**。2026-09-08 查实:
> DNS 18ms / TCP 20ms / TLS 123ms 全正常,**是 Openverse 自己的服务器在磨**,
> 而且慢得毫无规律 —— 同一分钟内 `roof repair` 31s、`metal roof installation` 32s、
> 而 `window replacement` 1.7s、`contractor house` 0.5s。
> **原来的超时正好是 20 秒,卡在生死线上**:快的过、慢的挂,表现出来就像
> "时好时坏的网络问题"。已给 Openverse 单独放宽到 45 秒(`_SOURCE_TIMEOUT`)。
> **教训:「连不上」和「对方很慢」是两回事** —— 前者查 DNS/TCP,后者看 `time_starttransfer`。
> `curl -w` 把这几段时间分开打出来,一眼就能分清,别凭"超时了"就下结论。

**但放宽超时救不了这个功能**,因为第二个毛病是硬的 —— 实测同样的关键词:

| 关键词 | 耗时 | 结果 |
|---|---|---|
| `metal roof installation crew` | 25s | **0 张** |
| `gutter cleaning` | 1.4s | **0 张** |
| `roofer working` | 1.1s | 10 张,里面还有 `0x0` 和竖图(质量闸门会毙掉) |
| `window replacement home` | 0.1s | 3 张,4032×3024 ✅ |

**恰恰是 roof / gutter 这些最需要的词没有图** —— 这和第六之十一节早就写下的
「家装维修这类品类的 CC0 存量很薄」完全对得上。

**2026-09-08 Cole 定了:不配这两把钥匙,配图一律走 AI 生图。**
所以图库这条路实际上是关着的(代码没删 —— 哪天填了钥匙它自己就活过来,而且免费的先试)。

**这个决定会连带改掉一批「出路」的措辞,不改就成了死胡同**(第六之二十节那条):
原来配不上图时,三处都写着「去申请一把免费的 Pexels / Pixabay 钥匙」——
那是一条**已经关掉的路**。现在三处一律指向「**用 AI 把图补上**」:
生成落地页时(一个图库都没启用 / 图库全挂)、以及 `swap_landing_image` 换图失败时。

**换图这件事还差点整个断掉**:`swap_landing_image` 只会去图库找,没钥匙就**永远成功不了**;
而 `propose_landing_images` 原来只填**空**位(隐藏占位),动不了已经有图的位置 ——
两条路一起堵死,「换掉这张图」就没门了。补法:

- `pending_image_slots(..., include_filled=True)` 把**已经有图**的位置也列出来(`filled: True`);
- `propose_landing_images` 里**点名第几张时**(`slots="1"`),已经有图的也允许**重做**;
  留空时行为不变(只补空位),不会让用户一句「把图补上」就把已有的图全重做一遍花冤枉钱;
- 报价里给那几张挂 `⚠️ 这个位置已经有图了,重做会把原来那张换掉`,`note` 里再单独讲一遍 ——
  **花钱换掉一张他已经看过的图,和「把空位补上」是两件事**,混着说他不会注意到原来那张要没了。
- **图库配上的那张也要记住 `data-want`(画面描述)。** 原来只记 `data-slot`,
  描述在配上图的那一刻就丢了 —— 而没有图库钥匙时,「换掉这张」只剩「用 AI 重做」一条路,
  重做要的正是那句描述。**凡是「以后可能要重做」的产物,当初的输入就得跟着一起存下来。**

### 改图这条链复查出来的四个洞(2026-09-09)

把上面这套改完之后逐条复查,挖出四个**都能静默出事**的问题:

| 在哪 | 后果 |
|---|---|
| `_execute_make_landing_images` | 待办躺着的期间页面被清理掉 → **先 `cr.render()` 花钱、再去填才发现文件没了**,实测两张 $0.40 全打水漂,而且图连盘都没落 |
| `fill_image_slot` | 剥旧 src 的正则**只认双引号**,而那个 `<img>` 是**模型**写的 —— 单引号时旧 src 剥不掉,标签上留下**两个 src**,浏览器用第一个,于是钱花了、画面还是旧图 |
| `swap_image` | 同一个毛病:单引号的图、**隐藏占位**(它压根没有 src)一个字都改不动,**却照样返回「换好了」** |
| `pending_image_slots` | 要求必须有 `data-want`,没有就整条跳过 —— 用户点名重做会被告知「这一版没有第 1 张图」,而图明明在页面上 |

改法:

- **`_set_src()` 一个函数管两处**,认双引号/单引号/不带引号/根本没有,自闭合写法也照顾;
  **原地替换、不挪位置** —— 换图要是纯粹的「src 值替换」,别的一个字不动(有老测试守着这条);
- **成功判据看「目标状态达成没有」,不是「有没有变化」**:`_src_landed()` 查标签最后
  是不是真挂着那张图且不再 hidden。拿「页面变没变」当判据的话,**换回内容一模一样的图**
  (文件名是内容 sha256,所以也一样)会被误报成「改不动」;真遇到同一张图要**如实说**
  「画面不会有变化」,别让用户以为功能坏了;
- **花钱之前先查位置还在不在**,不在就一张都不生;
- **永远不可能成功的待办要收走**(`dead` 标记 → `confirm_action` 弹出保险箱)。
  保险箱是持久化的、每轮还注入提示词,留一条死单等于每轮催用户去确认,而确认多少次都是同一个错;
- 没有 `data-want` 时回落 `alt`(模型写的,是页面自己的东西),都没有就**明说不知道该画什么**并给出路,
  绝不拿一句空话去花 $0.20。

- **「没找到合适的图」和「图库根本连不上」必须分开报。** 混着报的话用户会去换关键词,
  而真正该做的是换条路(现在是用 AI 生)—— 换多少词都没用。和坑表「5xx 里可能写着确定性的原因,
  别一律说成稍后再试」是同一条。
- **确认连不上之后就别一张一张白等**(`_DEAD_SOURCES`,只在一次生成内有效):
  一页 4 张 × 20 秒 = 80 秒,用户那头看着像卡死,还可能撞上前端的空闲上限。
  实测从 80 秒降到 26 秒(第一张付超时,后面直接跳过)。

## 六之二十四、编造的待办编号,和被算错账的花费闸门(2026-09-07)

同一天线上抓到的两件事,根子不同但都很贵。

### 一、模型会编一个待办编号,还会编一个理由

实测两次(**服务器日志里这些编号一次都没出现过**,是决定性证据):

| 它说的 | 真相 |
|---|---|
| 「刚才的待办编号由于**临时归档**原因没对上,我已经为你重新规范登记了」 | 素材工作室里它**根本没登记过任何待办** —— 闸门给了正确的移交单,它收到了也没照做,直接编了个编号让用户「回复确认」 |
| 「由于**文件路径索引**的小意外,我已经为你重新对齐了文件,现在重新发起 A/B 落地页的发布登记」 | 那条发布待办**已经执行成功了**(🔒 钢印就在同一条回复里),它却让用户以为还要再发一次 |

上一轮的修法是在 `confirm_action` 的报错里**禁掉「超时/重置/刷新」这几个词**。
**第二天它就换了说法。禁词表是打地鼠 —— 能编理由的位置有无限多种措辞。**

现在两层:

- **话术改成正面规定**:不再列禁词,而是写死「代码里没有任何一种机制会让待办编号
  消失或对不上 —— 不存在超时、重置、刷新、归档、索引、路径、同步、缓存这类事」+
  「**唯一诚实的说法是『刚才那个编号是我弄错的』**」;
- **真正管用的是代码回查**(`_fake_action_note`):`_finalize` 把回复里报出来的
  每个待办编号拿保险箱核一遍,编的当场盖 ⚠️ 章,并写明「**别按它说的回复确认**,
  确认了什么也不会发生」,顺带列出真实存在的编号。
  **和预览链接那道 `_fake_preview_note` 是同一个思路。**

几个细节都是踩出来的:

- **本轮真执行掉的编号要算数** —— 它已经不在保险箱里了,但提它是诚实的;
  不然「发布成功」那条正确回复会被误伤。
- **不许裸搜 8 位十六进制** —— 落地页预览文件名就是 `<8位十六进制>-xxx.html`,
  裸搜会把它当成编号。只认紧跟在「待办/编号/action_id/pending action」后面的那串。
- **三条 `_finalize` 返回路径都要查**。原来 `_fake_preview_note` 在三处各写一次,
  加第二道核验就要改三处 —— 现在收成一个 `_extra_notes()`
  (和「同一个判断散在三处,加一种就漏一处」同一条)。

**判据是「有没有真的登记过」,不是「现在还在不在保险箱里」。**
第一版按后者写,当天复查就抓到两处误报:用户**成功取消**一个待办之后,
模型正确地说「待办 xxx 已取消」——反而被盖章说「多半是助手编的」;
隔一轮复述上一次执行过的编号同样中招。
合法的编号本来就会离开保险箱(执行掉、取消掉、或者只是在复述前几轮的事)。
所以另存一份 `_KNOWN_ACTION_IDS`(只存编号,`_put_action` 和开机 `_load_actions` 时登记)。

> **喊错一次狼,这道章以后就没人信了。** 拆穿章这类"代码直出给用户看"的警告,
> 误报的代价比漏报更隐蔽:用户不会来报 bug,他只会开始忽略所有 ⚠️。
> 加一道拆穿章之后,一定要把**所有合法路径**走一遍,确认它们不会被误伤。

> **「拒绝必须带出路」是必要条件,不是充分条件。** 出路给了它也可能不走 ——
> 凡是由模型转述的两阶段流程,**编号必须能被代码回查**。

### 二、花费闸门原来是个「对话长度闸门」

实测:一段很长的对话里用户说「上广告」,**7 轮就被停掉**,报的是
`输入 111K + 输出/思考 0K`。**模型根本没在打转**,是整段历史每轮重发把输入顶上去的。

两个错叠在一起:

1. **`TURN_TOKEN_BUDGET` 算的是 输入+输出。** 输入随对话长度涨、不随打转涨,
   而且只占成本约 11%(见第六之十九节那笔账)。拿它当花费闸门,长对话必然误杀。
2. **Gemini 那条路的用量被抹成 0。** `take_usage` 按「覆盖」记(因为流式 chunk 带的是
   累计值),而**最后一个 chunk 可能只带 `prompt_token_count`、不带 `candidates`** ——
   一覆盖,前面记到的输出全没了。累计值要用 **`max`** 取。

改完:**闸门只算输出**(占成本 89%,且只随「真的又想了一轮」而涨),
默认 `TURN_TOKEN_BUDGET=40000`(一轮重推理 ≈ 7~9K 输出,约在第 5 轮,
赶在轮数上限 10 之前)。停下来那句话也改成明说「想掉了 NK 的**输出**额度」——
说成「用掉了额度」用户会以为是自己问得太多。

## 七、写操作护栏(核心安全设计,不许绕过)

两阶段 + 物理保险丝:
1. AI 只能先调 `propose_status_change` / `propose_create_campaign` /
   `propose_publish_landing_pages` 把动作放进「保险箱」
   (PENDING_ACTIONS),然后向用户复述 + **报出待办编号**请求确认;
2. 用户在**下一条消息**明确同意后,AI 才能调 `confirm_action` 执行;
3. 保险丝:`_REQUEST_SEQ` 每条用户消息+1,登记与执行同序号=同一条消息 → 代码层直接拒绝,AI 无法自问自答;
4. `cancel_action` 供用户反悔;`list_pending_actions` 供 AI 查编号。
   **`confirm_action` 和 `cancel_action` 都要查归属**(待办只能由登记它的账号执行/取消)——
   保险箱是全进程共享的一份,少查一个入口,别人就能删掉你的待办。

**防死循环三件套**(血泪教训:曾同一单登记 11 次、执行 0 次,把 Gemini 日额度烧光):
- `_find_duplicate`:内容相同的待办直接返回原编号,不重复登记;
- `_system_prompt_now()` 每轮把**保险箱现状(含编号)**注入 system prompt——AI 跨轮必忘编号,直接喂给它;
- 保险丝拦截话术里点名"待办已存在、不要重新登记、直接 confirm_action(编号)"。

## 八、踩过的坑(改代码前必读)

| 坑 | 结论 |
|----|------|
| Gemini 型号 | `gemini-2.5-flash` 对新用户 404;**用 `gemini-flash-latest`** 这类 -latest 别名 |
| Gemini vs OpenAI 工具调用 | Gemini 是自动挡(函数直接递给 SDK);OpenAI 是手动挡(JSON schema + 自跑循环) |
| AI 不知道今天日期 | 必须在 system prompt 里注入(见 `_system_prompt_now()`),否则日期范围瞎猜 |
| .env 热加载 | 曾用 `setdefault` 导致空值占坑、新钥匙视而不见;现在非空才覆盖写入 |
| 中文输入法回车误发送 | keydown 里判 `e.isComposing || keyCode===229` 放行 |
| 聊天气泡被压扁遮挡 | flex 容器里的消息必须 `flex-shrink: 0` |
| Markdown 表格显示为竖线乱码 | 前端要用 marked.js 渲染(本地文件 static/marked.min.js,勿依赖 CDN) |
| **迁移数据要先存后删** | 把老的 `adbot-chat-history` 迁成多会话时,原先"先删旧键、等下次发消息才落盘"→ 用户迁移后不发消息就刷新会**永久丢历史**。改成:先 `saveConversations()`,确认 `CONVS_KEY` 写成了才删旧键。前端流程测试抓到的 |
| **循环变量遮蔽全局函数** | `dashboard.html` 里 `for (var t = 0; ...)` 和全局翻译函数 `t()` 撞名 → **var 提升到整个函数开头**,该函数里所有 `t()` 调用都变成"调用一个数字",报 `t is not a function`。表现:KPI 显示了但趋势图/柱状图/明细表全空(因为 drawTrend 抛错后,后面的 drawBars/drawTable 没机会跑)。`node --check` 抓不到(语法合法),**必须真实执行**:已加 `dashboard_test.js`(最小 DOM 模拟跑三个绘图函数,9 项)|
| **点按钮没反应 = 初始化中途抛错** | 新写的模块放在文件末尾(`const` 声明),但早期就被 `applyLang()` 调用 → **函数声明会提升、`const` 变量不会** → 抛 `Cannot access 'X' before initialization` → 后面的 `addEventListener` 全没挂上 → 表现为"点按钮没反应"。**`node --check` 抓不到这类问题**,必须跑 `frontend_test.js` 真实执行一遍。解法:用 `var` 声明一个 `acctReady` 就绪标志(var 会提升且初始化为 undefined,早期读取安全),模块初始化完成后置 true |
| **改目录名会废掉 venv** | `venv/bin/` 里的脚本第一行写死了 python 的**绝对路径**,目录一改名就报 `bad interpreter: No such file`。`venv` 是纯依赖包,`rm -rf venv` 重建即可(代码和 `.env` 都不在里面)。同理 git 也会因为属主对不上报 `dubious ownership`,要重新加 `safe.directory` |
| **VS Code 的 git 凭据会突然失效** | 报 `ECONNREFUSED /tmp/vscode-git-*.sock` + `No anonymous write access` = VS Code 重启后凭据 socket 失效,`GIT_ASKPASS` 指向死进程,git 又因为有 askpass 就不肯退回让你手动输,于是当匿名推。**解法:`Cmd+Shift+P → Reload Window`**,别去折腾 token |
| **`.gitignore` 写 `.env` 不等于挡住了 `.env` 的备份** | 手工敲命令时留下的 `.env.bak.<时间戳>`(一份**完整副本**,全套真钥匙都在里面)被 `git add -A` 收进了版本库,一躺 25 个提交,直到 `git push` 被 **GitHub 推送保护**挡下才发现(所幸从未推上去)。**别只写 `.env`,要写 `.env.*` 把所有变体一起挡掉。** 清理办法:`git rm --cached` + 改 `.gitignore` 提交,再 `git filter-branch --index-filter` 把它从历史里抹掉,然后**删掉 `refs/original` 备份 ref 并 `git gc --prune=now`** —— 不删的话带钥匙的旧对象还躺在对象库里。**千万别点 GitHub 给的「allow the secret」链接**,那等于把钥匙公开。事后钥匙一律换一遍 |
| **`.gitignore` 不支持行尾注释** | 写成 `scheduled_tasks.json   # 说明文字` → `#` 只有在**行首**才算注释,这行整体被当成一个字面 pattern,匹配不到任何文件 → 下次 `git add -A` 又把它加回版本库了。注释必须**单独占一行**。改完用 `git check-ignore -v <文件>` 验一下真的生效 |
| **SSE 要加 `X-Accel-Buffering: no`** | 不加的话 nginx 会缓冲整条流,用户看到的还是"转圈很久然后一次蹦出来",流式白做。加在响应头里比改 nginx 配置省事(而且换机器不会忘) |
| **Gemini 工具参数不能标裸 `list`/`dict`** | Gemini 从函数签名自动生成 schema,数组必须带 `items`。写成 `extra_targets: list \| None` 生成不出来 → **每次请求都 400,整条 Gemini 路径全废**。改成 `list[dict] \| None`。冒烟测试里加了一条纯查签名的检查守着 |
| **400 和 401/403 不能混报** | 原来把 `400/401/403` 一起翻译成"钥匙无效",结果上面那个 schema bug 报出来是「Gemini 钥匙无效」,人会跑去反复检查钥匙,而真 bug 在代码里。**401/403 才是钥匙问题;400 INVALID_ARGUMENT 是我们自己发的请求不合法**,要照实说并附上平台原话 |
| **Gemini 手动挡要原样带回 part** | 自己跑工具循环时,把函数调用贴回对话**不能自己重新造 `Part`** —— 原始 part 里带着 `thought_signature`,Gemini 要拿它校验,少了直接报 400「Function call is missing a thought_signature」。解法:收下模型吐的 part 对象**原样存着**,别只取 `function_call` 再重建 |
| **Gemini 流式 + 自动工具调用 = 不工作** | `generate_content_stream` 配 `tools=` 时实测只回一个 `text=''` 的空块就 `finish_reason=STOP`,工具循环没跑。表现是用户收到一句"(Gemini 没有返回文字)"。**解法:关掉自动工具调用改手动挡**(已实施),顺带还能播报每一步在查什么 |
| **测"一闪而过"的东西别按时序抓** | 进度文字这类中途状态,用 `await tick()` 轮询去抓极不稳定(事件全在微任务里瞬间跑完,而且 helper 把 `setTimeout` 桩成了空函数)。改成**记账**:helper 记录每一次 `textContent` 写入,测试查记录 |
| **contextvars 要设在 call_next 之前** | 在 `BaseHTTPMiddleware` 里设的上下文变量,只有设在 `await call_next(request)` **之前**才会传到下游;设在之后不生效。同步接口被丢进线程池也没问题——`run_in_threadpool` 会复制当前上下文 |
| **"没绑就回落公用 token"= 没隔离** | 按人隔离时,`_token()` 必须区分「不在用户上下文」(None,回落 .env)和「在用户上下文但没绑」(空 dict,直接报错)。少了这个区分,B 登录后会直接用上 .env 里的公用 token,隔离形同虚设。冒烟测试里有一条专门守这个 |
| **cookie 的 secure 不能写死 True** | 写死了本地 `http://localhost` 开发时浏览器**根本不存这个 cookie**,直接登不进去。要按 `request.url.scheme` 判断。放在 nginx 后面时这个 scheme 来自 `X-Forwarded-Proto` 请求头,所以反代配置里那行不能少;缺了只是退回不加 secure,不会把人挡在门外 |
| **原生 `<input type="date">` 的日历换不了语言** | 页面已经把 `documentElement.lang` 设成 `en` 了,Chrome 的日期弹层照样是中文(`2026年07月` / `日一二三四五六` / `清除` / `今天`)—— **那个弹层只认浏览器自身的语言,不认页面的 `lang` 属性**,从页面这边改不了。要让它跟着切,只能自己画日历(顺带样式也能和页面统一,原生那个看着像另一个软件)。输入框改成 `type="text" readonly`,`.value` 语义不变、也不会再弹出原生日历。大屏测试里搜 `type="date"` 守着不许回退(**记得连 CSS 注释和 JS 行注释一起剥**,否则注释里写的说明会被当成命中)|
| **有页面从来没被任何测试执行过** | 四个页面里,`platforms.html`(登录后第一眼)和 `login.html`(没登录只能看到它)**从没被跑过** —— 而这两页恰恰最经不起「函数名写错」「`const` 提升」这类问题:一旦初始化中途抛错,表现就是整页空白或点了没反应,`node --check` 查不出来。helper 加了 `opts.page` 参数就能跑别的页面。**新加页面时记得问一句:它有人跑过吗?** |
| **测试的 DOM 模拟要够真** | helper 里 `remove()` 曾是空函数、`getElementById` 找不到动态创建的元素 → 测「提示有没有被撤掉」永远是假通过。已修:`appendChild` 记父节点、`remove()` 真摘、`id` setter 自动登记。另外 fetch 桩失败时要返回**失败的 Promise**而不是同步抛,否则测不出页面的 `.catch` 分支 |
| **探活别 curl `/`** | 加了登录门之后,未登录访问 `/` 返回 **302**(跳 `/login`),这是**正常**的。老口诀「不是 200 就重启」会把好端端的服务白重启一遍。改用 `curl .../login` 看 200,或接受 200/302 都算活 |
| **CSS 变量名写错不报错,只是静默失效** | 日期弹层写了 `background: var(--surface-0)`,而这个变量**根本不存在**(真名是 `--surface-1`)。CSS 取不到值时**不报错**,那条声明直接作废 → 弹层没有底色,浮在图表上是「透视」的,后面的 KPI 数字全透出来。**浏览器不吭声、`node --check` 也管不着**,只能扫:冒烟测试里比对四个页面 `var(--x)` 用到的和 `:root` 里定义过的,差集不为空就报错(顺带扫出 `--text` 也是编的,真名 `--text-primary`)。和「函数名要按实际的来」是同一类错:**别凭印象写名字,先 grep 一下真名** |
| **改文件前先看真实写法** | 这次批量改 index.html:锚点写成 `var I18N = {`,实际是 **`const I18N = {`** → 整块平台变量声明**静默没插进去**(replace 没匹配就是什么也不做,不报错),留下 `PLATFORM_NAME is not defined`。**批量替换后必须 grep 验证改动真的落地了**,别看脚本 print 的「✅」——那是无条件打的 |
| **函数名要按实际的来** | 新代码里写 `esc(...)`,而 index.html 里那个函数叫 **`escapeHtml`** → `esc is not defined`,只在「没绑定平台」这条分支才走到,`node --check` 和其他测试全都照过。是 `platform_test.js` 真执行才抓出来的 |
| 前端也能测 | 三套 node 测试都用极简 DOM 模拟**真实执行**页面脚本,不用开浏览器:`frontend_test.js`(多会话,16)、`dashboard_test.js`(大屏,9)、`platform_test.js`(多平台/未绑定引导,16)。helper 里已给 `location` 和按 URL 分发的 `fetch` 打桩 |
| **白屏转圈的元凶** | marked.js 曾是**阻塞式** `<script src>`:它一卡(常见于端口转发的桥半死),后面的内联脚本永不执行 → 整页白屏,比报错更难查。已改 `async` + `onMarkedReady` 补排版:排版库晚到/失败也只是表格丑点,页面照常可用。**教训:前端任何阻塞式外部资源都是白屏隐患** |
| OpenAI `insufficient_quota` | key 有效但账户没余额;OpenAI 是充值制,Gemini 才有免费日额度 |
| **模型偶尔一个字都不吐,不能就这么甩给用户** | 线上出现过一句 `(ChatGPT 没有返回文字)` 就没了下文 —— 那是死胡同:用户不知道是自己的问题还是系统坏了,也不知道下一步该干嘛;**日志里同样查不到线索**,因为 `_openai_once` 压根没读 `finish_reason` —— 上游唯一能说明「为什么空」的字段(截断?被安全策略拦了?)。实测同样的问题连发三次都正常,说明是偶发。解法三件套:①`finish_reason` 带回来并 print 进日志;②**原地重试一次**(只一次 —— 真坏了反复重试只是白烧额度),重试前 emit `reset` 把吐了一半的字作废;③还空就用 `_empty_reply_msg()` 说清原因 + 给下一步,中英双语。**两条大脑路径(Gemini / ofox)都要做**,只修一边的话换条路又是死胡同 |
| **接力只认 `APIError` 更不够** | 给 Gemini 设了超时之后,**超时抛的是 `httpx.ReadTimeout`,不是 genai 的 `APIError`** —— 原来的 `except genai_errors.APIError` 捕不到,于是明明配了 ofox 也不切,直接把错甩给用户。而没设超时更糟:SDK 默认不限时,对方不回音就干等到 TCP 自己放弃(实测 `check_brain.py` 跑了 **12 分 51 秒**)。现在:`_gemini_client()` 统一带 30s 超时,`_should_fallback()` 把网络异常也算进接力条件 |
| **熔断要等备用通道真的顶上了才记** | 「刚才 Gemini 干等了 30s,接下来 5 分钟直接走备用通道」这个冷却,**如果在调备用通道之前就记下,备用通道也坏时就一个能用的都不剩了**。实测正好撞上:Gemini 504 过载 + ofox 余额为负全 402。所以 `_mark_gemini_down()` 必须放在 `ask_openai()` **返回之后**(冒烟测试查这个顺序)。另外冷却只对「干等型」失败生效(网络超时 / 503 / 504),429 是秒回的,压五分钟没道理 |
| **接力只认 429 是不够的** | 原来只有 429(额度)才切下一级,结果 Gemini 报 **503(服务繁忙)** 时整条链直接断掉,明明有 ofox 兜底也不用,用户只看到"稍后再试"。现已定义 `_RETRYABLE_CODES = (429, 500, 502, 503, 504)`:额度类+上游临时故障都自动接力;**401/403 这类钥匙问题绝不接力**(要让用户看到真实原因,不能靠切换掩盖配置错误)|
| **ofox 聚合中转(现主力)** | `api: openai-completions` = 说 OpenAI 方言,但转卖各家模型,故模型名带厂牌前缀。配置只需三样:`OPENAI_BASE_URL=https://api.ofox.ai/v1`、`OPENAI_MODEL=google/gemini-3.1-pro-preview`、`OPENAI_API_KEY=<ofox token>`;同事模板里的 models[] 那堆 name/cost/contextWindow 是给别的工具界面用的,我们不需要。走"手动挡"工具调用路径,已实测对话+工具调用均正常 |
| **pkill 会杀掉自己** | `pkill -f "uvicorn agent_server"` 的**命令行本身就含这个字符串**,`-f` 匹配整条 cmdline → 把执行它的 shell 一起杀掉,表现为命令莫名退出(exit 144)、服务也起不来。解法:用方括号技巧 `pkill -f "[u]vicorn agent_server"`(正则里 `[u]` 匹配 u,但命令行文本里是 `[u]`,不自匹配)|
| Claude 这边起不了长驻服务 | 环境会杀掉 detached 的长驻进程(日志一行都来不及写)。**服务请 Cole 自己在终端跑 `./start.sh`**;Claude 侧要验证"启动时才发生的事"(如后台线程)用 `TestClient(app)` 上下文触发 startup 事件 |
| 后台服务被会话重启带走 | 服务要用 `setsid nohup` 脱离会话,或让 Cole 自己跑 `./start.sh` |
| 账户列表是套娃结构(**已修**) | getGroupsByOrgIds 返回"组织分组",真账户 id 在每组的 `adAccounts[].id` 里,**外层 id 是分组且常与账户同名,AI 分不出来**。现在 `nb.list_ad_accounts()` 统一**拍平**只吐真账户(带 `group_id` 仅供参考),别再自己拆套娃 |
| 多账户会静默出错(**已修**) | `_default_ad_account_id()` 以前默默挑第一个账户(可能把素材传错账户)。现在多于一个就**明确报错并列出账户**让用户选——宁可吵,不要错 |
| 上传素材 409 Conflict | 平台按**文件内容**查重("same content already exists");解法:捕获后降级 `saveToMediaLibrary=false` 重传,照样拿 assetUrl 建广告 |
| 报错要说人话 | 所有平台报错经 `_parse_or_raise` 提取 errMsg 给用户看,别甩 HTTP 状态码 |
| **AI 幻觉执行(最危险)** | AI 可能没调工具却声称"已创建成功"并编造连号 id。防线:`_finalize` 给回复盖钢印——真执行(`_EXECUTED_THIS_REQUEST`)附🔒系统核验;谎报(有待办未执行+回复含"已创建"类词)附⚠️拆穿提示;建完还会回查平台验证;所有写操作 print 进 server.log 可查 |
| **DATE 维度另有 31 天上限** | 报表按天取数(`dimensions=["DATE"]`)跨度**约 31 天**就报 400,和 CAMPAIGN 那层的 180 天是两条不同的限制。大屏选"近90/180天"时趋势图自动缩到 31 天并在页面上说明,KPI 和明细仍是全周期 |
| 报表跨度上限 | 平台硬限制 **180 天**,超了报英文错;已在 get_report 里提前拦截并说人话 |
| 报表字段其实很全 | 平台**不管 metrics 申请几个都返回全部 32 字段**(含 name/roas/conversionValue),别只取三个就以为其他没有 |
| **提示词管不住文案照抄** | 归纳竞品创意时,提示词里明写了「不许照抄」,实测产出的三版方案里**两版主标题是竞品原句照搬**。品牌名照抄一眼看得出,文案照抄看不出来,用户很可能直接拿去投。**必须代码层查重**(`_flag_copied`,实词重合 ≥70% 判定),和写操作护栏一个道理:别指望提示词能管住模型 |
| **多模态喂图前必须缩** | 竞品/图库原图能到 6000×4000(10MB),直接发给视觉模型又慢又贵;缩到长边 1024 后只有 139KB,而拆广告结构完全够用。另外**视觉模型同样要接力** —— 实测第一次调用就撞上 flash 的 503 高负载 |
| **花钱的步骤要排在最后** | 生图 $0.20/张。踩了两次:一次卡在给文件起名(`_re` 没导入)、一次卡在上传缺 `mediaName` —— **两次都是图已生成、钱已付才失败**,那张就白花了。**所有免费又可能失败的准备工作(起名、校验、查余额)必须排在付费调用之前**,付过钱的产物还要先落盘再做后续 |
| **按公开单价算的成本会差很远** | 按 ofox 报的 `output_image` 单价 × 官方 token 数算出一张 $0.05,**实测 $0.203,差 4 倍**。报低了用户以为很便宜,批量生成才发现烧了不少。**成本一律拿账单实测校正**(记余额→生成→再记余额),别信算出来的 |
| **测试用 index() 找源码会命中注释** | 守"文件名要排在 `cr.render()` 之前"那条测试,`src.index("cr.render(")` 先命中了**注释里**写的 `cr.render()`,于是误报。和之前 `minDaysRunning` 在 docstring 里被搜到是同一类。**读源码做判断前先把注释/docstring 剥掉** |
| **同一个判断散在三处,加一种就漏一处** | 「这个工作室能看见/能确认哪些待办」原来在三处各写死一个字符串 `publish_landing_pages`:待办清单、提示词注入、`_tool_call` 的模式闸门。加了第二种待办后**三处全漏**,而且三处的表现完全不同 —— 登记成功却在清单里看不见、AI 跨轮忘了编号没处查、确认时被「不能确认其它工作区的待办」拒掉。更阴的是**直接调 `confirm_action()` 测不出来**:那道闸门在 `_tool_call` 里,测试必须走真正的工具分发层才碰得到(和「锁了输入框不等于锁住了发送」同一类)。**这类清单要收成一份常量**,加新类型只改那一行 |
| **测试写死一份清单,加一项合法的就误红** | 「落地页工作室能确认哪些待办」那条测试写的是 `for want in LANDING_ACTION_TYPES: if want not in ("publish_landing_pages","swap_campaign_landers")` —— 加了一种**完全合法**的新待办类型(`make_landing_images`)就当场变红。它想守的其实是**安全性质**:落地页和素材两边不许重叠、都不许碰投放助手那几种花钱的写操作(开关广告/建广告)。改成直接断言那两条之后,既不会因为合法新增而误红,退回验证(把 `create_campaign` 塞进落地页清单)照样立刻变红。**断言"清单等于这几项"是在锁实现,断言"这两个集合不许相交"才是在守规矩**。
**同一个病第二次犯**:「图库连不上要说清原因」那条测试断言的是 `"PEXELS_API_KEY" in head or "钥匙" in head` ——
把**当时那个补救办法**写死进了测试。Cole 决定不配钥匙、出路改成「用 AI 把图补上」之后,
一条**行为完全正确**的代码把它打红了。它真正想守的是两条性质:①说清「换关键词没用」;
②**给出一条当下真走得通的出路**。补救办法会换,性质不会 |
| **后端替他兜住了,不等于界面上说得通** | 大屏自定义时间范围,后端 `_resolve_range()` 一直会把填反的起止日期调过来(「反了就替他调过来,不用报错烦他」)—— **可前端那两个输入框还留着反的那一对**。线上实测的样子是:标题写着「2026-02-02 ~ 2026-03-17」、框里写着「开始 2026-03-17 / 结束 2026-02-02」,**同一个屏幕上两组数字自相矛盾**,用户第一反应是「这筛选器坏了」。**「替他定了必须告诉他」不只对默认值成立,对「替他纠正」同样成立** —— 而且要在他**还看得见的时候**改(点日历那一刻),不是等弹层关了才在别处生效。顺带:这类提示别标红,事情已经办好了,红色是给「你得动手」用的 |
| **「一次调用小于前端上限」不等于「一个循环也小于」** | 前端的判据是「**多久没收到东西**就放弃」(`IDLE_TIMEOUT_MS` = 5 分钟),而生一张图最长 240 秒(`creative_render.TIMEOUT`)—— 单看一次调用是过的,**老的超时顺序测试也一直是绿的**。可生图是个**循环**,两张之间一个字都不吐 → 最长 **8 分钟静默** → 前端先放弃,而后端还在画:**钱照花($0.40)、图照传进素材库,用户那头永远等不到结果**(2026-09-09 线上实测,用户原话「钱已经花了,图片给不出来」)。**正解不是把超时调小**(那会把正常的长活掐掉),是**在每次长调用之前播报一次** —— 前端收到任何字节都会重新计时,顺带还让用户知道在画第几张(干等 4 分钟没动静,谁都以为卡死了)。**凡是「一个循环里做 N 次慢操作」的地方,要按累计静默算,不是按单次算**;测试也要跟着改成断言「每次长调用之前都吐过东西」 |
| **摘不干净的外链,只在「线上」出事** | 落地页里模型硬写的外部图片地址由代码摘掉 —— 而那条正则写的是 `<img …src="https…">`:**引号、标签名、属性名三样全假设死了**。实测漏掉两种模型真会写的:`<img src=https://…>`(不带引号,合法 HTML)和 `<picture><source srcset="https://…">`。漏掉的后果**本地一点都看不出来** —— 预览有 CSP(`img-src 'self' data:`)挡着,而**发布出去的页面我们没设过任何响应头**,那张第三方图会真的去加载:哪天失效就是裂图,还把访客的浏览行为报给了别人。**凡是「本地有一道防线、线上没有」的检查,测试必须按线上那套来验**,别被本地的假象骗过去 |
| **模型写出来的 HTML,别假设它长什么样** | 配图那个 `<img>` 标签是**模型**生成的,而 `fill_image_slot` 和 `swap_image` 各写了一个**只认双引号 src** 的正则。后果两条都静默:①换图碰上 `src='…'` 的图或**隐藏占位**(压根没有 src)时**一个字都没改**,却照样返回「换好了」——用户刷新预览看不出变化,只会以为是浏览器缓存;②AI 重做时旧 src 剥不掉,标签上留下**两个 src**,而浏览器用第一个,于是钱花了、画面还是旧图。**凡是要改写模型产出的标记,引号风格、属性顺序、自闭合写法都不能假设**,而且这种改写要收成**一个**函数——两处各写一遍就是两处各有一个洞 |
| **成功判据要看「目标达成没有」,不是「有没有变化」** | 给换图补「改不动就别说换好了」时,第一版判据写的是 `if out == html`。而配图文件名是**内容的 sha256** —— 换回一张**内容一模一样**的图时文件名也一样,页面本来就不该变,于是合法操作被误报成「代码改不动这个标签」。改成查「这个标签最后是不是真挂着那张图、且不再 hidden」两头都对;真遇到同一张图要**如实告诉用户画面不会变**,否则他以为功能坏了。**「没发生变化」既可能是失败,也可能是已经就位** |
| **拆穿章误报的代价比漏报更隐蔽** | 「回复里的待办编号拿保险箱回查」第一版的判据是「**现在**还在不在箱子里」。复查当天就抓到两处误报:用户**成功取消**一个待办后,模型正确地说「待办 xxx 已取消」,反被盖章说「多半是助手编的」;隔一轮复述上一次执行过的编号同样中招。**合法的编号本来就会离开保险箱**(执行掉/取消掉/只是复述历史),所以判据要改成「**有没有真的登记过**」(另存一份 `_KNOWN_ACTION_IDS`)。**喊错一次狼,这道章以后就没人信了** —— 用户不会来报这种 bug,他只会开始忽略所有 ⚠️。**加一道拆穿章之后,必须把所有合法路径都走一遍确认不被误伤** |
| **退回验证的分隔符别和锚点里的字符撞** | 用 shell 写「`old|new`」形式的退回用例,而锚点本身就是 `set(A) | B` —— `${rev%%|*}` 截在了锚点内部那个 `|` 上,替换出来的是 `set(A)| B`(**语法照样合法**),等于什么都没退回,于是"仍然全绿"看着像测试不管用。**退回没生效和修复没被守住,表现一模一样** —— 所以退回验证要么用 Python 写、要么退回后 `grep` 确认真的变了。
**还有第二种「退不干净」:后面的检查替你兜住了。** 给「花钱前先查页面还在不在」做退回时,我只关掉了第一道 `if not here`,而紧跟的「哪几个位置没了」照样拦下(页面没了 = 所有位置都没了)→ 显示「仍然全绿,没有测试守着」。**退回要把那条性质整个拿掉,不是关掉它的第一个分支** |
| **禁词表是打地鼠:模型能编的理由有无限多种措辞** | `confirm_action` 找不到编号时,上一版在报错里禁掉了「超时/重置/刷新」。线上第二天它换了说法:「由于**临时归档**原因没对上」「由于**文件路径索引**的小意外」——照样编,照样重新登记一遍。**列禁词永远追不上**。两条改法:①话术从「不许说 X」改成**正面规定唯一允许的说法**(「唯一诚实的说法是『刚才那个编号是我弄错的』」),并说明「代码里没有任何机制会让编号消失」;②真正管用的是**代码回查**:`_finalize` 把回复里报出来的每个待办编号拿保险箱核一遍,编的当场盖 ⚠️ 章并写明「别按它说的回复确认」。和预览链接那道是同一个思路 —— **凡是给出去的标识符,代码都要能回查** |
| **模型拿到了正确的出路,照样会编一个假流程** | 素材工作室里模型调 `propose_create_campaign` 被闸门拦下,**返回的移交单完全正确**(实测过:去哪、为什么、到那边第一句说什么都有)。它没照做,而是**凭空编了一个待办编号**让用户「回复确认」,还编了整张配置表。所以「拒绝必须带出路」是必要条件、**不是充分条件** —— 出路给了它也可能不走。凡是「两阶段确认」这类由模型转述的流程,**编号必须能被代码回查**,否则用户完全看不出这一轮到底登记没登记 |
| **已经执行成功的事,模型可能再编一遍「要重新发起」** | 发布待办已经执行成功(🔒 钢印就在同一条回复里),模型紧接着说「由于文件路径索引的小意外,我已经为你重新对齐了文件,现在重新发起发布登记」并给了个编的编号 —— **用户以为还得再发一次**,而真发两次就是整站覆盖。所以回查编号时,**本轮真执行掉的编号也要算数**(它已经不在保险箱里,但提它是诚实的),否则拆穿章会把正确的回复也误伤 |
| **花费闸门算输入 = 一个「对话长度闸门」** | `TURN_TOKEN_BUDGET` 原来算的是 输入+输出。线上实测:一段长对话里用户说「上广告」,**7 轮就被停掉**,报的是 `输入 111K + 输出/思考 0K` —— 模型根本没打转,是**整段历史每轮重发**把输入顶上去的。两个错叠在一起:①输入随对话长度涨、且只占成本约 11%,拿它当花费闸门是错的;②Gemini 那条路的用量按「覆盖」记,**最后一个 chunk 只带 prompt 不带 candidates 时,前面记到的输出被抹成 0**(累计值要用 `max` 取,不能盲目覆盖)。于是闸门彻底变成按对话长度杀人。**花费闸门只能算输出**:它占成本 89%,而且只随「真的又想了一轮」而涨 |
| **「不许引用外部图片」被模型理解成「画个占位框」** | 落地页生成提示词里写着「不要引用外部 JS/CSS/图片」,本意是别依赖外链;模型的执行结果是给你一个灰色方块写着 `[ Image: ... ]`。**它不是偷懒,是手上真的没有图** —— 工具连一个图片参数都没有。凡是「禁止某种做法」的规则,都要问一句:**那我给它的替代路径是什么?** 只禁不给,它只能交一个空壳。解法和 CTA 追踪链接一样:让它写占位符 + 说清要什么,**真实的东西由代码填进去** |
| **相对路径能让「本地预览」和「线上发布」共用一套写法** | 配图如果写成绝对地址,预览一套、发布一套,要在两个地方各拼一次,而且拼错了只有发布之后才看得出来。写成 `img/xxx.jpg` 之后,预览(`/landing-pages/xxx.html`)和线上(`/<slug>/a/index.html`)**各自按同一条相对规则解析**,代价只是发布时 A/B 目录下各放一份图。**凡是同一份产物要在两个地方被访问的,先看能不能用相对路径把差异消掉** |
| **图片一定要按内容命名,不能按序号** | 配图文件名用的是内容的 sha256 前 16 位。按 `image_1.jpg` 这种序号命名的话,两个实验、两版页面之间会互相覆盖,而覆盖之后**页面还是好的、只是图变成了别人那张** —— 从外面完全看不出来。按内容命名顺带还去了重:同一张图被两版引用只存一份 |
| **「连不上」和「对方很慢」是两回事** | 图库搜不出东西时我先下结论说"这台机器连不上 Openverse"——**查实是错的**:DNS 18ms、TCP 20ms、TLS 123ms 全正常,**是它自己的服务器在磨**(同一分钟内 0.5~32 秒乱跳),而我们的超时正好卡在 20 秒,快的过慢的挂,看着就像"时好时坏的网络问题"。`curl -w` 能把 `time_namelookup` / `time_connect` / `time_appconnect` / `time_starttransfer` 分开打出来,**一眼就能分清是连不上还是对方慢** —— 别凭一句"超时了"就往网络上推。顺带:放宽超时也救不了这个功能,因为第二个毛病是硬的(roof / gutter 这些词本来就没有 CC0 图),**先分清是"路不通"还是"那儿本来就没货",再决定改超时还是换来源** |
| **「没找到」和「连不上」是两回事,混着报会让人白折腾** | 图库一张图都没返回时,报「没找到合适的图」会让用户去换关键词 —— 而真实原因是**没配钥匙 / 图库连不上**,换多少个词都一样。而且确认连不上之后还一张一张去等,一页 4 张 × 20 秒读超时 = **80 秒**,用户看着像卡死。两条都要做:①措辞按真实原因分开;②这一轮里已经确认挂掉的来源直接跳过(实测 80 秒 → 26 秒)|
| **只堵「写」不堵「读」= 没隔离,还多造一个死胡同** | 上一轮给待办补了 `user_id`、confirm/cancel 查了归属,读这一头**五处**一个字没改:待办清单、提示词注入、拆穿章、定时任务执行结果、落地页预览目录。后果不只是泄露 —— 提示词里躺着别人的编号还催着「直接调 confirm_action」,用户照做撞上「属于另一个账号」,**这是代码亲手给他造的死胡同**(正是第六之二十「拒绝必须带出路」反过来的样子)。**同一份共享状态,读和写必须用同一把尺子**,而且要抽成一个判据函数给所有读取点共用 —— 写入侧已经收成 `_put_action()` 一个入口了,读取侧也该这么收 |
| **靠 contextvars 做隔离,就必须有测试真的跨一次线程** | 网页默认走流式,大脑丢进线程跑,靠 `ctx.run(work)` 把上下文带过去。审查实测:**把它改成 `target=work`,12 条按人隔离的测试全部照样绿** —— 它们都是直接调函数,一次都没走过那条线程。而线程里 `CURRENT_USER_ID.get()` 是空串的话,整轮隔离在生产主路径上完全失效(登记不记归属、查重归并两个人、素材和预览同桶、定时任务谁都看得见),**测试毫无察觉**。补的测试要**先断言确实换了线程**,否则哪天实现改成同步跑,它就变成假绿了 |
| **各类上限要按人算,不然 A 能把 B 的东西挤没** | `_SEARCHED_ASSETS` 的 200 条、`recent_runs` 的 5 条、`KEEP_GENERATED` 的 80 份,原来都是**所有人共用**一个额度。A 多用几轮就把 B 的挤掉,而 B 那边的表现是「我刚看到的东西不见了」——落地页那条更糟:B 的发布待办还指着被删掉的文件,他确认时才拿到「文件找不到」。**凡是带上限的登记表/缓存/目录,先问一句:这个额度是每人一份还是大家共用?** |
| **审查报告也要自己复核,别照单全收** | 这轮审查(38 个 agent)挖出的洞都是真的,但报告里也有夸大的:比如说「B 拿到 A 的 campaign id 就能去 propose_status_change 提权」——待办层面确实拦不住,**但真执行时走的是 B 自己的 token**(凭据按人存),他能不能开关那条 campaign 取决于他自己绑的账户,而那些 id 他本来就 `list_campaigns` 枚举得到。真正的问题是**信息泄露 + 死胡同文案**,不是提权。复核者自己把严重度从「高」调成了「中」并说明了理由 —— 这才是对的做法 |
| **「一次只建一组一条」这个前提,在「复用已有计划」时就不成立了** | `_child_names()` 的注释写着「我们一次只建一组一条,所以序号固定 001 —— 计划是全新的,底下不可能已有别的」。前提没错,**但执行时 campaign 那一层是幂等的(同名就复用)**,一旦复用,「计划是全新的」当场失效,而 ad set / ad 仍然无条件新建 → 同一条计划底下**两个同名广告组各带一份日预算**($20 变 $40),报表里两行同名分不出来。用户明说了要叫 `-002`,而函数连这个参数都没有,**没有任何地方能接住他的话**。**凡是写死某个常量的地方,注释里那句「因为……所以固定」要拿去和所有调用路径核一遍** —— 幂等分支最容易让前提失效。见第六之二十一节 |
| **模型会编一个待办编号,再编一个理由解释它为什么不存在** | 实测:模型报了编号 `0d51be21`(日志里从来没有),用户回「确认」,代码答「找不到待办 xxx(**可能已执行/已取消,或 id 有误**)」—— 它没承认编错,而是从括号里三个「可能」里挑了最不用担责的,对用户说「**系统的待办编号由于超时或刷新重置了**」,重新登记了一条。**含糊的措辞本身就是编造的素材。** 解法两层:①明说系统没有「重置编号」这回事,把退路堵死;②**把真实存在的编号列出来**(只列他自己的)—— 正确答案摆着就不用编了。三个入口(confirm / cancel / `_tool_call`)要用**同一份**措辞,话不一样模型会引用最软的那句。**光在 note 里补一句「不会重置」不够** —— 两句话打架时它引用软的那句,所以测试要直接禁掉含糊字样 |
| **模型会把「我们没做」说成「平台不支持」** | 加不进第二条广告之后,助手说「NewsBreak 平台的 API(和绝大多数主流广告平台一样)…修改广告名称的功能暂未开发实现」。**我们项目只封装了 `updateStatus`,连参考项目 qx-ad-bot 里也只有这一个 —— 平台支不支持改名我们根本没试过。** 这和「我没有权限」「接口限制」是同一个病:**能力边界不清楚时,模型会往外推给平台**,因为那样听起来不是它的问题。凡是「做不到」的回答,都要问一句:这是**代码里确实没有**,还是**平台确实不支持**?没验证过就不许说成后者 |
| **改完文件要拿 git 复核,别信补丁脚本打印的「完成」** | 这一轮出现过两次静默丢失:一次是补丁脚本报了「修③ 完成」+ 语法 OK,而 `grep` 查下去**文件里一个字都没有**(重跑一次才写进去);一次是**已经提交过的**一行(`_tool_call` 的模式闸门)在工作区被退回成了老写法。原因没查出来(工作区有 VS Code 的 fileWatcher 在跑)。**结论不变:改完必须 `git diff HEAD` 逐条看被删掉的行,确认每一处删除都是自己有意为之**,而且提交前把五套测试再跑一遍 —— 和坑表「批量替换后必须 grep 验证改动真的落地了」是同一条,只是这次连「脚本自己 print 的成功」都不能信 |
| **退回验证要保证代码还能跑,否则「变红」什么都没证明** | 给「老待办回落」这条做退回验证时,我把 `if a.get("ads"):` 换成了 `return a["ads"]` + `if False:`,造成 **IndentationError** → 整个模块导不进来 → 所有测试都红。看着是「✅ 变红」,其实**根本没走到那条测试**,等于什么都没验。退回改动必须是**语义上的倒退,不是语法上的破坏**;而且验证脚本要能分辨「某条测试红了」和「整个进程崩了」(打印具体红了哪一条) |
| **子 agent 会写你的真实数据文件** | 让审查 agent 对着 diff 找漏洞时,其中一个没隔离好,**把 `scheduled_tasks.json` 从 3 条写成了 1 条**(文件在 `.gitignore` 里,没有 git 历史可回滚)。靠翻它自己的 transcript 才把原文捞回来。**凡是让别的 agent 在真实工作区里跑探针,先假定它会写文件**:要么提前备份 `data/` 和那两个运行时 json,要么在提示词里点名哪些文件绝对不许碰 |
| **`for _ in range(10)` 不是省钱的闸门** | 它**只数轮数,不看内容也不看花费**。模型用**一模一样的参数**查第三遍时它一声不吭,非要等满十轮才停 —— 而实测一轮 ≈ 6.8K 输入 + 7~9K 输出($0.24),十轮 $2.4 **还没有答案**;停下来时只甩一句「轮数过多,请换个问法」,用户既不知道发生了什么也不知道花了多少,多半原样再发一遍,于是再烧一遍。要按**内容**判(同工具同参数第 3 次 = 在打转)和按**花费**判(累计 token 到顶),而且**两条大脑路径都要装**。见第六之十九节 |
| **本地估不出「思考 token」,拿它当花费闸门等于没闸门** | 推理型模型一次回复输出 7~9K token,而屏幕上只有几百字 —— 差额全是看不见的思考,**占成本约 89%**。按字符数本地估会小一个数量级,闸门永远不触发,却给人「已经管住了」的错觉。所以花费闸门**只在真拿到上游用量时才生效**,拿不到就如实说「查不到」并让这条失效(轮数和重复调用两道照常管用)。流式要用量必须传 `stream_options={"include_usage": True}`,而且**用量在最后一个 chunk 上、那个 chunk 的 `choices` 是空的**,要抢在 `if not chunk.choices: continue` 之前读 |
| **超时判「一共等了多久」会把正常的长活判成卡死** | 前端写死「3 分钟没回来就 abort」,而实测 **gpt-5.5 出一整页落地页要 61 秒**,再加上前面几轮查资料的工具调用,轻松破 3 分钟 —— 用户看到「等太久了,这条没发出去」,以为坏了,其实后端一直在正常干活,他重发一遍还白烧一次额度。**判据要改成「多久没收到任何东西」**:只要还在吐 status/delta 就重新计时,真卡死了照样能掐。顺序不变量照旧:**后端最长的一次调用必须小于前端的空闲上限**,后端先失败才能把真实原因说出来 |
| **换主力模型之前,先按「一条消息」算一次账** | 只看每百万 token 的单价会严重低估。推理型模型一次回复输出 **7~9K token**,而屏幕上只有几百字 —— 差额全是**看不见的思考**,照价收费,实测**输出占成本 89%**;再加上工具循环让**一条用户消息触发 2~4 次调用**,gpt-5.5 一条消息就是 $0.5~1.0。算账的公式是「(输入+**输出**)× 单价 × 每条消息的调用次数」,不是「单价便宜多少」。实测同一批流量 gpt-5.5 → gemini-3.1-pro 省 60% |
| **看中转的请求日志会以为"每秒都在调用"** | ofox 日志里一条挨一条,其实是**一条用户消息的工具循环**:查一次→想→再查,时间戳能差 7 秒。判断"是不是在空转"不能数日志条数,要看**同一条消息里调了几轮、有没有在重复同样的查询**(现在有 LoopGuard 管着,见第六之十九节)|
| **改完代码没重启,会把老代码的行为当成新 bug 查** | 今天前半段 Cole 报的好几个"问题"都是这样:线上进程还是两天前起的(生产没有 `--reload`),我照着新代码去查,当然查不出所以然,来回耗了几轮。**收到 bug 反馈的第一件事是确认「跑着的到底是哪一版」**:`ps aux \| grep "[u]vicorn"` 看进程起始时间,和 `git log -1` 的时间比一比。本地有 `--reload` 所以容易忘了还有这回事 |
| **换个模型,时间预算整个变了** | 从 Gemini 换到 gpt-5.5 之后,原来够用的 `BRAIN_TIMEOUT_S=75` 就顶不住了 —— 推理型模型出长文明显更慢(实测一整页 61s,而 gpt-5.4-mini 因为思考 token 更多要 169s,**小模型反而慢得多**)。**换模型后要重新量一遍最慢的那条路**(这里是一次性生成整页 HTML),别沿用旧模型的超时。长文生成和聊天该用两个不同的超时 |
| **切了大脑,还有一条路在偷偷用旧模型** | 把 `BRAIN` 切成 openai 之后,聊天、大屏诊断都跟着走了,**只有看图那条路写死了 Gemini** —— 结果 Gemini 一 503,拆素材和拆落地页截图整个坏掉,而用户以为早就不用它了。**「换模型」这种开关要盘一遍所有调用方**(`grep -rn gemini --include=*.py`),别只改主路径。现在 `_ask_vision` 跟着同一个 `BRAIN` 走,而且 **openai 挂了也不偷偷回落 Gemini** —— 用户明说不用,就是不用 |
| **「要几个」写死在代码里 = 用户说了不算** | `save_pages` 写死 `pages[:2]`,而且工具连「要几版」这个参数都没有 —— 用户明说「只生成一个」,照样出两个。多的那版是白花的模型钱,还逼他在两个里挑,等于没听他说话。**凡是「出 N 份」的功能,N 必须能从用户那儿一路传到底**(工具参数 → 生成提示词 → 落盘上限),漏一层就失效 |
| **模型会举一个「例子地址」,而用户会当真** | 助手为了解释 CTA 是什么,顺手写了 `https://track.clickflare.com/click/1` 当例子 —— 那是编的,格式也不对(真的是 `/cf/click/`)。用户很可能直接拿去用。**凡是「绝不许编造」的东西,连举例都不许编**,提示词要明写这一条 |
| **模型会许诺代码根本不允许的事** | 落地页助手说「你没有追踪代码也没关系,我先不放追踪代码把页面发出去」—— 而缺 CTA 占位符的页面**代码层直接拒绝发布**,答应了也做不到;真发出去更糟:买来的流量一条都统计不到。**代码里拦住的事,提示词里要同样明确地禁止承诺** —— 否则用户按它说的走一圈,到最后才发现是空头支票 |
| **5xx 里可能写着确定性的原因,别一律说成「稍后再试」** | 竞品查询报 503,我们照着老规矩翻译成「暂时不可用,过几分钟再试」—— 而正文里明明写着 `That filter combination is too broad to count right now. Narrow it and try again.`,是 **`search=` + `status=active` 这个组合**被拒(单独用哪个都 200)。**等多久都没用**,该做的是换参数。5xx 也要先看正文有没有解释,有就原话交出来。和「400 和 401/403 不能混报」是同一条 |
| **平台参数不能用时改本地筛,但「字段缺失」不等于「False」** | `status=active` 不能发了,改成本地按 `isActive` 筛。第一版写的是 `not one.get("还在投")` —— 而 `_normalize()` 里是 `bool(item.get("isActive"))`,**平台没给这个字段时也是 False**,于是「不知道在不在投」被当成「已停投」悄悄丢掉了(一条老测试的假数据正好没这个字段,当场变红抓出来的)。判据要用**原始行的 `is False`**:只有平台明确说了停投才丢。和 geos 那条「没有 geos 的条目保留」是同一个规矩:**宁可多给一条待确认的,也不要因为平台没给字段就把它扔了** |
| **绑定自定义域名 ≠ 这个域名能访问** | `POST .../pages/projects/x/domains` 只是把名字**登记到项目上**,走 API 这条路**不会替你建 DNS 记录**(后台点的时候会,所以很容易以为它会)。实测:发布报了成功、结果里给出了 A/B 地址,而 Cloudflare 那边是 `verification_data.error_message = "CNAME record not set"`、证书 pending,浏览器打开是 `ERR_SSL_PROTOCOL_ERROR` —— **页面根本没人能访问,我们却报了成功**,买来的流量落上去全丢。解法:`_ensure_dns()` 在绑定之后建一条**代理的** CNAME 指向 `<project>.pages.dev`(不代理的话证书签不出来);目标主机名上**已经有服务型记录就一个字不动**并说清楚(和「别抢占一个已经在服务的名字」同一条)。**「发布成功」的定义必须是「打得开」**,不是「接口返回 200」 |
| **ClickFlare 的规则关着时,里面的 path 再 enabled 也不接流量** | `rulePaths` 底下每条**规则**自己有 `enabled`,规则里面的 path 又各有一个 `enabled`。实测某条 campaign 的 US 规则是 `enabled: false`,而它里面那条 path 写着 `enabled: true` 并挂着**旧落地页** —— 只看内层的话,轻则逼用户在两条 path 里选一条,重则**把新落地页换进一条根本不接流量的规则里**,而用户以为 A/B 已经在跑。`_iter_paths()` 现在会把外层规则的开关带下来。**从 ClickFlare 后台的界面上看不出规则是开是关**(截图里那个 US 块看着和启用的一样),所以只能按接口返回的字段判断 |
| **模型不知道今年是哪年,会用训练时那一年** | 生成的 8 个落地页里出现了 **9 次 2024**,而当时是 2026 年 —— 7 次在页脚版权、2 次在**标题文案**里(`2024 Homeowner Alert`)。根因很简单:`landing_lab` / `creative_lab` 的生成提示词里**一个字都没提今天是哪年**,模型只能用训练时的默认年份。坑表里早有「AI 不知道今天日期,必须在 system prompt 里注入」那条,但这两条生成路径走的是 `_plain_completion()`,**根本不经过 `_system_prompt_now()`** —— 老教训没覆盖到新路径。2026 年的广告页写着 2024,用户一眼看出是旧的,信任感当场没了。解法两层:①两个生成提示词都注入今年;②`fix_stale_years()` 兜底,而且**必须分两类** —— **版权声明**(`© 2024`、`Copyright 2019-2024`)形状固定、不涉及文案,**直接改对**;**正文里的年份是广告文案,只报不改**(改了等于替用户改了广告的说法),报的时候把那个年份用【】标出来。扫正文前要把版权段遮掉,否则 `2019-2026` 的起始年会误报 |
| **拒绝必须带出路,否则模型会自己编一个** | 工作室闸门原来返回一句 `{"error": "当前工作室不能执行这项投放操作"}`。模型拿到一句光秃秃的拒绝,转述给用户时就变成了「我刚才切到落地页工作室的视角了,**没有权限**直接在图库里帮您找图」——而**根本没有「权限」这回事**,那件事换个工作室就能做。用户被留在死胡同里:不知道该去哪儿,也不知道切过去之后刚才聊的还在不在。解法:被拦时返回一张**代码写好的移交单**(去哪、为什么、到那边第一句说什么),模型只能原样转述。**凡是代码层的拒绝,都要问一句:模型拿着这句话,能对用户交代清楚吗?**交代不清楚,它就会编 |
| **工具说明的措辞决定模型认不认得出用户的问法** | `recommend_creatives` 的说明写的是「**推荐**素材……建新广告**第3步**用户说没素材时调它」—— 动作锁死在"推荐"、场景锁死在"第3步"。用户问「账户上的素材图有哪些」对不上号,模型就答「由于系统接口的限制,我暂时无法拉取」——**而那个工具当时完全可用**(campaign 模式放行全部 36 个工具)。两条规矩:①说明要按「**它能回答哪些问法**」来写,不是按「什么时候该调它」;②凡是「只此一家」的能力,加一句**排他声明**(「这是唯一能拿到 X 的工具,没有别的接口」),专治「我以为还有别的接口而我没有」。**改的时候 docstring 和 `_oa_tool` 两处都要改** —— `BRAIN=auto` 时 Gemini 走 docstring、ofox 走 schema,只改一处等于漏一半 |
| **老测试可能一直在为错误的原因通过** | 「素材工作室不能确认落地页的待办」这条测试,**先**让落地页工作室确认成功(待办被弹出保险箱),**再**去素材工作室试 —— 于是它撞的是「找不到待办」,压根没走到跨工作室闸门。把闸门整个拆掉,测试照样全绿。改法两条:①**把要测的那一关排在前面**,别让前面的步骤把前提消耗掉;②别只断言报错的措辞,要断言**真实的不变量**(这里是「没有执行」——拿一个标志位记 `apply_flow` 到底被调没被调) |
| **抠不出 id,模型会自己改成「按名字搜」** | 用户贴了投放链接 `https://trk.…/cf/r/6a0fc07a…?CALLBACK_PARAM=…`,而代码只认查询参数 `cpid=` (**这个假设本身就是错的** —— 实测 50 条 campaign 的 Campaign Tracking URL 全是 `/cf/r/<24位id>`,id 在**路径**里)。抠不出来后模型没有停下,而是改用 `list_clickflare_campaigns` 按名字搜了一条顶上 —— 把 `NewsBreak_333_Windows_20260522` 搜成了 `fb小苏苏-system1 - window replacement`,**差一点把买来的流量换到别人的计划上**。三层一起补:①解析支持路径形式;②**报错措辞里写死「绝不许改用名字去猜」**(中英提示词和 6 处工具说明也各写一遍);③待办里**回显这条计划自己的投放链接**,让用户和手里那条比一比 —— 只给名字他核对不了,这次正是靠肉眼比对 id 才发现的。凡是「模型必须精确定位一个对象」的场合,都要问一句:**定位失败时它会退到哪条路上去?** |
| **模型会照着文件名的「形状」编一个出来** | 落地页预览的文件名是 `<8位十六进制>-version-a---<英文名>.html`。线上实测:模型这一轮**根本没调生成工具**,却照着这个形状编了个 `e7c2e391-version-a---the-interactive-estimator.html` 给用户 —— 服务器日志里只有一次 chat 请求加一条 404,盘上从没有过这个文件。用户点开只看到 `{"error":"落地页预览不存在或已过期"}`,**完全看不出是编的**,只会以为功能坏了。提示词里早写了「不许编地址」,拦不住(和「提示词管不住文案照抄」同一条)。两层代码防线:①`_finalize` 把回复里每个 `/landing-pages/xxx.html` **拿到盘上回查一遍**,编的当场盖 ⚠️ 章并列出真实存在的那几个 —— 这道要和「保险箱里有没有待办」**解耦**,老的拆穿章挂在待办分支下,保险箱一空就什么都抓不到;②404 别甩 JSON,给一张说人话的页面并把真实预览列成可点的链接,用户不用回聊天里追问 |
| **提案里给的地址,用户一定会点** | 发布提案的表格里把还没发布的 A/B 地址标成「A版 正式网址」,用户当场点了过去 —— 拿到 `ERR_NAME_NOT_RESOLVED`,以为出错了。其实那个域名的 **DNS 记录要等确认发布那一刻才创建**,打不开是**对的**。`note` 里虽然写了「尚未创建项目、改 DNS」,但模型渲染成表格时那句话离得远、用户也不会去读。**把话写进键名里**(`A版(确认发布后才存在,现在打不开)`),模型就没法再把它说成「正式网址」。凡是「确认后才生效」的两阶段流程,提前给出去的地址/编号都要自带这句话 |
| **保险箱是进程内存里的一份,外部改文件会被写回去** | `PENDING_ACTIONS` 只在 import 时`_load_actions()` 读一次,之后 `_save_actions()` 每次都把**整个内存字典**覆盖写盘。所以在另一个 python 进程里删掉两条旧待办、写好文件之后,跑着的服务下一次登记新待办时又把它们**原样写了回来**(实测:早上删掉,下午又出现在提示词里)。**要改保险箱文件,必须连着让服务重启**(本地 `touch agent_server.py` 触发 reload,线上重启 Supervisor),而且顺序是**先改文件、再触发重启**。生产上只有一个进程、也没人手改文件,所以这是给「我们自己动手清理」准备的规矩 |
| **只给相对路径,模型会自己编个 host** | 落地页预览返回的是 `/landing-pages/xxx.html`,模型照着写成了 **`http://localhost:3000/...`** —— 那是用户本机另一个项目的端口,点开是那个项目的 404,完全看不出问题在哪(路由其实一直是好的)。**凡是要给用户点的地址,代码就得给完整的**:把请求的 `base_url` 存进 contextvar(设在 `call_next` 之前),返回前补成绝对地址,模型就没得编 |
| **要用户手工复制的东西,先翻翻平台有没有接口** | ClickFlare 的 Lander Tracking Script 和 CTA Click URL,让用户来回从后台复制粘贴了很久 —— 而平台一直有 `/api/scripts/direct` 和 `/api/scripts/links`,直接给模板。是探接口时顺手翻 swagger 才看见的。**每多一样要用户手工搬运的东西,就多一处会粘错、而且粘错了看不出来的地方**;动手做「贴一次就存起来」这类缓解方案之前,先确认平台是不是根本就能自动拿 |
| **可点的链接里混进追踪地址 = 用户一点就污染数据** | 竞品落地页表格里的域名做成可点之后,顺手会想把回复里所有链接都变可点 —— 但 ClickFlare 的 `/cf/click`、带 `cpid=` 的地址**点一下就是一次真实点击**,会记进那条 campaign 的统计,而且事后完全看不出是误点的。所以 linkify 时要挡住 `/cf/click`、`/cf/tags`、`cpid=`、`cftmid=`,降级成**纯文字**(还看得见、能复制),并用 title 说清为什么不能点。和「不自动访问追踪链接做健康检查」是同一条规矩 |
| **测「新函数」不等于测「它被接上了」** | 给回复里的域名加可点链接,测试直接调 `linkifyBubble()` 全绿 —— 而把 `renderBubble` 里那一行调用**删掉,测试照样全绿**。函数写对了、根本没被调用,页面上什么都不会变。**必须再补一条走真正入口的测试**(这里是 `renderBubble`)。和「闸门在 `_tool_call` 里,直接调 `confirm_action()` 测不出来」是同一类 |
| **别猜接口路径名,先找 swagger** | ClickFlare 的落地页接口叫 `/api/landings`,而所有人(包括它自己的界面)都管这东西叫 **lander** —— `/api/landers`、`/api/lander`、`/api/lp`、`/api/landing-pages` 全是 404,猜了十几次没中。真正解决问题的是 `GET /api/swagger.json`(200,54 个接口全在里面)。**探一个没有公开文档的 API,第一件事是找它的 spec 端点**(`/swagger.json`、`/openapi.json`、`/api/docs`),别从资源名开始猜 |
| **认证头对不对,看 404 还是 401** | 探认证方式时:用对了头 → 路径不对返回 **404「Path not found」**(过了认证、卡在路由);用错了头 → 一律 **401「not authorized」**(没走到路由)。所以拿一个**已知不存在的路径**去打,能拿到 404 的那个头就是对的 —— 比逐个试「哪个能返回 200」快得多,而且不需要先知道任何真实路径 |
| **LRU 淘汰别把刚放进去的那条算进候选** | 脚本库满 40 个要丢「最久没用的」,排序键是 `last_used_at` —— 而刚存进来的那条这个字段**是空的**,空串排最前 → **刚存就被自己淘汰掉**,接着读它直接 `KeyError`,而落盘的已经是「没有这条」的版本,**重贴多少次都是同样的错,这个账号再也加不进新域名**。顺带淘汰顺序整个是反的(真正最老的反而留着)。两条规矩:①候选里**排除刚插入的那个键**;②没用过的条目拿**存入时间**当「最近使用」,别让空串参与排序 |
| **「最后确认时间」不能因为内容没变就不刷新** | 脚本库记 `saved_at`,超过 180 天提醒用户回后台核对一次。原来写的是「内容相同就保留旧时间」—— 而平台没改版时,用户**照着提醒去复制回来的就是同一段**,于是那条提醒永远消不掉,提醒变成了骚扰。**凡是「多久没确认过」这类时间戳,记的是「最后一次确认」,不是「最后一次改变」** |
| **`cancel_action` 一直没查归属** | `confirm_action` 早就查了「这个待办是不是你登记的」,`cancel_action` 没查 —— 而保险箱 `PENDING_ACTIONS` 是**全进程共享的一份**,不是每人一份。于是 B 登录进来能把 A 登记好的待办**删掉**,A 那边只会看到「找不到待办 xxx」,完全不知道发生了什么。**同一份共享状态上的每个入口都要查同一道归属**,补了 confirm 忘了 cancel 等于没补 |
| **测试的桩没还原,会漏给后面的测试** | 配图那组测试的 `stubbed()` 存了 `cs.search` / `cr.render` 等一串准备还原,**唯独漏了 `cs.download`** —— 于是某条测试里换掉的下载桩一路漏给后面所有测试。这类漏子的表现正是坑表里最难查的那种:**测试为错误的原因通过**。凡是「存一串、跑完还原」的上下文管理器,加一个桩就要回头改那份名单(和「加了新字段忘了给它同样的保护」同一条)。
**前端测试同理,而且更隐蔽**:给日期弹层加测试时,我在测试里把模块级的 `CAL_TARGET`(日历正在给哪个框选日期)从「开始」翻到了「结束」**却没还原** —— 后面三条**一个字没改**的老测试当场连坐变红(它们点了日历却发现填进了另一个框)。**测试之间共享的不只是桩,还有页面自己的模块级状态** |
| **新功能会让老测试开始写真实数据** | 加了脚本库之后,`propose_publish_landing_pages` 里多了一次 `cfs.remember()` —— 于是一条**一个字都没改过**的老测试开始往用户真实的 `data/clickflare_scripts.json` 里写东西,跑一次留一条垃圾(实测真留下了)。**给已有函数加持久化副作用时,要回头看谁在测试里调它**;另外加了一条测试直接扫真实存储里有没有测试用的 id,以后再有这种事会立刻红 |
| **「两版一样」的 A/B,从外面完全看不出来** | 落地页 A/B 发布原来不检查两版内容 —— 同一个文件当 A 又当 B 也照发。跑起来两个网址都正常、数据照常上报、钱照花,唯一的后果是「胜者只是噪音」,而这**要到复盘时才可能想到**。彩排整条流程时才发现的(读代码没看出来)。**凡是「拿两份东西做对比实验」的功能,都要在代码层确认这两份真的不一样** |
| **拿不准「这东西每次都一样吗」,就要两份样本做逐字节比对** | ClickFlare 的 lander 脚本到底是每个 Lander 一段、还是通用一段,文档没写,而猜错的代价是「B 版上报成 A 版、A/B 数据全废且页面看不出异常」。解法不是问文档也不是赌:要两份真样本 `md5sum` + `cmp`,再把已知会变的那个变量(追踪域名)统一之后**重比一次** —— 差异恰好只有那一个字符串,结论就板上钉钉。顺带还定出了「什么情况下才会不一样」(换追踪域名),这正是能不能做脚本库的前提 |
| **别人的脚本可能会改写你的页面** | ClickFlare 的 lander 脚本会扫页面上所有 `/cf/click` 链接,把域名改写成**脚本里写死的那个追踪域名**。于是「脚本粘 A 域名的、CTA 粘 B 域名的」不报错,只是点击静静流到另一个追踪器。**引入第三方脚本前先看清楚它会不会改页面**,别只当它是个埋点 |
| **加了新字段,忘了给它同样的保护** | 待办清单和执行回显都会把 `tracking_script` 换成「[已保存,不回显]」,而后加的 `tracking_script_b` 没进那份名单 —— **脚本原文照样出现在待办清单里**。规矩写的是「脚本不在聊天/待办中回显」,代码写的却是「名字叫 tracking_script 的不回显」,加一个同类字段就漏一个。**凡是按字段名做安全处理的地方,加同类字段时必须回头改那份名单**,而且要拿真实数据跑一遍(读代码看不出来) |
| **绑定自定义域名 = 接管这个主机名** | 拿真实域名试跑时发现:某个根域名上有一条已代理的 A 记录,打开是一个**在投的落地页**。把它绑给 Pages 项目会让现有页面**当场下线**,而它正承接广告流量 —— 转化会静静归零,过一阵才发现。而当时代码只管往项目上加域名,**根本没查过目标域名上有没有东西在跑**。顺带一提,追踪子域名(CNAME 到 ClickFlare)绑了更糟,等于把追踪打断。**凡是「把某个名字指向我」的操作,先问一句:这个名字现在指着谁?** |
| **「整站替换」式的部署,本地目录就是线上内容的唯一真相** | `wrangler pages deploy <目录>` 用那个目录**整体覆盖**线上。而目录在 `data/` 里、不进 git。实测:删掉 `data/` 再发一次新实验,**线上原有的两个实验被静默删除**,而 ClickFlare 的 Lander 还指着它们 —— 买来的流量落到 404 上,钱照花。判据不能依赖本地映射文件(它一起丢了),要用「远端项目已存在 + 本地没有任何历史目录」;**默认拒绝**,用户明确说「覆盖发布」才放行。凡是「上传一个目录 = 替换整站」的部署方式,都要先问一句:**我怎么知道线上现在有什么?** |
| **两阶段流程里,校验只做在「登记」那一步是不够的** | 待办会在保险箱里躺很久(落盘,重启也不丢),而校验常常是**后来才加的**。彩排时实测:保险箱里正躺着一条「加检查之前」登记的假 A/B —— A、B 是同一个文件。它的两个 sha256 都能对上(文件确实没被改过),于是一路放行,发出去两个网址跑同一个页面。**执行那一步要把关键校验再做一遍**(发布查两版内容是否相同、换页查两个 Lander 是不是同一个),复查几乎不花钱,而漏掉就是花一周的钱跑一个学不到东西的实验。同理:引用了已被清理文件的老待办,报错要走 `confirm_action` 那层翻成人话 —— **直接调 `_execute_*` 测不出来**,兜异常的是外面那层 |
| **测试留下的待办会误导下一次真实操作** | 保险箱是**持久化**的,而且每轮都注入进提示词(「以下待办已登记完毕,严禁重新登记」)。9/3 彩排留下的两条待办一直躺到 9/4:文件早被清理、CTA 用的还是当时的测试追踪域名、其中一条 A/B 还是同一个文件。用户真要发布时,模型会照着提示词说「你已经有待办 xxx,直接确认就行」—— 他一确认,拿到的是一句莫名其妙的报错,运气差还会发错东西。**彩排/测试完必须把自己造的待办收走**,和「别把测试垃圾留在用户的保险箱里」是同一条。
**同理:执行时发现「这条永远不可能成功了」也要当场收走**(比如它指向的落地页已经被清理掉)。`confirm_action` 原来只在成功时才弹出保险箱,于是这种死单会一直躺着、一直被注入提示词催用户确认,而确认多少次都是同一个错 —— 一个**代码自己造的循环死胡同** |
| **待办里要存快照,不能只存「指向当前状态的编号」** | 生图待办原来只存 `indexes`(第几版),执行时再回 `_plans()` 里按编号取。用户还没点头就又归纳了一次 → 方案列表整个换掉 → **同样的编号指到别的方案**:报价时给他看的是 A,真花钱做出来的是 B。凡是「先报价、后执行」的两阶段流程,待办里存的必须是**当时给用户看的那份东西本身**,不是指向可变状态的引用(老待办没快照时回落按编号取,并说明风险)|
| **改了行为要顺手改掉所有说法** | 把生图默认从"叠字"改成"出干净图"时,**六处描述**(中英提示词、两个 docstring、提议话术、OpenAI schema)全留在原地,而五套测试当时全绿 —— 助手会照着旧话术跟用户说"文字是代码排上去的",实际图上没字。这就是第二节第 6 条。已加测试:默认不叠字时,这些地方出现「代码精确排版」等字样就报错 |
| **裁切锚点跟着用途走** | 3:2 原图裁成 1200×628 要去掉 22% 高度。叠字时从**顶部**裁(保住文字那块留白)是对的;改成不叠字后还固定切底部,就把主体在下半部分的照片(跪着干活的人、地上的材料)整个切没了。**同一段裁切代码,用途变了锚点就得变** |
| **放开并发之前,先查有没有「每条消息一份」的全局状态** | 聊天页改成多段对话同时跑之后,服务端两个模块级全局立刻成了炸弹:`_EXECUTED_THIS_REQUEST`(真实执行台账)会被后来的请求**清空** → 真执行的那轮盖不上 🔒 核验章,还可能被误判成谎报当众自我拆穿;反过来别人的回复会盖上你的执行记录,**声称做了它没做的事**。`_REQUEST_SEQ` 更要命:A 登记时是 5 号,B 一来全局变 6 号,A 同一条消息里再 `confirm_action` **保险丝就不响了** —— 两阶段确认整个失效(冒烟测试里把它退回全局写法复跑,真的把写操作发到平台去了,靠假 id 被 403 挡住)。解法:和 `CURRENT_USER_ID` 一样改 `contextvars`,`_new_turn()` 里 set,**必须设在起线程 / `copy_context()` 之前** |
| **前后端同名的上限必须一模一样** | 前端 `MAX_CONVS=30`、服务端 `MAX_CONVS=50`,而前端是**裁完之后把整份列表 PUT 上去**的 → 每次打开页面都在悄悄删掉服务器上第 31 段之后的对话。这类「一边裁一边全量覆盖」的组合,两个数字差一点就是静默丢数据 |
| **缓存键忘了带用户 = 数据串号** | 账户品类缓存的键写成 `ad_account_id or "_default"`,而网页上的人一般不指定账户 → 所有人都落在同一个 `_default` 上,B 登录后直接读到 A 的品类。**凡是按人隔离的项目里,任何缓存/登记表的键都要带上是谁** |
| **中转商的余额有延迟计费** | 实测什么都不做、隔 8 秒再读 ofox 余额,数字仍在往下掉(上一笔还在结算)。所以①`check_balance()` 只能当"够不够"的粗判,别拿来精确对账;②**靠"记余额→操作→再记余额"测成本时,中间不能有别的请求在飞** —— 我有一次测出 $0.4256,是超时被杀的那个请求延迟计费混了进来,真实值是 $0.204 |
| **先问清楚"文字归谁渲染"再决定要不要叠字** | 照搬朋友那套 Push 广告做法,把标题和 CTA 按钮都烧进图里,结果 NewsBreak 自己就渲染 `headline`/`callToAction`(它们是和 `assetUrl` 并列的独立字段)→ 标题出现两遍、图上的假按钮和平台的真按钮并排。**同一个技术方案换个平台就可能是错的**,动手前先看目标平台的字段结构 |
| **同一个模板出 N 版 = N 张一样的图** | 三版只有文案不同、画面用同一套模板和相近提示词,出来几乎一模一样 —— 拿去 A/B 时画面这个变量等于没变。**变化要来自镜头语言**(远景/特写/仰拍/过肩),不是文案 |
| **"高质量"要有依据,不能凭感觉** | 生图提示词里写 "advertising photography quality" 出来的是图库式完美摆拍,而实测跑得最好的竞品广告(3245 版位)全是**朴素纪实实拍**:真人真干活、自然光、旧工具。家装卖的是可信,太精致反而像广告。**质量方向要照着真跑得动的素材定** |
| **生图的文字不能让 AI 画** | AI 画英文经常拼错(`Free Estimate`→`Free Estimte`),而且每次字号位置都不一样,做 A/B 时分不清是文案问题还是排版问题。**AI 只画无文字底图,文字用代码叠**,确定性、不会错 |
| **生图要按目标比例出,别先方后裁** | 实测生成 1024×1024 再裁成 1200×628,把画面下半部分的**正主整个裁掉**,只剩虚化背景。解法不是裁得更聪明,是一开始就用横版尺寸生成,并在提示词里指定"主体放下半、上三分之一留空给文字" |
| **ofox 余额会被生图迅速烧掉** | 三次生图测试就把余额跑成负数(`-$0.02`),之后**ofox 全部 402 —— 连文字对话也断了**,等于接力的兜底没了(Gemini 免费额度还在所以聊天没停)。生图 ≈ $0.05/张,比文字贵两个数量级。**动生图前先看余额** |
| **待办落盘会让测试查重误命中** | `PENDING_ACTIONS` 是持久化到 `pending_actions.json` 的,上次跑测试登记的待办还在 → 新测试再登记同样内容会走"查重命中"分支,拿不到新登记的返回值,表现为"字段缺失"。测试要**先清场、跑完把自己造的收走**,别把测试垃圾留在用户的保险箱里 |
| **图标要放在登录门外面** | 站点图标是**在登录页就要显示**的。文件若被登录门拦住,未登录时标签页仍是浏览器默认图标 —— 而那恰恰是用户第一眼看到的页面。`/static/` 本来就在白名单里,但新加静态资源时要确认这一点(冒烟测试守着「未登录也能取到」)|
| **参数会「时好时坏」,别只针对一种坏法降级** | `geoCountry` 多数时候 503,偶尔 200 但返回 0 条。我先写了"撞 503 就降级本地筛",结果被第二种骗过去 —— 不报错、悄悄返回 0 条,看着像"这个词没有竞品在投"。**已知会坏的参数就干脆别发**,能自己算的一律自己算 |
| **不会过期的凭据别为它建界面** | 为 Insightrackr(登录态几小时失效)做的「网页贴 key」界面,换成不过期的 OpenAdLibrary 之后就成了纯负担:一个按钮、一个弹窗、两个接口、一套按人存的凭据逻辑、两条测试,全是维护成本,换不来任何价值。**先问这个凭据会不会过期、需不需要按人隔离,再决定要不要做界面** |
| **给 const 赋值 = 整条 then 链静默失效** | `conversations = remote`(它是 `const`)抛 `TypeError`,而这个异常正好被最外层 `.catch(console.warn)` 吞掉 → "以服务器为准"那段永远不执行:左栏空着、服务器上的历史读不回来,接着**第一条新消息一发就把服务器那份覆盖成本地这份**,老记录就此消失。页面照常显示、控制台才有一行 warn,极难发现。**要原地替换内容**(`length=0` + `push.apply`)——别处持有的是这个数组对象本身,换个对象也接不上 |
| **同步失败不能只 console.warn** | 读不回记录时用户看到的是"记录空了",而他接下来一发消息就会覆盖掉服务器上的旧记录。**必须在页面上明确提示并叫他先别发消息**。凡是"失败后继续操作会毁数据"的场景,报错都必须是用户看得见的 |
| **别用「锁住界面」来保证数据不出错** | 「思考中不许切会话」这道锁的**根子**是:回复写给谁,认的是**当前打开的那段**(全局一份 `history`)—— 所以一切走就落错地方,只好把界面锁死。而且锁还漏了口子:`switchConversation` 拦了,「新对话」和「删除」没拦,实测能把回复写进刚开的空对话、甚至一段**毫不相干的老对话**。正解不是把锁补全,是**让回复认自己的会话 id**(`send()` 一开始定死 `convId`,全程用 `appendMsg(convId, ...)` 写)。锁没了,数据反而对了。见第六之十六节 |
| **「想去做某事」不等于「现在不能做」** | 平台页点「先绑定账号」会带 `&bind=1` 进聊天页,而代码写成 `if (!PLATFORM_BOUND || WANT_BIND) showNotBound(p)` —— `showNotBound` 里是**无条件停用输入框**的。于是**已经绑好的用户只要地址里带着 `bind=1`(收藏了、刷新了、从平台页点进来),就被永久锁住**:顶栏明明显示着广告账户名,输入框却说「还没绑定」,刷新也好不了(参数一直在)。线上实测到的。**意图和状态要分开判**:真没绑才锁;已绑好只是想改,直接打开账户弹窗,什么都别锁。而且处理完要把 `bind=1` 从地址里 `history.replaceState` 掉 |
| **锁了输入框不等于锁住了发送** | 没绑账号时把 `input`/`sendBtn` 设成 disabled,但**快捷提问按钮是直接调 `send(q)` 的**,绕过那两个控件照样发得出去 —— 线上实测烧掉了额度、还在左栏留下以按钮文字命名的会话。**闸门要设在函数里**(`send()` 开头判 `PLATFORM_BOUND`),控件置灰只是让人看得懂,不是防线 |
| **不可逆操作别用浏览器的 `confirm()`** | 它是**系统级弹条**,贴在浏览器最上面、样式完全不受控,和页面像两个东西;更要命的是有些浏览器允许用户勾「不再显示此类对话框」—— **一勾这道确认就永久消失了**,而删对话是不可逆的。已换成页面内弹框 `askConfirm()`(返回 Promise):默认焦点给「再想想」(回车不该正好落在危险按钮上)、点空白处和 Esc 都算取消、监听只挂一次(挂在每次弹出时会越积越多)。前端测试**剥掉注释后**搜原生 `confirm(`,守着不许回退 |
| **悬停才显形的按钮 = 没有这个按钮** | 删除对话的 `×` 一直都在,但 CSS 写的是 `opacity: 0` + `:hover` 才显形 —— 用户根本不知道能删(实际反馈:「给历史聊天加个删除按钮」,而它本来就有)。**触屏设备更彻底:没有悬停这回事,那个按钮永远点不到。** 面向小白的界面里,功能可发现性比视觉干净重要得多:默认就露出来(淡一点),悬停再加深。前端测试直接读 CSS 守着这条 |
| **在点击处理里重画,会让「点外面就关闭」误判** | 日历上点某一天 → 它自己的 onclick 调 `renderCal()` 重画 → 重画第一步 `innerHTML = ""` 就把**刚点的那个按钮从 DOM 上摘掉了**。等事件冒泡到 document,它的 `parentNode` 已经是 `null`,「顺着往上找,不在弹层里就关闭」那段永远找不到弹层 → 判成点了外面 → **弹层当场关掉,用户表现是「根本选不了日期」**。解法:把这段挂到**捕获阶段**(`addEventListener(..., true)`)—— 它在目标自己的 onclick **之前**跑,那时节点还好端端挂着。凡是「点击处理里会重画自身」的浮层,都要这么做 |
| **给动态内容绑事件要用委托** | 聊天里的素材图是 AI 回复渲染出来的,每来一条新消息就是一批新 `img` 节点。给单张图绑监听的话,只有绑那一刻存在的图能点,之后新渲染的全失灵,而且表现是"点了没反应"很难查。**挂在父节点上做事件委托**,以后不管怎么渲染都自动生效(图片放大就是这么做的) |
| **测试的 DOM 模拟缺方法 = 页面在回调里崩** | helper 里原来没有 `removeAttribute`/`setAttribute`,页面代码一调就 TypeError。崩在事件回调里往往只表现为"点了没反应",`node --check` 也抓不到。模拟得越像真 DOM,越能提前抓到这类问题 |
| **防幻觉别做成画地为牢** | 为了防 AI 编关键词,改成一律按账户已有品类查 —— 结果用户问「现在什么广告跑得好」这类**开放问题**时,也被框回他自己那三个品类,永远看不到新机会。**约束要挑准场景**:用户没给品类且模型必须填一个时才拦;用户自己说了、或压根是开放问题时,不能拦 |
| **不给关键词时 AI 会自己编一个** | 用户问「同行都在跑什么广告」并没说品类,AI 编了 `roof`,而账户实际投的是 gutter/window —— 查回来全是**别的行业**的广告,用户看半天才发现对不上。八成是 `KNOWN_AD_TYPES` 每轮注入提示词,它抓了排第一个的词。**凡是"用户没给、模型要自己填"的参数,都要有个真实数据源兜底**(这里是从账户的计划名和广告文案认品类),并在代码层比对、对不上就点出来 |
| **翻页会悄悄换掉相关性** | 竞品平台的默认顺序**就是按相关性排的**:搜 roof repair 时 page=1 有 97% 的标题真含 roof,**page=2 只剩 17%**。"翻几页凑候选池再本地排序"看着是白赚,其实是拿相关性换排序 —— 不先筛一道,按"投放天数"排出来的就是一堆跑得久但完全无关的广告(实测撞上养老金、社保)。**凡是要在本地重排的场景,先问一句:平台原本的顺序是不是已经在表达某种信息?** |
| **参数被"静默忽略"比报错更阴险** | OpenAdLibrary 的 `sortBy` 和 `sort` 的多数值传了**不报错、也不生效**,返回和不传一模一样。上一家(Insightrackr)是传错就返回空,至少能发现;这家会让你以为排好了。**判断一个参数有没有用,要拿「和基准结果比对」来验,不能看有没有报错**。所以 `SORTS` 里只暴露实测有效的两个值 |
| **文档写了的参数不一定能用** | OpenAdLibrary 的 `minDaysRunning` 文档里有、看着正是我们要的,实测**稳定 503**(隔几秒重试三次都一样),是它服务端的问题。投放天数只能拿回来自己算。**官方文档和参考项目一样,只能当线索** |
| **会过期的凭据,换起来的成本决定功能死活** | Insightrackr 的登录态几小时就失效,而当时换一次要 SSH 改 `.env` + 重启 —— 重到人宁可不换,功能就荒废了。这是后来换平台的主因之一。现在:凭据按人存 `data/creds.json`、网页上贴一下就生效(第六之十四节) |
| **别照抄参考项目的参数常量** | 从 qx-ad-bot 抄来的 Insightrackr 排序代码(3=曝光/1=首投/2=最近投)**实测全是错的**:1/2 一条都返回不了,3 也不是曝光排序,真正有效的是 4。关键词字段同理,照抄的全字段匹配会让搜 "roof repair" 返回小说 App。**参考项目的代码只能当线索,参数值必须拿真凭据打一遍确认** —— 这就是第二节第 6 条「别只看注释和文档下结论」 |
| **HTTP 头只认 latin-1(又一次)** | 竞品凭据里混进中文/全角字符时,httpx 抛 `'ascii' codec can't encode` —— 用户看不懂该改什么。凡是**把用户输入塞进 HTTP 头**的地方,都要先 `.encode("latin-1")` 试一下并说人话。和 `WWW-Authenticate` 放中文会 500 是同一类,冒烟测试抓出来的 |
| **Wikimedia 下载要"政策格式"的 UA** | Openverse 的图大量托管在 upload.wikimedia.org,它按机器人政策挡人:**UA 里必须带括号联系方式**,格式形如 `名字/版本 (联系地址) 库/版本`。实测**浏览器 UA、curl 的 UA、不带联系方式的自定义 UA 全是 403**,只有政策格式能拿 200。403 正文里会写 "Please respect our robot policy" —— 看到这句就是它 |
| **质量评分不能用纯加分制** | 素材打分时"分辨率太小"和"竖图"是**硬伤,必须一票否决**。原来加分制下 400×210 因为宽高比正好(1.90:1)被判成"可用",等于把一张糊图推荐给用户。凡是"某一项不合格就整体不能用"的场景,别用总分,要先判否决条件 |
| **图库默认搜出来的可能禁止商用** | Openverse 不加 `license=cc0,pdm` 时,搜 roof 的**第一条就是 by-nc-sa**(NC = 禁止商用)。投广告是商业用途,拿 NC 的图就是侵权。这类过滤**必须写死在请求参数里**并有测试守着,不能只在提示词里叮嘱 |
| 素材类型判断 | 必须**优先用浏览器给的 MIME**,只看文件名后缀会把粘贴的 GIF、无后缀视频判错 → 平台拒收。上传时把类型记进 `_ASSET_TYPES[assetUrl]` |
| 前端并发发送 | AI 思考时再按回车会发出第二个请求(双倍额度+旧上下文);已用 `busy` 标志挡住 |
| 失败消息错位 | 发送失败会把消息从 history 撤回,但气泡还在屏幕上 → 必须打"未送达"标记,否则用户以为 AI 听见了 |
| **compare_digest 不吃非 ASCII** | `secrets.compare_digest("中文", ...)` 抛 `TypeError`。邀请码校验没转 bytes → 用户在邀请码栏敲中文/emoji,看到的是 **500 Internal Server Error** 而不是「邀请码不对」。**两边都 `.encode("utf-8")` 再比**,仍然是定时安全的。密码那条是 hex 字符串所以没踩到 |
| HTTP 头不能写中文 | `WWW-Authenticate` 里放中文会 500(只认 latin-1) |
| 空值配置读不到 | `.env` 里原本留空、后来才填的键(如 APP_PASSWORD),运行中的进程永远读不到 → 安全开关必须用 `_read_env_value()` 每次现读文件 |
| 语言切换的"假失效" | 切语言**只影响之后发出的请求**:切换前已在路上的那条回复仍是旧语言,而"切换提示"会插在它前面 → 看起来像切换没生效。已修:切换时若 `busy` 就额外提示一句;欢迎语(界面文字)跟着换;反复切换时替换旧提示不堆叠 |
| 英文规则要写成绝对要求 | 只写"Reply in English"不够牢:模型会被中文历史/中文提问带跑。现在把语言规则放在 `SYSTEM_PROMPT_EN` **最开头**并写明"overrides everything else,即使用户用中文写、即使历史是中文、即使用户明确要求中文"——实测连"请用中文回答我"都能顶住 |
| **英文提示词要整段英文** | `/api/analyze` 起初是"中文提示词 + 末尾加一句 reply in English",结果英文模式下 AI 照样输出中文 —— **满篇中文会把模型带跑**。这和下面「英文规则要写成绝对要求」那条 `SYSTEM_PROMPT_EN` 是同一条教训,但当时没推广到 analyze。现已改成:英文模式用**完全独立的英文提示词**,语言规则放最前面写成 absolute。实测中英各出一份都对 |
| 双语实现 | 前端一张 `I18N` 表 + `applyLang()`;后端 `SYSTEM_PROMPT_EN` 与中文版规则一一对应,请求体带 `lang` 透传到三个大脑;`_finalize` 钢印也要双语(它是代码直出、不过 AI) |
| 谎报关键词要不分大小写 | AI 英文写的是 "Successfully created",关键词表是小写 → 匹配前先 `.lower()` |
| 执行记录别提前拼死语言 | `_EXECUTED_THIS_REQUEST` 存结构化 dict(id/ok/detail),语言留到 `_finalize` 再决定 |
| 保险箱怕重启 | PENDING_ACTIONS 已落盘 pending_actions.json,热重载/重启不丢待办 |
| **Claude 那一级没接工具** | `ask_claude()` 只传 system+messages,**没传 tools** → 切到 `BRAIN=claude` 就只能闲聊,查不了数据也开关不了广告。要么别用,要么补上工具(Claude 是"手动挡",写法参考 `ask_openai`)|
| 钢印有两个盲区 | ⚠️ 拆穿章只在「保险箱还有待办 + 回复含谎报关键词」时触发:①保险箱空着时的胡说八道抓不到;②AI 只是复述历史也可能被误伤。🔒 章则**成功失败都会盖**,必须读后半句「执行成功→/执行失败→」|
| 查重跨天会失效 | `_find_duplicate` 比较除 seq 外的全部字段;建广告的计划名默认含「-年月日」,**同关键词跨天会被当成两单**,不算 bug 但要知道 |
| **命名日期必须走北京时间** | 曾用 `datetime.now(timezone.utc)`,导致北京时间 00:00~08:00 建的广告名字写成**前一天**,用户按日期筛就漏掉。现在统一走 `sched.now_beijing()`。**这类测试不能只比"名字里的日期==今天"**——北京和 UTC 一天里有 16 小时同天,测试多半在那 16 小时里跑,用 UTC 也照样绿。冒烟测试里是把 `now_beijing` 换成北京 03:00(UTC 还在前一天)再看名字跟谁走 |
| OpenAI 手动挡有轮数上限 | `ask_openai` 的工具循环最多 10 轮,超了直接回「工具调用轮数过多,已中止」。复杂任务(如一次查多层数据)可能撞上 |
| 保险丝计数器会续号 | `_REQUEST_SEQ` 重启后从「保险箱里已存待办的最大 seq」续起,所以重启后立刻确认旧待办也能通过(不会被误拦)|
| **`pageSize` 只认固定几个值** | 平台只接受 `[5,10,20,50,100,200,500]`,传 3 会报 `Invalid request... pageSize must be one of`。写死的 20 正好在列表里,但自己调接口时别乱传 |
| 列表接口每页写死 20 条 | `list_campaigns/ad_sets/ads` 的 limit 没暴露给 AI,固定 20;超过要靠 AI 主动翻页(返回里有 `total`/`has_next`)。另:ad_set/ad 只能按**账户**查 + 名字搜,不能按父级 id 过滤 |

## 九、当前进度 & 路线图

**已完成**(七层架构全通):
- 只读:组织/账户(已拍平)/计划/广告组/广告列表、转化事件列表、
  报表(三层汇总+自选日期+可指定账户,行内自带 name/revenue/roas,超 180 天友好拦截);
- 写操作:开启/暂停、**建广告三层**(**一个广告组下面可以放多条广告**,
  事后还能用 `propose_add_ad` 往已有的组里加 —— 见第六之二十一节),
  全部走保险箱两阶段确认 + 防死循环三件套 + 幻觉钢印;
- 建计划向导:**只问必须用户定的 4 件事**(落地页→转化事件→素材→命名),
  **预算和文案由系统配默认值**(日预算 $20 / AI 按落地页代拟文案),
  没素材可让 `recommend_creatives` 从**本账户历史广告**里挑效果好的复用,
  但必须讲清"用了什么、为什么、想改直接说";返回里的 `defaults_used` 列出哪些是系统定的,
  📎按钮 / 直接粘贴图片上传中转,命名按落地页类型走规范(见第六之十节),建好默认全 OFF;
- 前端:全屏 UI(渐变主题/头像气泡/快捷提问/动画)、Markdown 表格渲染、**素材图点击放大**、
  **回复里的域名可点开**(新标签;但**追踪链接故意不可点** —— 点一下就是一次真实点击、污染统计)、输入法回车修复、
  **聊天记录按账号存服务器**(`data/chats/<uid>.json`,换电脑登录同一账号还在;左栏多会话)、
  **中英文切换**(顶栏 🌐,界面 + AI 回复语言一起切,选择会记住)、
  **每段对话各跑各的**(切走了后台照样跑完,回复写回它自己;左栏橙点=生成中/蓝点=跑完没看,见第六之十六节)、
  失败消息打"未送达"标记、请求 3 分钟超时;
- **素材查找**:`search_stock_creatives` 从正规授权图库(Pexels/Pixabay/Openverse)找新素材,
  带质量评分和许可证;选中后 `use_found_creative` 转存进 NewsBreak 换 assetUrl(第六之十一节);
- **竞品广告查询**:`search_competitor_ads` 查同行正在投什么(OpenAdLibrary),
  带投放天数和覆盖版位数,并让 AI 总结高效广告的共同点(第六之十二节);
- **创意拆解**:`decompose_creative` 多模态看图出「素材模型」(会连广告文案一起看 ——
  原生广告的字不在图上),`summarize_creative_patterns` 归纳套路并写出我方文案方案,
  代码层查文案照抄(第六之十三节);
- **把方案做成广告图**:`propose_make_creatives` —— AI 画**无文字**底图 +
  **代码确定性叠字**,出 1200×628 成品直接传进素材库;走确认关卡先报价(第六之十五节);
- **ClickFlare 全通了**:接口全靠实测探出来(公开文档没有清单),
  `check_clickflare_params.py` 随时可复查;追踪链接里的 `/cf/r/<id>` 直接定位 campaign,
  workspace 和追踪域名由代码推导;**把 A/B 挂进 campaign 并设 50/50 也做完了**,
  走保险箱 + 指纹防覆盖,offer 不动(第六之十八节)。
- **落地页 A/B 发布**:不用 GitHub,通过 Pages Direct Upload 发布;域名每次选择,
  新域名自动建项目、旧域名复用;ClickFlare CTA/脚本确定性注入,发布走二次确认;
  **追踪脚本每个追踪域名只贴一次**(脚本库,按人存),**CTA 域名和脚本域名对不上直接拒**,
  **A/B 两版内容相同也直接拒**(第六之十七节);
- **落地页里能有配图了**:模型只写 `[[IMAGE_n]]` + 英文搜索词,Python 去授权图库找、
  下载、替换成相对路径,发布时跟着页面一起传上去;不满意可以 `swap_landing_image`
  只换某一张。**图库配不上时(家装类在免费图库里本来就薄)可以用 AI 生图补** ——
  留隐藏占位、`propose_landing_images` 报价、确认后才花钱(第六之二十三节);
- **工作室之间能交接了**:被闸门拦下时返回一张代码写好的移交单(去哪、为什么、
  到那边第一句说什么),模型不能再编「我没有权限」;切工作室时问一句
  「带着这段过去 / 另开一段新的」,不再静默丢上下文(第六之二十节);
- **「绕住了就停下」闸门**:模型在工具循环里原地打转时自己刹车(同工具同参数第 3 次 /
  轮数到顶 / 这条消息的 token 到顶),并如实报出「发生了什么 + 花了多少 + 下一步怎么办」;
  两条大脑路径都装,闸片按请求隔离(第六之十九节);
- 大脑:三级火箭 + `BRAIN` 开关(**看图也跟着这个开关走**,设 openai 就一次都不碰 Gemini);Gemini/OpenAI 工具共 39 个(两边名单和 schema 由冒烟测试对齐);
- **数据大屏**:KPI(带环比)/ 每日趋势 / 各计划对比 / 三层明细表 / **AI 投放诊断**,双语;
- 定时任务:一次性 + 每天重复,看表线程每 30 秒检查,错过 >15 分钟不补跑;
  **定时开启同样三层一起开**(任务里存 `targets`);
- **登录 + 多平台**:账号密码登录(加盐哈希)、聊天记录按账号存服务器(换电脑也能看到);
  登录后先进 `/platforms` 选平台,聊天页标题/副标题跟着选的平台变,没绑账号会明确挡一道并引导;
- **每人绑自己的平台账号**:token 按 user_id 存 `data/creds.json`,A 绑过 B 也蹭不到(第六之四节);
- **流式回复**:`/api/chat/stream`(SSE),边想边出字 + 播报"正在查什么",
  两条大脑路径都是手动挡工具循环(第六之五节);
- 安全:登录门 `AuthMiddleware`(未登录页面 302、接口 401)、`APP_PASSWORD` 当**注册邀请码**
  (留空=谁都能注册,分享端口/部署前必设),已关掉 `/docs`;
- 工程化:`README.md` 使用指南、五套测试(冒烟 270 + 前端 101 + 大屏 28 + 平台 43 + 流式 19)、
  `requirements.txt` + `.gitignore`(项目已可独立搬家,零依赖 qx-ad-bot)、
  **已纳入 git 版本管理**(提交前先跑冒烟测试;`.env` 已被 `.gitignore` 排除)。

**已实测跑通**:2026-07-29 建成真实三层 `gutter-0729`(campaign / ad set / ad 三层 id 见平台后台,
全 OFF,平台回查确认存在)。
过程中挖出三条平台真规则(trackingId 必填、description 3~90 字符、bidType 用
MAX_CONVERSION),均已固化进代码和第五节速查。

**账户事实**(具体值不写进本文件,现查见下):组织和广告账户各 1 个,
账户下有 5 个转化事件(默认自动选 submit form)。
> 要拿真实 id:`./venv/bin/python -c "import newsbreak_client as nb; \
> o=nb.list_organizations(); print(o); print(nb.list_ad_accounts(o[0]['id']))"`
> —— 账户唯一,所以 `_default_ad_account_id()` 不会挑错;哪天变成多账户,代码会明确报错让人选。
**大脑现状(2026-09-04 当天改过两次,以 `.env` 为准,别信这段的记忆)**:
现为 **`BRAIN=auto`** + ofox 中转 `https://api.ofox.ai/v1` +
**`OPENAI_MODEL=google/gemini-3.1-pro-preview`**。
(`GET /v1/models` 能列出这家支持的全部型号,换型号前先去那儿确认名字。)

> **这一天的来回值得记下来**:上午 Cole 说"不要用 Gemini 的模型",改成
> `BRAIN=openai` + `openai/gpt-5.5`;跑了不到两小时他看 ofox 账单叫停 ——
> **一条聊天消息 $0.5~1.0,当天 40 次调用约 $6**。原因见下面那笔账。
> 于是换回 `gemini-3.1-pro`(同样走 ofox,不是 Gemini 直连)并把 `BRAIN` 还原成 `auto`,
> 让日常聊天优先吃 Gemini 的免费日额度、失败才落到 ofox。
> **教训不是"哪个模型好",是"换主力模型之前先按一条消息算一次账"**。

**一笔实测的账(2026-09-04,gpt-5.5,看 ofox 请求日志 14 条)**:

| | 数字 |
|---|---|
| 一次调用 | 输入 6.8K + **输出 7~9K** |
| 屏幕上实际看到的字 | 几百字 —— **差额全是看不见的思考 token,照价收费** |
| 成本构成 | 输入 $0.17 / **输出 $1.38 → 输出占 89%** |
| 一条用户消息 | **触发 2~4 次调用**(工具循环),不是一次 |
| 单价对比(每百万 token) | gpt-5.5 $5/$30 · gemini-3.1-pro $2/$12 · gpt-5.4-mini $0.75/$4.5 |

同样这批流量换成 gemini-3.1-pro:$1.55 → $0.62,**省 60%**。
ofox **只有 `GET /user/balance`,没有用量查询接口**;而且余额是延迟计费的
(什么都不做隔 8 秒再读还在往下掉),只能当"够不够"的粗判。

Gemini 官方免费额度每日重置(北京时间下午 3~4 点);OpenAI 官方账户无余额(key 在 .env 里注释保留)。

**线上部署(2026-08-14)**:已上线到宝塔服务器(Debian 13 / Python 3.13,机器在欧洲),
Supervisor 守护、nginx 反代。细节和四条硬约束见第六之六节。

**定时任务(2026-08-05 实测跑通)**:19:34 登记 → 确认 → 19:45 看表线程自动执行 → 成功打开
`gutter-0805`(存档 `state: done, last_result: 成功`);之后 Cole 在平台手动关回 OFF(测试结束)。
顺带验证:平台上的手动改动,助手查询时能立刻反映真实状态,没有缓存问题。

**接下来(按优先级)**:
1. ~~建计划向导端到端验收~~ ✅ 已完成(2026-08-05 聊天版建成 `gutter-0805` 三层);
2. 真接第二个平台(Nextdoor / Meta):现在只是注册表占位,按第六之二节最后那三步做;
3. ~~ClickFlare 第三步:把 A/B 挂进 campaign 并设 50/50~~ ✅ 已完成(2026-09-04);
4. 补齐 Claude 那一级的工具支持(现在 `BRAIN=claude` 只能闲聊,见第八节坑表);
5. 调预算等更多写操作(需先在 qx-ad-bot 里查 update 接口的 payload 格式);
6. ~~流式回复(边想边出字)~~ ✅ 已完成(2026-08-14);把 README 推广给团队。

**落地页 A/B 这条线:2026-09-07 端到端真跑通了。**

| | 状态 |
|---|---|
| Cloudflare 域名 + Pages 项目 | ✅ 已建、已绑、DNS 记录已自动创建 |
| CTA Click URL / Lander Tracking Script | ✅ **不用人管** —— `clickflare_publish_kit(campaign)` 从平台现取(第六之十八节末),脚本库只当兜底 |
| A/B 两版页面 | ✅ 已生成并发布,**两个地址实测 HTTP 200** |
| 挂进 campaign | ✅ 已 `propose_swap_campaign_landers` → 确认 → 生效(🔒 钢印可查) |
| 配图 | ⬜ 代码全通了,但 **图库钥匙还没配**,现在一张也配不上(第六之二十三节末) |

> **配图走 AI 生图,不走图库**(2026-09-08 Cole 定的):`PEXELS_API_KEY` /
> `PIXABAY_API_KEY` 保持空着,Openverse 对 roof / gutter 这些词本来就没货。
> 所以配不上图时,代码给的出路一律是「用 AI 把图补上」(报价 → 确认 → 才花钱),
> **不再劝人去申请钥匙**。见第六之二十三节。

> **2026-09-04 实测到的一次真事**:助手**根本没调生成工具**,却编了一个预览链接
> `e7c2e391-version-a---the-interactive-estimator.html` 给 Cole,点开是 404。
> 现在代码会把回复里每个预览链接拿到盘上回查(见上面第六之十七节那条),
> 所以**「链接点开是 404」不再是个谜** —— 要么当场被 ⚠️ 标出来,要么 404 页面
> 直接把真实存在的几个列给你点。

另外两件运维上的事:**服务器上还有若干提交没部署**;
**`data/` 目录要备份** —— 它是「线上有什么」的唯一依据,护栏能拦住误删但恢复不了内容。

## 十、下次接手先做这三件事

1. **确认服务活着**:`curl -s -o /dev/null -w "%{http_code}" http://localhost:18100/login`
   → **200 = 活着**。注意别去 curl `/`:自从加了登录门,`/` 未登录时返回 **302**(跳登录页),
   那是正常的,不是挂了。连不上(000/7)才 `./start.sh`
   (后台跑要 `setsid nohup ./start.sh >> server.log 2>&1 &`);
2. **跑一遍冒烟测试**:`./venv/bin/python smoke_test.py` —— 270 项全绿说明钥匙、
   平台连通、护栏都正常,比逐个手测快得多,也能立刻发现平台规则变动;
3. **看 `git log --oneline`** 了解最近改了什么,再看本文件第八节(踩过的坑)和第九节(进度)。

**改代码的固定节奏**:说清要做什么 → 改 → **跑五套测试**(冒烟 270 / 前端 101 / 大屏 28 / 平台 43 / 流式 19)
→ 更新本文件相关章节 → 提交 git。
