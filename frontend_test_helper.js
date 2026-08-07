// 极简 DOM 模拟,供多会话流程测试用
const fs = require("fs");
// 当前这次 boot 的元素登记表 —— 让"动态创建的元素设了 id 之后,
// getElementById 也能找到它",跟真实 DOM 一致。不这样的话
// 代码里 `row.id = "x"` 再 `getElementById("x")` 会拿到个不相干的空壳,
// 测试就测不出"某个节点到底有没有被摘掉"。
let currentRegistry = null;
function makeEl(tag) {
  const el = {
    tagName: tag, className: "", textContent: "", innerHTML: "", title: "",
    dataset: {}, style: {}, children: [], value: "", placeholder: "",
    scrollHeight: 0, scrollTop: 0, files: [],
    get lastElementChild() { return this.children[this.children.length - 1] || null; },
    classList: { _s:new Set(), add(c){this._s.add(c)}, remove(c){this._s.delete(c)},
                 toggle(c){this._s.has(c)?this._s.delete(c):this._s.add(c)}, contains(c){return this._s.has(c)} },
    appendChild(c){ c._parent = this; this.children.push(c); return c; },
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    addEventListener(ev, fn){ (this._h ||= {})[ev] = fn; },
    fire(ev, arg){ this._h && this._h[ev] && this._h[ev](arg || {stopPropagation(){}}); },
    focus(){},
    remove(){
      const p = this._parent;
      if (!p) return;
      const i = p.children.indexOf(this);
      if (i >= 0) p.children.splice(i, 1);
      this._parent = null;
    },
  };
  Object.defineProperty(el, "id", {
    get(){ return this._id || ""; },
    set(v){ this._id = v; if (currentRegistry && v) currentRegistry[v] = this; },
  });
  Object.defineProperty(el, "innerHTML", {
    get(){ return this._html || ""; },
    set(v){ this._html = v; if (v === "") this.children.length = 0; },
  });
  return el;
}
module.exports = function boot(store, opts) {
  opts = opts || {};
  const registry = {};
  currentRegistry = registry;
  global.localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  global.window = { marked: undefined, addEventListener(){} };
  global.document = {
    documentElement: {}, title: "",
    getElementById: (id) => (registry[id] ||= makeEl("div")),
    createElement: makeEl, querySelectorAll: () => [], addEventListener(){},
  };
  global.confirm = () => true;
  // 网址栏(带 ?platform= / ?bind=1 时用得上)
  global.location = { search: opts.search || "", href: "/", pathname: "/", assign(){}, replace(){} };
  // fetch 按 URL 分发,默认还是老行为,保证旧测试不受影响
  global.fetch = (url, init) => {
    const u = String(url || "");
    let body;
    try {
      // fetchBody 优先:测试要模拟"同一个地址前后返回不同结果"时用得上
      if (opts.fetchBody) body = opts.fetchBody(u, init);
    } catch (e) {
      // 真实的 fetch 网络失败时返回**失败的 Promise**,不是同步抛异常。
      // 模型要对,否则测不出页面的 .catch 分支到底管不管用。
      return Promise.reject(e);
    }
    if (body === undefined || body === null) {
      if (u.indexOf("/api/platforms") === 0) body = opts.platforms || { platforms: [], default: "newsbreak" };
      else if (u.indexOf("/api/me") === 0) body = opts.me || {};
      else body = { reply: "ok" };
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => body });
  };

  const html = fs.readFileSync("/root/workspace/my-agent/static/index.html", "utf8");
  const js = html.match(/<script>([\s\S]*?)<\/script>/g).pop().replace(/<\/?script>/g, "");
  // 把页面脚本里的函数捞出来,好让测试能直接调(模拟"用户点了某个按钮之后")
  const exposed = {};
  new Function("__expose", js + "\n;try{__expose.refreshPlatformBinding=refreshPlatformBinding;"
               + "__expose.clearNotBound=clearNotBound;}catch(e){}")(exposed);
  return { registry, win: exposed, convs: () => JSON.parse(store["adbot-conversations"] || "[]"),
           curId: () => store["adbot-current-conv"] };
};
