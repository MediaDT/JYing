// 极简 DOM 模拟,供多会话流程测试用
const fs = require("fs");
function makeEl(tag) {
  const el = {
    tagName: tag, className: "", textContent: "", innerHTML: "", title: "",
    dataset: {}, style: {}, children: [], value: "", placeholder: "",
    scrollHeight: 0, scrollTop: 0, files: [],
    get lastElementChild() { return this.children[this.children.length - 1] || null; },
    classList: { _s:new Set(), add(c){this._s.add(c)}, remove(c){this._s.delete(c)},
                 toggle(c){this._s.has(c)?this._s.delete(c):this._s.add(c)}, contains(c){return this._s.has(c)} },
    appendChild(c){ this.children.push(c); return c; },
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    addEventListener(ev, fn){ (this._h ||= {})[ev] = fn; },
    fire(ev, arg){ this._h && this._h[ev] && this._h[ev](arg || {stopPropagation(){}}); },
    focus(){}, remove(){},
  };
  Object.defineProperty(el, "innerHTML", {
    get(){ return this._html || ""; },
    set(v){ this._html = v; if (v === "") this.children.length = 0; },
  });
  return el;
}
module.exports = function boot(store, opts) {
  opts = opts || {};
  const registry = {};
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
    let body = { reply: "ok" };
    if (u.indexOf("/api/platforms") === 0) body = opts.platforms || { platforms: [], default: "newsbreak" };
    else if (u.indexOf("/api/me") === 0) body = opts.me || {};
    else if (opts.fetchBody) body = opts.fetchBody(u, init) || body;
    return Promise.resolve({ ok: true, status: 200, json: async () => body });
  };
  global.setTimeout = () => 0; global.clearTimeout = () => {};
  const html = fs.readFileSync("/root/workspace/my-agent/static/index.html", "utf8");
  const js = html.match(/<script>([\s\S]*?)<\/script>/g).pop().replace(/<\/?script>/g, "");
  new Function(js)();
  return { registry, convs: () => JSON.parse(store["adbot-conversations"] || "[]"),
           curId: () => store["adbot-current-conv"] };
};
