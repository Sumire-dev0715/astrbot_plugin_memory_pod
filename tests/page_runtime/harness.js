/* 运行时测试的公共部分：迷你 DOM + 假桥接（连真实的 Python 后端）+ 加载真实页面脚本 + 断言工具。
 * 后端由 tests/test_page_runtime.py 起在本机（合成数据），端口经环境变量 MP_PORT 传进来。 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const dom = require("./dom.js");

const REPO = path.resolve(__dirname, "..", "..");
const PAGE = path.join(REPO, "pages", "memory");
const I18N_DIR = path.join(REPO, ".astrbot-plugin", "i18n");

const results = { pass: 0, fail: 0, failures: [] };

function assert(cond, msg) {
  if (cond) {
    results.pass += 1;
  } else {
    results.fail += 1;
    results.failures.push(msg);
    console.log("  ✗ " + msg);
  }
  return Boolean(cond);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function until(fn, label, timeout) {
  const end = Date.now() + (timeout || 4000);
  let last;
  while (Date.now() < end) {
    try {
      last = fn();
      if (last) return last;
    } catch (err) {
      last = null;
    }
    await sleep(8);
  }
  assert(false, "等待超时: " + label);
  return null;
}

function loadLocale(name) {
  try {
    return JSON.parse(fs.readFileSync(path.join(I18N_DIR, name + ".json"), "utf-8"));
  } catch (err) {
    return {};
  }
}

function getByPath(obj, key) {
  return String(key).split(".").reduce((cur, part) => (cur && typeof cur === "object" && part in cur ? cur[part] : undefined), obj);
}

/** 直接访问后端（不经页面）：不带 body 是 GET，带 body 是 POST JSON；返回解析后的 JSON。
 *  场景里用它改服务端的数据（和别的管理员同时在改一样），或用 /__fail 安排下一次失败。 */
