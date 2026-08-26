/**
 * 流式回复测试 —— 真实执行聊天页脚本,验证 SSE 那条链路。
 *
 * 用法(在 my-agent 目录下):
 *     node stream_test.js
 *
 * 覆盖:分片拼接、进度播报、开场白作废(reset)、
 *       最终用盖过钢印的文本排版、断流和报错的处理、流式不可用时退回老接口。
 */
const boot = require("./frontend_test_helper.js");

let pass = 0, fail = 0;
const t = (name, ok, extra = "") => {
  ok ? pass++ : fail++;
  console.log(`  ${ok ? "✅" : "❌"} ${name}${extra ? " → " + extra : ""}`);
};

const tick = () => new Promise((r) => setImmediate(r));
const settle = async () => { for (let i = 0; i < 30; i++) await tick(); };

const allText = (el) =>
  (el.innerHTML || "") + (el.textContent || "") +
  (el.children || []).map(allText).join("");
const screen = (app) => app.registry["messages"].children.map(allText).join(" | ");

const BOUND = {
  default: "newsbreak",
  platforms: [{ id: "newsbreak", name: "NewsBreak", icon: "📰", status: "ready",
                bound: true, desc_zh: "x", desc_en: "x", bind_hint_zh: "", bind_hint_en: "" }],
};

