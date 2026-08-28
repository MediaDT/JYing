// 极简 DOM 模拟,供多会话流程测试用
const fs = require("fs");
// 当前这次 boot 的元素登记表 —— 让"动态创建的元素设了 id 之后,
// getElementById 也能找到它",跟真实 DOM 一致。不这样的话
// 代码里 `row.id = "x"` 再 `getElementById("x")` 会拿到个不相干的空壳,
// 测试就测不出"某个节点到底有没有被摘掉"。
let currentRegistry = null;
// 记下每一次写进 DOM 的文字。用来测「过程中一闪而过的东西」——
// 那种东西按时序去抓非常不稳,记账才可靠。
let textLog = [];
function makeEl(tag) {
  const el = {
    tagName: tag, className: "", innerHTML: "", title: "",
    dataset: {}, style: {}, children: [], value: "", placeholder: "",
    scrollHeight: 0, scrollTop: 0, files: [],
    get lastElementChild() { return this.children[this.children.length - 1] || null; },
    classList: { _s:new Set(), add(c){this._s.add(c)}, remove(c){this._s.delete(c)},
                 toggle(c){this._s.has(c)?this._s.delete(c):this._s.add(c)}, contains(c){return this._s.has(c)} },
    appendChild(c){ c._parent = this; this.children.push(c); return c; },
    // 按 .class 在子孙里找第一个 —— 原来永远返回 null,导致
    // 「查到某个节点再改它」这类代码在测试里静默空转,测了等于没测
    querySelector(sel){
      const want = String(sel || "").replace(/^\./, "");
      const walk = (el) => {
        for (const c of (el.children || [])) {
          if (String(c.className || "").split(/\s+/).indexOf(want) >= 0) return c;
          const hit = walk(c);
          if (hit) return hit;
        }
        return null;
      };
      const hit = walk(this);
      if (hit) return hit;
      // 页面里大量用 `el.innerHTML = "<div class=x>…"` 建内容,而模拟**不解析 HTML**,
      // 于是 children 是空的、querySelector 一律返回 null ——
      // 页面下一行 `.onclick = ...` 就崩在 null 上,测出来像是页面有 bug,其实是模拟不够真。
      // 这里退一步:只要刚写进去的 innerHTML 里出现过这个 class,就给个替身节点,
      // 让页面代码能往下走(替身也能 fire("click"),行为测得到)。
      if (want && String(this.innerHTML || "").indexOf(want) >= 0) {
        this._stubs = this._stubs || {};
        return (this._stubs[want] = this._stubs[want] || makeEl("div"));
      }
      return null;
    },
    querySelectorAll(){ return []; },
    addEventListener(ev, fn){ (this._h ||= {})[ev] = fn; },
    fire(ev, arg){ this._h && this._h[ev] && this._h[ev](arg || {stopPropagation(){}}); },
    // 真实 DOM 元素有 .click():页面代码里「点一下另一个按钮」是很常见的写法。
    // 模拟里缺了它,那段代码就静默不执行,测试会得出"这个功能没做"的错误结论。
    click(){ this.fire("click"); },
    focus(){},
    // 真实 DOM 有这两个,模拟里缺了的话页面代码一调就崩,
    // 而崩在事件回调里往往只表现为"点了没反应",很难查
    setAttribute(k, v){ this[k] = v; },
    removeAttribute(k){ delete this[k]; },
    getAttribute(k){ return this[k]; },
    remove(){
      const p = this._parent;
      if (!p) return;
      const i = p.children.indexOf(this);
      if (i >= 0) p.children.splice(i, 1);
      this._parent = null;
    },
  };
  Object.defineProperty(el, "textContent", {
    get(){ return this._text || ""; },
    set(v){ this._text = v; textLog.push({ cls: this.className || "", text: String(v) }); },
  });
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
  textLog = [];
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
  // 用 Node 原生 setTimeout 的话,1 秒防抖的代码在测试跑完之前根本来不及执行,
  // 测出来永远是"没上传"(假阴性)。这里忽略延时、下一拍就跑,
  // 让防抖逻辑也能被测到。clearTimeout 仍然要能真取消。
  const _timers = new Map();
  let _tid = 0;
  global.setTimeout = (fn, _ms) => {
    const id = ++_tid;
    _timers.set(id, true);
    setImmediate(() => {
      if (!_timers.get(id)) return;
      _timers.delete(id);
      try { fn(); } catch (e) { /* 定时回调抛错不该弄挂整个测试 */ }
    });
    return id;
  };
  global.clearTimeout = (id) => { _timers.delete(id); };
  global.TextDecoder = function () { this.decode = (v) => (v === undefined ? "" : String(v)); };
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
    // 流式接口:opts.sse 给一串事件,这里做成"一块一块读"的 ReadableStream,
    // 跟真实 SSE 一样分片到达,才能测出前端的分片拼接逻辑
    if (u.indexOf("/api/chat/stream") === 0) {
      // opts.sse 也可以是个**函数**:(url, init) => 事件数组 | Promise。
      // 这样测试就能"按会话给不同的流",甚至让某一段挂着不回 ——
      // 多段对话同时在跑的场景,非这样不能测。
      let spec = typeof opts.sse === "function" ? opts.sse(u, init) : opts.sse;
      if (!spec) return Promise.resolve({ ok: false, status: 404, headers: { get: () => "application/json" }, json: async () => ({}) });
      return Promise.resolve(spec).then((events) => {
        const frames = events.map((e) => "data: " + JSON.stringify(e) + "\n\n");
        let i = 0;
        return {
          ok: true, status: 200,
          headers: { get: (k) => (String(k).toLowerCase() === "content-type" ? "text/event-stream" : null) },
          body: { getReader: () => ({ read: () => Promise.resolve(
            i < frames.length ? { done: false, value: frames[i++] } : { done: true, value: undefined }) }) },
        };
      });
    }
    if (body === undefined || body === null) {
      if (u.indexOf("/api/platforms") === 0) body = opts.platforms || { platforms: [], default: "newsbreak" };
      else if (u.indexOf("/api/me") === 0) body = opts.me || {};
      else if (u.indexOf("/api/chats") === 0) body = opts.chats || { conversations: [] };
      else body = { reply: "ok" };
    }
    return Promise.resolve({ ok: true, status: 200,
      headers: { get: () => "application/json" }, json: async () => body });
  };

  // 默认跑聊天页;opts.page 可以指定别的页面(登录页、平台选择页也要有人真跑一遍 ——
  // 它们是用户最先看到的两页,一个运行时错误就是"点按钮没反应"或整页空白)
  const html = fs.readFileSync("/root/workspace/my-agent/static/" + (opts.page || "index.html"), "utf8");
  const js = html.match(/<script>([\s\S]*?)<\/script>/g).pop().replace(/<\/?script>/g, "");
  // 把页面脚本里的函数捞出来,好让测试能直接调(模拟"用户点了某个按钮之后")
  const exposed = {};
  new Function("__expose", js + "\n;try{__expose.refreshPlatformBinding=refreshPlatformBinding;"
               + "__expose.clearNotBound=clearNotBound;__expose.send=send;__expose.convs=()=>conversations;__expose.cached=()=>cachedConvs;__expose.push=pushToServer;__expose.history=()=>history;__expose.running=()=>running;__expose.unread=()=>unread;__expose.live=()=>({typing:liveTyping,stream:liveStream});}catch(e){}")(exposed);
  return { registry, win: exposed, textLog: () => textLog, convs: () => JSON.parse(store["adbot-conversations"] || "[]"),
           curId: () => store["adbot-current-conv"] };
};
