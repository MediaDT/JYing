/**
 * 多平台流程测试 —— 真实执行聊天页脚本,验证"选了哪个平台"这条链路。
 *
 * 用法(在 my-agent 目录下):
 *     node platform_test.js
 *
 * 为什么必须真执行:`node --check` 只查语法,查不出
 * "变量没定义 / const 提前引用"这类运行期错误 —— 而那正是
 * CLAUDE.md 里记过两次的「点按钮没反应」根因。
 */
const boot = require("./frontend_test_helper.js");

let pass = 0, fail = 0;
const t = (name, ok, extra = "") => {
  ok ? pass++ : fail++;
  console.log(`  ${ok ? "✅" : "❌"} ${name}${extra ? " → " + extra : ""}`);
};

// 假的 /api/platforms 返回,模仿后端 public_list()
const FAKE = (bound) => ({
  default: "newsbreak",
  platforms: [
    { id: "newsbreak", name: "NewsBreak", icon: "📰", status: "ready", bound: bound,
      desc_zh: "美国本地新闻 App", desc_en: "US local news app",
      bind_hint_zh: "去 Ad Manager 生成 token", bind_hint_en: "Generate a token in Ad Manager" },
    { id: "nextdoor", name: "Nextdoor", icon: "🏘️", status: "coming", bound: false,
      desc_zh: "对接开发中", desc_en: "In progress", bind_hint_zh: "", bind_hint_en: "" },
  ],
});

// fetch 是异步的,等一拍让 .then 里的回调跑完
const tick = () => new Promise((r) => process.nextTick(r));

// 把一个节点连同子孙的文字全捞出来 —— addBubble 建的气泡文字在子节点里,
// 只看顶层的 innerHTML 是空的,会得出"没显示"的错误结论
const allText = (el) =>
  (el.innerHTML || "") + (el.textContent || "") +
  (el.children || []).map(allText).join("");

