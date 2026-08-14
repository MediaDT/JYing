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
5. **改完代码必跑五套测试全绿才提交 git**:`./venv/bin/python smoke_test.py`(34)、`node frontend_test.js`(16)、`node dashboard_test.js`(9)、`node platform_test.js`(22)、`node stream_test.js`(13)
   (项目已纳入版本管理,改坏了可以 `git diff` / 回滚);
6. **别只看注释和文档下结论**——本项目已多次出现"注释/CLAUDE.md 说的和代码实际行为不一致"
   (docstring 还写着"只读客户端"、BRAIN 实际值等)。以代码和实测为准,发现不一致顺手改掉。

## 三、架构(七层,全部打通)

```
① 交互层  static/login.html      登录/注册(账号密码,记录跟人走)
          static/platforms.html  平台选择页(登录后先选平台:已绑定/未绑定/敬请期待)
          static/index.html      聊天页(气泡/表格渲染/快捷提问/输入法修复/中英切换/记录存盘)
          static/dashboard.html  数据大屏(KPI卡/趋势图/柱状图/AI诊断/明细表,同样双语)
② 大脑层  agent_server.py     三级火箭:Gemini flash → flash-lite → OpenAI通道
                             `.env` 里 BRAIN 可指定主力:auto(默认)/openai/claude
③ 工具层  agent_server.py     15个工具:6查询(含转化事件) + 1报表 + 5写操作护栏 + 3定时
④ 客户端  newsbreak_client.py NewsBreak API 封装(读+写)
⑤ 安全层  agent_server.py     写操作"保险箱+保险丝"(第七节)+ AuthMiddleware 登录门
          accounts.py         账号/加盐哈希密码/会话/每人的聊天记录(第六之三节)
⑥ 平台层  platforms.py        投放平台注册表(NewsBreak 已通;Nextdoor/Meta 标 coming)
⑦ 配置层  .env                APP_PASSWORD(注册邀请码)+ BRAIN;每人的平台 token 在 data/creds.json
                             + 四把钥匙:GEMINI / OPENAI / ANTHROPIC / NEWSBREAK
```

其他文件:`start.sh` 一键启动;`README.md` 面向使用者的指南(给 Cole 和团队看);
五套测试:`smoke_test.py` 后端冒烟(34)+ `frontend_test.js` 多会话(16)+ `dashboard_test.js` 大屏绘图(9)+ `platform_test.js` 多平台(22)+ `stream_test.js` 流式(13),改完都要跑;`requirements.txt` + `.gitignore` 让项目可独立搬家
(**`.gitignore` 已排除 `.env`、`data/`(每个人的聊天记录)、`pending_actions.json`、`scheduled_tasks.json` —— 后两个是运行时状态,跟机器走,别进 git**);`chat.py`、`newsbreak_hello.py` 是学习期的小练习。

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
  平台页点「先绑定账号」会带 `&bind=1` 进来,强制弹这个引导。
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

## 七、写操作护栏(核心安全设计,不许绕过)

两阶段 + 物理保险丝:
1. AI 只能先调 `propose_status_change` / `propose_create_campaign` 把动作放进「保险箱」
   (PENDING_ACTIONS),然后向用户复述 + **报出待办编号**请求确认;
