/* 运行时测试用的迷你 DOM：只实现管理页用到的那部分，够跑起来、够做断言，没有任何依赖。
 * 故意让 innerHTML / outerHTML / insertAdjacentHTML / document.write 抛错：
 * 页面代码只要碰到就会在测试里暴露。localStorage / sessionStorage 也抛错，和 AstrBot 的
 * 沙箱 iframe（opaque origin）一致。 */
"use strict";

let activeElement = null;

class Event {
  constructor(type, init) {
    init = init || {};
    this.type = type;
    this.bubbles = init.bubbles !== false;
    this.cancelable = init.cancelable !== false;
    this.defaultPrevented = false;
    this.target = null;
    this.currentTarget = null;
    this._stop = false;
    Object.assign(this, init);
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this._stop = true; }
  stopImmediatePropagation() { this._stop = true; this._stopNow = true; }
}

class EventTarget {
  constructor() { this._listeners = {}; }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  removeEventListener(type, fn) {
    const list = this._listeners[type] || [];
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  }
  _fire(event) {
    event.currentTarget = this;
    const list = (this._listeners[event.type] || []).slice();
    for (const fn of list) {
      try {
        fn.call(this, event);
      } catch (err) {
        DOM.errors.push(err);
        console.error("[事件处理器抛错]", event.type, err && err.stack ? err.stack : err);
      }
      if (event._stopNow) break;
    }
  }
  dispatchEvent(event) {
    event.target = event.target || this;
    let node = this;
    while (node) {
      node._fire(event);
      if (event._stop || !event.bubbles) break;
      node = node.parentNode || (node === DOM.document ? DOM.window : null);
      if (node === DOM.window) {
        node._fire(event);
        break;
      }
    }
    return !event.defaultPrevented;
  }
}

