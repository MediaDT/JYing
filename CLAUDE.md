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
5. **改完代码必跑五套测试全绿才提交 git**:`./venv/bin/python smoke_test.py`(62)、`node frontend_test.js`(22)、`node dashboard_test.js`(9)、`node platform_test.js`(29)、`node stream_test.js`(13)
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
③ 工具层  agent_server.py     22个工具:13查询(含转化事件、素材推荐、图库查找、竞品广告、创意拆解、投放树) + 1报表 + 5写操作护栏 + 3定时
④ 客户端  newsbreak_client.py NewsBreak API 封装(读+写)
          creative_search.py   授权图库素材搜索(第六之十一节)
          openadlibrary_client.py 竞品广告查询(第六之十二节)
          creative_lab.py      创意拆解与方案(第六之十三节,多模态看图)
⑤ 安全层  agent_server.py     写操作"保险箱+保险丝"(第七节)+ AuthMiddleware 登录门
          accounts.py         账号/加盐哈希密码/会话/每人的聊天记录(第六之三节)
⑥ 平台层  platforms.py        投放平台注册表(NewsBreak 已通;Nextdoor/Meta 标 coming)
⑦ 配置层  .env                APP_PASSWORD(注册邀请码)+ BRAIN;每人的平台 token 在 data/creds.json
                             + 四把钥匙:GEMINI / OPENAI / ANTHROPIC / NEWSBREAK
```

其他文件:`start.sh` 一键启动;`README.md` 面向使用者的指南(给 Cole 和团队看);
五套测试:`smoke_test.py` 后端冒烟(62)+ `frontend_test.js` 多会话(22)+ `dashboard_test.js` 大屏绘图(9)+ `platform_test.js` 多平台(29)+ `stream_test.js` 流式(13),改完都要跑;`requirements.txt` + `.gitignore` 让项目可独立搬家
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
| ad | `AD-年月日-类型-NNN` | `AD-260817-Roof-001` | 这个组里的第几条广告(三位) |

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
- 我们一次只建一组一条,所以组和广告的序号固定 `001`(计划是全新的,底下不可能已有别的)。
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
| `status=active` | ✅ 有效,12892 → 1921(只看还在投的) |
| **`minDaysRunning`** | ❌ **稳定 503**,隔几秒重试三次都一样,是平台服务端的问题。**投放天数只能拿回来自己算** |
| `mediaType=image` | ❌ 无效 |

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
- **key 按人存**(`data/creds.json` 的 `openadlibrary` 段),网页顶栏 🕵️ 贴一下就生效。
  取值规则:**自己贴过用自己的,没贴过回落 `.env`** —— 和 NewsBreak 那种
  "没绑就报错、绝不回落"故意不同(那边关系到各人的钱,这边是公司一份订阅的只读查询)。
- **先验证再保存**:`oal.validate()` 打一次最小请求,验不过**不覆盖旧 key**。

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

## 六之十四、竞品 key 怎么换

换成 OpenAdLibrary 之后 key **不会过期**了,但"能随手换"这条仍然值得保留:
换人用、换套餐、key 泄露要轮换,都用得上。而且这套机制本身是通用的。

> 历史教训留档:上一个平台(Insightrackr)是浏览器登录态、几小时就失效,
> 而当时换一次要 SSH 上服务器改 `.env` 再重启 —— 重到人宁可不换、功能就荒废了。
> **会过期的凭据,换起来的成本决定了功能活不活得下去。** 这也是换平台的主因之一。

- **存哪儿**:`data/creds.json` 里这个人的 `insightrackr` 段(复用 `accounts.set_creds`,
  它本来就是通用的)。网页顶栏 **🕵️** 按钮 → 贴 Authorization(+可选 Cookie)→ 立刻生效。
- **取值规则和 NewsBreak 故意不同,别当 bug 改掉**:
  - NewsBreak:`CURRENT_CREDS` 是空 dict(登录了但没绑)→ **直接报错,绝不回落 `.env`**,
    因为那关系到各人自己的广告账户和钱;
  - OpenAdLibrary:**自己贴过就用自己的,没贴过就回落 `.env` 那份公用的** ——
    这是公司一份订阅的只读查询,没有"谁的数据"之分。
    各人贴各自的还有个额外好处:万一平台是单会话互踢,就不会互相顶掉。
  两边的注释里都写明了原因。
- **先验证再保存**(`ir.validate()`):验不过就**不覆盖旧凭据**,并回 `kept_old: true`
  让前端说清楚。和存 NewsBreak token 一个规矩 —— 填错一次不该把原来能用的那份冲掉。
- **失败原因必须分得清**,否则用户只会瞎换凭据:
  | 情况 | 提示 |
  |---|---|
  | `-3106` | 登录已过期,**要换 `Authorization` 那一行**,光换 Cookie 没用 |
  | `-3108` | 没收到登录票据,Authorization 是空的或填错了 |
  | HTTP 5xx | **平台自己不可用,不是凭据问题** —— 实测遇到过 504,混报会让人白折腾 |
  | 含非 ASCII | 凭据里混进中文/全角字符。HTTP 头只认 latin-1,不提前挡的话 httpx 抛 `'ascii' codec can't encode`,那是天书 |
