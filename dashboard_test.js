// 最小 DOM 模拟:真实执行 dashboard 的绘图逻辑,抓出"语法合法但运行时炸"的 bug
const fs = require("fs");
const html = fs.readFileSync("static/dashboard.html", "utf8");
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).filter(s => s.trim()).pop();

const els = {};
const docHandlers = {};   // document 上挂的监听,按事件名分组

function makeEl(id0) {
  // classList 原来是空壳(toggle/contains 什么都不做)—— 那样"弹层开没开"
  // 这类判断永远测不出来,是假通过。这里给它一个真的实现。
  const set = new Set();
  const el = {
    textContent: "", className: "", style: {}, dataset: {}, title: "",
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
  // 真实 DOM 里 `el.innerHTML = ""` 会把子节点全部清掉。模拟里只当普通属性的话,
  // 重画过的内容会一层层叠在 children 里,测出来的是"新旧混在一起"的假结果。
  let _html = "";
  Object.defineProperty(el, "innerHTML", {
    get(){ return _html; },
    set(v){
      _html = String(v);
      // 真实 DOM 清空内容时,原来的子节点会被**摘出文档**,parentNode 变成 null。
      // 模拟里不断开的话,"重画之后原来那个节点还连着"就成了假象,
      // 而"选不了日期"那个 bug 恰恰就死在这上面。
      el.children.forEach(function (c) { c.parentNode = null; });
      el.children.length = 0;
    },
  });

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
  // 原来是空函数 —— 挂在 document 上的监听(比如"点外面关弹层")完全测不到。
  // 而且要**区分捕获和冒泡**:这两者跑的时机不同,正是那个"选不了日期"的 bug 的关键。
  addEventListener: (ev, fn, capture) => {
    (docHandlers[ev] || (docHandlers[ev] = [])).push({ fn: fn, capture: !!capture });
  },
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
  ; return { drawKPIs, drawTrend, drawBars, drawTable, rangeQuery, load, applyLang, renderCal, openCal,
             setLang: function(v){ lang = v; },
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

// ===== 日历要跟着语言切 =====
// 原生 <input type="date"> 的弹层只认浏览器自身的语言,页面切成英文了它还是中文
// (2026年07月 / 日一二三四五六)。所以日历是我们自己画的 —— 这几条守着它真的会跟着切。
const calText = () => {
  const walk = (el) => (el.textContent || "") + (el.children || []).map(walk).join(" ");
  return walk(els["cal"]);
};

check("中文下日历是中文", () => {
  api.setLang("zh");
  els["custom-btn"].fire("click");           // 打开弹层会初始化日历
  const txt = calText();
  assert(txt.indexOf("年") >= 0, "月份标题不是中文:" + txt.slice(0, 40));
  assert(txt.indexOf("日") >= 0 && txt.indexOf("六") >= 0, "星期名不是中文");
});

check("切成英文后,日历里的中文全部消失", () => {
  api.setLang("en");
  api.applyLang();
  const txt = calText();
  assert(!/[\u4e00-\u9fa5]/.test(txt), "日历里还有中文:" + txt.replace(/\s+/g, " ").slice(0, 60));
  assert(/January|February|March|April|May|June|July|August|September|October|November|December/.test(txt),
         "没有英文月份名:" + txt.slice(0, 60));
  assert(txt.indexOf("Su") >= 0 && txt.indexOf("Sa") >= 0, "星期名没换成英文");
});

check("点日历上的某一天会填进输入框", () => {
  api.setLang("zh"); api.applyLang();
  const days = els["cal"].children.filter((c) => c.className === "cal-grid")[0]
                 .children.filter((c) => String(c.className).indexOf("cal-day") === 0);
  assert(days.length === 42, "日历格子数不对:" + days.length);
  const target = days.find((d) => String(d.className).indexOf("out") < 0);
  target.fire("click");
  assert(els["date-from"].value === target.dataset.d,
         "点了没填进去:" + els["date-from"].value + " vs " + target.dataset.d);
});

// ===== 开始日期比结束日期还晚 =====
// 线上实测:自定义里选出「开始 2026-03-17 / 结束 2026-02-02」,弹层一声不吭。
// 后端 `_resolve_range()` 其实会替他调过来,所以标题写的是「02-02 ~ 03-17」——
// **但那两个输入框还留着反的那一对**,标题和框里两组数字自相矛盾,
// 用户只能怀疑是不是坏了。要在他还看得见的时候换过来,并且说一句。
//
// 这两条跑完都要把焦点还给「开始日期」—— 它是模块级的 `CAL_TARGET`,
// **不还的话后面几条测试全会去填错的框**(实测连坐红了三条)。

function openDatePop() {
  els["date-pop"].classList.remove("show");     // 它是 toggle,先确保是关着的
  els["custom-btn"].fire("click");
}
function focusBackToStart() {
  els["date-from"].fire("click");               // CAL_TARGET 还原成「开始日期」
}

check("选出「开始比结束晚」→ 当场换过来,并且说一句", () => {
  api.setLang("zh"); api.applyLang();
  openDatePop();
  els["date-from"].value = "2026-03-17";
  els["date-to"].value = "2026-03-17";
  els["date-to"].fire("click");                 // 焦点给「结束日期」,日历翻到 2026-03
  const days = els["cal"].children.filter((c) => c.className === "cal-grid")[0]
                 .children.filter((c) => String(c.className).indexOf("cal-day") === 0);
  const early = days.find((d) => String(d.className).indexOf("out") < 0 &&
                                 d.dataset.d < "2026-03-17");
  assert(early, "这个月里找不到比 2026-03-17 更早的一天,测试前提不成立");
  early.fire("click");

  assert(els["date-from"].value === early.dataset.d,
         "换过来之后,刚点的那天该落在「开始日期」里:" + els["date-from"].value);
  assert(els["date-to"].value === "2026-03-17",
         "原来的开始日期该挪到「结束日期」:" + els["date-to"].value);
  assert(els["date-err"].classList.contains("show"),
         "换了却一声不吭 —— 用户不知道为什么两个框自己动了");
  assert(els["date-err"].classList.contains("note"),
         "按「错误」标红了:事情已经替他办好了,标红会吓人");
  focusBackToStart();
});

check("点「确定」那一层也兜得住,而且先别关弹层", () => {
  api.setLang("zh"); api.applyLang();
  openDatePop();
  els["date-from"].value = "2026-08-20";
  els["date-to"].value = "2026-08-01";
  els["date-ok"].fire("click");
  assert(els["date-from"].value === "2026-08-01" && els["date-to"].value === "2026-08-20",
         "确定那一层没兜住:" + els["date-from"].value + " ~ " + els["date-to"].value);
  assert(els["date-pop"].classList.contains("show"),
         "换过来就把弹层关了 —— 用户只看到范围自己变了,不知道为什么");
  focusBackToStart();
});

check("不再用浏览器原生的 date 输入框(那个换不了语言)", () => {
  // **先剥掉注释再查**:注释里正当地写着"为什么不用 <input type=date>",
  // 直接搜会把说明文字也算进去(这坑项目里踩过好几次:docstring、CSS 注释、JS 注释)
  const html = fs.readFileSync("static/dashboard.html", "utf8")
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n").filter((ln) => !ln.trim().startsWith("//")).join("\n");
  assert(html.indexOf('type="date"') < 0, "还留着 <input type=\"date\">,它的弹层永远跟着浏览器语言走");
});

// ===== 点日历上的某一天,弹层不能被关掉 =====
// 线上实测:「根本选不了时间,一点日历就关了」。成因是点某天时它自己的 onclick
// 会 renderCal() 重画,重画第一步就把刚点的按钮从 DOM 上摘掉;等事件冒泡到
// document 时它的 parentNode 已是 null,"点外面就关闭"那段顺着往上找不到弹层,
// 于是判成点了外面。解法是把那段挂到**捕获阶段**(在目标自己的 onclick 之前跑)。
// 模拟不解析 HTML,所以 getElementById 拿到的都是彼此无关的孤立节点。
// 而"点的是不是弹层里面"正是靠 parentNode 一路往上找判断的 —— 
// 这里得把 markup 里的真实层级补上:#cal 在 #date-pop 里面。
function wireMarkup() {
  els["cal"].parentNode = els["date-pop"];
  els["date-from"].parentNode = els["date-pop"];
  els["date-to"].parentNode = els["date-pop"];
}

function realClick(el) {
  const hs = docHandlers["click"] || [];
  hs.filter((h) => h.capture).forEach((h) => h.fn({ target: el }));   // 捕获:先跑
  el.fire("click");                                                   // 目标自己
  hs.filter((h) => !h.capture).forEach((h) => h.fn({ target: el }));  // 冒泡:后跑
}
const dayCells = () =>
  els["cal"].children.filter((c) => c.className === "cal-grid")[0]
    .children.filter((c) => String(c.className).indexOf("cal-day") === 0);

check("点日历上的某一天,弹层还开着(不是一点就关)", () => {
  api.setLang("zh"); api.setCustom(null);
  api.setData(fakeData);
  els["date-pop"].classList.remove("show");   // 它是 toggle,先确保是关着的
  els["custom-btn"].fire("click");
  wireMarkup();
  assert(els["date-pop"].classList.contains("show"), "前置:弹层没打开");
  const cell = dayCells().find((d) => String(d.className).indexOf("out") < 0);
  const want = cell.dataset.d;
  realClick(cell);
  assert(els["date-pop"].classList.contains("show"), "点了一天就把弹层关了 —— 等于选不了日期");
  assert(els["date-from"].value === want, "日期没填进去:" + els["date-from"].value);
});

check("连点两天(改主意)也不会被关掉", () => {
  const cells = dayCells().filter((d) => String(d.className).indexOf("out") < 0);
  realClick(cells[5]);
  assert(els["date-pop"].classList.contains("show"), "第二次点就关了");
  assert(els["date-from"].value === cells[5].dataset.d, "第二次没改成新日期");
});

check("翻月份也不会被关掉", () => {
  const nav = els["cal"].children.filter((c) => c.className === "cal-head")[0]
                .children.filter((c) => c.className === "cal-nav");
  realClick(nav[1]);          // 下一月
  assert(els["date-pop"].classList.contains("show"), "翻个月就把弹层关了");
});

check("点弹层外面才关", () => {
  const outside = doc.getElementById("kpis");
  realClick(outside);
  assert(!els["date-pop"].classList.contains("show"), "点外面没关掉");
});

check("看得出日历正在给哪个框选日期", () => {
  els["date-pop"].classList.remove("show");
  els["custom-btn"].fire("click");
  wireMarkup();
  assert(els["date-from"].classList.contains("picking"), "默认没标出在选「开始日期」");
  assert(!els["date-to"].classList.contains("picking"), "两个都亮着,反而看不出选哪个");
  realClick(els["date-to"]);
  assert(els["date-to"].classList.contains("picking"), "点了「结束日期」没跟着切");
  assert(!els["date-from"].classList.contains("picking"), "上一个没熄掉");
});

check("点上/下月的灰格子,日历跟着翻过去", () => {
  els["date-from"].value = "2026-07-15";
  api.openCal("date-from");
  const out = dayCells().find((c) => String(c.className).indexOf("out") >= 0);
  const want = out.dataset.d;                       // 形如 2026-06-28
  realClick(out);
  const title = els["cal"].children.filter((c) => c.className === "cal-head")[0]
                  .children.filter((c) => c.className === "cal-title")[0].textContent;
  const m = Number(want.split("-")[1]);
  assert(title.indexOf(m + "月") >= 0, "没翻到那一月,用户看不到自己选中了什么:" + title);
  assert(els["date-from"].value === want, "日期没填对");
});

check("按 Esc 能关掉日期弹层", () => {
  els["date-pop"].classList.remove("show");
  els["custom-btn"].fire("click");
  assert(els["date-pop"].classList.contains("show"), "前置:没打开");
  (docHandlers["keydown"] || []).forEach((h) => h.fn({ key: "Escape" }));
  assert(!els["date-pop"].classList.contains("show"), "Esc 关不掉");
});

console.log(`\n结果:${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
