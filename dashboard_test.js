// 最小 DOM 模拟:真实执行 dashboard 的绘图逻辑,抓出"语法合法但运行时炸"的 bug
const fs = require("fs");
const html = fs.readFileSync("static/dashboard.html", "utf8");
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).filter(s => s.trim()).pop();

function makeEl(id) {
  const el = {
    id, innerHTML: "", textContent: "", className: "", style: {}, dataset: {}, title: "",
    children: [], classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    appendChild(c){ this.children.push(c); return c; },
    addEventListener(){}, setAttribute(){}, remove(){},
  };
  return el;
}
const els = {};
const doc = {
  getElementById: (id) => els[id] || (els[id] = makeEl(id)),
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
  var fetch = function () { return Promise.resolve({ json: () => Promise.resolve(__data) }); };
  var setTimeout = function (fn) { return 0; };
  var confirm = () => true;
`;
const runner = new Function("__doc", "__data", sandboxPrelude + script + `
  ; return { drawKPIs, drawTrend, drawBars, drawTable, setData: function(d){ DATA = d; } };
`);

let pass = 0, fail = 0;
function check(name, fn) {
  try { fn(); console.log("  ✅ " + name); pass++; }
  catch (e) { console.log("  ❌ " + name + " → " + e.message); fail++; }
}

console.log("【数据大屏 · 真实执行测试】");
let api;
check("脚本能加载(顶层代码不抛错)", () => { api = runner(doc, fakeData); });
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
console.log(`\n结果:${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
