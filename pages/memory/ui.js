/* 鲸群记忆管理页：DOM 构造、图标、格式化、接口、提示与弹窗。
 *
 * 安全约定：页面里所有用户内容（记忆正文、文件名、群名、署名、摘要……）
 * 一律经 createTextNode / textContent 进入页面，整个页面不使用 innerHTML、
 * insertAdjacentHTML 这类把字符串当 HTML 解析的接口。图标也用
 * createElementNS 逐个建元素。
 */
(function (MP) {
  "use strict";

  const t = MP.t;
  const SVG_NS = "http://www.w3.org/2000/svg";
  // 只把这几个名字当 DOM 属性（property）赋值，其余一律 setAttribute
  const PROPS = new Set(["value", "checked", "disabled", "selected", "indeterminate", "multiple"]);

  // ------------------------------------------------------------ DOM

  function append(el, child) {
    if (child === null || child === undefined || child === false || child === true) {
      return;
    }
    if (Array.isArray(child)) {
      child.forEach(function (item) {
        append(el, item);
      });
      return;
    }
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }

  /** h("button", {class: "btn", onclick: fn}, "文字", node, [更多子节点]) */
  function h(tag, attrs) {
    const el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (name) {
        const value = attrs[name];
        if (value === null || value === undefined || value === false) {
          return;
        }
        if (name === "class") {
          el.className = Array.isArray(value) ? value.filter(Boolean).join(" ") : String(value);
        } else if (name === "style" && typeof value === "object") {
          Object.keys(value).forEach(function (key) {
            el.style[key] = value[key];
          });
        } else if (name.slice(0, 2) === "on") {
          // 事件只接受函数；字符串形式的内联事件一律不设
          if (typeof value === "function") {
            el.addEventListener(name.slice(2).toLowerCase(), value);
          }
        } else if (PROPS.has(name)) {
          el[name] = value;
        } else {
          el.setAttribute(name, value === true ? "" : String(value));
        }
      });
    }
    for (let i = 2; i < arguments.length; i += 1) {
      append(el, arguments[i]);
    }
    return el;
  }

  function clear(el) {
    while (el && el.firstChild) {
      el.removeChild(el.firstChild);
    }
    return el;
  }

  function replace(el) {
    const active = document.activeElement;
    const hadFocus = Boolean(el && active && active !== el && el.contains(active));
    clear(el);
    for (let i = 1; i < arguments.length; i += 1) {
      append(el, arguments[i]);
    }
    if (hadFocus) {
      const now = document.activeElement;
      if (!now || now === document.body || !now.isConnected) {
        // 焦点原来在被换掉的内容里（比如刚点过的按钮已经不在了）：停在这个区域上，
        // 键盘用户接着按 Tab 就从这里往下走，而不是回到整个页面的开头
        if (!el.hasAttribute("tabindex")) {
          el.setAttribute("tabindex", "-1");
        }
        el.focus();
      }
    }
    return el;
  }

  let idSeq = 0;
  function uid(prefix) {
    idSeq += 1;
    return (prefix || "mp") + "-" + idSeq;
  }

  /** 只给读屏软件读的文字 */
  function srOnly(text) {
    return h("span", { class: "sr-only" }, text);
  }

  // ------------------------------------------------------------ 图标

  // 24×24、描边风格；每个图标是若干 [元素, 属性] 对
  const ICONS = {
    board: [["path", { d: "M4 10.5v3a1 1 0 0 0 1 1h2.5l5 4v-13l-5 4H5a1 1 0 0 0-1 1z" }], ["path", { d: "M16 9a4.5 4.5 0 0 1 0 6" }], ["path", { d: "M18.5 6.5a8 8 0 0 1 0 11" }]],
    notebook: [["path", { d: "M6 4h10.5A2.5 2.5 0 0 1 19 6.5V20H8a2 2 0 0 1-2-2z" }], ["path", { d: "M6 18a2 2 0 0 1 2-2h11" }], ["path", { d: "M10 8h5" }]],
    whisper: [["path", { d: "M5 18.5 4 21l3.6-1.3A8.5 8.5 0 1 0 5 18.5z" }], ["path", { d: "M9 11.5h.01M12 11.5h.01M15 11.5h.01" }]],
    context: [["path", { d: "M12 3.5 21 8l-9 4.5L3 8z" }], ["path", { d: "M3 12.5 12 17l9-4.5" }], ["path", { d: "M3 16.5 12 21l9-4.5" }]],
    manage: [["path", { d: "M4 7h9M17 7h3M4 17h3M11 17h9" }], ["circle", { cx: "15", cy: "7", r: "2" }], ["circle", { cx: "9", cy: "17", r: "2" }]],
    search: [["circle", { cx: "11", cy: "11", r: "6.5" }], ["path", { d: "M20 20l-4-4" }]],
    plus: [["path", { d: "M12 5v14M5 12h14" }]],
    refresh: [["path", { d: "M20 11a8 8 0 0 0-14.9-4M4 4v4h4" }], ["path", { d: "M4 13a8 8 0 0 0 14.9 4M20 20v-4h-4" }]],
    group: [["circle", { cx: "9", cy: "8", r: "3.2" }], ["path", { d: "M3.5 19a5.5 5.5 0 0 1 11 0" }], ["path", { d: "M16 5.2a3 3 0 0 1 0 5.6M17.5 14a5 5 0 0 1 3 5" }]],
    person: [["circle", { cx: "12", cy: "8", r: "3.6" }], ["path", { d: "M5 20a7 7 0 0 1 14 0" }]],
    bot: [["rect", { x: "5", y: "8", width: "14", height: "11", rx: "2.5" }], ["path", { d: "M12 4v4M9.5 13h.01M14.5 13h.01M9.5 16h5" }]],
    trash: [["path", { d: "M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" }]],
    check: [["path", { d: "M5 12.5l4.5 4.5L19 7.5" }]],
    edit: [["path", { d: "M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3z" }]],
    sparkles: [["path", { d: "M11 3l1.8 4.7 4.7 1.8-4.7 1.8L11 16l-1.8-4.7L4.5 9.5l4.7-1.8z" }], ["path", { d: "M18.5 14.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z" }]],
    archive: [["rect", { x: "3", y: "4", width: "18", height: "5", rx: "1" }], ["path", { d: "M5 9v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 13h4" }]],
    back: [["path", { d: "M15 18l-6-6 6-6" }]],
    chevronRight: [["path", { d: "M9 6l6 6-6 6" }]],
    chevronDown: [["path", { d: "M6 9l6 6 6-6" }]],
    close: [["path", { d: "M6 6l12 12M18 6L6 18" }]],
    link: [["path", { d: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" }], ["path", { d: "M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" }]],
    warn: [["path", { d: "M12 4l9 16H3z" }], ["path", { d: "M12 10v4M12 17h.01" }]],
    info: [["circle", { cx: "12", cy: "12", r: "9" }], ["path", { d: "M12 11v5M12 8h.01" }]],
    file: [["path", { d: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" }], ["path", { d: "M14 3v5h5" }]],
    history: [["path", { d: "M4 12a8 8 0 1 0 2.4-5.7L4 8.6" }], ["path", { d: "M4 4v4.6h4.6M12 8v4l3 2" }]],
    clock: [["circle", { cx: "12", cy: "12", r: "9" }], ["path", { d: "M12 7v5l3 2" }]],
    calendar: [["rect", { x: "4", y: "5", width: "16", height: "15", rx: "2" }], ["path", { d: "M4 10h16M9 3v4M15 3v4" }]],
    restore: [["path", { d: "M4 12a8 8 0 1 0 2.4-5.7L4 8.6" }], ["path", { d: "M4 4v4.6h4.6" }]],
    download: [["path", { d: "M12 4v11M7 10l5 5 5-5M5 20h14" }]],
    upload: [["path", { d: "M12 16V5M7 10l5-5 5 5M5 20h14" }]],
    transfer: [["path", { d: "M4 8h13l-3.5-3.5M20 16H7l3.5 3.5" }]],
    eye: [["path", { d: "M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" }], ["circle", { cx: "12", cy: "12", r: "3" }]],
    list: [["path", { d: "M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01" }]],
    compare: [["path", { d: "M7 4v13M17 7v13M4 7l3-3 3 3M14 17l3 3 3-3" }]],
    lock: [["rect", { x: "5", y: "11", width: "14", height: "9", rx: "2" }], ["path", { d: "M8 11V8a4 4 0 0 1 8 0v3" }]],
    stop: [["rect", { x: "6.5", y: "6.5", width: "11", height: "11", rx: "2" }]],
    pen: [["path", { d: "M3 21c3-.5 5.5-1.6 7.6-3.7l8.9-8.9a2.1 2.1 0 0 0-3-3l-8.9 8.9C5.5 16.4 4 18.6 3 21z" }]],
  };

  function icon(name, extraClass) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon" + (extraClass ? " " + extraClass : ""));
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    (ICONS[name] || []).forEach(function (spec) {
      const node = document.createElementNS(SVG_NS, spec[0]);
      Object.keys(spec[1]).forEach(function (key) {
        node.setAttribute(key, spec[1][key]);
      });
      svg.append(node);
    });
    return svg;
  }

  // ------------------------------------------------------------ 格式化

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function toDate(ts) {
    return new Date(Number(ts) * 1000);
  }

  function fmtDate(ts) {
    if (!ts) {
      return "—";
    }
    const d = toDate(ts);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function fmtTime(ts) {
    if (!ts) {
      return "—";
    }
    const d = toDate(ts);
    return fmtDate(ts) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function fmtRel(ts) {
    if (!ts) {
      return "—";
    }
    const diff = Math.max(0, Date.now() / 1000 - Number(ts));
    if (diff < 60) {
      return t("justNow");
    }
    if (diff < 3600) {
      return t("minutesAgo", { n: Math.floor(diff / 60) });
    }
    if (diff < 86400) {
      return t("hoursAgo", { n: Math.floor(diff / 3600) });
    }
    if (diff < 86400 * 30) {
      return t("daysAgo", { n: Math.floor(diff / 86400) });
    }
    return fmtDate(ts);
  }

  /** 相对时间文字，悬停可看完整时间 */
  function timeEl(ts, rel) {
    return h(
      "time",
      { title: ts ? fmtTime(ts) : null, datetime: ts ? toDate(ts).toISOString() : null },
      rel === false ? fmtTime(ts) : fmtRel(ts)
    );
  }

  function fmtNum(n) {
    const value = Number(n || 0);
    try {
      return value.toLocaleString(MP.i18n.locale());
    } catch (err) {
      return String(value);
    }
  }

  function fmtBytes(n) {
    const value = Number(n || 0);
    if (value < 1024) {
      return value + " B";
    }
    if (value < 1024 * 1024) {
      return (value / 1024).toFixed(1) + " KB";
    }
    return (value / 1024 / 1024).toFixed(1) + " MB";
  }

  function fmtPct(ratio) {
    const value = Math.max(0, Number(ratio || 0)) * 100;
    if (value > 0 && value < 0.1) {
      return "<0.1%";
    }
    return (value < 10 ? value.toFixed(1) : Math.round(value)) + "%";
  }

  /** 今天往后数 days 天的日期（YYYY-MM-DD，本机时区） */
  function dateAfter(days) {
    const d = new Date();
    d.setDate(d.getDate() + days);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function stem(file) {
    return String(file || "").replace(/\.md$/i, "");
  }

  // ------------------------------------------------------------ 接口

  let bridge = null;

  function setBridge(value) {
    bridge = value;
  }

  function toError(err) {
    const message = err && err.message ? String(err.message) : String(err || "");
    return new Error(message || t("requestFailed"));
  }

  function cleanParams(params) {
    const out = {};
    Object.keys(params || {}).forEach(function (key) {
      const value = params[key];
      if (value !== undefined && value !== null) {
        out[key] = String(value);
      }
    });
    return out;
  }

  async function apiGet(endpoint, params) {
    try {
      return (await bridge.apiGet(endpoint, cleanParams(params))) || {};
    } catch (err) {
      throw toError(err);
    }
  }

  async function apiPost(endpoint, body) {
    try {
      return (await bridge.apiPost(endpoint, body || {})) || {};
    } catch (err) {
      throw toError(err);
    }
  }

  /** 下载接口返回的内容：优先由面板代为下载（不受 iframe 沙箱限制），
   *  旧版本没有这个能力时在页面里用 Blob 链接下载。 */
  async function download(endpoint, params, filename) {
    if (bridge && typeof bridge.download === "function") {
      try {
        await bridge.download(endpoint, cleanParams(params), filename);
        return;
      } catch (err) {
        // 面板代下载失败时退回页面内下载
      }
    }
    const data = await apiGet(endpoint, params);
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const link = h("a", { href: URL.createObjectURL(blob), download: filename, class: "sr-only" });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(function () {
      URL.revokeObjectURL(link.href);
    }, 1000);
  }

  // ------------------------------------------------------------ 提示

  function toast(message, kind) {
    const box = document.getElementById("toasts");
    if (!box) {
      return;
    }
    const isError = kind === "error";
    const close = h("button", { type: "button", class: "toast-close", "aria-label": t("close") }, icon("close"));
    const el = h(
      "div",
      { class: "toast" + (isError ? " is-error" : ""), role: isError ? "alert" : "status" },
      icon(isError ? "warn" : kind === "info" ? "info" : "check"),
      h("div", { class: "toast-text" }, message || (isError ? t("requestFailed") : t("done"))),
      close
    );
    let timer = 0;
    function dismiss() {
      clearTimeout(timer);
      el.remove();
    }
    function arm() {
      clearTimeout(timer);
      timer = setTimeout(dismiss, isError ? 8000 : 4000);
    }
    close.addEventListener("click", dismiss);
    el.addEventListener("mouseenter", function () {
      clearTimeout(timer);
    });
    el.addEventListener("mouseleave", arm);
    el.addEventListener("focusin", function () {
      clearTimeout(timer);
    });
    el.addEventListener("focusout", arm);
    while (box.children.length >= 4) {
      box.firstChild.remove();
    }
    box.append(el);
    arm();
  }

  /** 界面是不是中文（后端写的说明只有中文） */
  function isChinese() {
    return /^zh/i.test(MP.i18n.locale());
  }

  /** 后端写的中文说明（理由、片段……）：只在中文界面里显示，其他语言不露出中文 */
  function zhOnly(text) {
    return isChinese() ? String(text || "") : "";
  }

  /**
   * 操作成功后的提示文字：中文界面用后端写的回执（带具体文件名、索引同步情况），
   * 其他语言用页面自己的译文，因为后端的回执只有中文。
   */
  function resultMessage(res, fallback) {
    const message = res && typeof res.message === "string" ? res.message : "";
    return message && isChinese() ? message : fallback;
  }

  // ------------------------------------------------------------ 状态块

  function spinner(label) {
    return h(
      "div",
      { class: "state state-loading", role: "status" },
      h("span", { class: "spinner", "aria-hidden": "true" }),
      h("span", null, label || t("loading"))
    );
  }

  function emptyState(opts) {
    return h(
      "div",
      { class: "state state-empty" + (opts.compact ? " is-compact" : "") },
      opts.icon ? h("span", { class: "state-icon" }, icon(opts.icon)) : null,
      h("strong", { class: "state-title" }, opts.title),
      opts.hint ? h("p", { class: "state-hint" }, opts.hint) : null,
      opts.action || null
    );
  }

  function errorState(message, onRetry) {
    return h(
      "div",
      { class: "state state-error", role: "alert" },
      h("span", { class: "state-icon" }, icon("warn")),
      h("strong", { class: "state-title" }, t("loadFailed")),
      h("p", { class: "state-hint" }, message || t("requestFailed")),
      onRetry
        ? h("button", { type: "button", class: "btn", onclick: onRetry }, icon("refresh"), t("retry"))
        : null
    );
  }

  function notice(kind, content, extra) {
    return h(
      "div",
      { class: "notice notice-" + kind, role: kind === "danger" ? "alert" : null },
      icon(kind === "info" ? "info" : kind === "ok" ? "check" : "warn"),
      h("div", { class: "notice-body" }, content, extra || null)
    );
  }

  // ------------------------------------------------------------ 弹窗

  const openDialogs = [];

  function focusables(root) {
    return Array.prototype.filter.call(
      root.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      ),
      function (el) {
        return !el.disabled && el.getClientRects().length > 0;
      }
    );
  }

  function setAppInert(on) {
    const app = document.getElementById("app");
    if (app) {
      app.inert = on;
      app.classList.toggle("is-covered", on);
      if (on) {
        app.setAttribute("aria-hidden", "true");
      } else {
        app.removeAttribute("aria-hidden");
      }
    }
  }

  /**
   * 打开弹窗。opts: title, body(Node), size("sm"|"md"|"lg"), alert(是否危险确认),
   * actions([{label, kind, onClick(ctrl), autofocus, icon}]), onClose(value)
   */
  function dialog(opts) {
    const titleId = uid("dlg-title");
    const bodyId = uid("dlg-body");
    const previous = document.activeElement;
    const body = h("div", { class: "dialog-body", id: bodyId });
    const foot = h("div", { class: "dialog-foot" });
    let resolveDone;
    const done = new Promise(function (resolve) {
      resolveDone = resolve;
    });
    const ctrl = {
      busy: false,
      done: done,
      body: body,
      close: function (value) {
        if (!overlay.isConnected) {
          return;
        }
        overlay.remove();
        const index = openDialogs.indexOf(ctrl);
        if (index >= 0) {
          openDialogs.splice(index, 1);
        }
        if (!openDialogs.length) {
          setAppInert(false);
        }
        if (previous && previous.isConnected && typeof previous.focus === "function") {
          previous.focus();
        }
        resolveDone(value === undefined ? null : value);
      },
      setBusy: function (value) {
        ctrl.busy = Boolean(value);
        box.setAttribute("aria-busy", ctrl.busy ? "true" : "false");
        // 不用 disabled：禁用的按钮会丢掉键盘焦点；点击在 onClick 里按 busy 忽略
        Array.prototype.slice.call(foot.querySelectorAll("button")).concat([closeBtn]).forEach(function (btn) {
          if (ctrl.busy) {
            btn.setAttribute("aria-disabled", "true");
          } else {
            btn.removeAttribute("aria-disabled");
          }
        });
      },
      setBody: function (node) {
        replace(body, node);
      },
      setActions: function (actions) {
        clear(foot);
        let first = null;
        (actions || []).forEach(function (action) {
          if (action.node) {
            foot.append(action.node);
            return;
          }
          const btn = h(
            "button",
            {
              type: "button",
              class: "btn" + (action.kind ? " btn-" + action.kind : ""),
              onclick: function () {
                if (ctrl.busy) {
                  return;
                }
                if (action.onClick) {
                  action.onClick(ctrl, btn);
                } else {
                  ctrl.close(action.value);
                }
              },
            },
            action.icon ? icon(action.icon) : null,
            action.label
          );
          if (action.autofocus && !first) {
            first = btn;
          }
          foot.append(btn);
        });
        return first;
      },
    };
    const closeBtn = h(
      "button",
      {
        type: "button",
        class: "btn btn-ghost btn-icon",
        "aria-label": t("close"),
        title: t("close"),
        onclick: function () {
          if (!ctrl.busy) {
            ctrl.close(null);
          }
        },
      },
      icon("close")
    );
    const box = h(
      "div",
      {
        class: "dialog dialog-" + (opts.size || "md") + (opts.alert ? " is-alert" : ""),
        role: opts.alert ? "alertdialog" : "dialog",
        "aria-modal": "true",
        "aria-labelledby": titleId,
        "aria-describedby": bodyId,
      },
      h(
        "div",
        { class: "dialog-head" },
        opts.alert ? h("span", { class: "dialog-alert-icon" }, icon("warn")) : null,
        h("h2", { class: "dialog-title", id: titleId }, opts.title),
        closeBtn
      ),
      body,
      foot
    );
    const overlay = h("div", { class: "overlay" }, box);
    overlay.addEventListener("mousedown", function (event) {
      if (event.target === overlay && !ctrl.busy && opts.dismissible !== false) {
        ctrl.close(null);
      }
    });
    box.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        if (!ctrl.busy) {
          ctrl.close(null);
        }
        return;
      }
      if (event.key !== "Tab") {
        return;
      }
      const items = focusables(box);
      if (!items.length) {
        return;
      }
      const firstItem = items[0];
      const lastItem = items[items.length - 1];
      if (event.shiftKey && document.activeElement === firstItem) {
        event.preventDefault();
        lastItem.focus();
      } else if (!event.shiftKey && document.activeElement === lastItem) {
        event.preventDefault();
        firstItem.focus();
      }
    });
    if (opts.body) {
      ctrl.setBody(opts.body);
    }
    const autofocusBtn = ctrl.setActions(opts.actions || []);
    document.getElementById("dialogs").append(overlay);
    openDialogs.push(ctrl);
    setAppInert(true);
    const target =
      (opts.initialFocus && opts.initialFocus()) ||
      body.querySelector("[data-autofocus]") ||
      autofocusBtn ||
      focusables(body)[0] ||
      closeBtn;
    target.focus();
    return ctrl;
  }

  /**
   * 确认框。opts: title, message, points(后果列表), detail(Node), confirmLabel,
   * danger(危险操作：默认焦点放在"取消"上)
   */
  function confirm(opts) {
    const body = h(
      "div",
      { class: "confirm" },
      opts.message ? h("p", { class: "confirm-message" }, opts.message) : null,
      opts.points && opts.points.length
        ? h(
            "ul",
            { class: "confirm-points" },
            opts.points.filter(Boolean).map(function (point) {
              return h("li", null, point);
            })
          )
        : null,
      opts.detail || null
    );
    const ctrl = dialog({
      title: opts.title,
      body: body,
      size: opts.size || "sm",
      alert: Boolean(opts.danger),
      actions: [
        { label: opts.cancelLabel || t("cancel"), value: false, autofocus: Boolean(opts.danger) },
        {
          label: opts.confirmLabel || t("confirm"),
          kind: opts.danger ? "danger" : "primary",
          value: true,
          autofocus: !opts.danger,
        },
      ],
    });
    return ctrl.done.then(function (value) {
      return value === true;
    });
  }

  /** 按钮执行异步任务期间显示转圈并忽略重复点击；不用 disabled，键盘焦点留在按钮上 */
  async function withBusy(button, task) {
    if (button) {
      if (button.getAttribute("aria-busy") === "true") {
        return undefined;
      }
      button.classList.add("is-busy");
      button.setAttribute("aria-busy", "true");
    }
    try {
      return await task();
    } finally {
      if (button) {
        button.classList.remove("is-busy");
        button.removeAttribute("aria-busy");
      }
    }
  }

  // ------------------------------------------------------------ 表单件

  /** 带标签、说明与错误提示的表单项；返回 {el, input, setError} */
  function field(opts) {
    const id = opts.id || uid("fld");
    const hintId = opts.hint ? id + "-hint" : null;
    const errorId = id + "-err";
    const input = opts.input;
    input.id = id;
    const describedBy = [hintId, errorId].filter(Boolean).join(" ");
    input.setAttribute("aria-describedby", describedBy);
    if (opts.required) {
      input.setAttribute("aria-required", "true");
    }
    const error = h("p", { class: "field-error", id: errorId, hidden: true });
    const el = h(
      "div",
      { class: "field" + (opts.class ? " " + opts.class : "") },
      h(
        "label",
        { class: "field-label", for: id },
        opts.label,
        opts.optional ? h("span", { class: "field-optional" }, t("optional")) : null
      ),
      opts.before || null,
      input,
      opts.after || null,
      opts.hint ? h("p", { class: "field-hint", id: hintId }, opts.hint) : null,
      error
    );
    function setError(message) {
      if (message) {
        error.textContent = message;
        error.hidden = false;
        input.setAttribute("aria-invalid", "true");
      } else {
        error.textContent = "";
        error.hidden = true;
        input.removeAttribute("aria-invalid");
      }
    }
    return { el: el, input: input, setError: setError };
  }

  /** 分段切换（单选按钮组），返回元素；onChange(value) */
  function segmented(options, value, onChange, label) {
    const group = h("div", { class: "segmented", role: "radiogroup", "aria-label": label || null });
    function paint(current) {
      Array.prototype.forEach.call(group.children, function (btn) {
        const on = btn.getAttribute("data-value") === current;
        btn.setAttribute("aria-checked", on ? "true" : "false");
        btn.tabIndex = on ? 0 : -1;
      });
    }
    options.forEach(function (opt) {
      group.append(
        h(
          "button",
          {
            type: "button",
            role: "radio",
            class: "segment",
            "data-value": opt.value,
            onclick: function () {
              if (opt.value !== value) {
                Promise.resolve(onChange(opt.value)).then(function (accepted) {
                  if (accepted !== false) {
                    value = opt.value;
                    paint(value);
                  }
                });
              }
            },
          },
          opt.icon ? icon(opt.icon) : null,
          opt.label
        )
      );
    });
    group.addEventListener("keydown", function (event) {
      if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].indexOf(event.key) < 0) {
        return;
      }
      event.preventDefault();
      const buttons = Array.prototype.slice.call(group.children);
      const index = buttons.indexOf(document.activeElement);
      const step = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
      const next = buttons[(index + step + buttons.length) % buttons.length];
      next.focus();
      next.click();
    });
    paint(value);
    return group;
  }

  /** 选项卡：tabs [{value, label, count}], onChange(value) */
  function tabs(items, value, onChange, label) {
    const list = h("div", { class: "tabs", role: "tablist", "aria-label": label || null });
    items.forEach(function (item) {
      const on = item.value === value;
      list.append(
        h(
          "button",
          {
            type: "button",
            role: "tab",
            class: "tab",
            id: item.id || null,
            "aria-selected": on ? "true" : "false",
            "aria-controls": item.controls || null,
            tabindex: on ? "0" : "-1",
            "data-value": item.value,
            onclick: function () {
              if (!on) {
                onChange(item.value);
              }
            },
          },
          item.label,
          item.count !== undefined && item.count !== null
            ? h("span", { class: "tab-count" }, fmtNum(item.count))
            : null
        )
      );
    });
    list.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight" && event.key !== "Home" && event.key !== "End") {
        return;
      }
      event.preventDefault();
      const buttons = Array.prototype.slice.call(list.children);
      let index = buttons.indexOf(document.activeElement);
      if (event.key === "Home") {
        index = 0;
      } else if (event.key === "End") {
        index = buttons.length - 1;
      } else {
        index = (index + (event.key === "ArrowLeft" ? -1 : 1) + buttons.length) % buttons.length;
      }
      buttons[index].focus();
      buttons[index].click();
    });
    return list;
  }

  /** 列表的方向键导航（roving tabindex）：只有一项可 Tab 进入 */
  function listNav(container) {
    function items() {
      return Array.prototype.slice.call(container.querySelectorAll("[data-nav-item]"));
    }
    function sync() {
      const all = items();
      let current = all.find(function (el) {
        return el.getAttribute("aria-current") === "true" || el.getAttribute("aria-selected") === "true";
      });
      if (!current) {
        current = all[0];
      }
      all.forEach(function (el) {
        el.tabIndex = el === current ? 0 : -1;
      });
    }
    container.addEventListener("keydown", function (event) {
      const keys = ["ArrowDown", "ArrowUp", "Home", "End"];
      if (keys.indexOf(event.key) < 0) {
        return;
      }
      const all = items();
      const index = all.indexOf(document.activeElement);
      if (index < 0) {
        return;
      }
      event.preventDefault();
      let next = index;
      if (event.key === "ArrowDown") {
        next = Math.min(all.length - 1, index + 1);
      } else if (event.key === "ArrowUp") {
        next = Math.max(0, index - 1);
      } else if (event.key === "Home") {
        next = 0;
      } else {
        next = all.length - 1;
      }
      all[index].tabIndex = -1;
      all[next].tabIndex = 0;
      all[next].focus();
    });
    container.addEventListener("focusin", function (event) {
      const target = event.target.closest ? event.target.closest("[data-nav-item]") : null;
      if (target && container.contains(target)) {
        items().forEach(function (el) {
          el.tabIndex = el === target ? 0 : -1;
        });
      }
    });
    sync();
    return sync;
  }

  /** 重建容器内容时保住焦点：按 data-fk 找回对应的新元素 */
  function keepFocus(container, build) {
    const active = document.activeElement;
    const key = active && container.contains(active) ? active.getAttribute("data-fk") : null;
    const selStart = key && typeof active.selectionStart === "number" ? active.selectionStart : null;
    const selEnd = key && typeof active.selectionEnd === "number" ? active.selectionEnd : null;
    build();
    if (key) {
      const escaped =
        window.CSS && typeof window.CSS.escape === "function"
          ? window.CSS.escape(key)
          : key.replace(/["\\]/g, "");
      const next = container.querySelector('[data-fk="' + escaped + '"]');
      if (next && typeof next.focus === "function") {
        next.focus();
        if (selStart !== null && typeof next.setSelectionRange === "function") {
          try {
            next.setSelectionRange(selStart, selEnd);
          } catch (err) {
            // 部分输入框（如 date）不支持选区
          }
        }
      }
    }
  }

  function isTyping(target) {
    if (!target) {
      return false;
    }
    const tag = target.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
  }

  MP.ui = {
    h: h,
    clear: clear,
    replace: replace,
    uid: uid,
    srOnly: srOnly,
    icon: icon,
    fmtDate: fmtDate,
    fmtTime: fmtTime,
    fmtRel: fmtRel,
    timeEl: timeEl,
    fmtNum: fmtNum,
    fmtBytes: fmtBytes,
    fmtPct: fmtPct,
    dateAfter: dateAfter,
    stem: stem,
    setBridge: setBridge,
    apiGet: apiGet,
    apiPost: apiPost,
    download: download,
    toast: toast,
    resultMessage: resultMessage,
    zhOnly: zhOnly,
    spinner: spinner,
    emptyState: emptyState,
    errorState: errorState,
    notice: notice,
    dialog: dialog,
    confirm: confirm,
    withBusy: withBusy,
    field: field,
    segmented: segmented,
    tabs: tabs,
    listNav: listNav,
    keepFocus: keepFocus,
    isTyping: isTyping,
    hasOpenDialog: function () {
      return openDialogs.length > 0;
    },
  };
})(window.MemoryPod = window.MemoryPod || {});