(async () => {

console.log("【0】默认进入(不带参数):页面初始化不能中断");
{
  const app = boot({}, { platforms: FAKE(true) });
  await tick(); await tick();
  t("账户按钮挂上了监听(证明脚本跑到了底)", !!(app.registry["acct-btn"]._h || {}).click);
  t("切换平台按钮存在", !!app.registry["platform-btn"]);
  t("副标题填了平台名,没留 {p} 占位符",
    app.registry["app-sub"].textContent.indexOf("{p}") === -1 &&
    app.registry["app-sub"].textContent.indexOf("NewsBreak") >= 0,
    app.registry["app-sub"].textContent);
  t("已绑定 → 输入框可用", app.registry["input"].disabled !== true);
}

console.log("\n【1】带 ?platform=newsbreak:记住选择");
{
  const store = {};
  boot(store, { search: "?platform=newsbreak", platforms: FAKE(true) });
  await tick(); await tick();
  t("平台已存进 localStorage", store["adbot-platform"] === "newsbreak", store["adbot-platform"]);
}

console.log("\n【2】平台名会跟着变(拿 Nextdoor 冒充已上线)");
{
  const fake = FAKE(true);
  fake.platforms[1].status = "ready";
  fake.platforms[1].bound = true;
  const app = boot({}, { search: "?platform=nextdoor", platforms: fake });
  await tick(); await tick();
  t("副标题显示 Nextdoor 而不是写死的 NewsBreak",
    app.registry["app-sub"].textContent.indexOf("Nextdoor") >= 0,
    app.registry["app-sub"].textContent);
}

console.log("\n【3】没绑定账号:必须挡一道并引导");
{
  const app = boot({}, { platforms: FAKE(false) });
  await tick(); await tick();
  const bubble = app.registry["messages"].children.map((c) => c.innerHTML || "").join("");
  t("聊天区出现绑定提示", bubble.indexOf("⚠️") >= 0 && bubble.indexOf("绑定") >= 0);
  t("提示里带上了平台名", bubble.indexOf("NewsBreak") >= 0);
  t("提示里带上了绑定方法", bubble.indexOf("Ad Manager") >= 0);
  t("输入框被停用(别让用户白打字)", app.registry["input"].disabled === true);
  t("发送按钮也停用", app.registry["send"].disabled === true);
}

console.log("\n【3.5】绑好账号后:界面要自动解锁,不用用户手动刷新");
{
  // 一开始没绑 → 输入框锁住;绑好之后再问 /api/platforms 就变成 bound
  let bound = false;
  const app = boot({}, {
    fetchBody: (u) => {
      if (u.indexOf("/api/platforms") === 0) return FAKE(bound);
      return { reply: "ok" };
    },
  });
  await tick(); await tick();
  t("一开始输入框是锁的", app.registry["input"].disabled === true);

  // 模拟"用户存好了 token":后端现在说已绑定,前端回查一次
  bound = true;
  app.win.refreshPlatformBinding();
  await tick(); await tick();

  t("输入框自动解锁了(不用按 F5)", app.registry["input"].disabled === false);
  t("发送键也解锁了", app.registry["send"].disabled === false);
  const html = app.registry["messages"].children.map(allText).join("");
  t("聊天区告诉用户已连接", html.indexOf("已连接") >= 0, html.slice(0, 60));
  t("⚠️ 那条提示被撤掉了", !app.registry["messages"].children.some((c) => c.id === "not-bound"));
  t("输入框提示词恢复正常", app.registry["input"].placeholder.indexOf("回车发送") >= 0,
    app.registry["input"].placeholder);
}

console.log("\n【4】带 &bind=1 进来(从平台页点「先绑定账号」)");
{
  const app = boot({}, { search: "?platform=newsbreak&bind=1", platforms: FAKE(true) });
  await tick(); await tick();
  const bubble = app.registry["messages"].children.map((c) => c.innerHTML || "").join("");
  t("即使后端说已绑定,也照样弹绑定引导", bubble.indexOf("⚠️") >= 0);
}

console.log("\n【5】英文模式:提示也要是英文");
{
  const app = boot({ "adbot-lang": "en" }, { platforms: FAKE(false) });
  await tick(); await tick();
  const bubble = app.registry["messages"].children.map((c) => c.innerHTML || "").join("");
  t("绑定提示走英文文案", bubble.indexOf("Generate a token") >= 0 || bubble.indexOf("connect") >= 0);
  t("副标题是英文", app.registry["app-sub"].textContent.indexOf("Online") >= 0,
    app.registry["app-sub"].textContent);
}

console.log("\n【6】/api/platforms 挂了:页面不能跟着崩");
{
  const app = boot({}, { fetchBody: () => { throw new Error("boom"); } });
  await tick(); await tick();
  t("聊天页照常可用(容错)", app.registry["input"].disabled !== true);
  t("按钮监听还在", !!(app.registry["acct-btn"]._h || {}).click);
}

console.log("\n【7】竞品凭据弹窗:凭据会过期,必须能随手换掉");
{
  // 这个模块在文件末尾用 const 声明,而 applyLang 在那之前就会被调一次 ——
  // 项目踩过这个坑(「点按钮没反应 = 初始化中途抛错」)。必须真跑一遍才测得出来。
  const app = boot({}, { platforms: FAKE(true),
    fetchBody: (u) => (u.indexOf("/api/competitor") === 0
      ? { configured: true, source: "公用配置", masked: "", updated_at: "" } : null) });
  await tick(); await tick();
  t("🕵️ 按钮挂上了点击监听", !!(app.registry["spy-btn"]._h || {}).click);
  t("其它按钮没被带崩", !!(app.registry["acct-btn"]._h || {}).click);

  app.registry["spy-btn"].fire("click");
  await tick(); await tick();
  t("点了会打开弹窗", app.registry["spy-overlay"].classList.contains("show"));
  t("标题渲染出来了", app.registry["spy-title"].textContent.indexOf("竞品") >= 0,
    app.registry["spy-title"].textContent);
  t("用公用配置时如实说明", app.registry["spy-status"].textContent.indexOf("公用") >= 0,
    app.registry["spy-status"].textContent);

  // Authorization 是真票据,空着直接拦下,别让用户白等一次请求
  document.getElementById("spy-auth").value = "";
  app.registry["spy-save"].fire("click");
  await tick();
  t("Authorization 空着会被拦下", app.registry["spy-msg"].textContent.indexOf("不能为空") >= 0,
    app.registry["spy-msg"].textContent);
}

console.log("\n【8】一份凭据都没有时,要如实说没有");
{
  const app = boot({}, { platforms: FAKE(true),
    fetchBody: (u) => (u.indexOf("/api/competitor") === 0 ? { configured: false } : null) });
  await tick(); await tick();
  app.registry["spy-btn"].fire("click");
  await tick();
  t("没凭据时如实说没有", app.registry["spy-status"].textContent.indexOf("还没有") >= 0,
    app.registry["spy-status"].textContent);
}

console.log(`\n结果:${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);

})();
