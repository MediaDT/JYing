/**
 * 前端流程测试 —— 用极简 DOM 模拟跑一遍多会话逻辑,不用开浏览器。
 *
 * 用法(在 my-agent 目录下):
 *     node frontend_test.js
 *
 * 覆盖:老数据迁移、会话列表渲染、新建对话、删除对话。
 * 纯本地、不联网、不碰真实广告。
 */
const boot = require("./frontend_test_helper.js");

let pass = 0, fail = 0;
const t = (name, ok, extra = "") => {
  ok ? pass++ : fail++;
  console.log(`  ${ok ? "✅" : "❌"} ${name}${extra ? " → " + extra : ""}`);
};

console.log("【0】页面初始化跑到底 + 账户按钮可用");
{
  // 这条专防「初始化中途抛错」:一旦中断,后面的事件监听就挂不上,
  // 表现是「点按钮没反应」,而 JS 语法检查抓不到这类问题。
  const app = boot({});
  const btn = app.registry["acct-btn"];
  const overlay = app.registry["acct-overlay"];
  t("账户按钮挂上了点击监听(证明初始化没中断)", !!(btn._h && btn._h.click));
  btn.fire("click");
  t("点击后弹窗打开", overlay.classList.contains("show"));
  app.registry["acct-close"].fire("click");
  t("点关闭后弹窗收起", !overlay.classList.contains("show"));
  t("输入框也挂上了监听(初始化确实走完了)", !!(app.registry["input"]._h));
}

console.log("【1】老版本单条历史 → 自动迁移,且立刻落盘");
{
  const store = { "adbot-chat-history": JSON.stringify([
    { role: "user", content: "最近90天各计划花费,用表格" },
    { role: "assistant", content: "这是报表..." }]) };
  const app = boot(store);
  const c = app.convs();
  t("迁移出 1 个会话", c.length === 1, `实际 ${c.length}`);
  t("消息完整保留", c[0] && c[0].messages.length === 2);
  t("标题取了第一句用户话", c[0] && c[0].title.includes("最近90天"), c[0] && c[0].title);
  t("新数据已落盘(刷新不会丢)", !!store["adbot-conversations"]);
  t("旧键已清理", !("adbot-chat-history" in store));
}

console.log("\n【1.5】三个工作区能独立切换");
{
  const app = boot({});
  app.registry["landing-mode"].fire("click");
  const landing = app.convs().find((c) => c.id === app.curId());
  t("能切到落地页工作室", landing && landing.mode === "landing");
  t("落地页 Tab 显示选中", app.registry["landing-mode"].classList.contains("active"));
  t("页面标题切成落地页工作室", app.registry["app-title"].textContent === "落地页工作室");
  // **空对话原地切,不新建。** 原来每点一下标签就开一段新的空对话,
  // 用户随手点两下就在左栏留下两个空壳。空的本来就没什么可带,直接换视角。
  t("空对话切工作室 = 原地切,不留空壳", app.convs().length === 1, `实际 ${app.convs().length} 段`);
  app.registry["creative-mode"].fire("click");
  const creative = app.convs().find((c) => c.id === app.curId());
  t("能继续切到素材工作室", creative && creative.mode === "creative");
  t("再切一次还是同一段", app.convs().length === 1, `实际 ${app.convs().length} 段`);
}

console.log("\n【2】已有两个会话:打开的是当前那个");
{
  const store = {
    "adbot-conversations": JSON.stringify([
      { id: "c1", title: "会话一", messages: [{ role: "user", content: "一" }], updatedAt: 2 },
      { id: "c2", title: "会话二", messages: [{ role: "user", content: "二" }], updatedAt: 1 }]),
    "adbot-current-conv": "c2" };
  const app = boot(store);
  t("当前会话是 c2", app.curId() === "c2", app.curId());
  t("左栏列出 2 个会话", app.registry["conv-list"].children.length === 2);
}

