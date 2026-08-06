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

console.log(`\n结果:${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