(async () => {

console.log("【1】正常流式:分片到达 → 拼起来 → 最终用 done 的文本");
{
  const app = boot({}, {
    platforms: BOUND,
    sse: [
      { type: "status", text: "正在拉报表数据…" },
      { type: "delta", text: "最近7天" },
      { type: "delta", text: "花了 $186" },
      { type: "done", reply: "最近7天花了 $186。\n\n🔒 系统核验" },
    ],
  });
  await settle();
  app.win.send("最近7天花了多少钱?");
  await settle();

  const txt = screen(app);
  t("最终显示的是 done 里的完整文本", txt.indexOf("最近7天花了 $186") >= 0);
  t("钢印保留下来了(不能被流式吃掉)", txt.indexOf("🔒") >= 0);
  t("没有把分片重复显示两遍",
    (txt.match(/最近7天花了 \$186/g) || []).length === 1,
    (txt.match(/最近7天花了 \$186/g) || []).length + " 次");
  t("打字气泡已收走", txt.indexOf("typing-bubble") < 0);
}

console.log("\n【2】进度播报:用户能看到「正在查什么」");
{
  const app = boot({}, {
    platforms: BOUND,
    sse: [{ type: "status", text: "正在拉报表数据…" },
          { type: "delta", text: "共 $186" },
          { type: "done", reply: "共 $186。" }],
  });
  await settle();
  app.win.send("查一下");
  await settle();

  // 进度文字是"一闪而过"的,按时序去抓极不稳定;改成查记账:
  // 只要它被写进过打字气泡上的那个 span,就说明播报链路是通的
  const noted = app.textLog().filter((r) => r.cls.indexOf("typing-note") >= 0);
  t("进度文字写进了打字气泡", noted.some((r) => r.text.indexOf("正在拉报表数据") >= 0),
    JSON.stringify(noted.map((r) => r.text)));
  t("答案照常出来了", screen(app).indexOf("共 $186") >= 0);
}

console.log("\n【3】开场白作废:模型先说话又去调工具,那段不能留");
{
  const app = boot({}, {
    platforms: BOUND,
    sse: [
      { type: "delta", text: "让我查一下…" },
      { type: "reset" },
      { type: "status", text: "正在查广告…" },
      { type: "delta", text: "共有 3 条广告" },
      { type: "done", reply: "共有 3 条广告。" },
    ],
  });
  await settle();
  app.win.send("有几条广告?");
  await settle();
  const txt = screen(app);
  t("作废的开场白没留在屏幕上", txt.indexOf("让我查一下") < 0, txt.slice(0, 60));
  t("真正的答案在", txt.indexOf("共有 3 条广告") >= 0);
}

console.log("\n【4】后端报错:要显示真实原因,不能盖成「连不上」");
{
  const app = boot({}, {
    platforms: BOUND,
    sse: [{ type: "status", text: "正在思考…" },
          { type: "error", error: "Gemini 免费额度暂时用完", code: 429 }],
  });
  await settle();
  const before = app.convs();
  app.win.send("随便问问");
  await settle();
  const txt = screen(app);
  t("显示了后端给的真实原因", txt.indexOf("额度暂时用完") >= 0, txt.slice(-70));
  t("没有误报成网络问题", txt.indexOf("连不上") < 0);
}

console.log("\n【5】流式中途断了:要明确告诉用户这条没算数");
{
  const app = boot({}, {
    platforms: BOUND,
    sse: [{ type: "delta", text: "最近7天花了" }],   // 没有 done 就结束了
  });
  await settle();
  app.win.send("查一下");
  await settle();
  const txt = screen(app);
  t("提示回复被截断了", txt.indexOf("断了") >= 0, txt.slice(-60));
  t("半截文字没被当成答案留下", txt.indexOf("最近7天花了") < 0 || txt.indexOf("断了") >= 0);
}

console.log("\n【6】流式接口不可用(旧后端 404)→ 自动退回一次性接口");
{
  const app = boot({}, {
    platforms: BOUND,
    // 没给 sse,helper 会让 /api/chat/stream 返回 404
    fetchBody: (u) => (u.indexOf("/api/chat") === 0 && u.indexOf("stream") < 0
      ? { reply: "老接口的答案" } : undefined),
  });
  await settle();
  app.win.send("查一下");
  await settle();
  t("退回老接口后照常出答案", screen(app).indexOf("老接口的答案") >= 0, screen(app).slice(-60));
}

console.log("\n【9】两段对话同时流式:各回各家,后台那段收尾不许动屏幕上这段");
{
  // 线上走的就是这条流式路径。两段同时在跑时,后台那段收尾如果无条件
  // clearLive(),会把当前开着的那段的「思考中」气泡一起抹掉,那段看着像卡死。
  const store = {
    "adbot-conversations": JSON.stringify([
      { id: "c1", title: "第一段", messages: [{ role: "user", content: "第一段" }], updatedAt: 2 },
      { id: "c2", title: "第二段", messages: [{ role: "user", content: "第二段" }], updatedAt: 1 }]),
    "adbot-current-conv": "c1" };
  let releaseC2 = null;
  const app = boot(store, { platforms: BOUND, sse: (u, init) => {
    const last = JSON.parse(init.body).messages.filter((m) => m.role === "user").pop().content;
    if (last === "问题二") return new Promise((r) => {   // 第二段挂着不回
      releaseC2 = () => r([{ type: "delta", text: "答二" }, { type: "done", reply: "答:问题二" }]);
    });
    return [{ type: "delta", text: "答一" }, { type: "done", reply: "答:问题一" }];
  } });

  const p1 = app.win.send("问题一");
  app.registry["conv-list"].children[1].fire("click");    // 生成中切到 c2
  const p2 = app.win.send("问题二");
  t("c2 屏幕上有「思考中」气泡", !!app.win.live().typing);

  await p1; await settle();
  t("c1 在后台收尾,没抹掉 c2 的气泡", !!app.win.live().typing);
  const c1 = app.convs().find((c) => c.id === "c1") || { messages: [] };
  t("c1 的回复写回了 c1", (c1.messages[c1.messages.length - 1] || {}).content === "答:问题一",
    JSON.stringify((c1.messages[c1.messages.length - 1] || {}).content));
  t("屏幕上(c2)没被串进 c1 的回复", screen(app).indexOf("答:问题一") < 0);

  releaseC2(); await p2; await settle();
  const c2 = app.convs().find((c) => c.id === "c2") || { messages: [] };
  t("c2 的回复写回了 c2", (c2.messages[c2.messages.length - 1] || {}).content === "答:问题二");
  t("c2 收尾后气泡才收走", !app.win.live().typing);
}

console.log(`\n结果:${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);

})();