- 工具报错时的 `note` 会告诉 AI:**让用户点 🕵️ 贴一下就行,别叫他去改配置文件。**
- 前端那个模块在 `index.html` 末尾,用 `var spyReady` 护着(理由见第八节「点按钮没反应」那条)。

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
| **Gemini 工具参数不能标裸 `list`/`dict`** | Gemini 从函数签名自动生成 schema,数组必须带 `items`。写成 `extra_targets: list \| None` 生成不出来 → **每次请求都 400,整条 Gemini 路径全废**。改成 `list[dict] \| None`。冒烟测试里加了一条纯查签名的检查守着 |
| **400 和 401/403 不能混报** | 原来把 `400/401/403` 一起翻译成"钥匙无效",结果上面那个 schema bug 报出来是「Gemini 钥匙无效」,人会跑去反复检查钥匙,而真 bug 在代码里。**401/403 才是钥匙问题;400 INVALID_ARGUMENT 是我们自己发的请求不合法**,要照实说并附上平台原话 |
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
| **提示词管不住文案照抄** | 归纳竞品创意时,提示词里明写了「不许照抄」,实测产出的三版方案里**两版主标题是竞品原句照搬**。品牌名照抄一眼看得出,文案照抄看不出来,用户很可能直接拿去投。**必须代码层查重**(`_flag_copied`,实词重合 ≥70% 判定),和写操作护栏一个道理:别指望提示词能管住模型 |
| **多模态喂图前必须缩** | 竞品/图库原图能到 6000×4000(10MB),直接发给视觉模型又慢又贵;缩到长边 1024 后只有 139KB,而拆广告结构完全够用。另外**视觉模型同样要接力** —— 实测第一次调用就撞上 flash 的 503 高负载 |
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
- 写操作:开启/暂停、**建广告三层**,全部走保险箱两阶段确认 + 防死循环三件套 + 幻觉钢印;
- 建计划向导:**只问必须用户定的 4 件事**(落地页→转化事件→素材→命名),
  **预算和文案由系统配默认值**(日预算 $20 / AI 按落地页代拟文案),
  没素材可让 `recommend_creatives` 从**本账户历史广告**里挑效果好的复用,
  但必须讲清"用了什么、为什么、想改直接说";返回里的 `defaults_used` 列出哪些是系统定的,
  📎按钮 / 直接粘贴图片上传中转,命名按落地页类型走规范(见第六之十节),建好默认全 OFF;
- 前端:全屏 UI(渐变主题/头像气泡/快捷提问/动画)、Markdown 表格渲染、输入法回车修复、
  **聊天记录按账号存服务器**(`data/chats/<uid>.json`,换电脑登录同一账号还在;左栏多会话)、
  **中英文切换**(顶栏 🌐,界面 + AI 回复语言一起切,选择会记住)、
  并发发送保护(`busy` 标志)、失败消息打"未送达"标记、请求 3 分钟超时;
- **素材查找**:`search_stock_creatives` 从正规授权图库(Pexels/Pixabay/Openverse)找新素材,
  带质量评分和许可证;选中后 `use_found_creative` 转存进 NewsBreak 换 assetUrl(第六之十一节);
- **竞品广告查询**:`search_competitor_ads` 查同行正在投什么(OpenAdLibrary),
  带投放天数和覆盖版位数,并让 AI 总结高效广告的共同点(第六之十二节);
- **创意拆解(P0)**:`decompose_creative` 多模态看图出「素材模型」,
  `summarize_creative_patterns` 归纳套路并写出我方文案方案;**只出文字不出图**,
  且代码层查文案照抄(第六之十三节);
- 大脑:三级火箭 + `BRAIN` 开关;工具共 22 个;
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
- 工程化:`README.md` 使用指南、五套测试(冒烟 62 + 前端 22 + 大屏 9 + 平台 29 + 流式 13)、
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
2. **跑一遍冒烟测试**:`./venv/bin/python smoke_test.py` —— 62 项全绿说明钥匙、
   平台连通、护栏都正常,比逐个手测快得多,也能立刻发现平台规则变动;
3. **看 `git log --oneline`** 了解最近改了什么,再看本文件第八节(踩过的坑)和第九节(进度)。

**改代码的固定节奏**:说清要做什么 → 改 → **跑五套测试**(冒烟 62 / 前端 22 / 大屏 9 / 平台 29 / 流式 13)
→ 更新本文件相关章节 → 提交 git。
