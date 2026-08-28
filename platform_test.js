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

console.log("\n【4】带 &bind=1 进来:「想去绑定」不等于「真的没绑」");
{
  // 线上踩到的:已经绑好的人,只要地址里带着 bind=1(平台页点「先绑定账号」进来,
  // 或者把这个地址刷新/收藏了),就被**永久锁住** —— 顶栏明明显示着账户名,
  // 输入框却说"还没绑定"。根子是 showNotBound 里无条件停用了输入框。
  const app = boot({}, { search: "?platform=newsbreak&bind=1", platforms: FAKE(true) });
  await tick(); await tick();
  const bubble = app.registry["messages"].children.map(allText).join("");
  t("已绑定的人不会被告知「还没绑定」", bubble.indexOf("⚠️") < 0, bubble.slice(0, 36));
  t("输入框照样能用", app.registry["input"].disabled !== true);
  t("发送键照样能用", app.registry["send"].disabled !== true);
  t("而是直接把账户弹窗打开(那才是他想做的事)",
    app.registry["acct-overlay"].classList.contains("show"));

  // 反过来:真没绑的人带 bind=1 进来,该挡的一样要挡
  const app2 = boot({}, { search: "?platform=newsbreak&bind=1", platforms: FAKE(false) });
  await tick(); await tick();
  const b2 = app2.registry["messages"].children.map(allText).join("");
  t("真没绑的人照样挡住并引导", b2.indexOf("⚠️") >= 0);
  t("真没绑时输入框要停用", app2.registry["input"].disabled === true);
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

console.log("\n【7】点素材图能放大看(缩略图才 100px,看不清画面就白给了)");
{
  const app = boot({}, { platforms: FAKE(true) });
  await tick(); await tick();
  const box = app.registry["lightbox"];
  t("默认是收起的", !box.classList.contains("show"));

  // 图片是 AI 回复渲染出来的,每次新消息都产生新节点 —— 所以必须是事件委托,
  // 给单张图绑监听的话,以后新渲染的图就点不动了
  const msgs = app.registry["messages"];
  t("消息区挂了委托监听", !!(msgs._h || {}).click);

  const img = document.createElement("img");
  img.tagName = "IMG"; img.src = "https://openadlibrary.com/api/public/assets/x.webp";
  img.alt = "广告1";
  msgs.fire("click", { target: img, stopPropagation() {} });
  await tick();
  t("点图后浮层打开了", box.classList.contains("show"));
  t("加载的是被点的那张", app.registry["lightbox-img"].src === img.src,
    app.registry["lightbox-img"].src);
  t("给了新标签页打开原图的入口", app.registry["lightbox-open"].href === img.src);

  app.registry["lightbox-close"].fire("click");
  await tick();
  t("点关闭会收起", !box.classList.contains("show"));
  t("关掉后不占着大图内存", !app.registry["lightbox-img"].src);

  // 点非图片元素不该弹出来,否则点表格文字也会炸出个浮层
  msgs.fire("click", { target: { tagName: "TD" }, stopPropagation() {} });
  await tick();
  t("点非图片不会误弹", !box.classList.contains("show"));
}

console.log("\n【8】平台选择页真的跑得起来(它是登录后第一眼看到的页面)");
{
  // 这一页此前**从没被测试执行过**。它里面有 esc()、render()、applyLang() ——
  // 正是「函数名写错」「const 提升」这类问题最容易藏身的地方,而且一旦初始化
  // 中途抛错,表现就是整页空白或按钮点了没反应,node --check 完全查不出来。
  const app = boot({}, { page: "platforms.html", platforms: FAKE(true) });
  await tick(); await tick();
  const cards = app.registry["cards"];
  t("页面初始化没中断(卡片区被填了内容)",
    (cards.innerHTML || "").length > 0 || (cards.children || []).length > 0);
  const html = String(cards.innerHTML || "") +
    (cards.children || []).map((c) => String(c.innerHTML || "") + String(c.textContent || "")).join("");
  t("已上线的平台列出来了", html.indexOf("NewsBreak") >= 0, html.slice(0, 60));
  t("没对接的平台也列出来了(标敬请期待)", html.indexOf("Nextdoor") >= 0);
  t("语言切换按钮挂上了监听", !!(app.registry["lang-toggle"]._h || {}).click);
}

console.log("\n【9】登录页真的跑得起来(没登录的人只能看到它)");
{
  // 同样从没被执行过。登录页崩了 = 谁都进不来。
  const app = boot({}, { page: "login.html" });
  await tick(); await tick();
  t("表单挂上了提交监听(不然点登录没反应)", !!(app.registry["form"]._h || {}).submit);
  t("render() 跑到了(标题被写进去了)", String(app.registry["title"]._text || "").length > 0,
    String(app.registry["title"]._text || ""));
  t("提交按钮的文案也写了", String(app.registry["submit"]._text || "").length > 0);
  // 「去注册 / 去登录」那个链接是用 innerHTML 字符串建的,模拟不解析 HTML,
  // 只能验它确实被写进去了,点击行为得靠浏览器里点一遍
  t("切换登录/注册的链接渲染了",
    String(app.registry["switch"].innerHTML || "").indexOf("<a") >= 0,
    String(app.registry["switch"].innerHTML || "").slice(0, 40));
}

console.log(`\n结果:${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);

})();