console.log("\n【3】点「新对话」:旧的留在列表,不删除");
{
  const store = {
    "adbot-conversations": JSON.stringify([
      { id: "c1", title: "要保留的对话", messages: [{ role: "user", content: "保留我" }], updatedAt: 1 }]),
    "adbot-current-conv": "c1" };
  const app = boot(store);
  app.registry["new-chat"].fire("click");
  const c = app.convs();
  t("旧会话还在", c.some((x) => x.id === "c1"), `现有 ${c.length} 个`);
  t("当前已切到新会话", app.curId() !== "c1");
}


// 下面两条要等 fetch 的回调跑完,所以放在 async 块里
const tick = () => new Promise((r) => setImmediate(r));
const settle = async () => { for (let i = 0; i < 10; i++) await tick(); };

(async () => {

console.log("\n【1.6】有内容时切工作室:必须问,不能静默丢上下文");
{
  // **这是线上踩到的死胡同的另一半。** 助手让用户「去素材工作室」,
  // 他切过去发现刚才聊的全没了 —— 原来 startNewConversation() 直接开一段空的。
  const mk = () => boot({
    "adbot-conversations": JSON.stringify([
      { id: "c1", title: "聊了一半", mode: "campaign", updatedAt: 2,
        messages: [{ role: "user", content: "帮我做落地页" },
                   { role: "assistant", content: "好的" }] }]),
    "adbot-current-conv": "c1" });

  {
    const app = mk();
    app.registry["landing-mode"].fire("click");
    t("有内容时不静默切,先弹框问", app.registry["confirm-overlay"].classList.contains("show"));
    const conv = app.convs().find((c) => c.id === "c1");
    t("问的时候一个字都还没改", conv && conv.mode === "campaign" && conv.messages.length === 2);
    t("弹框里给了「带着这段过去」这个选项", !app.registry["confirm-alt"].hidden);
    t("非破坏性操作不顶着垃圾桶图标", app.registry["confirm-icon"].hidden === true);
  }

  {
    const app = mk();
    app.registry["landing-mode"].fire("click");
    app.registry["confirm-alt"].fire("click");
    await tick();
    const c = app.convs();
    t("选「带着过去」→ 还是同一段", app.curId() === "c1" && c.length === 1, `${c.length} 段`);
    t("选「带着过去」→ 消息一条没丢", c[0].messages.length === 2);
    t("选「带着过去」→ 视角换成了落地页", c[0].mode === "landing", c[0].mode);
  }

  {
    const app = mk();
    app.registry["creative-mode"].fire("click");
    app.registry["confirm-ok"].fire("click");
    await tick();
    const c = app.convs();
    t("选「另开一段」→ 新开了一段", app.curId() !== "c1" && c.length === 2, `${c.length} 段`);
    t("选「另开一段」→ 老的原样留着", c.some((x) => x.id === "c1" && x.messages.length === 2));
    t("选「另开一段」→ 新的那段是素材工作室",
      (c.find((x) => x.id === app.curId()) || {}).mode === "creative");
  }

  {
    const app = mk();
    app.registry["landing-mode"].fire("click");
    app.registry["confirm-cancel"].fire("click");
    await tick();
    const conv = app.convs().find((c) => c.id === "c1");
    t("点「再想想」→ 什么都没变", conv && conv.mode === "campaign" && app.convs().length === 1);
  }

  {
    // 正在生成的那一段带不过去:在飞的请求带的是旧视角,半路换挡只会让人以为切了却没切
    const app = mk();
    app.win.running().set("c1", { note: "", text: "" });
    app.registry["landing-mode"].fire("click");
    t("那一段还在生成时,不给「带过去」这个选项", app.registry["confirm-alt"].hidden === true);
    t("并且说清了为什么",
      String(app.registry["confirm-text"].textContent).includes("还在生成"),
      app.registry["confirm-text"].textContent);
  }
}

console.log("\n【4】删除会话");
{
  const store = {
    "adbot-conversations": JSON.stringify([
      { id: "c1", title: "删我", messages: [{ role: "user", content: "a" }], updatedAt: 2 },
      { id: "c2", title: "留我", messages: [{ role: "user", content: "b" }], updatedAt: 1 }]),
    "adbot-current-conv": "c1" };
  const app = boot(store);
  const delBtn = app.registry["conv-list"].children[0].children.find((x) => x.className === "conv-del");

  // 删除现在是两步:点垃圾桶 → 页面内弹框里再点一次「删除」。
  // 先验"只点垃圾桶不会删" —— 少了这一步,误触就是不可逆的数据丢失。
  delBtn.fire("click");
  await tick();
  t("点垃圾桶只是弹框,还没真删", app.convs().length === 2, `剩 ${app.convs().length} 个`);
  t("弹框弹出来了", app.registry["confirm-overlay"].classList.contains("show"));
  t("弹框里写清了后果",
    String(app.registry["confirm-text"]._text || "").indexOf("找不回来") >= 0,
    String(app.registry["confirm-text"]._text || ""));

  // 先点「再想想」,确认取消是真的不删
  app.registry["confirm-cancel"].fire("click");
  await tick();
  t("点「再想想」不删", app.convs().length === 2, `剩 ${app.convs().length} 个`);
  t("取消后弹框收起", !app.registry["confirm-overlay"].classList.contains("show"));

  delBtn.fire("click");
  await tick();
  app.registry["confirm-ok"].fire("click");
  await tick();
  const c = app.convs();
  t("确认后才真删", !c.some((x) => x.id === "c1"), `剩 ${c.length} 个`);
  t("另一个还在", c.some((x) => x.id === "c2"));
  t("自动切到剩下那个", app.curId() === "c2", app.curId());

  // 按钮**不能默认隐身**。原来是 opacity:0、悬停才显形 —— 对小白等于
  // "没有这个功能",触屏设备上更是永远点不到(没有悬停这回事)。
  const css = require("fs").readFileSync("/root/workspace/my-agent/static/index.html", "utf8")
    .match(/\.conv-del\s*\{[^}]*\}/)[0];
  const base = (css.match(/opacity:\s*([\d.]+)/) || [])[1];
  t("删除按钮默认就看得见", base !== undefined && parseFloat(base) >= 0.4, `默认 opacity=${base}`);
  t("每段历史对话都带删除按钮",
    app.registry["conv-list"].children.every((it) =>
      (it.children || []).some((x) => x.className === "conv-del")));
  // 图标要真画出来。TRASH_ICON 是 const(不提升),万一被挪到 renderConvList
  // 之后声明,这里拿到的就是 undefined —— 表现是按钮空白一片
  t("按钮里是垃圾桶图标(不是空白/不是 undefined)",
    /<svg[\s\S]*<\/svg>/.test(String(delBtn.innerHTML || "")),
    String(delBtn.innerHTML || "").slice(0, 30));

  // 浏览器原生 confirm() 不能再出现:它是系统级弹条,样式不受控,
  // 而且有些浏览器允许用户勾"不再显示" —— 一勾这道确认就彻底没了,
  // 而删对话是不可逆的。
  // **先剥注释再查**:注释里正当地写着"不用浏览器自带的 confirm()",
  // 直接搜会把它算进去(这坑项目里踩过好几次)。
  const page = require("fs").readFileSync("/root/workspace/my-agent/static/index.html", "utf8")
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n").map((ln) => ln.replace(/\s\/\/.*$/, "").replace(/^\s*\/\/.*$/, "")).join("\n");
  const native = page.match(/(?<![A-Za-z0-9_$.])confirm\s*\(/g) || [];
  t("没有再用浏览器原生 confirm()", native.length === 0, `还有 ${native.length} 处`);
}

console.log("\n【5】同一个人重新登录:自己的记录不能被误删");
{
  const MINE = JSON.stringify([
    { id: "m1", title: "我自己的对话", messages: [{ role: "user", content: "我的内容" }], updatedAt: 9 },
  ]);
  const store = { "adbot-conversations": MINE, "adbot-current-conv": "m1",
                  "adbot-cache-uid": "uid-demo" };
  let pushed = null;
  const app = boot(store, {
    me: { username: "demo", id: "uid-demo" },       // 同一个人
    chats: { conversations: [] },                   // 服务器上还没同步过
    fetchBody: (u, init) => {
      if (u.indexOf("/api/chats") === 0 && init && init.method === "PUT") {
        pushed = JSON.parse(init.body); return { ok: true };
      }
      return undefined;
    },
  });
  await settle();
  t("自己的记录还在(没被当成别人的删掉)", app.convs().length === 1, `剩 ${app.convs().length} 个`);
  t("本地记录被补传到服务器", !!pushed && JSON.stringify(pushed).indexOf("我的内容") >= 0,
    pushed ? "已上传" : "(没上传)");
}

console.log("\n【6】同一台电脑换账号登录:绝不能看到/上传别人的记录");
{
  const OTHERS = JSON.stringify([
    { id: "x1", title: "别人的对话", messages: [{ role: "user", content: "别人的秘密" }], updatedAt: 9 },
  ]);
  const store = { "adbot-conversations": OTHERS, "adbot-current-conv": "x1",
                  "adbot-cache-uid": "uid-demo" };   // 缓存是 demo 的
  let pushed = null;
  const app = boot(store, {
    me: { username: "demon", id: "uid-demon" },      // 现在换成 demon 登录
    chats: { conversations: [] },                    // 他自己的服务器记录是空的
    fetchBody: (u, init) => {
      if (u.indexOf("/api/chats") === 0 && init && init.method === "PUT") {
        pushed = JSON.parse(init.body); return { ok: true };
      }
      return undefined;
    },
  });
  await settle();
  const shown = app.registry["messages"].children
    .map((c) => (c.textContent || "") + (c.innerHTML || "")).join("");
  t("没把别人的记录上传到新账号",
    !pushed || JSON.stringify(pushed).indexOf("别人的秘密") < 0,
    pushed ? JSON.stringify(pushed).slice(0, 50) : "(没上传)");
  t("屏幕上看不到别人的对话", shown.indexOf("别人的秘密") < 0);
  t("本地缓存已清掉别人的记录", !store["adbot-conversations"], store["adbot-conversations"] || "(已清)");
  t("缓存归属改成了当前账号", store["adbot-cache-uid"] === "uid-demon", store["adbot-cache-uid"]);
}

console.log("\n【X】登录后必须能把服务器上的聊天记录读回来");
{
  // 线上真实故障:服务器上明明有记录,左栏却显示"还没有对话记录";
  // 一发新消息,服务器那份就被覆盖成本地这份 —— 老记录就此消失。
  // 根因是 `conversations = remote` 给 const 赋值抛 TypeError,
  // 又正好被最外层 .catch 吞掉,整个"以服务器为准"的分支静默失效。
  const remote = [
    { id: "s1", title: "查看广告账户余额", updatedAt: 200,
      messages: [{ role: "user", content: "余额还有多少?" },
                 { role: "assistant", content: "还有 $100" }] },
    { id: "s2", title: "同行都在投什么", updatedAt: 100,
      messages: [{ role: "user", content: "同行都在投什么?" }] },
  ];
  const puts = [];
  const app = boot({ "adbot-uid": "u-demo" }, {
    me: { username: "demo", id: "u-demo" },
    fetchBody: (u, init) => {
      if (u.indexOf("/api/chats") === 0) {
        if (init && init.method === "PUT") {
          puts.push(JSON.parse(init.body).conversations);
          return { ok: true };
        }
        return { conversations: remote };
      }
      return null;
    },
  });
  await tick(); await tick(); await tick(); await tick();

  t("服务器上的记录读回来了", app.win.convs().length === 2,
    `拿到 ${app.win.convs().length} 段`);
  t("标题对得上", (app.win.convs()[0] || {}).title === "查看广告账户余额",
    String((app.win.convs()[0] || {}).title));
  t("左栏真的画出来了(不是空的)",
    app.registry["conv-list"].children.length === 2,
    `左栏 ${app.registry["conv-list"].children.length} 项`);
  t("当前这段的消息也恢复了", app.win.history().length === 2, `${app.win.history().length} 条`);

  // 最要命的一条:读不回来的话,第一条新消息会把服务器那份覆盖掉
  const before = puts.length;
  app.registry["input"].value = "新问题";
  app.registry["send"].fire("click");
  await tick(); await tick(); await tick();
  const last = puts[puts.length - 1] || app.win.convs();
  // 按 id 判,不按标题 —— 标题是从第一条消息现算的,发了新消息可能会变
  const ids = last.map((c) => c.id);
  t("发新消息不会把老记录冲掉",
    ids.indexOf("s1") >= 0 && ids.indexOf("s2") >= 0,
    `上传了 ${last.length} 段:${ids.join("/")}`);
  t("确实往服务器推了一次", puts.length > before, `PUT ${puts.length - before} 次`);
}

console.log("\n【Y】每段对话各跑各的:回复认自己的会话,不认当前打开的那段");
{
  // 这是"多线程"的核心。以前 history 全局只有一份、绑在当前会话上,
  // 所以只能靠锁住界面来保证不落错地方(切不了、新建不了、删不了)。
  // 现在 send() 一开始就把 convId 定死,回复写回**当初提问的那一段**。
  const store = {
    "adbot-conversations": JSON.stringify([
      { id: "c1", title: "第一段", messages: [{ role: "user", content: "第一段" }], updatedAt: 2 },
      { id: "c2", title: "第二段", messages: [{ role: "user", content: "第二段" }], updatedAt: 1 }]),
    "adbot-current-conv": "c1" };
  // 让后端"照着收到的问题回答",这样才验得出回复有没有配错对话
  const app = boot(store, { fetchBody: (u, init) => {
    if (u !== "/api/chat") return undefined;
    const msgs = JSON.parse(init.body).messages;
    const last = msgs.filter((m) => m.role === "user").pop();
    return { reply: "答:" + last.content };
  } });

  const p1 = app.win.send("问题一");                      // c1 跑起来,不 await
  app.registry["conv-list"].children[1].fire("click");    // ← 生成中切到 c2
  t("生成中可以切走(不再锁死界面)", app.curId() === "c2", app.curId());

  const p2 = app.win.send("问题二");                      // c2 同时也跑起来
  t("另一段能同时提问", app.win.history().length === 2, `${app.win.history().length} 条`);

  // 正在生成的那一段不许删(删了回复回来又会把它建出来,看着像删不掉)
  const c2item = app.registry["conv-list"].children[1];
  (c2item.querySelector("conv-del") || c2item.children[c2item.children.length - 1]).fire("click");
  t("正在生成的那一段挡住删除", app.convs().length === 2, `${app.convs().length} 段`);

  await Promise.all([p1, p2]);
  for (let i = 0; i < 6; i++) await tick();

  const c1 = app.convs().find((c) => c.id === "c1") || { messages: [] };
  const c2 = app.convs().find((c) => c.id === "c2") || { messages: [] };
  const lastOf = (c) => (c.messages[c.messages.length - 1] || {}).content || "";
  t("第一段拿到的是第一段的答案", lastOf(c1) === "答:问题一", lastOf(c1));
  t("第二段拿到的是第二段的答案", lastOf(c2) === "答:问题二", lastOf(c2));
  t("两段互不串", c1.messages.length === 3 && c2.messages.length === 3,
    `${c1.messages.length} / ${c2.messages.length}`);
  t("后台那段跑完会提示一句",
    app.textLog().some((x) => String(x.text).indexOf("回复已经好了") >= 0));
}

console.log("\n【Y2】切回一段还在生成的对话:进度和已经出的字要接上");
{
  // 切走时屏幕上的气泡被收走了,但状态记在 running 里(不是记在 DOM 上)——
  // 切回来照着它重画。少了这一步,用户切回去会看到一段"死掉的"对话,
  // 以为回复丢了,然后重发一遍,白烧一次额度。
  const store = {
    "adbot-conversations": JSON.stringify([
      { id: "c1", title: "第一段", messages: [{ role: "user", content: "第一段" }], updatedAt: 2 },
      { id: "c2", title: "第二段", messages: [{ role: "user", content: "第二段" }], updatedAt: 1 }]),
    "adbot-current-conv": "c2" };
  const app = boot(store, {});
  app.win.running().set("c1", { note: "正在查报表…", text: "已经出的这几个字" });

  app.registry["conv-list"].children[0].fire("click");   // 切回 c1
  const said = (kw) => app.textLog().some((x) => String(x.text).indexOf(kw) >= 0);
  t("进度文字接上了", said("正在查报表…"));
  t("已经出的字接上了", said("已经出的这几个字"));
  t("这段在忙 → 发送键禁用", app.registry["send"].disabled === true);

  app.registry["conv-list"].children[1].fire("click");   // 再切到不忙的 c2
  t("换到不忙的那段 → 发送键放开", app.registry["send"].disabled === false);
}

console.log("\n【Z】后台那段出错:错误不能悄悄消失,那句话也要还给用户");
{
  // 它当时没开着,错误气泡画出来用户也看不到 —— 存起来,切回去补给他。
  // 而且失败会把那句话从记录里撤掉,不带上的话它就凭空没了。
  const store = {
    "adbot-conversations": JSON.stringify([
      { id: "c1", title: "第一段", messages: [{ role: "user", content: "第一段" }], updatedAt: 2 },
      { id: "c2", title: "第二段", messages: [{ role: "user", content: "第二段" }], updatedAt: 1 }]),
    "adbot-current-conv": "c1" };
  const app = boot(store, { fetchBody: (u) => {
    if (u === "/api/chat") throw new Error("网络断了");
    return undefined;
  } });

  const p = app.win.send("会失败的问题");
  app.registry["conv-list"].children[1].fire("click");   // 切走
  await p; for (let i = 0; i < 4; i++) await tick();

  const c1 = app.convs().find((c) => c.id === "c1") || { messages: [] };
  t("失败的那句已撤回(方便重发)", c1.messages.length === 1, `${c1.messages.length} 条`);
  t("切走时不乱画错误气泡",
    !app.textLog().some((x) => String(x.text).indexOf("会失败的问题") >= 0 &&
                               String(x.text).indexOf("没能发出去") >= 0));

  app.registry["conv-list"].children[0].fire("click");   // 切回去
  await tick();
  t("切回来补上错误提示 + 原话",
    app.textLog().some((x) => String(x.text).indexOf("没能发出去") >= 0 &&
                              String(x.text).indexOf("会失败的问题") >= 0));
}

console.log("\n【Y3】两段同时在跑:后台那段收尾时,不许把屏幕上这段的气泡抹掉");
{
  // 真实 DOM 才犯的错:收尾时无条件 clearLive(),而屏幕上那个「思考中」
  // 气泡属于**当前开着的另一段** —— 它会被连坐抹掉,那段看着就像卡死了。
  const store = {
    "adbot-conversations": JSON.stringify([
      { id: "c1", title: "第一段", messages: [{ role: "user", content: "第一段" }], updatedAt: 2 },
      { id: "c2", title: "第二段", messages: [{ role: "user", content: "第二段" }], updatedAt: 1 }]),
    "adbot-current-conv": "c1" };
  let releaseC2 = null;
  const app = boot(store, { fetchBody: (u, init) => {
    if (u !== "/api/chat") return undefined;
    const last = JSON.parse(init.body).messages.filter((m) => m.role === "user").pop().content;
    // 让第二段一直挂着不回,这样"第一段收尾"发生时它还在屏幕上转圈
    if (last === "问题二") return new Promise((r) => { releaseC2 = () => r({ reply: "答:问题二" }); });
    return { reply: "答:" + last };
  } });

  const p1 = app.win.send("问题一");
  app.registry["conv-list"].children[1].fire("click");   // 切到 c2
  const p2 = app.win.send("问题二");                     // c2 转圈中
  t("c2 屏幕上有「思考中」气泡", !!app.win.live().typing);

  await p1; for (let i = 0; i < 4; i++) await tick();    // c1 在后台收尾
  t("c1 收尾没把 c2 的气泡抹掉", !!app.win.live().typing);
  t("c1 的答案照样写回了 c1",
    ((app.convs().find((c) => c.id === "c1") || { messages: [] }).messages.pop() || {}).content === "答:问题一");

  releaseC2(); await p2; for (let i = 0; i < 4; i++) await tick();
  t("c2 收尾后气泡才收走", !app.win.live().typing);
}

console.log("\n【Z2】切走又切回来之后才失败:那句话不能凭空消失");
{
  // 屏幕重画过,当初拿到的那一行已经"掉线",往它身上打「未送达」等于打给空气 ——
  // 用户看到的是:问题不见了、只剩一句报错,不知道该重发什么。
  const store = {
    "adbot-conversations": JSON.stringify([
      { id: "c1", title: "第一段", messages: [{ role: "user", content: "第一段" }], updatedAt: 2 },
      { id: "c2", title: "第二段", messages: [{ role: "user", content: "第二段" }], updatedAt: 1 }]),
    "adbot-current-conv": "c1" };
  const app = boot(store, { fetchBody: (u) => {
    if (u === "/api/chat") throw new Error("网络断了");
    return undefined;
  } });

  const p = app.win.send("切来切去的问题");
  app.registry["conv-list"].children[1].fire("click");   // 切走
  app.registry["conv-list"].children[0].fire("click");   // 又切回来(屏幕重画了两次)
  await p; for (let i = 0; i < 4; i++) await tick();

  t("报错里带上了原话,可以照着重发",
    app.textLog().some((x) => String(x.text).indexOf("没能发出去") >= 0 &&
                              String(x.text).indexOf("切来切去的问题") >= 0));
}

console.log("【L】回复里的域名要能点开,而追踪链接绝不许可点");
{
  // 竞品落地页表格里的域名是 `www.x.com` 这种反引号内容,marked 渲染成 <code>,
  // 本身不可点。而点一下 ClickFlare 的 /cf/click 就是一次**真实点击**,会污染
  // 那条 campaign 的统计、事后还看不出是误点的。两件事必须一起守。
  const app = boot({});
  const linkify = app.win.linkify;
  const isTracking = app.win.tracking;

  // linkifyBubble 只用到 querySelectorAll / 建节点 / 替换,搭个够用的小 DOM 就行
  const mkCode = (text) => ({
    tagName: "CODE", className: "", title: "", textContent: text,
    children: [], parentNode: null,
    querySelector() { return this.children.length ? this.children[0] : null; },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
  });
  const mkLink = (href, text) => ({
    tagName: "A", attrs: { href }, textContent: text, parentNode: null,
    getAttribute(k) { return this.attrs[k]; },
    setAttribute(k, v) { this.attrs[k] = v; },
  });
  const mkBubble = (codes, links) => {
    const parent = { replaced: [], replaceChild(nw, old) { this.replaced.push([old, nw]); } };
    (links || []).forEach((a) => { a.parentNode = parent; });
    return { _parent: parent,
             querySelectorAll(sel) { return sel === "code" ? (codes || []) : (links || []); } };
  };

  t("linkifyBubble 拿得到(说明初始化没中断)", typeof linkify === "function");

  const c1 = mkCode("www.leaffilterusa.com");
  linkify(mkBubble([c1], []));
  const a1 = c1.children[0];
  // mock 的 tagName 不像真 DOM 那样自动大写,比较时统一一下
  t("裸域名变成了链接", !!a1 && String(a1.tagName).toUpperCase() === "A");
  t("自动补上 https", !!a1 && a1.href === "https://www.leaffilterusa.com", a1 && a1.href);
  t("新标签打开,不顶掉聊天页", !!a1 && a1.target === "_blank");
  t("带 noopener(新标签打开的安全要求)", !!a1 && /noopener/.test(a1.rel || ""));

  const c2 = mkCode("https://www.homebuddy.com/roof");
  linkify(mkBubble([c2], []));
  t("整条 http(s) 地址原样可点",
    c2.children.length === 1 && c2.children[0].href === "https://www.homebuddy.com/roof");

  // 追踪链接:一个都不许变成可点的
  ["https://trk.example.com/cf/click/1",
   "https://trk.example.com/abc?cpid=aaaa",
   "https://trk.example.com/cf/tags/xyz"].forEach((u) => {
    t("认得出是追踪链接:" + u.slice(8, 40), isTracking(u) === true);
    const c = mkCode(u);
    linkify(mkBubble([c], []));
    t("没被变成可点的:" + u.slice(8, 40), c.children.length === 0);
    t("标成不可点并说明原因", c.className === "no-click" && !!c.title);
  });

  // 模型自己写的 markdown 链接若指向追踪地址,要降级成纯文字(还看得见、能复制)
  const bad = mkLink("https://trk.example.com/cf/click/1", "点这里");
  const b4 = mkBubble([], [bad]);
  linkify(b4);
  t("指向追踪地址的链接被换掉了", b4._parent.replaced.length === 1);
  const swapped = b4._parent.replaced.length ? b4._parent.replaced[0][1] : null;
  t("换成纯文字,内容没丢",
    !!swapped && String(swapped.tagName).toUpperCase() === "CODE" && swapped.textContent === "点这里");

  const ok = mkLink("/landing-pages/abc.html", "预览 A 版");
  linkify(mkBubble([], [ok]));
  t("普通链接照常,但新标签打开", ok.attrs.target === "_blank");
  t("普通链接也带 noopener", /noopener/.test(ok.attrs.rel || ""));

  // **最要紧的一条:linkify 真的被接进渲染流程了吗。**
  // 上面那些都是直接调 linkify 测的 —— 把 renderBubble 里那一行删掉,它们照样全绿。
  // 所以必须走真正的渲染入口验一次(和「闸门在 _tool_call 里,直接调函数测不出来」同一类)。
  {
    // 浏览器里 window.marked 和裸 marked 是同一个;Node 的模拟里要两边都设
    const realMarked = global.window.marked, realBare = global.marked;
    global.window.marked = global.marked = { parse: (x) => x };
    const c = mkCode("www.leaffilterusa.com");
    const bubble = { innerHTML: "", _parent: null,
                     querySelectorAll(sel) { return sel === "code" ? [c] : []; } };
    app.win.render(bubble, "随便什么文字 `www.leaffilterusa.com`");
    t("renderBubble 里真的调了 linkify(不是只有函数存在)", c.children.length === 1);
    global.window.marked = realMarked; global.marked = realBare;
  }

  // 表格里全是 id、数字、字段名 —— 别把它们当域名
  ["8,960", "landers_offers", "6620e4845b7d5a001272ec97", "cta_count", "30 天", "1.88%"]
    .forEach((x) => {
      const c = mkCode(x);
      linkify(mkBubble([c], []));
      t("没把「" + x + "」当成域名", c.children.length === 0);
    });
}

console.log(`\n结果:${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);

})();