2. 用户在**下一条消息**明确同意后,AI 才能调 `confirm_action` 执行;
3. 保险丝:`_REQUEST_SEQ` 每条用户消息+1,登记与执行同序号=同一条消息 → 代码层直接拒绝,AI 无法自问自答;
4. `cancel_action` 供用户反悔;`list_pending_actions` 供 AI 查编号。

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
| **`.gitignore` 不支持行尾注释** | 写成 `scheduled_tasks.json   # 说明文字` → `#` 只有在**行首**才算注释,这行整体被当成一个字面 pattern,匹配不到任何文件 → 下次 `git add -A` 又把它加回版本库了。注释必须**单独占一行**。改完用 `git check-ignore -v <文件>` 验一下真的生效 |
| **SSE 要加 `X-Accel-Buffering: no`** | 不加的话 nginx 会缓冲整条流,用户看到的还是"转圈很久然后一次蹦出来",流式白做。加在响应头里比改 nginx 配置省事(而且换机器不会忘) |
| **Gemini 手动挡要原样带回 part** | 自己跑工具循环时,把函数调用贴回对话**不能自己重新造 `Part`** —— 原始 part 里带着 `thought_signature`,Gemini 要拿它校验,少了直接报 400「Function call is missing a thought_signature」。解法:收下模型吐的 part 对象**原样存着**,别只取 `function_call` 再重建 |
| **Gemini 流式 + 自动工具调用 = 不工作** | `generate_content_stream` 配 `tools=` 时实测只回一个 `text=''` 的空块就 `finish_reason=STOP`,工具循环没跑。表现是用户收到一句"(Gemini 没有返回文字)"。**解法:关掉自动工具调用改手动挡**(已实施),顺带还能播报每一步在查什么 |
| **测"一闪而过"的东西别按时序抓** | 进度文字这类中途状态,用 `await tick()` 轮询去抓极不稳定(事件全在微任务里瞬间跑完,而且 helper 把 `setTimeout` 桩成了空函数)。改成**记账**:helper 记录每一次 `textContent` 写入,测试查记录 |
| **contextvars 要设在 call_next 之前** | 在 `BaseHTTPMiddleware` 里设的上下文变量,只有设在 `await call_next(request)` **之前**才会传到下游;设在之后不生效。同步接口被丢进线程池也没问题——`run_in_threadpool` 会复制当前上下文 |
| **"没绑就回落公用 token"= 没隔离** | 按人隔离时,`_token()` 必须区分「不在用户上下文」(None,回落 .env)和「在用户上下文但没绑」(空 dict,直接报错)。少了这个区分,B 登录后会直接用上 .env 里的公用 token,隔离形同虚设。冒烟测试里有一条专门守这个 |
| **cookie 的 secure 不能写死 True** | 写死了本地 `http://localhost` 开发时浏览器**根本不存这个 cookie**,直接登不进去。要按 `request.url.scheme` 判断。放在 nginx 后面时这个 scheme 来自 `X-Forwarded-Proto` 请求头,所以反代配置里那行不能少;缺了只是退回不加 secure,不会把人挡在门外 |
| **测试的 DOM 模拟要够真** | helper 里 `remove()` 曾是空函数、`getElementById` 找不到动态创建的元素 → 测「提示有没有被撤掉」永远是假通过。已修:`appendChild` 记父节点、`remove()` 真摘、`id` setter 自动登记。另外 fetch 桩失败时要返回**失败的 Promise**而不是同步抛,否则测不出页面的 `.catch` 分支 |
| **探活别 curl `/`** | 加了登录门之后,未登录访问 `/` 返回 **302**(跳 `/login`),这是**正常**的。老口诀「不是 200 就重启」会把好端端的服务白重启一遍。改用 `curl .../login` 看 200,或接受 200/302 都算活 |
| **改文件前先看真实写法** | 这次批量改 index.html:锚点写成 `var I18N = {`,实际是 **`const I18N = {`** → 整块平台变量声明**静默没插进去**(replace 没匹配就是什么也不做,不报错),留下 `PLATFORM_NAME is not defined`。**批量替换后必须 grep 验证改动真的落地了**,别看脚本 print 的「✅」——那是无条件打的 |
| **函数名要按实际的来** | 新代码里写 `esc(...)`,而 index.html 里那个函数叫 **`escapeHtml`** → `esc is not defined`,只在「没绑定平台」这条分支才走到,`node --check` 和其他测试全都照过。是 `platform_test.js` 真执行才抓出来的 |
| 前端也能测 | 三套 node 测试都用极简 DOM 模拟**真实执行**页面脚本,不用开浏览器:`frontend_test.js`(多会话,16)、`dashboard_test.js`(大屏,9)、`platform_test.js`(多平台/未绑定引导,16)。helper 里已给 `location` 和按 URL 分发的 `fetch` 打桩 |
| **白屏转圈的元凶** | marked.js 曾是**阻塞式** `<script src>`:它一卡(常见于端口转发的桥半死),后面的内联脚本永不执行 → 整页白屏,比报错更难查。已改 `async` + `onMarkedReady` 补排版:排版库晚到/失败也只是表格丑点,页面照常可用。**教训:前端任何阻塞式外部资源都是白屏隐患** |
| OpenAI `insufficient_quota` | key 有效但账户没余额;OpenAI 是充值制,Gemini 才有免费日额度 |
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
| 查重跨天会失效 | `_find_duplicate` 比较除 seq 外的全部字段;建广告的计划名默认含「-月日」,**同关键词跨天会被当成两单**,不算 bug 但要知道 |
| OpenAI 手动挡有轮数上限 | `ask_openai` 的工具循环最多 10 轮,超了直接回「工具调用轮数过多,已中止」。复杂任务(如一次查多层数据)可能撞上 |
| 保险丝计数器会续号 | `_REQUEST_SEQ` 重启后从「保险箱里已存待办的最大 seq」续起,所以重启后立刻确认旧待办也能通过(不会被误拦)|
| 列表接口每页写死 20 条 | `list_campaigns/ad_sets/ads` 的 limit 没暴露给 AI,固定 20;超过要靠 AI 主动翻页(返回里有 `total`/`has_next`)。另:ad_set/ad 只能按**账户**查 + 名字搜,不能按父级 id 过滤 |

## 九、当前进度 & 路线图

**已完成**(七层架构全通):
- 只读:组织/账户(已拍平)/计划/广告组/广告列表、转化事件列表、
  报表(三层汇总+自选日期+可指定账户,行内自带 name/revenue/roas,超 180 天友好拦截);
