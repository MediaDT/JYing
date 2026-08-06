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

console.log(`\n结果:${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);

})();
