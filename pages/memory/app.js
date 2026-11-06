/* 鲸群记忆管理页：外壳、顶层导航、渲染调度与启动。
 *
 * 页面由 AstrBot WebUI 的「插件页面」放在沙箱 iframe 里加载：接口经桥接对象
 * （window.AstrBotPluginView，旧名 AstrBotPluginPage）调用，深浅主题由桥接
 * 写在 <html data-theme> 上，语言切换时桥接推送新的上下文。
 */
(function (MP) {
  "use strict";

  const bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;
  const t = MP.t;
  const ui = MP.ui;
  const h = ui.h;
  const icon = ui.icon;
  const store = MP.store;
  const S = store.S;

  // 顶层导航：留言板、小本本（按机器人分）、悄悄话（按对话分）、上下文、管理
  const NAV = [
    ["board", "tabBoard", "board"],
    ["notebook", "tabNotebook", "notebook"],
    ["whisper", "tabWhisper", "whisper"],
    ["context", "tabContext", "context"],
    ["manage", "tabManage", "manage"],
  ];
  const els = {};
  let scheduled = false;
  let locale = "";

  // ------------------------------------------------------------ 渲染调度

  /** 合并同一轮里的多次渲染请求 */
  function render() {
    if (scheduled) {
      return;
    }
    scheduled = true;
    Promise.resolve().then(renderNow);
  }

  function renderNow() {
    scheduled = false;
    if (!els.shell) {
      buildShell();
    }
    renderNav();
    renderBanner();
    renderWorkspace();
  }

  // ------------------------------------------------------------ 外壳

  function buildShell() {
    const root = document.getElementById("app");
    els.nav = h("nav", { class: "nav", "aria-label": t("navLabel") });
    els.refresh = h(
      "button",
      { type: "button", class: "btn btn-ghost btn-icon", "aria-label": t("refresh"), title: t("refreshHint"), onclick: function (event) { refreshAll(event.currentTarget); } },
      icon("refresh")
    );
    els.banner = h("div", { class: "banner-area" });
    els.listPane = h("section", { class: "pane pane-list", "aria-label": t("listPaneLabel") });
    els.detailPane = h("section", { class: "pane pane-detail", "aria-label": t("detailPaneLabel") });
    els.split = h("div", { class: "split" }, els.listPane, els.detailPane);
    els.shell = h(
      "div",
      { class: "shell" },
      h(
        "header",
        { class: "topbar" },
        h(
          "div",
          { class: "brand" },
          h("span", { class: "brand-mark", "aria-hidden": "true" }, icon("notebook")),
          h("div", { class: "brand-text" }, h("h1", { class: "brand-title" }, t("title")), h("p", { class: "brand-sub" }, t("subtitle")))
        ),
        els.nav,
        h("div", { class: "topbar-actions" }, els.refresh)
      ),
      els.banner,
      h("main", { class: "workspace", id: "workspace" }, els.split)
    );
    ui.replace(root, els.shell);
    root.removeAttribute("aria-busy");
  }

  function navCount(view) {
    const ov = S.ov;
    if (!ov) {
      return null;
    }
    if (view === "board") {
      return ov.board ? ov.board.fileCount : 0;
    }
    if (view === "notebook") {
      return (ov.notebooks || []).length;
    }
    if (view === "whisper") {
      return (ov.whispers || []).filter(function (s) { return s.fileCount > 0; }).length;
    }
    return null;
  }

  function renderNav() {
    ui.keepFocus(els.nav, function () {
      ui.replace(
        els.nav,
        NAV.map(function (item) {
          const count = navCount(item[0]);
          return h(
            "button",
            {
              type: "button",
              class: "nav-item",
              "data-fk": "nav:" + item[0],
              "aria-current": S.view === item[0] ? "page" : null,
              onclick: function () { go(item[0]); },
            },
            icon(item[2]),
            h("span", { class: "nav-label" }, t(item[1])),
            count === null ? null : h("span", { class: "nav-count", "aria-label": t("navCount", { n: count }) }, ui.fmtNum(count))
          );
        })
      );
    });
  }

  function renderBanner() {
    const items = [];
    if (S.ov && S.ov.memoryEnabled === false) {
      items.push(ui.notice("warn", t("memoryOffNotice")));
    }
    if (S.ovError && !S.ov) {
      items.push(
        ui.notice(
          "danger",
          t("overviewFailed", { message: S.ovError }),
          h("button", { type: "button", class: "btn btn-sm", onclick: function () { store.loadOverview(); } }, icon("refresh"), t("retry"))
        )
      );
    }
    ui.replace(els.banner, items);
    els.banner.hidden = !items.length;
  }

  function listFor(view) {
    if (view === "context") {
      return MP.manage.contextListPane();
    }
    if (view === "manage") {
      return MP.manage.manageListPane();
    }
    return MP.memory.listPane(view);
  }

  function detailFor(view) {
    if (view === "context") {
      return MP.manage.contextDetailFor();
    }
    if (view === "manage") {
      return MP.manage.manageDetailFor();
    }
    return MP.memory.detailFor(view);
  }

  function renderWorkspace() {
    const view = S.view;
    els.split.setAttribute("data-view", view);
    els.split.setAttribute("data-show", S.showDetail[view] ? "detail" : "list");
    // 列表随数据重建；保住焦点与滚动位置
    const oldBody = els.listPane.querySelector("[data-list-body]");
    const scrollTop = oldBody ? oldBody.scrollTop : 0;
    const sameList = els.listPane.getAttribute("data-for") === listKey(view);
    ui.keepFocus(els.listPane, function () {
      ui.replace(els.listPane, listFor(view));
    });
    els.listPane.setAttribute("data-for", listKey(view));
    const newBody = els.listPane.querySelector("[data-list-body]");
    if (newBody && sameList) {
      newBody.scrollTop = scrollTop;
    }
    // 右侧组件 id 不变就沿用，未保存的修改不会因为列表刷新而丢
    const desc = detailFor(view);
    const current = store.current.detail;
    if (!current || current.id !== desc.id) {
      const comp = desc.create();
      comp.id = desc.id;
      store.current.detail = comp;
      ui.replace(els.detailPane, comp.el);
      els.detailPane.scrollTop = 0;
    } else if (current.el.parentNode !== els.detailPane) {
      ui.replace(els.detailPane, current.el);
    }
  }

  function listKey(view) {
    if (view === "notebook") {
      return "notebook:" + S.notebookKey;
    }
    if (view === "whisper") {
      return "whisper:" + S.whisperKey;
    }
    return view;
  }

  async function refreshAll(button) {
    await ui.withBusy(button, async function () {
      const view = S.view;
      const r = MP.memory.viewRef(view);
      await Promise.all([store.loadOverview(true), r ? store.loadScope(r) : Promise.resolve()]);
      const d = store.current.detail;
      if (d && typeof d.reload === "function") {
        await d.reload();
      }
      ui.toast(t("refreshed"), "info");
    });
  }

  // ------------------------------------------------------------ 导航

  async function go(view) {
    if (view === S.view) {
      // 窄屏下再点一次当前导航回到列表
      if (S.showDetail[view] && S.narrow && (await store.guard())) {
        S.showDetail[view] = false;
        renderNow();
      }
      return;
    }
    if (!(await store.guard())) {
      return;
    }
    S.view = view;
    renderNow();
  }

  function focusDetail() {
    const heading = els.detailPane && els.detailPane.querySelector("[data-detail-heading]");
    if (heading) {
      heading.focus();
    }
  }

  function focusList() {
    const target = els.listPane.querySelector('[aria-current="true"]') || els.listPane.querySelector("[data-nav-item]") || els.nav.querySelector('[aria-current="page"]');
    if (target) {
      target.focus();
    }
  }

  /** 窄屏的"返回"：回到左边的列表，右侧组件保留 */
  async function back(view) {
    S.showDetail[view || S.view] = false;
    renderNow();
    focusList();
  }

  async function openConversation(key, switchView) {
    if (!(await store.guard())) {
      return;
    }
    S.whisperKey = key;
    S.pane.whisper = { kind: "session" };
    S.showDetail.whisper = false;
    if (switchView) {
      S.view = "whisper";
    }
    renderNow();
    const back = els.listPane.querySelector('[data-fk="whisper-back"]');
    if (back) {
      back.focus();
    }
  }

  async function leaveConversation(force) {
    if (!force && !(await store.guard())) {
      return;
    }
    const key = S.whisperKey;
    S.whisperKey = "";
    S.pane.whisper = { kind: "session" };
    S.showDetail.whisper = false;
    renderNow();
    const row = key && els.listPane.querySelector('[data-fk="conv:' + (window.CSS && CSS.escape ? CSS.escape(key) : key) + '"]');
    if (row) {
      row.focus();
    }
  }

  async function showContext(key) {
    if (!(await store.guard())) {
      return;
    }
    S.view = "context";
    S.contextKey = key;
    S.showDetail.context = true;
    S.paneVersion += 1;
    renderNow();
    if (S.narrow) {
      focusDetail();
    }
  }

  /** 打开"管理"里的一节；extra 可以带 tidySid / maintSid / transferTab */
  async function openManage(section, extra) {
    if (!(await store.guard())) {
      return;
    }
    S.view = "manage";
    if (section !== S.manage.section) {
      S.manage.tidySid = "";
      S.manage.maintSid = "";
    }
    S.manage.section = section;
    Object.assign(S.manage, extra || {});
    S.showDetail.manage = true;
    S.paneVersion += 1;
    renderNow();
    if (S.narrow) {
      focusDetail();
    }
  }

  // ------------------------------------------------------------ 键盘与窄屏

  function onKeydown(event) {
    if (ui.hasOpenDialog() || event.defaultPrevented) {
      return;
    }
    // "/" 跳到当前列表的搜索框
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !ui.isTyping(event.target)) {
      const input = els.listPane && els.listPane.querySelector("[data-search]");
      if (input) {
        event.preventDefault();
        if (S.narrow && S.showDetail[S.view]) {
          back(S.view);
        }
        input.focus();
        input.select();
      }
    }
  }

  function watchNarrow() {
    const query = window.matchMedia ? window.matchMedia("(max-width: 900px)") : null;
    function apply() {
      S.narrow = Boolean(query && query.matches);
      document.documentElement.classList.toggle("is-narrow", S.narrow);
    }
    apply();
    if (query && typeof query.addEventListener === "function") {
      query.addEventListener("change", function () {
        apply();
        render();
      });
    }
  }

  // ------------------------------------------------------------ 语言与启动

  function applyLocale() {
    locale = MP.i18n.locale();
    document.documentElement.lang = locale;
    document.title = t("title");
  }

  /** 面板换了语言：重建外壳；右侧有未保存的修改就先保留原组件 */
  function onContext() {
    if (MP.i18n.locale() === locale || !els.shell) {
      return;
    }
    applyLocale();
    const keep = store.isDirty();
    if (!keep) {
      store.current.detail = null;
    }
    els.shell = null;
    renderNow();
  }

  function wait(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  async function boot() {
    const root = document.getElementById("app");
    if (!bridge || typeof bridge.apiGet !== "function") {
      ui.replace(root, h("div", { class: "boot" }, ui.emptyState({ icon: "warn", title: t("loadFailed"), hint: t("noBridge") })));
      root.removeAttribute("aria-busy");
      return;
    }
    MP.i18n.setBridge(bridge);
    ui.setBridge(bridge);
    // 桥接通常在脚本加载时就带着初始上下文；没有时最多等 1.5 秒再用默认语言
    if (typeof bridge.ready === "function") {
      try {
        await Promise.race([bridge.ready(), wait(1500)]);
      } catch (err) {
        // 拿不到上下文也照常显示，文案回落到简体中文
      }
    }
    applyLocale();
    watchNarrow();
    document.addEventListener("keydown", onKeydown);
    if (typeof bridge.onContext === "function") {
      bridge.onContext(onContext);
    }
    renderNow();
    store.loadOverview();
  }

  MP.render = render;
  MP.renderNow = renderNow;
  MP.app = {
    go: go,
    back: back,
    focusDetail: focusDetail,
    openConversation: openConversation,
    leaveConversation: leaveConversation,
    showContext: showContext,
    openManage: openManage,
  };

  boot();
})(window.MemoryPod = window.MemoryPod || {});