- 写操作:开启/暂停、**建广告三层**,全部走保险箱两阶段确认 + 防死循环三件套 + 幻觉钢印;
- 建计划向导:**只问必须用户定的 4 件事**(落地页→转化事件→素材→命名),
  **预算和文案由系统配默认值**(日预算 $20 / AI 按落地页代拟文案),
  但必须讲清"用了什么、为什么、想改直接说";返回里的 `defaults_used` 列出哪些是系统定的,
  📎按钮 / 直接粘贴图片上传中转,命名默认「关键词-月日」,建好默认全 OFF;
- 前端:全屏 UI(渐变主题/头像气泡/快捷提问/动画)、Markdown 表格渲染、输入法回车修复、
  **聊天记录按账号存服务器**(`data/chats/<uid>.json`,换电脑登录同一账号还在;左栏多会话)、
  **中英文切换**(顶栏 🌐,界面 + AI 回复语言一起切,选择会记住)、
  并发发送保护(`busy` 标志)、失败消息打"未送达"标记、请求 3 分钟超时;
- 大脑:三级火箭 + `BRAIN` 开关;工具共 15 个;
- **数据大屏**:KPI(带环比)/ 每日趋势 / 各计划对比 / 三层明细表 / **AI 投放诊断**,双语;
- 定时任务:一次性 + 每天重复,看表线程每 30 秒检查,错过 >15 分钟不补跑;
- **登录 + 多平台**:账号密码登录(加盐哈希)、聊天记录按账号存服务器(换电脑也能看到);
  登录后先进 `/platforms` 选平台,聊天页标题/副标题跟着选的平台变,没绑账号会明确挡一道并引导;
- **每人绑自己的平台账号**:token 按 user_id 存 `data/creds.json`,A 绑过 B 也蹭不到(第六之四节);
- **流式回复**:`/api/chat/stream`(SSE),边想边出字 + 播报"正在查什么",
  两条大脑路径都是手动挡工具循环(第六之五节);
- 安全:登录门 `AuthMiddleware`(未登录页面 302、接口 401)、`APP_PASSWORD` 当**注册邀请码**
  (留空=谁都能注册,分享端口/部署前必设),已关掉 `/docs`;
- 工程化:`README.md` 使用指南、五套测试(冒烟 34 + 前端 16 + 大屏 9 + 平台 22 + 流式 13)、
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
`.env` 现为 **BRAIN=auto**(免费 Gemini 优先,额度尽了切 ofox);ofox 中转已接通并实测
(`https://api.ofox.ai/v1` + `google/gemini-3.1-pro-preview`,公司通道);
Gemini 官方免费额度每日重置(北京时间下午 3~4 点);OpenAI 官方账户无余额(key 在 .env 里注释保留)。

**线上部署(2026-08-14)**:已上线到宝塔服务器(Debian 13 / Python 3.13,机器在欧洲),
Supervisor 守护、nginx 反代。细节和四条硬约束见第六之六节。

**定时任务(2026-08-05 实测跑通)**:19:34 登记 → 确认 → 19:45 看表线程自动执行 → 成功打开
`gutter-0805`(存档 `state: done, last_result: 成功`);之后 Cole 在平台手动关回 OFF(测试结束)。
顺带验证:平台上的手动改动,助手查询时能立刻反映真实状态,没有缓存问题。

**接下来(按优先级)**:
1. ~~建计划向导端到端验收~~ ✅ 已完成(2026-08-05 聊天版建成 `gutter-0805` 三层);
2. 真接第二个平台(Nextdoor / Meta):现在只是注册表占位,按第六之二节最后那三步做;
3. 补齐 Claude 那一级的工具支持(现在 `BRAIN=claude` 只能闲聊,见第八节坑表);
4. 调预算等更多写操作(需先在 qx-ad-bot 里查 update 接口的 payload 格式);
5. ~~流式回复(边想边出字)~~ ✅ 已完成(2026-08-14);把 README 推广给团队。

## 十、下次接手先做这三件事

1. **确认服务活着**:`curl -s -o /dev/null -w "%{http_code}" http://localhost:18100/login`
   → **200 = 活着**。注意别去 curl `/`:自从加了登录门,`/` 未登录时返回 **302**(跳登录页),
   那是正常的,不是挂了。连不上(000/7)才 `./start.sh`
   (后台跑要 `setsid nohup ./start.sh >> server.log 2>&1 &`);
2. **跑一遍冒烟测试**:`./venv/bin/python smoke_test.py` —— 34 项全绿说明钥匙、
   平台连通、护栏都正常,比逐个手测快得多,也能立刻发现平台规则变动;
3. **看 `git log --oneline`** 了解最近改了什么,再看本文件第八节(踩过的坑)和第九节(进度)。

**改代码的固定节奏**:说清要做什么 → 改 → **跑五套测试**(冒烟 34 / 前端 16 / 大屏 9 / 平台 22 / 流式 13)
→ 更新本文件相关章节 → 提交 git。
