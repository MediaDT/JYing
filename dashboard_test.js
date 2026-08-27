// 最小 DOM 模拟:真实执行 dashboard 的绘图逻辑,抓出"语法合法但运行时炸"的 bug
const fs = require("fs");
const html = fs.readFileSync("static/dashboard.html", "utf8");
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).filter(s => s.trim()).pop();

const els = {};

function makeEl(id0) {
  // classList 原来是空壳(toggle/contains 什么都不做)—— 那样"弹层开没开"
  // 这类判断永远测不出来,是假通过。这里给它一个真的实现。
  const set = new Set();
  const el = {
    innerHTML: "", textContent: "", className: "", style: {}, dataset: {}, title: "",
    value: "", children: [], _h: {},
    classList: {
      add(c){ set.add(c); }, remove(c){ set.delete(c); },
      toggle(c, on){ if (on === undefined) { set.has(c) ? set.delete(c) : set.add(c); }
                     else { on ? set.add(c) : set.delete(c); } },
      contains(c){ return set.has(c); },
    },
    appendChild(c){ c.parentNode = el; this.children.push(c); return c; },
    addEventListener(ev, fn){ this._h[ev] = fn; },
    fire(ev){ if (this.onclick && ev === "click") this.onclick({ target: this });
              else if (this._h[ev]) this._h[ev]({ target: this }); },
    setAttribute(){}, remove(){},
  };
  // 真实 DOM 里 `x.id = "foo"` 之后 getElementById("foo") 就找得到它。
  // 模拟里少了这一步,动态创建的按钮在测试里永远查不到,只能得出"没这个按钮"的假结论。
  let _id = id0;
  Object.defineProperty(el, "id", {
    get(){ return _id; },
    set(v){ _id = v; if (v) els[v] = el; },
  });
  if (id0) els[id0] = el;
  return el;
}
const doc = {
  getElementById: (id) => els[id] || makeEl(id),
  createElement: (tag) => makeEl("<" + tag + ">"),
  querySelectorAll: () => [],
  addEventListener: () => {},
  documentElement: { lang: "", dataset: {} },
};
const fakeData = {
  range: { start: "2026-07-01", end: "2026-08-06", days: 37 },
  kpi:      { cost: 186.05, revenue: 4.59, impressions: 58649, clicks: 2058, conversions: 1, ctr: 3.51, cvr: 0.05, cpc: 0.09, cpm: 3.17, cpa: 186.05, roas: 0.02 },
  kpi_prev: { cost: 1080, revenue: 0, impressions: 141000, clicks: 1979, conversions: 6, ctr: 1.4, cvr: 0.3, cpc: 0.55, cpm: 7.6, cpa: 180, roas: 0.21 },
  trend: [
    { name: "2026-08-01", cost: 26.2, clicks: 503, impressions: 11982, conversions: 0 },
    { name: "2026-08-02", cost: 100.7, clicks: 1140, impressions: 22000, conversions: 1 },
    { name: "2026-08-03", cost: 15.5, clicks: 113, impressions: 5000, conversions: 0 },
  ],
  trend_days: 3, trend_capped: true,
  campaigns: [
    { id:"1", name:"NewsBreak_333_Windows_20260522", cost:137.85, revenue:4.59, impressions:48095, clicks:1860, conversions:1, ctr:3.87, cvr:0.05, cpc:0.07, cpm:2.87, cpa:137.85, roas:0.03 },
    { id:"2", name:"gutter-0601", cost:48.2, revenue:0, impressions:10554, clicks:198, conversions:0, ctr:1.88, cvr:0, cpc:0.24, cpm:4.57, cpa:-1, roas:0 },
  ],
  ad_sets: [], ads: [],
};

const sandboxPrelude = `
  var document = __doc;
  var window = { marked: null };
  var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  var location = { href: "" };
  var fetch = function (url) { __calls.push(String(url)); return Promise.resolve({ json: () => Promise.resolve(__data) }); };
  var setTimeout = function (fn) { return 0; };
  var confirm = () => true;
`;
const calls = [];      // 记下每次 fetch 的地址,用来验"查的是哪一段时间"
const runner = new Function("__doc", "__data", "__calls", sandboxPrelude + script + `
  ; return { drawKPIs, drawTrend, drawBars, drawTable, rangeQuery, load,
             setData: function(d){ DATA = d; },
             setCustom: function(c){ CUSTOM = c; },
             getCustom: function(){ return CUSTOM; } };
`);

