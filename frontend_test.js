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

console.log("\n【4】删除会话");
{
  const store = {
    "adbot-conversations": JSON.stringify([
      { id: "c1", title: "删我", messages: [{ role: "user", content: "a" }], updatedAt: 2 },
      { id: "c2", title: "留我", messages: [{ role: "user", content: "b" }], updatedAt: 1 }]),
    "adbot-current-conv": "c1" };
  const app = boot(store);
  const delBtn = app.registry["conv-list"].children[0].children.find((x) => x.className === "conv-del");
  delBtn.fire("click");
  const c = app.convs();
  t("被删的会话消失", !c.some((x) => x.id === "c1"), `剩 ${c.length} 个`);
  t("另一个还在", c.some((x) => x.id === "c2"));
  t("自动切到剩下那个", app.curId() === "c2", app.curId());
}

// 下面两条要等 fetch 的回调跑完,所以放在 async 块里
const tick = () => new Promise((r) => setImmediate(r));
const settle = async () => { for (let i = 0; i < 10; i++) await tick(); };

(async () => {

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

console.log(`\n结果:${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);

})();