class Node extends EventTarget {
  constructor() {
    super();
    this.parentNode = null;
    this.childNodes = [];
  }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get isConnected() {
    let n = this;
    while (n) {
      if (n === DOM.document) return true;
      n = n.parentNode;
    }
    return false;
  }
  contains(node) {
    while (node) {
      if (node === this) return true;
      node = node.parentNode;
    }
    return false;
  }
  _adopt(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
  }
  _toNode(x) {
    if (x instanceof Node) return x;
    return new Text(String(x));
  }
  appendChild(child) {
    if (child instanceof DocumentFragment) {
      child.childNodes.slice().forEach((c) => this.appendChild(c));
      return child;
    }
    this._adopt(child);
    this.childNodes.push(child);
    return child;
  }
  append(...items) {
    items.forEach((x) => this.appendChild(this._toNode(x)));
  }
  prepend(...items) {
    items.reverse().forEach((x) => {
      const n = this._toNode(x);
      this._adopt(n);
      this.childNodes.unshift(n);
    });
  }
  insertBefore(child, ref) {
    if (!ref) return this.appendChild(child);
    this._adopt(child);
    const i = this.childNodes.indexOf(ref);
    this.childNodes.splice(i < 0 ? this.childNodes.length : i, 0, child);
    return child;
  }
  removeChild(child) {
    const i = this.childNodes.indexOf(child);
    if (i < 0) throw new Error("removeChild: 不是子节点");
    this.childNodes.splice(i, 1);
    child.parentNode = null;
    if (activeElement && child.contains(activeElement)) {
      activeElement = null;
    }
    return child;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  replaceChildren(...items) {
    this.childNodes.slice().forEach((c) => this.removeChild(c));
    this.append(...items);
  }
  get textContent() {
    return this.childNodes.map((c) => c.textContent).join("");
  }
  set textContent(value) {
    this.childNodes.slice().forEach((c) => this.removeChild(c));
    if (value !== "" && value !== null && value !== undefined) {
      this.appendChild(new Text(String(value)));
    }
  }
}

class Text extends Node {
  constructor(data) {
    super();
    this.nodeType = 3;
    this.data = String(data);
  }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}

class DocumentFragment extends Node {}

// ---------------------------------------------------------------- 选择器

function parseCompound(text) {
  const out = { tag: null, id: null, classes: [], attrs: [], nots: [] };
  let i = 0;
  const readName = () => {
    const m = /^[A-Za-z0-9_-]+/.exec(text.slice(i));
    if (!m) throw new Error("选择器无法解析: " + text);
    i += m[0].length;
    return m[0];
  };
  if (text[i] === "*") i += 1;
  else if (/[A-Za-z]/.test(text[i] || "")) out.tag = readName().toLowerCase();
  while (i < text.length) {
    const ch = text[i];
    if (ch === "#") { i += 1; out.id = readName(); }
    else if (ch === ".") { i += 1; out.classes.push(readName()); }
    else if (ch === "[") {
      const end = text.indexOf("]", i);
      const body = text.slice(i + 1, end);
      const m = /^([A-Za-z0-9_:-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^"'\]]*)))?$/.exec(body);
      if (!m) throw new Error("属性选择器无法解析: " + body);
      const raw = m[2] !== undefined ? m[2] : m[3] !== undefined ? m[3] : m[4];
      // 值里的 CSS 转义（CSS.escape 产生的 \:）还原成字面字符，和浏览器一样按字面值比较
      out.attrs.push({ name: m[1], value: raw === undefined ? raw : raw.replace(/\\(.)/g, "$1") });
      i = end + 1;
    } else if (text.startsWith(":not(", i)) {
      let depth = 0;
      let j = i + 5;
      for (; j < text.length; j += 1) {
        if (text[j] === "(") depth += 1;
        if (text[j] === ")") { if (depth === 0) break; depth -= 1; }
      }
      out.nots.push(parseCompound(text.slice(i + 5, j)));
      i = j + 1;
    } else {
      throw new Error("不支持的选择器: " + text);
    }
  }
  return out;
}

function splitTop(sel, ch) {
  // 按顶层的分隔符切分（忽略 [] () 与引号里的）
  const parts = [];
  let depth = 0;
  let quote = null;
  let cur = "";
  for (const c of sel) {
    if (quote) { cur += c; if (c === quote) quote = null; continue; }
    if (c === '"' || c === "'") { quote = c; cur += c; continue; }
    if (c === "[" || c === "(") depth += 1;
    if (c === "]" || c === ")") depth -= 1;
    if (depth === 0 && c === ch) { parts.push(cur); cur = ""; continue; }
    cur += c;
  }
  parts.push(cur);
  return parts;
}

function parseSelector(sel) {
  return splitTop(sel, ",").map((s) => s.trim()).filter(Boolean).map((part) => {
    // 复合选择器链：[{compound, comb}]，comb 是与前一个的关系（" " 或 ">"）
    const tokens = [];
    let cur = "";
    let depth = 0;
    let quote = null;
    let pendingComb = null;
    const flush = () => {
      if (cur) { tokens.push({ c: parseCompound(cur), comb: pendingComb || " " }); cur = ""; pendingComb = null; }
    };
    for (const ch of part) {
      if (quote) { cur += ch; if (ch === quote) quote = null; continue; }
      if (ch === '"' || ch === "'") { quote = ch; cur += ch; continue; }
      if (ch === "[" || ch === "(") depth += 1;
      if (ch === "]" || ch === ")") depth -= 1;
      if (depth === 0 && (ch === " " || ch === ">")) {
        flush();
        if (ch === ">") pendingComb = ">";
        continue;
      }
      cur += ch;
    }
    flush();
    return tokens;
  });
}

function matchCompound(el, c) {
  if (c.tag && el.localName !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  for (const cls of c.classes) if (!el.classList.contains(cls)) return false;
  for (const a of c.attrs) {
    if (!el.hasAttribute(a.name)) return false;
    if (a.value !== undefined && el.getAttribute(a.name) !== a.value) return false;
  }
  for (const n of c.nots) if (matchCompound(el, n)) return false;
  return true;
}

function matchChain(el, chain) {
  // 从最后一个复合选择器往前匹配
  let i = chain.length - 1;
  if (!matchCompound(el, chain[i].c)) return false;
  function up(node, idx) {
    // node 已匹配 chain[idx]；检查 chain[idx-1] 的关系
    if (idx === 0) return true;
    const comb = chain[idx].comb;
    let anc = node.parentNode;
    while (anc && anc.nodeType === 1) {
      if (matchCompound(anc, chain[idx - 1].c) && up(anc, idx - 1)) return true;
      if (comb === ">") return false;
      anc = anc.parentNode;
    }
    return false;
  }
  return up(el, i);
}

// ---------------------------------------------------------------- 元素

class NodeList {
  constructor(items) {
    items.forEach((x, i) => { this[i] = x; });
    Object.defineProperty(this, "length", { value: items.length });
  }
  item(i) { return this[i] || null; }
  forEach(fn, thisArg) { for (let i = 0; i < this.length; i += 1) fn.call(thisArg, this[i], i, this); }
  [Symbol.iterator]() { return Array.prototype[Symbol.iterator].call(this); }
  entries() { return Array.prototype.entries.call(this); }
  keys() { return Array.prototype.keys.call(this); }
  values() { return Array.prototype.values.call(this); }
}

const BOOLEAN_ATTRS = new Set(["hidden", "disabled", "checked", "selected", "multiple", "readonly", "required"]);

class Element extends Node {
  constructor(tag, ns) {
    super();
    this.nodeType = 1;
    this.namespaceURI = ns || "http://www.w3.org/1999/xhtml";
    this.localName = ns ? tag : tag.toLowerCase();
    this.tagName = ns ? tag : tag.toUpperCase();
    this._attrs = new Map();
    this.style = {};
    this._value = "";
    this._checked = false;
    this._selected = false;
    this._files = null;
    this.scrollTop = 0;
    this.inert = false;
    this.selectionStart = 0;
    this.selectionEnd = 0;
    const self = this;
    this.classList = {
      add(...c) { const s = new Set(self._classes()); c.forEach((x) => s.add(x)); self._setClasses([...s]); },
      remove(...c) { const s = new Set(self._classes()); c.forEach((x) => s.delete(x)); self._setClasses([...s]); },
      toggle(c, force) {
        const has = self._classes().includes(c);
        const want = force === undefined ? !has : Boolean(force);
        if (want && !has) this.add(c);
        if (!want && has) this.remove(c);
        return want;
      },
      contains(c) { return self._classes().includes(c); },
    };
  }
  _classes() { return (this._attrs.get("class") || "").split(/\s+/).filter(Boolean); }
  _setClasses(list) { this._attrs.set("class", list.join(" ")); }
  get attributes() { return Array.from(this._attrs.entries()).map(([name, value]) => ({ name, value })); }
  get className() { return this._attrs.get("class") || ""; }
  set className(v) { this._attrs.set("class", String(v)); }
  get id() { return this._attrs.get("id") || ""; }
  set id(v) { this._attrs.set("id", String(v)); }
  setAttribute(name, value) {
    name = String(name).toLowerCase();
    if (name === "value" && this.localName === "input") this._value = String(value);
    this._attrs.set(name, String(value));
  }
  getAttribute(name) {
    name = String(name).toLowerCase();
    return this._attrs.has(name) ? this._attrs.get(name) : null;
  }
  hasAttribute(name) { return this._attrs.has(String(name).toLowerCase()); }
  removeAttribute(name) { this._attrs.delete(String(name).toLowerCase()); }
  get hidden() { return this._attrs.has("hidden"); }
  set hidden(v) { if (v) this._attrs.set("hidden", ""); else this._attrs.delete("hidden"); }
  get disabled() { return this._attrs.has("disabled"); }
  set disabled(v) { if (v) this._attrs.set("disabled", ""); else this._attrs.delete("disabled"); }
  get tabIndex() {
    if (this._attrs.has("tabindex")) return parseInt(this._attrs.get("tabindex"), 10);
    return ["button", "input", "select", "textarea", "a"].includes(this.localName) ? 0 : -1;
  }
  set tabIndex(v) { this._attrs.set("tabindex", String(v)); }
  get type() { return this._attrs.get("type") || (this.localName === "input" ? "text" : ""); }
  get checked() { return this._checked; }
  set checked(v) { this._checked = Boolean(v); }
  get selected() { return this._selected; }
  set selected(v) { this._selected = Boolean(v); }
  get indeterminate() { return false; }
  set indeterminate(v) {}
  get multiple() { return this.hasAttribute("multiple"); }
  set multiple(v) { if (v) this.setAttribute("multiple", ""); }
  get files() { return this._files || []; }
  set files(v) { this._files = v; }
  get value() {
    if (this.localName === "select") {
      const opts = this._all("option");
      const picked = opts.find((o) => o._selected) || opts[0];
      return picked ? picked.value : "";
    }
    if (this.localName === "option") {
      return this._attrs.has("value") ? this._attrs.get("value") : this.textContent;
    }
    if (this.localName === "textarea") return this._value;
    return this._value;
  }
  set value(v) {
    v = String(v);
    if (this.localName === "select") {
      this._all("option").forEach((o) => { o._selected = o.value === v; });
      return;
    }
    if (this.localName === "option") { this._attrs.set("value", v); return; }
    this._value = v;
    this.selectionStart = this.selectionEnd = v.length;
  }
  get rows() { return parseInt(this._attrs.get("rows") || "2", 10); }
  set rows(v) { this._attrs.set("rows", String(v)); }
  get innerHTML() { throw new Error("页面不应读取 innerHTML"); }
  set innerHTML(v) { throw new Error("页面不应设置 innerHTML（XSS 风险）: " + String(v).slice(0, 60)); }
  get outerHTML() { throw new Error("页面不应读取 outerHTML"); }
  set outerHTML(v) { throw new Error("页面不应设置 outerHTML"); }
  insertAdjacentHTML() { throw new Error("页面不应调用 insertAdjacentHTML"); }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  get firstElementChild() { return this.children[0] || null; }
  getClientRects() { return this.isConnected && !this._hiddenChain() ? [{ width: 1, height: 1 }] : []; }
  _hiddenChain() {
    let n = this;
    while (n && n.nodeType === 1) {
      if (n.hidden) return true;
      n = n.parentNode;
    }
    return false;
  }
  setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; }
  select() { this.selectionStart = 0; this.selectionEnd = String(this._value).length; }
  focus() {
    if (!this.isConnected) return;
    // 浏览器行为：inert 子树、disabled 元素、hidden 链上的元素不能获得焦点
    let n = this;
    while (n && n.nodeType === 1) {
      if (n.inert) return;
      n = n.parentNode;
    }
    if (this.disabled || this._hiddenChain()) return;
    const was = activeElement;
    if (was === this) return;
    activeElement = this;
    if (was) was._fire(Object.assign(new Event("blur", { bubbles: false }), { target: was }));
    this._fire(Object.assign(new Event("focus", { bubbles: false }), { target: this }));
    this.dispatchEvent(new Event("focusin"));
  }
  blur() { if (activeElement === this) activeElement = null; }
  click() {
    if (this.disabled) return;
    if (this.localName === "input" && this.type === "checkbox") this._checked = !this._checked;
    const ev = new Event("click");
    ev.target = this;
    this.dispatchEvent(ev);
    if (ev.defaultPrevented) return;
    if (this.localName === "input" && this.type === "checkbox") {
      const c = new Event("change");
      c.target = this;
      this.dispatchEvent(c);
    }
    if (this.localName === "button" && (this.getAttribute("type") || "submit") === "submit") {
      let f = this.parentNode;
      while (f && f.localName !== "form") f = f.parentNode;
      if (f) {
        const s = new Event("submit");
        s.target = f;
        f.dispatchEvent(s);
      }
    }
  }
  querySelectorAll(sel) { return new NodeList(this._all(sel)); }
  _all(sel) {
    const groups = parseSelector(sel);
    const out = [];
    const walk = (node) => {
      node.childNodes.forEach((c) => {
        if (c.nodeType === 1) {
          if (groups.some((g) => matchChain(c, g))) out.push(c);
          walk(c);
        }
      });
    };
    walk(this);
    return out;
  }
  querySelector(sel) { return this._all(sel)[0] || null; }
  matches(sel) { return parseSelector(sel).some((g) => matchChain(this, g)); }
  closest(sel) {
    let n = this;
    while (n && n.nodeType === 1) {
      if (n.matches(sel)) return n;
      n = n.parentNode;
    }
    return null;
  }
  // 方便断言：可见文字
  get text() { return this.textContent; }
}

class Document extends Node {
  constructor() {
    super();
    this.nodeType = 9;
    this.documentElement = new Element("html");
    this.documentElement.parentNode = this;
    this.childNodes.push(this.documentElement);
    this.head = new Element("head");
    this.body = new Element("body");
    this.documentElement.appendChild(this.head);
    this.documentElement.appendChild(this.body);
    this.title = "";
  }
  createElement(tag) { return new Element(tag); }
  createElementNS(ns, tag) { return new Element(tag, ns); }
  createTextNode(t) { return new Text(t); }
  getElementById(id) {
    const hit = this.documentElement._all("[id]").find((e) => e.id === id);
    return hit || null;
  }
  get activeElement() { return activeElement || this.body; }
  write() { throw new Error("页面不应调用 document.write"); }
  querySelector(sel) { return this.documentElement.querySelector(sel); }
  querySelectorAll(sel) { return this.documentElement.querySelectorAll(sel); }
}

const DOM = { errors: [], document: null, window: null, Event, Node, Element, Text };

function install(globalObj, opts) {
  opts = opts || {};
  const document = new Document();
  DOM.document = document;
  const win = new EventTarget();
  DOM.window = win;
  activeElement = null;
  Object.assign(win, {
    document,
    Node,
    Event,
    CSS: { escape: (s) => String(s).replace(/([^A-Za-z0-9_-])/g, "\\$1") },
    matchMedia: (q) => {
      const narrow = Boolean(opts.narrow);
      const m = /max-width:\s*(\d+)px/.exec(q);
      const matches = m ? (opts.width || 1280) <= parseInt(m[1], 10) : false;
      return { matches: narrow ? true : matches, media: q, addEventListener() {}, removeEventListener() {} };
    },
    getComputedStyle: () => ({}),
    location: { href: "http://localhost/", origin: "http://localhost" },
    navigator: { userAgent: "page-runtime-test" },
  });
  Object.defineProperty(win, "localStorage", {
    get() { throw new Error("SecurityError: 沙箱 iframe 里不能用 localStorage"); },
  });
  Object.defineProperty(win, "sessionStorage", {
    get() { throw new Error("SecurityError: 沙箱 iframe 里不能用 sessionStorage"); },
  });
  Object.assign(globalObj, {
    window: win,
    document,
    Node,
    Event,
    CSS: win.CSS,
  });
  win.window = win;
  return { document, window: win };
}

module.exports = { install, DOM, Event, Node, Element, Text, NodeList };