async function backend(urlPath, body) {
  const base = `http://127.0.0.1:${process.env.MP_PORT}`;
  const res = body === undefined
    ? await fetch(base + urlPath)
    : await fetch(base + urlPath, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return res.json();
}

/** 启动一个页面实例。opts: {locale, narrow, width, port, theme} */
async function boot(opts) {
  opts = opts || {};
  const port = opts.port || process.env.MP_PORT;
  if (!port) {
    throw new Error("缺少环境变量 MP_PORT（后端端口）");
  }
  dom.DOM.errors.length = 0;
  const g = {};
  const { document, window } = dom.install(g, { narrow: opts.narrow, width: opts.width });
  const body = document.body;
  const mk = (id, cls) => {
    const el = document.createElement("div");
    el.id = id;
    if (cls) el.className = cls;
    body.append(el);
    return el;
  };
  // 与 index.html 一致的骨架（下面会校验 index.html 里确实有这些 id）
  const app = mk("app", "app");
  app.setAttribute("aria-busy", "true");
  mk("dialogs");
  mk("toasts", "toasts");

  const locales = { "zh-CN": loadLocale("zh-CN"), "en-US": loadLocale("en-US") };
  const ctx = { locale: opts.locale || "zh-CN", i18n: locales, isDark: opts.theme === "dark" };
  document.documentElement.setAttribute("data-theme", ctx.isDark ? "dark" : "light");
  const handlers = new Set();
  const calls = [];
  const downloads = [];
  // 页面内的 Blob 下载：URL.createObjectURL 收到的内容，和被点击的 <a download>
  const blobs = [];
  const saved = [];
  // 想让某个接口的请求停在半路时用：hold(endpoint) 返回放行函数
  const gates = {};
  async function passGate(endpoint) {
    const gate = gates[endpoint];
    if (gate) {
      await gate;
    }
  }
  const holds = {};
  function hold(endpoint) {
    let release = null;
    gates[endpoint] = new Promise(function (resolve) { release = resolve; });
    holds[endpoint] = function () {
      delete gates[endpoint];
      release();
    };
    return holds[endpoint];
  }
  (opts.hold || []).forEach(hold);
  const fakeURL = {
    createObjectURL(blob) {
      if (opts.blobFails) {
        throw new Error("页面内保存不可用（测试里模拟）");
      }
      blobs.push(blob);
      return "blob:page-runtime/" + blobs.length;
    },
    revokeObjectURL() {},
  };
  const fallbackHits = new Set();
  const bridge = {
    ready: () => Promise.resolve(ctx),
    getContext: () => ctx,
    getLocale: () => ctx.locale,
    getI18n: () => locales,
    t(key, fallback) {
      for (const loc of [ctx.locale, "zh-CN", "en-US"]) {
        const v = getByPath(locales[loc], key);
        if (v !== undefined && v !== null) return typeof v === "string" ? v : String(v);
      }
      fallbackHits.add(key);
      return fallback || "";
    },
    onContext(fn) {
      handlers.add(fn);
      fn(ctx);
      return () => handlers.delete(fn);
    },
    async apiGet(endpoint, params) {
      calls.push({ method: "GET", endpoint, params: params || {} });
      await passGate(endpoint);
      const qs = new URLSearchParams(params || {}).toString();
      const res = await fetch(`http://127.0.0.1:${port}/${endpoint}${qs ? "?" + qs : ""}`);
      const data = await res.json();
      if (data && data.status === "error") throw new Error(data.message || "Plugin GET request failed.");
      return data && data.data !== undefined && data.data !== null ? data.data : data;
    },
    async apiPost(endpoint, bodyData) {
      calls.push({ method: "POST", endpoint, body: bodyData || {} });
      await passGate(endpoint);
      const res = await fetch(`http://127.0.0.1:${port}/${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(bodyData || {}),
      });
      const data = await res.json();
      if (data && data.status === "error") throw new Error(data.message || "Plugin POST request failed.");
      return data && data.data !== undefined && data.data !== null ? data.data : data;
    },
    // 和 AstrBot 面板的 files:download 一样：原样保存响应体，不检查里面的 status
    async download(endpoint, params, filename) {
      calls.push({ method: "DOWNLOAD", endpoint, params: params || {}, filename });
      await passGate(endpoint);
      const qs = new URLSearchParams(params || {}).toString();
      const res = await fetch(`http://127.0.0.1:${port}/${endpoint}${qs ? "?" + qs : ""}`);
      downloads.push({ endpoint, params, filename, body: await res.text() });
      return { filename };
    },
  };
  if (opts.noDownload) {
    delete bridge.download;
  }
  window.AstrBotPluginView = bridge;
  window.AstrBotPluginPage = bridge;
  document.addEventListener("click", function (event) {
    const link = event.target;
    if (link && link.localName === "a" && link.hasAttribute("download")) {
      const match = /(\d+)$/.exec(link.getAttribute("href") || "");
      saved.push({ filename: link.getAttribute("download"), blob: match ? blobs[Number(match[1]) - 1] : null });
    }
  });

  const sandbox = {
    window,
    document,
    Node: dom.Node,
    Event: dom.Event,
    CSS: window.CSS,
    console,
    setTimeout,
    clearTimeout,
    TextEncoder,
    Blob,
    URL: fakeURL,
    Promise,
    Uint32Array,
    JSON,
    Date,
    Math,
  };
  vm.createContext(sandbox);
  // 按 index.html 里的顺序加载脚本
  const html = fs.readFileSync(path.join(PAGE, "index.html"), "utf-8");
  const scripts = [...html.matchAll(/<script[^>]*\ssrc="\.\/([^"]+)"/g)].map((m) => m[1]);
  for (const id of ["app", "dialogs", "toasts"]) {
    assert(new RegExp(`id="${id}"`).test(html), `index.html 缺少 #${id}`);
  }
  const errors = [];
  process.removeAllListeners("unhandledRejection");
  process.on("unhandledRejection", (err) => {
    errors.push(err);
    console.log("  [unhandledRejection]", err && err.stack ? err.stack : err);
  });
  for (const file of scripts) {
    const code = fs.readFileSync(path.join(PAGE, file), "utf-8");
    vm.runInContext(code, sandbox, { filename: file });
  }
  const ctl = {
    document,
    window,
    sandbox,
    bridge,
    calls,
    downloads,
    saved,
    fallbackHits,
    errors,
    ctx,
    handlers,
    MP: window.MemoryPod,
    hold,
    release(endpoint) {
      if (holds[endpoint]) {
        holds[endpoint]();
      }
    },
    setLocale(locale) {
      ctx.locale = locale;
      handlers.forEach((fn) => fn(ctx));
    },
    $: (sel, root) => (root || document).querySelector(sel),
    $$: (sel, root) => Array.from((root || document).querySelectorAll(sel)),
    text: (el) => (el === null ? "" : (el || document.body).textContent.replace(/\s+/g, " ").trim()),
    find(sel, text, root) {
      return Array.from((root || document).querySelectorAll(sel)).find((e) => e.textContent.replace(/\s+/g, " ").includes(text)) || null;
    },
    click(el) {
      if (!el) throw new Error("click: 元素不存在");
      el.click();
    },
    input(el, value) {
      el.value = value;
      const ev = new dom.Event("input");
      ev.target = el;
      el.dispatchEvent(ev);
    },
    change(el, value) {
      if (value !== undefined) el.value = value;
      const ev = new dom.Event("change");
      ev.target = el;
      el.dispatchEvent(ev);
    },
    key(el, key, init) {
      const ev = new dom.Event("keydown", Object.assign({ key }, init || {}));
      ev.target = el;
      el.dispatchEvent(ev);
      return ev;
    },
    lastCall(endpoint) {
      for (let i = calls.length - 1; i >= 0; i -= 1) {
        if (calls[i].endpoint === endpoint) return calls[i];
      }
      return null;
    },
    async getJson(path) {
      return (await fetch(`http://127.0.0.1:${port}${path}`)).json();
    },
    setFile(el, name, text) {
      el.files = [{ text: async () => text, name }];
    },
    async peek(scope, key, file) {
      const qs = new URLSearchParams({ scope, key: key || "", file }).toString();
      return (await (await fetch(`http://127.0.0.1:${port}/__peek?${qs}`)).json());
    },
    async reset(empty) {
      await fetch(`http://127.0.0.1:${port}/__reset${empty ? "?empty=1" : ""}`);
    },
    async fail(endpoint, count) {
      await fetch(`http://127.0.0.1:${port}/__fail?endpoint=${encodeURIComponent(endpoint)}&count=${count || 1}`);
    },
    async settle() {
      await sleep(40);
    },
    uncaught: () => dom.DOM.errors.concat(errors),
  };
  return ctl;
}

function section(name) {
  console.log("\n● " + name);
}

function summary() {
  console.log(`\n== 通过 ${results.pass}，失败 ${results.fail}`);
  if (results.fail) {
    console.log(results.failures.map((f) => " - " + f).join("\n"));
  }
  return results.fail === 0;
}

module.exports = { boot, backend, assert, until, sleep, section, summary, results, dom, REPO };