let pass = 0, fail = 0;
function check(name, fn) {
  try { fn(); console.log("  ✅ " + name); pass++; }
  catch (e) { console.log("  ❌ " + name + " → " + e.message); fail++; }
}

console.log("【数据大屏 · 真实执行测试】");
let api;
check("脚本能加载(顶层代码不抛错)", () => { api = runner(doc, fakeData, calls); });
if (api) {
  check("设置数据", () => api.setData(fakeData));
  check("drawKPIs 能跑", () => api.drawKPIs());
  check("drawTrend 能跑(t 遮蔽 bug 就死在这)", () => api.drawTrend());
  check("drawBars 能跑", () => api.drawBars());
  check("drawTable 能跑", () => api.drawTable());
  check("趋势图真的画出了 SVG", () => {
    if (!/<svg/.test(els["trend-chart"].innerHTML)) throw new Error("trend-chart 是空的");
  });
  check("柱状图真的画出了 SVG", () => {
    if (!/<svg/.test(els["bar-chart"].innerHTML)) throw new Error("bar-chart 是空的");
  });
  check("明细表真的画出了 table", () => {
    if (!/<table/.test(els["table-box"].innerHTML)) throw new Error("table-box 是空的");
  });
}
// ===== 自定义时间范围 =====
const assert = (ok, msg) => { if (!ok) throw new Error(msg); };

check("默认按「近 N 天」查", () => {
  api.setCustom(null);
  assert(api.rangeQuery() === "days=30", "实际:" + api.rangeQuery());
});

check("选了自定义就按起止日期查(不再发 days)", () => {
  api.setCustom({ start: "2026-08-01", end: "2026-08-10" });
  const q = api.rangeQuery();
  assert(q.indexOf("start=2026-08-01") >= 0 && q.indexOf("end=2026-08-10") >= 0, "实际:" + q);
  assert(q.indexOf("days=") < 0, "还带着 days,后端会按哪个算就说不清了:" + q);
});

check("点「自定义」会把弹层打开,并预填当前范围", () => {
  api.setCustom(null);
  api.setData(fakeData);
  els["custom-btn"].fire("click");
  assert(els["date-pop"].classList.contains("show"), "弹层没打开");
  assert(els["date-from"].value === "2026-07-01", "开始日期没预填:" + els["date-from"].value);
  assert(els["date-to"].value === "2026-08-06", "结束日期没预填:" + els["date-to"].value);
});

check("日期没填全时说清楚,而且不发请求", () => {
  els["date-from"].value = "2026-08-01";
  els["date-to"].value = "";
  const before = calls.length;
  els["date-ok"].fire("click");
  assert(els["date-err"].classList.contains("show"), "没给错误提示,点了像没反应");
  assert(calls.length === before, "居然还发了请求");
  assert(els["date-pop"].classList.contains("show"), "弹层不该在填错时关掉");
});

check("两个日期都填了才真的去查", () => {
  els["date-from"].value = "2026-08-01";
  els["date-to"].value = "2026-08-10";
  const before = calls.length;
  els["date-ok"].fire("click");
  assert(calls.length > before, "没发请求");
  const last = calls[calls.length - 1];
  assert(last.indexOf("start=2026-08-01") >= 0 && last.indexOf("end=2026-08-10") >= 0, "查的不是这一段:" + last);
  assert(!els["date-pop"].classList.contains("show"), "查完弹层该收起来");
});

check("点回快捷键会清掉自定义(否则两个条件打架)", () => {
  api.setCustom({ start: "2026-08-01", end: "2026-08-10" });
  const quick = els["range-btns"].children.filter((b) => b.id !== "custom-btn")[0];
  quick.fire("click");
  assert(api.getCustom() === null, "自定义没被清掉,查出来的还是老范围");
  assert(api.rangeQuery().indexOf("days=") === 0, "实际:" + api.rangeQuery());
});

console.log(`\n结果:${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
