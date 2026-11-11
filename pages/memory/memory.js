/* 鲸群记忆管理页：留言板、小本本、悄悄话三个记忆视图。
 *
 * 每个视图左边是列表（告示卡片 / 文件列表 / 对话列表），右边是详情：
 * 文件详情（正文渲染、frontmatter、来源、历史版本时间线）、编辑器、发布告示、
 * 索引、回收站与已遗忘、整理。右侧组件是带自己状态的小对象
 * {el, reload?, isDirty?, markClean?}：列表刷新时不重建，未保存的修改不会丢。
 * 顶部「刷新」时有 reload 的组件自己重读（重读失败返回 false）；没有 reload 的组件
 * 在创建时读一次数据，由 app.js 在没有未保存修改时整个重建。
 */
(function (MP) {
  "use strict";

  const t = MP.t;
  const ui = MP.ui;
  const h = ui.h;
  const icon = ui.icon;
  const store = MP.store;
  const S = store.S;
  const TYPES = ["person", "agreement", "note", "journal"];
  // 与 core/memstore.py 的 _SAFE_NAME_RE 同长度：整个文件名（含 .md）最多 121 个字符，
  // 所以 .md 之前最多 1 + 117 个字符（tests/test_page_source.py 逐个长度核对过）
  const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,117}\.md$/;
  // 与 core/memstore.py 的人物档案文件名规则一致（PLAN 3.10.2）
  const PERSON_RE = /^user-[A-Za-z0-9_]+(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?\.md$/i;
  const PERSON_OWNER_RE = /^user-[A-Za-z0-9_]+(?:-.*)?\.md$/i;
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  // 后端的搜索结果只命中文件头时给的片段占位（web_api.py 的 search）；只用于比较，不显示
  const BACKEND_HEADER_HIT = "（正文匹配）";

  // ------------------------------------------------------------ 小工具

  function viewRef(view) {
    if (view === "board") {
      return store.ref("board", "");
    }
    if (view === "notebook") {
      return S.notebookKey ? store.ref("notebook", S.notebookKey) : null;
    }
    if (view === "whisper") {
      return S.whisperKey ? store.ref("whisper", S.whisperKey) : null;
    }
    return null;
  }

  function scopeData(r) {
    const entry = r ? store.scopeEntry(r) : null;
    return entry && entry.data ? entry.data : null;
  }

  function findRow(r, name) {
    const data = scopeData(r);
    return data ? (data.files || []).find(function (row) { return row.file === name; }) || null : null;
  }

  /**
   * 总览还没读到（加载中或读取失败）时，依赖总览的地方不能当成"什么都没有"：
   * 返回转圈（加载中）或带重试的错误态（失败）；总览已经有了返回 null。
   */
  function overviewGate() {
    if (S.ov) {
      return null;
    }
    return S.ovError ? ui.errorState(S.ovError, function () { store.loadOverview(); }) : ui.spinner();
  }

  /** 总览的三种状态，用在组件 id 里：没读到的组件在总览到了以后要重建 */
  function overviewState() {
    return S.ov ? "ready" : S.ovError ? "failed" : "loading";
  }

  function isPersonFile(name) {
    const match = PERSON_RE.exec(name || "");
    if (!match) {
      return false;
    }
    // 主题必须小写：user-<ID>-<主题>.md
    const rest = String(name).replace(/^user-[A-Za-z0-9_]+/i, "").replace(/\.md$/i, "");
    return rest === rest.toLowerCase();
  }

  /** 文件名校验：返回错误文案，合规返回空串 */
  function nameProblem(name) {
    if (!name) {
      return t("errFileRequired");
    }
    if (name.toLowerCase() === "memory.md") {
      return t("errFileIndex");
    }
    if (!NAME_RE.test(name)) {
      return t("errFileName");
    }
    return "";
  }

  function normalizeName(raw) {
    const name = String(raw || "").trim();
    return name && !/\.md$/.test(name) ? name + ".md" : name;
  }

  function typeTag(type) {
    if (TYPES.indexOf(type) >= 0) {
      return h("span", { class: "tag tag-" + type, title: t("typeHint_" + type) }, t("type_" + type));
    }
    return type ? h("span", { class: "tag" }, type) : null;
  }

  /** 列表里的一行来源：最近一次写入的说话人与会话类型 */
  function originShort(row) {
    const list = (row && row.origin) || [];
    if (list.length) {
      const last = list[list.length - 1];
      const who = last.name || last.speaker || t("originUnknown");
      return who + " · " + (last.kind === "group" ? t("kindGroup") : t("kindPrivate"));
    }
    if (row && row.source) {
      return t("sourceShort", { ids: row.source });
    }
    return "";
  }

  /** 详情里的完整来源：说话人、私聊或群聊「群名」、时间（群名来自审计日志） */
  function originFull(item) {
    const who = item.name ? item.name + (item.speaker ? t("paren", { text: item.speaker }) : "") : item.speaker || t("originUnknown");
    let where = t("kindPrivate");
    if (item.kind === "group") {
      where = item.group ? t("originGroup", { group: item.group }) : t("kindGroup");
    }
    return [who, where, item.ts ? ui.fmtTime(item.ts) : ""].filter(Boolean).join(" · ");
  }

  function flagTags(row, skipExpired) {
    if (!row) {
      return [];
    }
    const tags = [];
    if (row.expired && !skipExpired) {
      tags.push(h("span", { class: "tag tag-warn" }, t("expired")));
    }
    if ((row.dangling || []).length) {
      tags.push(h("span", { class: "tag tag-warn", title: row.dangling.join(t("listSep")) }, t("flagDangling")));
    }
    if ((row.long || []).length) {
      tags.push(h("span", { class: "tag tag-muted", title: ui.zhOnly(row.long.join(t("semiSep"))) || null }, t("flagLong")));
    }
    return tags;
  }

  // ------------------------------------------------------------ 导航

  /** 右侧换成另一个面板；有未保存的修改先确认 */
  async function openPane(view, pane, opts) {
    if (!(opts && opts.skipGuard) && !(await store.guard())) {
      return false;
    }
    S.pane[view] = Object.assign({ tab: "content" }, pane);
    if (opts && opts.fresh) {
      S.paneVersion += 1;
    }
    S.showDetail[view] = pane.kind !== "empty";
    MP.renderNow();
    if (S.narrow) {
      MP.app.focusDetail();
    }
    return true;
  }

  function openFile(view, name, opts) {
    return openPane(view, { kind: "file", file: name }, opts);
  }

  /** 详情还没读到时的标题栏：用列表里那一行的类型和描述，读完后标题栏不会整块跳变 */
  function pendingHead(view, r, name) {
    const board = r.scope === "board";
    const row = findRow(r, name) || {};
    return detailHead({
      view: view,
      title: board ? row.description || name : name,
      mono: !board,
      tags: board || !row.type ? null : typeTag(row.type),
      desc: board ? name : row.description || "",
    });
  }

  /** 详情区的标题栏：窄屏返回按钮、标题、标签、描述与操作按钮 */
  function detailHead(opts) {
    const headingId = ui.uid("detail-title");
    return {
      id: headingId,
      el: h(
        "header",
        { class: "detail-head" },
        h(
          "button",
          { type: "button", class: "btn btn-ghost btn-icon only-narrow", "aria-label": t("back"), title: t("back"), onclick: function () { MP.app.back(opts.view); } },
          icon("back")
        ),
        h(
          "div",
          { class: "detail-heading" },
          h(
            "div",
            { class: "detail-title-row" },
            h("h2", { class: "detail-title" + (opts.mono ? " mono" : ""), id: headingId, tabindex: "-1", "data-detail-heading": "" }, opts.title),
            opts.tags || null
          ),
          opts.desc ? h("p", { class: "detail-desc" }, opts.desc) : null
        ),
        opts.actions ? h("div", { class: "detail-actions" }, opts.actions) : null
      ),
    };
  }

  // ------------------------------------------------------------ 列表：标题栏

  function listHead(opts) {
    return h(
      "header",
      { class: "list-head" },
      opts.back || null,
      h(
        "div",
        { class: "list-head-row" },
        h(
          "div",
          { class: "list-heading" },
          h("h2", { class: "list-title" }, opts.title, opts.tag || null),
          opts.sub ? h("p", { class: "list-sub" }, opts.sub) : null
        ),
        opts.actions ? h("div", { class: "list-actions" }, opts.actions) : null
      ),
      opts.extra || null
    );
  }

  function notebookPicker() {
    const notebooks = (S.ov && S.ov.notebooks) || [];
    if (notebooks.length < 2) {
      return null;
    }
    const id = ui.uid("nb");
    const select = h(
      "select",
      {
        id: id,
        class: "input input-sm",
        "data-fk": "notebook-picker",
        onchange: async function (event) {
          const value = event.target.value;
          if (!(await store.guard())) {
            event.target.value = S.notebookKey;
            return;
          }
          S.notebookKey = value;
          S.pane.notebook = { kind: "empty" };
          S.showDetail.notebook = false;
          MP.renderNow();
        },
      },
      notebooks.map(function (item) {
        return h("option", { value: item.key, selected: item.key === S.notebookKey }, item.platform || item.key);
      })
    );
    return h("div", { class: "picker" }, h("label", { class: "picker-label", for: id }, t("botLabel")), select);
  }

  // ------------------------------------------------------------ 列表：搜索与筛选

  function searchBox(opts) {
    const input = h("input", {
      type: "search",
      class: "input search-input",
      placeholder: opts.placeholder,
      "aria-label": opts.label || opts.placeholder,
      title: opts.help || null,
      value: opts.value || "",
      "data-fk": opts.fk,
      "data-search": "",
      autocomplete: "off",
      oninput: function (event) {
        opts.onInput(event.target.value);
      },
      onkeydown: function (event) {
        if (event.key === "Enter" && opts.onEnter) {
          event.preventDefault();
          opts.onEnter(event.target.value);
        } else if (event.key === "Escape" && event.target.value) {
          event.preventDefault();
          event.stopPropagation();
          event.target.value = "";
          opts.onInput("");
        }
      },
    });
    return h("div", { class: "search" }, icon("search", "search-icon"), input);
  }

  /** 一组互斥的筛选按钮（aria-pressed）；点了只重绘列表，焦点留在按钮上 */
  function chipGroup(items, value, onPick, label) {
    const group = h("div", { class: "chips", role: "group", "aria-label": label });
    items.forEach(function (item) {
      group.append(
        h(
          "button",
          {
            type: "button",
            class: "chip",
            "aria-pressed": item.value === value ? "true" : "false",
            "data-fk": "chip:" + label + ":" + item.value,
            onclick: function () {
              group.querySelectorAll(".chip").forEach(function (chip) {
                chip.setAttribute("aria-pressed", chip === this ? "true" : "false");
              }, this);
              onPick(item.value);
            },
          },
          item.label,
          item.count !== undefined ? h("span", { class: "chip-count" }, ui.fmtNum(item.count)) : null
        )
      );
    });
    return group;
  }

  // ------------------------------------------------------------ 列表：文件

  function byUpdated(a, b) {
    return (b.updated || b.mtime || 0) - (a.updated || a.mtime || 0) || a.file.localeCompare(b.file);
  }

  function visibleRows(r, st) {
    const data = scopeData(r);
    if (!data) {
      return [];
    }
    let rows;
    if (st.results) {
      rows = st.results.slice();
    } else {
      const query = st.query.trim().toLowerCase();
      rows = (data.files || []).filter(function (row) {
        if (!query) {
          return true;
        }
        return [row.file, row.description, row.signature, row.excerpt, originShort(row)].some(function (value) {
          return String(value || "").toLowerCase().indexOf(query) >= 0;
        });
      });
      rows.sort(st.sort === "name" ? function (a, b) { return a.file.localeCompare(b.file); } : byUpdated);
    }
    if (st.type && st.type !== "all") {
      rows = rows.filter(function (row) { return row.type === st.type; });
    }
    return rows;
  }

  function isOpen(view, name) {
    const pane = S.pane[view] || {};
    return (pane.kind === "file" || pane.kind === "edit") && pane.file === name;
  }

  function fileRow(view, r, row) {
    const origin = originShort(row);
    return h(
      "button",
      {
        type: "button",
        class: "row",
        "data-nav-item": "",
        "data-fk": "row:" + row.file,
        "aria-current": isOpen(view, row.file) ? "true" : null,
        onclick: function () { openFile(view, row.file); },
      },
      h("span", { class: "row-top" }, h("span", { class: "row-name mono" }, row.file), typeTag(row.type)),
      h("span", { class: "row-desc" + (row.hit ? " is-hit" : "") }, row.hit || row.description || t("noDescription")),
      h("span", { class: "row-meta" }, ui.timeEl(row.updated), origin ? h("span", { class: "row-origin" }, origin) : null, flagTags(row))
    );
  }

  /** 填充文件列表（加载中 / 出错 / 空 / 搜索结果 / 日记折叠） */
  function fillFileList(body, view, r, st) {
    const entry = store.scopeEntry(r);
    ui.keepFocus(body, function () {
      ui.clear(body);
      if (!entry || (!entry.data && entry.loading)) {
        body.append(ui.spinner());
        return;
      }
      if (!entry.data) {
        body.append(ui.errorState(entry.error, function () { store.loadScope(r); }));
        return;
      }
      const all = entry.data.files || [];
      const rows = visibleRows(r, st);
      if (st.results) {
        body.append(
          h(
            "div",
            { class: "list-note" },
            h("span", null, t("searchResults", { n: rows.length })),
            h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { st.results = null; MP.renderNow(); } }, t("clearSearch"))
          )
        );
      }
      const searchNote = bodySearchNote(r, st);
      if (searchNote) {
        body.append(searchNote);
      }
      if (!rows.length) {
        body.append(
          st.results || st.query
            ? ui.emptyState({ icon: "search", title: t("noSearchResults"), hint: st.results ? null : t("searchEnterHint"), compact: true })
            : all.length
              ? ui.emptyState({ icon: "file", title: t("noTypeMatch"), compact: true })
              : ui.emptyState({ icon: "file", title: t("noMemoriesTitle"), hint: t("noMemoriesHint"), compact: true })
        );
        return;
      }
      const list = h("div", { class: "rows", role: "list" });
      const grouped = !st.results && st.type === "all";
      const journals = grouped ? rows.filter(function (row) { return row.type === "journal"; }) : [];
      rows.forEach(function (row) {
        if (!grouped || row.type !== "journal") {
          list.append(h("div", { role: "listitem" }, fileRow(view, r, row)));
        }
      });
      if (journals.length) {
        const open = st.journalsOpen || journals.some(function (row) { return isOpen(view, row.file); });
        list.append(
          h(
            "div",
            { role: "listitem" },
            h(
              "button",
              { type: "button", class: "group-toggle", "data-nav-item": "", "data-fk": "journals", "aria-expanded": open ? "true" : "false", onclick: function () { st.journalsOpen = !open; MP.renderNow(); } },
              icon(open ? "chevronDown" : "chevronRight"),
              h("span", null, t("journalGroup", { n: journals.length })),
              h("span", { class: "group-hint" }, t("journalGroupHint"))
            )
          )
        );
        if (open) {
          journals.forEach(function (row) {
            list.append(h("div", { role: "listitem", class: "is-nested" }, fileRow(view, r, row)));
          });
        }
      }
      body.append(list);
      ui.listNav(list);
    });
  }

  /** 输入了筛选词时，列表顶上给一个按钮：在正文里搜索（服务端全文搜索）；回车也一样 */
  function bodySearchNote(r, st) {
    const q = st.query.trim();
    if (!q || st.results) {
      return null;
    }
    if (st.searching) {
      return h("div", { class: "list-note" }, h("span", null, t("loading")));
    }
    const shown = q.length > 24 ? q.slice(0, 24) + "…" : q;
    return h(
      "div",
      { class: "list-note" },
      h(
        "button",
        { type: "button", class: "btn btn-sm", "data-fk": "body-search", onclick: function () { runSearch(r, st, st.query); } },
        icon("search"),
        t("searchBody", { q: shown })
      )
    );
  }

  /** 全文搜索（服务端，最多 8 条）；结果按相关度排列，带命中片段 */
  async function runSearch(r, st, query) {
    const q = String(query || "").trim();
    if (!q) {
      st.results = null;
      MP.renderNow();
      return;
    }
    st.searching = true;
    MP.renderNow();
    try {
      const data = await ui.apiGet("search", { scope: r.scope, key: r.key, q: q });
      const byFile = {};
      ((scopeData(r) || {}).files || []).forEach(function (row) {
        byFile[row.file] = row;
      });
      st.results = (data.results || []).map(function (item) {
        // 命中的片段是后端截出来的正文；只命中了文件头、没有片段时后端给的是一句中文占位。
        // 这是后端数据里的固定字符串（web_api.py 的 search），不是界面文字，所以原样比较
        const hit = item.excerpt === BACKEND_HEADER_HIT ? t("searchBodyMatch") : item.excerpt;
        return Object.assign({}, byFile[item.file] || { file: item.file, type: "", description: "" }, { hit: hit });
      });
    } catch (err) {
      ui.toast(err.message, "error");
    } finally {
      st.searching = false;
    }
    MP.renderNow();
  }

  /** 列表底部：这一处的索引、已遗忘、回收站与整理入口 */
  function maintFooter(view, r) {
    const data = scopeData(r);
    const pane = S.pane[view] || {};
    const index = data ? data.index || {} : {};
    const indexIssues = Boolean(index.truncated || (index.missing || []).length || (index.dangling || []).length);
    function link(kind, label, count, iconName, extra) {
      const active = pane.kind === kind && (!extra || !extra.tab || pane.tab === extra.tab);
      return h(
        "button",
        {
          type: "button",
          class: "foot-link",
          "aria-current": active ? "true" : null,
          "data-fk": "foot:" + kind,
          onclick: function () { openPane(view, Object.assign({ kind: kind }, extra || {})); },
        },
        icon(iconName),
        h("span", null, label),
        count ? h("span", { class: "foot-count" }, ui.fmtNum(count)) : null
      );
    }
    return h(
      "footer",
      { class: "list-foot" },
      link("index", t("tabIndex"), 0, "list", null),
      indexIssues ? h("span", { class: "dot dot-warn", title: t("indexHasIssues") }, ui.srOnly(t("indexHasIssues"))) : null,
      link("maint", t("tabForgotten"), data ? (data.forgotten || []).length : 0, "archive", { tab: "forgotten" }),
      link("maint", t("tabTrash"), data ? data.trashCount : 0, "trash", { tab: "trash" }),
      r.scope !== "board" ? link("tidy", t("tabTidy"), 0, "sparkles", null) : null
    );
  }

  function typeChips(r, st, body, view) {
    const files = (scopeData(r) || {}).files || [];
    const counts = { all: files.length };
    TYPES.forEach(function (type) {
      counts[type] = files.filter(function (row) { return row.type === type; }).length;
    });
    const items = [{ value: "all", label: t("typeAll"), count: counts.all }].concat(
      TYPES.map(function (type) { return { value: type, label: t("type_" + type), count: counts[type] }; })
    );
    return chipGroup(items, st.type, function (value) {
      st.type = value;
      fillFileList(body, view, r, st);
    }, t("typeFilterLabel"));
  }

  function sortSelect(r, st, body, view) {
    const id = ui.uid("sort");
    return h(
      "div",
      { class: "sort" },
      h("label", { class: "sr-only", for: id }, t("sortLabel")),
      h(
        "select",
        {
          id: id,
          class: "input input-sm",
          "data-fk": "sort",
          onchange: function (event) {
            st.sort = event.target.value;
            fillFileList(body, view, r, st);
          },
        },
        h("option", { value: "updated", selected: st.sort === "updated" }, t("sortUpdated")),
        h("option", { value: "name", selected: st.sort === "name" }, t("sortName"))
      )
    );
  }

  /** 文件列表的工具栏：搜索框（输入即筛选，回车搜正文）、类型筛选、排序 */
  function fileToolbar(view, r, st, body) {
    return h(
      "div",
      { class: "list-tools" },
      h(
        "div",
        { class: "tools-row" },
        searchBox({
          placeholder: t("searchPlaceholder"),
          help: t("searchHelp"),
          value: st.query,
          fk: "search:" + store.sid(r),
          onInput: function (value) {
            st.query = value;
            st.results = null;
            fillFileList(body, view, r, st);
          },
          onEnter: function (value) { runSearch(r, st, value); },
        }),
        sortSelect(r, st, body, view)
      ),
      typeChips(r, st, body, view)
    );
  }

  function newButton(view) {
    return h(
      "button",
      { type: "button", class: "btn btn-sm", "data-fk": "new:" + view, onclick: function () { openPane(view, { kind: "new" }, { fresh: true }); } },
      icon("plus"),
      t("newMemory")
    );
  }

  function notebookListPane() {
    const notebooks = (S.ov && S.ov.notebooks) || [];
    const r = viewRef("notebook");
    if (!r) {
      // 没有选中的小本本：总览读到了才算"真的没有"，没读到（加载中、失败）要如实显示
      return [
        listHead({ title: t("notebookLabel"), sub: t("notebookSub") }),
        h("div", { class: "list-body" }, overviewGate() || ui.emptyState({ icon: "notebook", title: t("notebookNone"), hint: t("notebookNoneHint") })),
      ];
    }
    store.ensureScope(r);
    const st = store.listState(r);
    const body = h("div", { class: "list-body", "data-list-body": "" });
    const current = notebooks.find(function (item) { return item.key === S.notebookKey; });
    const head = listHead({
      title: t("notebookLabel"),
      tag: notebooks.length < 2 && current ? h("span", { class: "title-tag" }, current.platform || current.key) : null,
      sub: t("notebookSub"),
      actions: newButton("notebook"),
      extra: notebookPicker(),
    });
    const tools = fileToolbar("notebook", r, st, body);
    fillFileList(body, "notebook", r, st);
    return [head, tools, body, maintFooter("notebook", r)];
  }

  function whisperFilesPane() {
    const r = viewRef("whisper");
    const session = store.whisperByKey(S.whisperKey) || { key: S.whisperKey };
    store.ensureScope(r);
    const st = store.listState(r);
    const body = h("div", { class: "list-body", "data-list-body": "" });
    const back = h(
      "button",
      { type: "button", class: "btn btn-ghost btn-sm back-link", "data-fk": "whisper-back", onclick: function () { MP.app.leaveConversation(); } },
      icon("back"),
      t("allConversations")
    );
    const head = listHead({
      back: back,
      title: store.sessionTitle(session),
      tag: h("span", { class: "title-tag" }, store.kindLabel(session.kind)),
      sub: store.sessionSub(session) || t("whisperSub"),
      actions: [
        h("button", { type: "button", class: "btn btn-sm btn-ghost", "data-fk": "session-info", onclick: function () { openPane("whisper", { kind: "session" }); } }, icon("info"), t("sessionInfo")),
        newButton("whisper"),
      ],
    });
    const tools = fileToolbar("whisper", r, st, body);
    fillFileList(body, "whisper", r, st);
    return [head, tools, body, maintFooter("whisper", r)];
  }

  // ------------------------------------------------------------ 对话列表（悄悄话、上下文共用）

  function conversations() {
    const list = ((S.ov && S.ov.whispers) || []).slice();
    const query = S.conv.query.trim().toLowerCase();
    return list
      .filter(function (s) {
        if (S.conv.kind !== "all" && s.kind !== S.conv.kind) {
          return false;
        }
        if (S.conv.withMemory && !(s.fileCount > 0)) {
          return false;
        }
        if (!query) {
          return true;
        }
        return [s.remark, s.name, s.target, s.key, s.platform, s.umo].some(function (value) {
          return String(value || "").toLowerCase().indexOf(query) >= 0;
        });
      })
      .sort(function (a, b) {
        return (b.lastActive || b.updatedAt || 0) - (a.lastActive || a.updatedAt || 0) || a.key.localeCompare(b.key);
      });
  }

  function conversationRow(s, opts) {
    const selected = opts.selectedKey === s.key;
    return h(
      "button",
      {
        type: "button",
        class: "row row-conv",
        "data-nav-item": "",
        "data-fk": "conv:" + s.key,
        "aria-current": selected ? "true" : null,
        title: s.umo || s.key,
        onclick: function () { opts.onSelect(s.key); },
      },
      h("span", { class: "avatar avatar-" + (s.kind === "group" ? "group" : "private") }, icon(s.kind === "group" ? "group" : "person")),
      h(
        "span",
        { class: "row-main" },
        h("span", { class: "row-name" }, store.sessionTitle(s)),
        h("span", { class: "row-meta" }, [store.sessionSub(s), ui.fmtRel(s.lastActive || s.updatedAt)].filter(function (v) { return v && v !== "—"; }).join(" · "))
      ),
      opts.showCount ? ui.countBadge("count", ui.fmtNum(s.fileCount || 0), t("fileCount", { n: s.fileCount || 0 })) : null
    );
  }

  function fillConversations(body, opts) {
    ui.keepFocus(body, function () {
      ui.clear(body);
      if (!S.ov) {
        body.append(S.ovError ? ui.errorState(S.ovError, function () { store.loadOverview(); }) : ui.spinner());
        return;
      }
      const all = S.ov.whispers || [];
      const items = conversations();
      if (!items.length) {
        body.append(
          ui.emptyState({
            icon: "whisper",
            title: all.length ? t("noSessions") : t("whisperNone"),
            hint: all.length ? null : t("whisperNoneHint"),
            compact: true,
          })
        );
        return;
      }
      const list = h("div", { class: "rows", role: "list" });
      items.forEach(function (s) {
        list.append(h("div", { role: "listitem" }, conversationRow(s, opts)));
      });
      body.append(list);
      ui.listNav(list);
    });
  }

  /** 对话列表面板：opts = {title, sub, selectedKey, onSelect, showCount, memoryFilter} */
  function conversationPane(opts) {
    const body = h("div", { class: "list-body", "data-list-body": "" });
    const kinds = [
      { value: "all", label: t("filterAll") },
      { value: "group", label: t("kindGroup") },
      { value: "private", label: t("kindPrivate") },
    ];
    const tools = h(
      "div",
      { class: "list-tools" },
      searchBox({
        placeholder: t("filterPlaceholder"),
        value: S.conv.query,
        fk: "conv-search",
        onInput: function (value) {
          S.conv.query = value;
          fillConversations(body, opts);
        },
      }),
      h(
        "div",
        { class: "tools-row" },
        chipGroup(kinds, S.conv.kind, function (value) {
          S.conv.kind = value;
          fillConversations(body, opts);
        }, t("kindFilterLabel")),
        opts.memoryFilter
          ? h(
              "button",
              {
                type: "button",
                class: "chip",
                "aria-pressed": S.conv.withMemory ? "true" : "false",
                "data-fk": "conv-with-memory",
                onclick: function (event) {
                  S.conv.withMemory = !S.conv.withMemory;
                  event.currentTarget.setAttribute("aria-pressed", S.conv.withMemory ? "true" : "false");
                  fillConversations(body, opts);
                },
              },
              t("filterHasMemory")
            )
          : null
      )
    );
    fillConversations(body, opts);
    return [listHead({ title: opts.title, sub: opts.sub }), tools, body];
  }

  // ------------------------------------------------------------ 留言板：告示卡片

  function expiryTag(row) {
    if (row.expired) {
      return h("span", { class: "tag tag-warn" }, icon("calendar"), t("expiredOn", { date: row.expires }));
    }
    if (row.expires) {
      return h("span", { class: "tag tag-muted" }, icon("calendar"), t("expiresOn", { date: row.expires }));
    }
    return h("span", { class: "tag tag-muted" }, t("noExpiry"));
  }

  function noticeCard(row) {
    return h(
      "button",
      {
        type: "button",
        class: "notice-card" + (row.expired ? " is-expired" : ""),
        "data-nav-item": "",
        "data-fk": "row:" + row.file,
        "aria-current": isOpen("board", row.file) ? "true" : null,
        onclick: function () { openFile("board", row.file); },
      },
      h("span", { class: "notice-title" }, row.description || row.file),
      row.hit || row.excerpt ? h("span", { class: "notice-excerpt" + (row.hit ? " is-hit" : "") }, row.hit || row.excerpt) : null,
      h(
        "span",
        { class: "notice-meta" },
        h("span", { class: "notice-sign" + (row.signature ? "" : " is-missing") }, icon("pen"), row.signature || t("noSignature")),
        ui.timeEl(row.updated),
        expiryTag(row),
        flagTags(row, true)
      )
    );
  }

  function boardRows(r, st) {
    let rows = visibleRows(r, Object.assign({}, st, { sort: "updated", type: "all" }));
    if (st.status === "active") {
      rows = rows.filter(function (row) { return !row.expired; });
    } else if (st.status === "expired") {
      rows = rows.filter(function (row) { return row.expired; });
    }
    return rows;
  }

  function fillBoard(body, r, st) {
    const entry = store.scopeEntry(r);
    ui.keepFocus(body, function () {
      ui.clear(body);
      if (!entry || (!entry.data && entry.loading)) {
        body.append(ui.spinner());
        return;
      }
      if (!entry.data) {
        body.append(ui.errorState(entry.error, function () { store.loadScope(r); }));
        return;
      }
      const rows = boardRows(r, st);
      if (st.results) {
        body.append(
          h(
            "div",
            { class: "list-note" },
            h("span", null, t("searchResults", { n: rows.length })),
            h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { st.results = null; MP.renderNow(); } }, t("clearSearch"))
          )
        );
      }
      const searchNote = bodySearchNote(r, st);
      if (searchNote) {
        body.append(searchNote);
      }
      if (!rows.length) {
        const none = !(entry.data.files || []).length;
        body.append(
          none
            ? ui.emptyState({
                icon: "board",
                title: t("boardEmptyTitle"),
                hint: t("boardEmptyHint"),
                action: h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: function () { openPane("board", { kind: "publish" }, { fresh: true }); } }, icon("plus"), t("publishNotice")),
                compact: true,
              })
            : ui.emptyState({ icon: "search", title: st.query || st.results ? t("noSearchResults") : t("noStatusMatch"), compact: true })
        );
        return;
      }
      const list = h("div", { class: "cards", role: "list" });
      rows.forEach(function (row) {
        list.append(h("div", { role: "listitem" }, noticeCard(row)));
      });
      body.append(list);
      ui.listNav(list);
    });
  }

  function boardListPane() {
    const r = viewRef("board");
    store.ensureScope(r);
    const st = store.listState(r);
    const body = h("div", { class: "list-body", "data-list-body": "" });
    const files = (scopeData(r) || {}).files || [];
    const expired = files.filter(function (row) { return row.expired; }).length;
    const head = listHead({
      title: t("boardTitle"),
      sub: t("boardSub"),
      actions: h(
        "button",
        { type: "button", class: "btn btn-primary btn-sm", "data-fk": "publish", onclick: function () { openPane("board", { kind: "publish" }, { fresh: true }); } },
        icon("plus"),
        t("publishNotice")
      ),
    });
    const tools = h(
      "div",
      { class: "list-tools" },
      searchBox({
        placeholder: t("boardSearchPlaceholder"),
        help: t("boardSearchHelp"),
        value: st.query,
        fk: "search:board",
        onInput: function (value) {
          st.query = value;
          st.results = null;
          fillBoard(body, r, st);
        },
        onEnter: function (value) { runSearch(r, st, value); },
      }),
      chipGroup(
        [
          { value: "all", label: t("filterAll"), count: files.length },
          { value: "active", label: t("statusActive"), count: files.length - expired },
          { value: "expired", label: t("expired"), count: expired },
        ],
        st.status,
        function (value) {
          st.status = value;
          fillBoard(body, r, st);
        },
        t("statusFilterLabel")
      )
    );
    fillBoard(body, r, st);
    return [head, tools, body, maintFooter("board", r)];
  }

  // ------------------------------------------------------------ 共用：动作标签与查看器

  function byteLength(text) {
    try {
      return new TextEncoder().encode(String(text || "")).length;
    } catch (err) {
      return String(text || "").length;
    }
  }

  function key(prefix, value) {
    return prefix + String(value || "").replace(/-/g, "_");
  }

  /** "webui" → 管理页、"tidy" → 整理……其余（机器人的平台 ID）原样显示 */
  function actorLabel(actor) {
    if (!actor) {
      return t("actorUnknown");
    }
    const k = key("actor_", actor);
    return MP.i18n.has(k) ? t(k) : actor;
  }

  function versionLabel(action) {
    const k = key("ver_", action);
    return MP.i18n.has(k) ? t(k) : action || "—";
  }

  function trashReason(item) {
    const k = key("trash_", item.action);
    return MP.i18n.has(k) ? t(k) : item.action || "—";
  }

  function retention(r) {
    const data = scopeData(r);
    return (data && data.retention) || { historyDays: 90, trashDays: 90, minVersions: 10 };
  }

  function trashKeepText(r) {
    const days = retention(r).trashDays;
    return days > 0 ? t("trashKeep", { days: days }) : t("trashKeepForever");
  }

  /**
   * 查看一份内容（历史版本、回收站条目、已遗忘文件）。
   * opts: title, meta, content(原文), current(当前在册原文，可选：有就能对比),
   * actions([{label, kind, run}])：run 返回 Promise，完成后关闭弹窗
   */
  function openViewer(opts) {
    const modes = [];
    if (typeof opts.current === "string") {
      modes.push({ value: "compare", label: t("viewCompare") });
    }
    modes.push({ value: "preview", label: t("viewPreview") }, { value: "raw", label: t("viewRaw") });
    const area = h("div", { class: "viewer-area" });
    function paint(mode) {
      if (mode === "compare") {
        ui.replace(
          area,
          h("p", { class: "hint" }, t("compareHint")),
          MP.diff.render(MP.diff.diffLines(opts.current, opts.content))
        );
      } else if (mode === "raw") {
        ui.replace(area, h("pre", { class: "pre" }, opts.content || ""));
      } else {
        const bodyText = MP.markdown.stripFrontmatter(opts.content || "");
        ui.replace(area, bodyText.trim() ? MP.markdown.render(bodyText) : ui.emptyState({ title: t("bodyEmpty"), compact: true }));
      }
      return true;
    }
    const body = h(
      "div",
      { class: "viewer" },
      opts.meta ? h("p", { class: "viewer-meta" }, opts.meta) : null,
      ui.segmented(modes, modes[0].value, paint, t("viewModeLabel")),
      area
    );
    paint(modes[0].value);
    const actions = (opts.actions || []).map(function (action) {
      return {
        label: action.label,
        kind: action.kind,
        icon: action.icon,
        onClick: async function (ctrl, btn) {
          ctrl.setBusy(true);
          try {
            const ok = await action.run();
            ctrl.setBusy(false);
            if (ok !== false) {
              ctrl.close(true);
            }
          } catch (err) {
            ctrl.setBusy(false);
            ui.toast(err.message, "error");
          }
        },
      };
    });
    actions.push({ label: t("close"), autofocus: true });
    ui.dialog({ title: opts.title, body: body, size: "lg", actions: actions });
  }

  // ------------------------------------------------------------ 文件详情

  function afterRemoval(view, r) {
    return store.changed(r).then(function () {
      return openPane(view, { kind: view === "whisper" ? "session" : "empty" }, { skipGuard: true, fresh: true });
    });
  }

  async function confirmAndPost(opts) {
    const ok = await ui.confirm(opts.confirm);
    if (!ok) {
      return false;
    }
    const res = await ui.apiPost(opts.endpoint, opts.body);
    ui.toast(ui.resultMessage(res, opts.done || t("done")));
    return true;
  }

  function deleteFile(view, r, name) {
    return confirmAndPost({
      confirm: {
        title: t("deleteTitle", { file: name }),
        points: [t("deletePoint"), trashKeepText(r)],
        confirmLabel: t("delete"),
        danger: true,
      },
      endpoint: "file/delete",
      body: { scope: r.scope, key: r.key, file: name },
      done: t("deleted"),
    }).then(function (ok) {
      return ok ? afterRemoval(view, r) : false;
    });
  }

  function forgetFile(view, r, name) {
    const data = scopeData(r) || {};
    return confirmAndPost({
      confirm: {
        title: t("forgetTitle", { file: name }),
        points: [
          t("forgetPoint"),
          // 自动恢复说的是"这个人再出现 / 机器人去读"，留言板的告示不是这样：只能手动恢复
          data.forgettingEnabled && r.scope !== "board" ? t("forgetAutoRestore") : t("forgetManualRestore"),
        ],
        confirmLabel: t("forget"),
      },
      endpoint: "file/forget",
      body: { scope: r.scope, key: r.key, file: name },
      done: t("forgotten"),
    }).then(function (ok) {
      return ok ? afterRemoval(view, r) : false;
    });
  }

  /** 彻底删除（含历史）：在册或已遗忘的文件、全部历史版本、回收站副本、整理记录里的副本 */
  function purgeFile(r, name) {
    return confirmAndPost({
      confirm: {
        title: t("purgeTitle", { file: name }),
        message: t("purgeMessage"),
        points: [t("purgePoint1"), t("purgePoint2"), t("purgePoint3")],
        confirmLabel: t("purgeFile"),
        danger: true,
      },
      endpoint: "file/purge",
      body: { scope: r.scope, key: r.key, file: name },
      done: t("purged"),
    });
  }

  /** 按钮点击：执行期间转圈并忽略重复点击，出错弹提示 */
  function action(fn) {
    return function (event) {
      const btn = event.currentTarget;
      ui.withBusy(btn, async function () {
        try {
          await fn(btn);
        } catch (err) {
          ui.toast(err.message, "error");
        }
      });
    };
  }

  function metaRow(label, value) {
    if (value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length)) {
      return null;
    }
    return h("div", { class: "meta-item" }, h("dt", null, label), h("dd", null, value));
  }

  function wikiRenderer(view, r) {
    return function (target) {
      const name = normalizeName(target);
      if (findRow(r, name)) {
        return h(
          "button",
          { type: "button", class: "md-wiki", title: t("openLink"), onclick: function () { openFile(view, name); } },
          icon("link"),
          target
        );
      }
      return h("span", { class: "md-wiki is-missing", title: t("linkMissing") }, icon("link"), target);
    };
  }

  function fileMeta(r, d) {
    const board = r.scope === "board";
    const origins = (d.origin || []).slice().reverse();
    return h(
      "dl",
      { class: "meta-grid" },
      board ? null : metaRow(t("fieldType"), [typeTag(d.type), h("span", { class: "meta-note" }, t("typeHint_" + d.type))]),
      board ? metaRow(t("fieldSignature"), d.signature || t("noSignature")) : null,
      board ? metaRow(t("fieldExpiresShort"), d.expires ? [d.expires, d.expired ? h("span", { class: "tag tag-warn" }, t("expired")) : null] : t("noExpiry")) : null,
      metaRow(t("metaCreated"), d.created || "—"),
      metaRow(t("metaUpdated"), d.updated || ui.fmtTime(d.mtime)),
      metaRow(
        t("metaOrigin"),
        origins.length
          ? h("ul", { class: "origin-list" }, origins.map(function (item) { return h("li", null, originFull(item)); }))
          : d.source
            ? t("sourceShort", { ids: d.source })
            : h("span", { class: "meta-note" }, t("originNone"))
      ),
      metaRow(t("metaUsage"), d.uses ? t("usageLine", { time: ui.fmtRel(d.lastUsed), n: d.uses }) : t("neverUsed")),
      metaRow(t("metaSize"), ui.fmtBytes(byteLength(d.content)))
    );
  }

  function contentPanel(view, r, name, d) {
    const row = findRow(r, name) || {};
    const notices = [];
    if (!d.hasFrontmatter) {
      notices.push(ui.notice("info", t("noFrontmatterNotice")));
    }
    if (d.expired) {
      notices.push(ui.notice("warn", t("expiredNotice", { date: d.expires })));
    }
    if ((row.long || []).length) {
      // 过长的原因是后端写的中文句子：中文界面带上，其他语言只说"偏长"
      notices.push(
        ui.notice("warn", ui.zhOnly(row.long.join(t("semiSep"))) ? t("longNotice", { reasons: row.long.join(t("semiSep")) }) : t("longNoticePlain"))
      );
    }
    if ((row.dangling || []).length) {
      notices.push(ui.notice("warn", t("danglingNotice", { files: row.dangling.join(t("listSep")) })));
    }
    const bodyText = d.body || "";
    return [
      notices,
      fileMeta(r, d),
      h(
        "section",
        { class: "doc", "aria-label": t("fieldBody") },
        bodyText.trim() ? MP.markdown.render(bodyText, { wiki: wikiRenderer(view, r) }) : ui.emptyState({ title: t("bodyEmpty"), compact: true })
      ),
    ];
  }

  function dangerZone(title, hint, button) {
    return h(
      "section",
      { class: "danger-zone", "aria-label": title },
      h("div", { class: "danger-text" }, h("h3", null, title), h("p", null, hint)),
      button
    );
  }

  function historyPanel(r, name, st, actions) {
    const keep = retention(r);
    const hint = keep.historyDays > 0
      ? t("historyHint", { days: keep.historyDays, n: keep.minVersions })
      : t("historyHintForever");
    if (st.versionsError) {
      return ui.errorState(st.versionsError, actions.loadVersions);
    }
    if (!st.versions) {
      return ui.spinner();
    }
    if (!st.versions.length) {
      return [h("p", { class: "hint" }, hint), ui.emptyState({ icon: "history", title: t("noHistoryTitle"), hint: t("noHistoryHint"), compact: true })];
    }
    return [
      h("p", { class: "hint" }, hint),
      h(
        "ol",
        { class: "timeline" },
        st.versions.map(function (v) {
          return h(
            "li",
            { class: "tl-item" },
            h("span", { class: "tl-dot", "aria-hidden": "true" }),
            h(
              "div",
              { class: "tl-body" },
              h(
                "div",
                { class: "tl-head" },
                h("strong", null, ui.fmtTime(v.ts)),
                h("span", { class: "tag tag-muted" }, versionLabel(v.action)),
                h("span", { class: "tl-meta" }, actorLabel(v.actor) + " · " + ui.fmtBytes(v.size))
              ),
              h(
                "div",
                { class: "tl-actions" },
                h("button", { type: "button", class: "btn btn-sm", onclick: action(function () { return actions.view(v); }) }, icon("compare"), t("viewVersion")),
                h("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: action(function () { return actions.restore(v); }) }, icon("restore"), t("restoreVersion"))
              )
            )
          );
        })
      ),
    ];
  }

  // ------------------------------------------------------------ 移动文件

  /** 移动的目标：悄悄话 → 小本本列出各机器人；小本本 → 悄悄话只列已有对话 */
  function moveTargets(r, toNotebook, info) {
    const items = [];
    if (toNotebook) {
      const source = store.whisperByKey(r.key);
      const platform = (source && source.platform) || "";
      const seen = {};
      function add(name) {
        if (!name || seen[name]) {
          return;
        }
        seen[name] = true;
        items.push({
          platform: name,
          label: t("notebookTitle", { platform: name }),
          sub: "",
          selected: name === platform,
        });
      }
      ((S.ov && S.ov.notebooks) || []).forEach(function (item) { add(item.platform || item.key); });
      // 这段对话自己的机器人先列出来：拿不到机器人清单（migrate/info 失败）时，
      // 默认目标也不会缺失
      add(platform);
      ((info && info.platforms) || []).forEach(function (item) { add(item.platform); });
      ((info && info.adapters) || []).forEach(add);
      return items;
    }
    ((S.ov && S.ov.whispers) || []).forEach(function (item) {
      items.push({
        ref: store.ref("whisper", item.key),
        label: store.sessionTitle(item),
        sub: store.sessionSub(item),
        selected: false,
      });
    });
    return items;
  }

  /**
   * 移动一个文件（PLAN 3.12.2）：悄悄话 ↔ 小本本。
   * 先选目标；目标里有同名文件时，让用户选保留两份（新名字加序号）、
   * 覆盖（旧版进历史）或取消；取消只关弹窗，不发写请求。
   */
  async function moveFile(view, r, name) {
    const toNotebook = r.scope === "whisper";
    let info = null;
    try {
      info = await ui.apiGet("migrate/info");
    } catch (err) {
      // 拿不到机器人清单时仍然可以列已有小本本与对话，不影响移动
      info = null;
    }
    const targets = moveTargets(r, toNotebook, info);
    if (!targets.length) {
      ui.toast(t("moveNoTargets"), "info");
      return;
    }
    let picked = targets.find(function (item) { return item.selected; }) || null;
    const search = h("input", { class: "input", type: "search", placeholder: t("moveSearch"), "aria-label": t("moveSearch") });
    const list = h("div", { class: "move-targets", role: "group", "aria-label": t("moveTargetLabel") });
    const empty = h("p", { class: "hint" }, t("moveNoMatch"));
    empty.hidden = true;
    const warning = ui.notice("warn", "");
    const warningBody = warning.querySelector(".notice-body");

    function targetText(item) {
      return (item.label + " " + (item.sub || "") + " " + (item.platform || "")).toLowerCase();
    }

    function updateWarning() {
      let text = t("movePickHint");
      if (picked) {
        text = toNotebook
          ? t("moveNotebookConsequence", { platform: picked.platform })
          : t("moveWhisperConsequence");
      }
      ui.replace(warningBody, text);
    }

    function selectTarget(item, button) {
      picked = item;
      Array.prototype.forEach.call(list.querySelectorAll("button"), function (other) {
        const on = other === button;
        other.setAttribute("aria-pressed", on ? "true" : "false");
        other.classList.toggle("is-on", on);
      });
      updateWarning();
    }

    const buttons = targets.map(function (item) {
      return h(
        "button",
        {
          type: "button",
          class: "move-target" + (picked === item ? " is-on" : ""),
          "aria-pressed": picked === item ? "true" : "false",
          "data-search": targetText(item),
          onclick: function (event) { selectTarget(item, event.currentTarget); },
        },
        h("span", { class: "move-target-title" }, item.label),
        item.sub ? h("span", { class: "move-target-sub" }, item.sub) : null
      );
    });
    ui.replace(list, buttons);
    search.addEventListener("input", function () {
      const query = search.value.trim().toLowerCase();
      let shown = 0;
      buttons.forEach(function (button) {
        const hit = !query || button.getAttribute("data-search").indexOf(query) >= 0;
        button.hidden = !hit;
        if (hit) {
          shown += 1;
        }
      });
      empty.hidden = shown > 0;
    });
    updateWarning();

    function payloadOf() {
      return {
        from: { scope: r.scope, key: r.key },
        to: picked.platform
          ? { scope: "notebook", platform: picked.platform }
          : { scope: "whisper", key: picked.ref.key },
        file: name,
        dryRun: true,
        conflict: "overwrite",
      };
    }

    function afterMove(targetRef) {
      return store.changed(r)
        .then(function () { return store.loadScope(targetRef); })
        .then(function () {
          return openPane(view, { kind: view === "whisper" ? "session" : "empty" }, { skipGuard: true, fresh: true });
        });
    }

    async function sendMove(dlg, conflict, payload, targetRef) {
      if (dlg) {
        dlg.setBusy(true);
      }
      try {
        const res = await ui.apiPost("migrate/file", Object.assign({}, payload, { dryRun: false, conflict: conflict }));
        const renamed = res.renamed && res.renamed.length ? res.renamed[0].to : "";
        const fallback = !res.moved
          ? t("moveSourceKept")
          : renamed
            ? t("movedRenamed", { file: name, renamed: renamed })
            : t("moved");
        ui.toast(ui.resultMessage(res, fallback), res.moved ? "info" : "warn");
        if (dlg) {
          dlg.close(null);
        }
        await afterMove(targetRef);
      } catch (err) {
        // 接口报错（文件刚被别处删掉、名字加不上序号等）：说明原因，弹窗留着可以重试或取消
        ui.toast(err.message, "error");
      } finally {
        if (dlg) {
          dlg.setBusy(false);
        }
      }
    }

    /** 同名冲突：三个选择，取消不发请求 */
    function askConflict(payload, keepPreview, targetRef) {
      const renamed = keepPreview.renamed && keepPreview.renamed.length ? keepPreview.renamed[0].to : "";
      const ctrl = ui.dialog({
        title: t("conflictTitle"),
        size: "sm",
        alert: true,
        body: h(
          "div",
          { class: "confirm" },
          h("p", { class: "confirm-message" }, t("conflictMessage", { file: name })),
          toNotebook
            ? ui.notice("warn", t("moveNotebookConsequence", { platform: picked.platform }))
            : ui.notice("info", t("moveWhisperConsequence")),
          h(
            "ul",
            { class: "confirm-points" },
            h("li", null, renamed ? t("conflictKeepPoint", { name: renamed }) : t("conflictKeepPointPlain")),
            h(
              "li",
              null,
              t(keepPreview.forgotten ? "conflictOverwriteForgottenPoint" : "conflictOverwritePoint")
            )
          )
        ),
        actions: [
          { label: t("cancel"), value: "cancel", autofocus: true },
          {
            label: t("conflictKeep"),
            onClick: function (dlg) { return sendMove(dlg, "keep", payload, targetRef); },
          },
          {
            label: t("conflictOverwrite"),
            kind: "danger",
            onClick: function (dlg) { return sendMove(dlg, "overwrite", payload, targetRef); },
          },
        ],
      });
      return ctrl.done;
    }

    async function confirmNoConflict(payload, preview, targetRef) {
      const counts = preview.counts || {};
      const ok = await ui.confirm({
        title: t("moveConfirmTitle", { file: name }),
        points: [
          toNotebook
            ? t("moveNotebookConsequence", { platform: picked.platform })
            : t("moveWhisperConsequence"),
          t("moveToTrashPoint"),
          t("moveWillWrite", { n: (counts.added || 0) + (counts.replaced || 0) }),
        ],
        confirmLabel: t("moveConfirm"),
        danger: true,
      });
      if (ok) {
        // create：预览时目标里没有同名文件；真正写入时若刚出现同名文件，
        // 后端会拒绝，不能替用户直接覆盖
        await sendMove(null, "create", payload, targetRef);
      }
    }

    const ctrl = ui.dialog({
      title: toNotebook ? t("moveToNotebook") : t("moveToWhisper"),
      size: "md",
      body: h(
        "div",
        { class: "stack" },
        h("p", { class: "hint" }, t("moveHint")),
        search,
        list,
        empty,
        warning
      ),
      actions: [
        { label: t("cancel"), value: false },
        {
          label: t("moveConfirm"),
          kind: "primary",
          onClick: async function (dlg) {
            if (!picked) {
              ui.toast(t("movePickHint"), "error");
              return;
            }
            const payload = payloadOf();
            dlg.setBusy(true);
            let previews;
            try {
              previews = await Promise.all([
                ui.apiPost("migrate/file", payload),
                ui.apiPost("migrate/file", Object.assign({}, payload, { conflict: "keep" })),
              ]);
            } catch (err) {
              // 预演失败（文件刚被别处删掉、目标标识非法等）：说明原因，弹窗留着
              ui.toast(err.message, "error");
              return;
            } finally {
              dlg.setBusy(false);
            }
            const preview = previews[0];
            const keepPreview = previews[1];
            const targetRef = { scope: preview.to.scope, key: preview.to.key };
            dlg.close(null);
            if (preview.conflict) {
              await askConflict(payload, keepPreview, targetRef);
            } else {
              await confirmNoConflict(payload, preview, targetRef);
            }
          },
        },
      ],
    });
    return ctrl.done;
  }

  function FileDetail(view, r, name, initialTab) {
    const root = h("article", { class: "detail" });
    const st = { data: null, error: "", tab: initialTab || "content", versions: null, versionsError: "" };
    let token = 0;

    /** 读文件。成功返回 true；失败返回 false，已有内容时留着旧内容并在上面提示 */
    async function load() {
      const mine = ++token;
      try {
        const data = await ui.apiGet("file", { scope: r.scope, key: r.key, file: name });
        if (mine === token) {
          st.data = data;
          st.error = "";
        }
      } catch (err) {
        if (mine === token) {
          st.error = err.message;
        }
      }
      if (mine === token) {
        paint();
        return !st.error;
      }
      return true;
    }

    /** 顶部刷新用：文件和历史版本都重读（历史版本只在看过以后才有） */
    async function reload() {
      const had = st.versions !== null;
      const results = await Promise.all([load(), had ? loadVersions() : true]);
      return results.every(function (item) { return item !== false; });
    }

    async function loadVersions() {
      st.versionsError = "";
      try {
        const data = await ui.apiGet("history/versions", { scope: r.scope, key: r.key, file: name });
        st.versions = data.versions || [];
      } catch (err) {
        st.versionsError = err.message;
      }
      paint();
      return !st.versionsError;
    }

    async function viewVersion(v) {
      const data = await ui.apiGet("history/file", { scope: r.scope, key: r.key, file: name, version: v.version });
      openViewer({
        title: t("versionTitle", { time: ui.fmtTime(v.ts) }),
        meta: versionLabel(v.action) + " · " + actorLabel(v.actor) + " · " + ui.fmtBytes(v.size),
        content: data.content || "",
        current: st.data ? st.data.content || "" : undefined,
        actions: [{ label: t("restoreVersion"), kind: "primary", icon: "restore", run: function () { return restoreVersion(v); } }],
      });
    }

    async function restoreVersion(v) {
      const ok = await confirmAndPost({
        confirm: {
          title: t("restoreVersionTitle"),
          message: t("restoreVersionMessage", { file: name, time: ui.fmtTime(v.ts) }),
          points: [t("restoreVersionPoint")],
          confirmLabel: t("restoreVersion"),
        },
        endpoint: "history/restore",
        body: { scope: r.scope, key: r.key, file: name, version: v.version },
        done: t("versionRestored"),
      });
      if (ok) {
        st.versions = null;
        await Promise.all([store.changed(r), load(), loadVersions()]);
      }
      return ok;
    }

    function headActions(d) {
      const board = r.scope === "board";
      return [
        h("button", { type: "button", class: "btn btn-primary btn-sm", "data-fk": "edit", onclick: function () { openPane(view, { kind: "edit", file: name }, { fresh: true }); } }, icon("edit"), t("edit")),
        // 留言板不参与移动（PLAN 3.12.2）：只有小本本与悄悄话之间能搬
        board
          ? null
          : h(
              "button",
              { type: "button", class: "btn btn-sm", "data-fk": "move", onclick: action(function () { return moveFile(view, r, name); }) },
              icon("transfer"),
              r.scope === "whisper" ? t("moveToNotebook") : t("moveToWhisper")
            ),
        board
          ? h("button", { type: "button", class: "btn btn-sm", onclick: action(function () { return renewNotice(r, name, d).then(function (ok) { if (ok) { load(); } }); }) }, icon("calendar"), t("renew"))
          : null,
        // 留言板的告示也能手动遗忘（旧页面就可以）：撤下但保留文件，在「已遗忘」里恢复
        h("button", { type: "button", class: "btn btn-sm", onclick: action(function () { return forgetFile(view, r, name); }) }, icon("archive"), t("forget")),
        h("button", { type: "button", class: "btn btn-sm btn-danger-ghost", onclick: action(function () { return deleteFile(view, r, name); }) }, icon("trash"), t("delete")),
      ];
    }

    function paint() {
      const d = st.data;
      if (!d) {
        // 读的时候和读失败时也有标题栏：窄屏有返回按钮，打开详情后的焦点也有地方落。
        // 标题、类型、描述先用列表里已有的那一行，读完后不会整块跳变
        const pending = pendingHead(view, r, name);
        root.setAttribute("aria-labelledby", pending.id);
        // 标题已经出来、正文还在读：告诉读屏软件这块还在加载
        root.setAttribute("aria-busy", "true");
        ui.replace(root, pending.el, st.error ? ui.errorState(st.error, load) : ui.spinner());
        return;
      }
      root.removeAttribute("aria-busy");
      const board = r.scope === "board";
      const head = detailHead({
        view: view,
        title: board ? d.description || name : name,
        mono: !board,
        tags: board ? null : typeTag(d.type),
        desc: board ? name : d.description || t("noDescription"),
        actions: headActions(d),
      });
      root.setAttribute("aria-labelledby", head.id);
      const tabId = ui.uid("tab");
      const panel = h("div", { class: "tab-panel", role: "tabpanel", id: tabId + "-panel", "aria-labelledby": tabId + "-" + st.tab });
      if (st.tab === "history") {
        ui.replace(panel, historyPanel(r, name, st, { loadVersions: loadVersions, view: viewVersion, restore: restoreVersion }));
      } else {
        ui.replace(
          panel,
          contentPanel(view, r, name, d),
          dangerZone(
            t("purgeFile"),
            t("purgeFileHint"),
            h("button", { type: "button", class: "btn btn-sm btn-danger-ghost", onclick: action(function () { return purgeFile(r, name).then(function (ok) { return ok ? afterRemoval(view, r) : false; }); }) }, icon("trash"), t("purgeFile"))
          )
        );
      }
      const tabs = ui.tabs(
        [
          { value: "content", label: t("tabContent"), id: tabId + "-content", controls: panel.id },
          { value: "history", label: t("historyTitle"), id: tabId + "-history", controls: panel.id, count: st.versions ? st.versions.length : null },
        ],
        st.tab,
        function (value) {
          st.tab = value;
          if (value === "history" && !st.versions) {
            loadVersions();
          }
          paint();
          const next = root.querySelector('[role="tab"][aria-selected="true"]');
          if (next) {
            next.focus();
          }
        },
        t("detailTabsLabel")
      );
      // 重新读取失败时还显示着上次读到的内容：在上面说明，别让人以为是最新的
      ui.replace(root, head.el, st.error ? ui.staleNotice(st.error, load) : null, tabs, panel);
    }

    paint();
    load();
    if (st.tab === "history") {
      loadVersions();
    }
    return { el: root, reload: reload };
  }

  // ------------------------------------------------------------ 编辑器

  const FIELDS = ["file", "type", "description", "body", "signature", "expires", "raw"];
  const FORM_FIELDS = ["file", "type", "description", "body", "signature", "expires"];

  function autoRows(textarea, min, max) {
    const lines = String(textarea.value || "").split("\n").length;
    textarea.rows = Math.max(min, Math.min(max, lines + 1));
  }

  /** 到期日输入框加几个常用选项：一周、一个月、三个月后，或不设 */
  function expiryInput(value, onChange) {
    const input = h("input", { type: "date", class: "input input-date", value: value || "" });
    input.addEventListener("input", function () { onChange(input.value); });
    function quick(label, days) {
      return h(
        "button",
        {
          type: "button",
          class: "chip",
          onclick: function () {
            input.value = days === null ? "" : ui.dateAfter(days);
            onChange(input.value);
          },
        },
        label
      );
    }
    const extra = h(
      "div",
      { class: "chips chips-quick", role: "group", "aria-label": t("expiryQuickLabel") },
      quick(t("expiryWeek"), 7),
      quick(t("expiryMonth"), 30),
      quick(t("expiryQuarter"), 90),
      quick(t("expiryNone"), null)
    );
    return { input: input, after: extra };
  }

  /** 表单字段拼成的一行（「署名：堇」/「Signature: 堇」）：冒号跟着界面语言走 */
  function fieldLine(label, value) {
    return t("fieldLine", { label: label, value: value });
  }

  function formText(board, v) {
    const lines = [fieldLine(t("fieldFile"), v.file)];
    if (!board) {
      lines.push(fieldLine(t("fieldType"), TYPES.indexOf(v.type) >= 0 ? t("type_" + v.type) : v.type || "—"));
    }
    lines.push(fieldLine(t("fieldDescription"), v.description));
    if (board) {
      lines.push(fieldLine(t("fieldSignature"), v.signature));
      lines.push(fieldLine(t("fieldExpiresShort"), v.expires || t("noExpiry")));
    }
    lines.push("");
    return lines.join("\n") + "\n" + (v.body || "");
  }

  /** 编辑或新建一条记忆。opts: {isNew, file}；表单与原文两种方式，保存前显示改动对比 */
  function Editor(view, r, opts) {
    const board = r.scope === "board";
    const root = h("div", { class: "detail editor" });
    const st = { mode: "form", preview: false, error: "", ready: Boolean(opts.isNew) };
    const fields = {};
    let base = null;
    let cur = null;
    let clean = false;
    let dirtyBadge = null;
    let previewBox = null;

    function initFrom(data) {
      base = {
        file: data ? data.file : "",
        type: board ? "" : (data && data.type) || "note",
        description: data ? data.description || "" : "",
        body: data ? data.body || "" : "",
        signature: data ? data.signature || "" : "",
        expires: data ? data.expires || "" : "",
        raw: data ? data.content || "" : "",
        mtime: data ? data.mtime || 0 : 0,
        hasFrontmatter: data ? data.hasFrontmatter !== false : true,
      };
      cur = Object.assign({}, base);
    }

    function changedIn(keys) {
      return Boolean(cur) && keys.some(function (k) { return cur[k] !== base[k]; });
    }

    function isDirty() {
      return !clean && changedIn(FIELDS);
    }

    function touch() {
      if (dirtyBadge) {
        dirtyBadge.hidden = !isDirty();
      }
      if (st.preview) {
        paintPreview();
      }
    }

    function bodyText() {
      return st.mode === "raw" ? MP.markdown.stripFrontmatter(cur.raw) : cur.body;
    }

    function paintPreview() {
      if (!previewBox) {
        return;
      }
      const text = bodyText();
      ui.replace(previewBox, text.trim() ? MP.markdown.render(text, { wiki: wikiRenderer(view, r) }) : ui.emptyState({ title: t("previewEmpty"), compact: true }));
    }

    /** 换编辑方式：当前方式里有没保存的修改就先确认放弃，免得保存时不知道以哪边为准 */
    async function switchMode(next) {
      const keys = st.mode === "form" ? FORM_FIELDS.filter(function (k) { return k !== "file"; }) : ["raw"];
      if (changedIn(keys)) {
        const ok = await ui.confirm({
          title: t("switchModeTitle"),
          message: t(st.mode === "form" ? "switchToRawMessage" : "switchToFormMessage"),
          confirmLabel: t("discardAndSwitch"),
          danger: true,
        });
        if (!ok) {
          return false;
        }
        keys.forEach(function (k) { cur[k] = base[k]; });
      }
      st.mode = next;
      paint();
      return true;
    }

    function personCheck() {
      const name = normalizeName(cur.file);
      if (board || st.mode !== "form" || !name) {
        return "";
      }
      const person = isPersonFile(name);
      if (!person && PERSON_OWNER_RE.test(name)) {
        return t("errPersonBadName");
      }
      if (cur.type === "person" && !person) {
        return t("errPersonName");
      }
      if (person && cur.type !== "person") {
        return t("errPersonType");
      }
      return "";
    }

    function textInput(key, attrs) {
      return h("input", Object.assign({
        class: "input",
        value: cur[key],
        autocomplete: "off",
        oninput: function (event) {
          cur[key] = event.target.value;
          touch();
        },
      }, attrs || {}));
    }

    function bodyArea(key, mono) {
      const area = h("textarea", {
        class: "textarea" + (mono ? " mono" : ""),
        value: cur[key],
        spellcheck: "false",
        oninput: function (event) {
          cur[key] = event.target.value;
          autoRows(event.target, 12, 36);
          touch();
        },
      });
      autoRows(area, 12, 36);
      return area;
    }

    function fileField() {
      const input = textInput("file", { class: "input mono", spellcheck: "false", placeholder: board ? "notice-20261111.md" : "user-123456.md", "data-autofocus": opts.isNew ? "" : null });
      input.addEventListener("input", function () {
        fields.file.setError(personCheck() && cur.type === "person" ? personCheck() : "");
      });
      fields.file = ui.field({ label: t("fieldFile"), input: input, hint: opts.isNew ? t("fieldFileHint") : t("fieldFileRenameHint"), required: true });
      return fields.file.el;
    }

    function typeField() {
      const hint = h("span", null, t("typeHint_" + cur.type));
      const select = h(
        "select",
        {
          class: "input",
          onchange: function (event) {
            cur.type = event.target.value;
            hint.textContent = t("typeHint_" + cur.type);
            fields.type.setError("");
            touch();
          },
        },
        TYPES.map(function (type) {
          return h("option", { value: type, selected: cur.type === type }, t("type_" + type));
        })
      );
      fields.type = ui.field({ label: t("fieldType"), input: select, hint: hint });
      return fields.type.el;
    }

    function formFields() {
      const out = [];
      if (!base.hasFrontmatter) {
        out.push(ui.notice("info", t("noFrontmatterNotice")));
      }
      out.push(h("div", { class: "form-grid two" }, fileField(), board ? null : typeField()));
      fields.description = ui.field({ label: t("fieldDescription"), input: textInput("description", { maxlength: board ? "120" : null }), hint: board ? t("noticeTitleHint") : t("fieldDescriptionHint"), required: true });
      out.push(fields.description.el);
      if (board) {
        fields.signature = ui.field({ label: t("fieldSignature"), input: textInput("signature", { maxlength: "100" }), hint: t("fieldSignatureEditHint"), required: true });
        const expiry = expiryInput(cur.expires, function (value) {
          cur.expires = value;
          touch();
        });
        fields.expires = ui.field({ label: t("fieldExpires"), input: expiry.input, after: expiry.after, hint: t("fieldExpiresHint"), optional: true });
        out.push(h("div", { class: "form-grid two" }, fields.signature.el, fields.expires.el));
      }
      fields.body = ui.field({ label: t("fieldBody"), input: bodyArea("body", false), hint: t("fieldBodyHint") });
      out.push(fields.body.el);
      return out;
    }

    function rawFields() {
      fields.raw = ui.field({ label: t("fieldRaw"), input: bodyArea("raw", true), hint: board ? t("fieldRawBoardHint") : t("fieldRawHint") });
      return [h("div", { class: "form-grid two" }, fileField()), fields.raw.el];
    }

    /** 校验并把错误写到对应的输入框下面；返回第一个出错的输入框 */
    function validate() {
      Object.keys(fields).forEach(function (k) { fields[k].setError(""); });
      const problems = [];
      const name = normalizeName(cur.file);
      const nameError = nameProblem(name);
      if (nameError) {
        problems.push(["file", nameError]);
      } else if (!opts.isNew && name !== base.file && findRow(r, name)) {
        problems.push(["file", t("errFileExists", { file: name })]);
      } else if (opts.isNew && findRow(r, name)) {
        problems.push(["file", t("errFileExists", { file: name })]);
      }
      if (st.mode === "form") {
        const person = personCheck();
        if (person) {
          problems.push([cur.type === "person" || !fields.type ? "file" : "type", person]);
        }
        if (!cur.description.trim()) {
          problems.push(["description", t("needDescription")]);
        }
        if (board && !cur.signature.trim()) {
          problems.push(["signature", t("signatureRequired")]);
        }
        if (board && cur.expires && !DATE_RE.test(cur.expires)) {
          problems.push(["expires", t("errDate")]);
        }
      } else if (!cur.raw.trim()) {
        problems.push(["raw", t("errEmpty")]);
      }
      problems.forEach(function (item) {
        if (fields[item[0]]) {
          fields[item[0]].setError(item[1]);
        }
      });
      return problems.length && fields[problems[0][0]] ? fields[problems[0][0]].input : null;
    }

    function confirmSave(name, rows, renamed) {
      const body = h(
        "div",
        { class: "save-confirm" },
        renamed ? ui.notice("info", t("renameNotice", { from: base.file, to: name })) : null,
        h("p", { class: "hint" }, t(st.mode === "raw" ? "diffRawHint" : "diffFormHint")),
        MP.diff.render(rows)
      );
      const ctrl = ui.dialog({
        title: opts.isNew ? t("createTitle", { file: name }) : t("saveTitle", { file: name }),
        body: body,
        size: "lg",
        actions: [
          { label: t("cancel"), value: false },
          { label: opts.isNew ? t("create") : t("save"), kind: "primary", value: true, autofocus: true },
        ],
      });
      return ctrl.done.then(function (value) { return value === true; });
    }

    async function save() {
      if (!cur) {
        return;
      }
      const invalid = validate();
      if (invalid) {
        invalid.focus();
        return;
      }
      const name = normalizeName(cur.file);
      const renamed = !opts.isNew && name !== base.file;
      const before = opts.isNew ? "" : st.mode === "raw" ? base.raw : formText(board, base);
      const after = st.mode === "raw" ? cur.raw : formText(board, Object.assign({}, cur, { file: name }));
      const rows = MP.diff.diffLines(before, after);
      if (!MP.diff.stats(rows).changed && !renamed) {
        ui.toast(t("noChanges"), "info");
        return;
      }
      if (!(await confirmSave(name, rows, renamed))) {
        return;
      }
      const payload = {
        scope: r.scope,
        key: r.key,
        file: name,
        oldFile: opts.isNew ? "" : base.file,
        mode: st.mode === "raw" ? "raw" : "fields",
        type: board ? "" : cur.type,
        description: cur.description,
        body: cur.body,
        signature: cur.signature,
        content: cur.raw,
        baseMtime: base.mtime,
      };
      if (board) {
        // 到期日只属于留言板；空串表示清掉到期日
        payload.expires = cur.expires || "";
      }
      const res = await ui.apiPost("file/save", payload);
      clean = true;
      ui.toast(ui.resultMessage(res, t("saved")));
      await store.changed(r);
      await openPane(view, { kind: "file", file: res.file || name }, { skipGuard: true, fresh: true });
    }

    function cancel() {
      if (opts.isNew) {
        return openPane(view, { kind: view === "whisper" ? "session" : "empty" });
      }
      return openPane(view, { kind: "file", file: base.file }, { fresh: true });
    }

    const saveAction = action(save);

    function paint() {
      if (!st.ready) {
        const pending = opts.isNew ? detailHead({ view: view, title: t("newMemory"), mono: true }) : pendingHead(view, r, opts.file);
        root.setAttribute("aria-labelledby", pending.id);
        root.setAttribute("aria-busy", "true");
        ui.replace(root, pending.el, st.error ? ui.errorState(st.error, load) : ui.spinner());
        return;
      }
      root.removeAttribute("aria-busy");
      dirtyBadge = h("span", { class: "dirty", hidden: !isDirty() }, h("span", { class: "dot dot-warn", "aria-hidden": "true" }), t("unsaved"));
      const title = opts.isNew ? t("newMemory") : board ? t("editNotice") : t("editFile", { file: base.file });
      const head = detailHead({
        view: view,
        title: title,
        tags: dirtyBadge,
        actions: [
          h("button", { type: "button", class: "btn btn-primary btn-sm", "data-fk": "save", onclick: saveAction }, icon("check"), opts.isNew ? t("create") : t("save")),
          h("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: function () { cancel(); } }, t("cancel")),
        ],
      });
      root.setAttribute("aria-labelledby", head.id);
      const previewToggle = h(
        "button",
        {
          type: "button",
          class: "btn btn-sm btn-ghost",
          "aria-pressed": st.preview ? "true" : "false",
          onclick: function () {
            st.preview = !st.preview;
            paint();
          },
        },
        icon("eye"),
        t("preview")
      );
      const toolbar = h(
        "div",
        { class: "editor-tools" },
        ui.segmented([{ value: "form", label: t("modeForm") }, { value: "raw", label: t("modeRaw") }], st.mode, switchMode, t("editModeLabel")),
        previewToggle,
        h("span", { class: "kbd-hint" }, t("saveShortcut"))
      );
      previewBox = st.preview ? h("div", { class: "preview", "aria-label": t("preview"), role: "region" }) : null;
      const formBox = h("div", { class: "editor-form" }, st.mode === "raw" ? rawFields() : formFields());
      const work = h("div", { class: "editor-work" + (st.preview ? " has-preview" : "") }, formBox, previewBox);
      ui.replace(root, head.el, toolbar, work);
      paintPreview();
    }

    async function load() {
      try {
        const data = await ui.apiGet("file", { scope: r.scope, key: r.key, file: opts.file });
        initFrom(data);
        st.ready = true;
        st.error = "";
      } catch (err) {
        st.error = err.message;
      }
      paint();
    }

    root.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "s") {
        event.preventDefault();
        saveAction({ currentTarget: root.querySelector('[data-fk="save"]') });
      }
    });

    if (opts.isNew) {
      initFrom(null);
      paint();
    } else {
      paint();
      load();
    }
    return { el: root, isDirty: isDirty, markClean: function () { clean = true; } };
  }

  // ------------------------------------------------------------ 发布告示

  /** 发布留言板告示：署名预填上次用过的（服务端记着），到期日可选 */
  function PublishForm() {
    const r = viewRef("board");
    const root = h("div", { class: "detail editor" });
    // 上次的署名：刚发布过就用页面里记着的（总览还在重新加载时也不会取到旧值），否则用服务端记的
    const last = S.lastSignature || (S.ov && S.ov.board && S.ov.board.lastSignature) || "";
    const cur = { file: "", signature: last, description: "", expires: "", body: "" };
    const base = Object.assign({}, cur);
    const fields = {};
    let clean = false;
    let preview = false;
    let previewBox = null;

    function isDirty() {
      return !clean && Object.keys(cur).some(function (k) { return cur[k] !== base[k]; });
    }

    function paintPreview() {
      if (previewBox) {
        ui.replace(previewBox, cur.body.trim() ? MP.markdown.render(cur.body) : ui.emptyState({ title: t("previewEmpty"), compact: true }));
      }
    }

    function input(k, attrs) {
      return h("input", Object.assign({
        class: "input",
        value: cur[k],
        autocomplete: "off",
        oninput: function (event) {
          cur[k] = event.target.value;
          fields[k].setError("");
        },
      }, attrs || {}));
    }

    function validate() {
      Object.keys(fields).forEach(function (k) { fields[k].setError(""); });
      const problems = [];
      if (!cur.signature.trim()) {
        problems.push(["signature", t("signatureRequired")]);
      }
      if (!cur.description.trim()) {
        problems.push(["description", t("errTitleRequired")]);
      }
      if (!cur.body.trim()) {
        problems.push(["body", t("errBodyRequired")]);
      } else if (cur.body.length > 8000) {
        problems.push(["body", t("errBodyTooLong", { n: 8000 })]);
      }
      if (cur.expires && !DATE_RE.test(cur.expires)) {
        problems.push(["expires", t("errDate")]);
      }
      if (cur.file.trim()) {
        // 自己起名时先查一遍：规则与后端一致，名字已被占用也不等到发布时才让服务端拒
        const name = normalizeName(cur.file.trim());
        const problem = nameProblem(name) || (findRow(r, name) ? t("errFileExists", { file: name }) : "");
        if (problem) {
          problems.push(["file", problem]);
        }
      }
      problems.forEach(function (item) { fields[item[0]].setError(item[1]); });
      return problems.length ? fields[problems[0][0]].input : null;
    }

    async function submit() {
      const invalid = validate();
      if (invalid) {
        invalid.focus();
        return;
      }
      const res = await ui.apiPost("board/publish", {
        // 留空就由服务端按时间生成 notice-<时间>.md
        file: cur.file.trim() ? normalizeName(cur.file.trim()) : "",
        signature: cur.signature.trim(),
        description: cur.description.trim(),
        body: cur.body,
        expires: cur.expires || "",
      });
      clean = true;
      S.lastSignature = cur.signature.trim().replace(/\s+/g, " ");
      ui.toast(ui.resultMessage(res, t("published")));
      await store.changed(r);
      await openPane("board", { kind: "file", file: res.file }, { skipGuard: true, fresh: true });
    }
    const submitAction = action(submit);

    function paint() {
      const head = detailHead({
        view: "board",
        title: t("publishTitle"),
        actions: [
          h("button", { type: "button", class: "btn btn-primary btn-sm", "data-fk": "save", onclick: submitAction }, icon("check"), t("publishNotice")),
          h("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: function () { openPane("board", { kind: "empty" }); } }, t("cancel")),
        ],
      });
      root.setAttribute("aria-labelledby", head.id);
      fields.signature = ui.field({ label: t("fieldSignature"), input: input("signature", { maxlength: "100", "data-autofocus": last ? null : "" }), hint: last ? t("fieldSignatureLastHint") : t("fieldSignatureHint"), required: true });
      fields.file = ui.field({ label: t("publishFile"), input: input("file", { maxlength: "121", class: "input mono", autocomplete: "off", spellcheck: "false" }), hint: t("publishFileHint"), optional: true });
      fields.description = ui.field({ label: t("noticeTitle"), input: input("description", { maxlength: "120", "data-autofocus": last ? "" : null }), hint: t("noticeTitleHint"), required: true });
      const expiry = expiryInput(cur.expires, function (value) {
        cur.expires = value;
        fields.expires.setError("");
      });
      fields.expires = ui.field({ label: t("fieldExpires"), input: expiry.input, after: expiry.after, hint: t("fieldExpiresHint"), optional: true });
      const area = h("textarea", {
        class: "textarea",
        value: cur.body,
        rows: "12",
        oninput: function (event) {
          cur.body = event.target.value;
          fields.body.setError("");
          autoRows(event.target, 12, 30);
          paintPreview();
        },
      });
      fields.body = ui.field({ label: t("fieldBody"), input: area, hint: t("noticeBodyHint"), required: true });
      previewBox = preview ? h("div", { class: "preview", role: "region", "aria-label": t("preview") }) : null;
      const toggle = h(
        "button",
        { type: "button", class: "btn btn-sm btn-ghost", "aria-pressed": preview ? "true" : "false", onclick: function () { preview = !preview; paint(); } },
        icon("eye"),
        t("preview")
      );
      ui.replace(
        root,
        head.el,
        h("p", { class: "hint" }, t("publishHint")),
        h("div", { class: "form-grid two" }, fields.signature.el, fields.expires.el),
        fields.description.el,
        fields.file.el,
        h("div", { class: "editor-tools" }, toggle, h("span", { class: "kbd-hint" }, t("publishShortcut"))),
        h("div", { class: "editor-work" + (preview ? " has-preview" : "") }, h("div", { class: "editor-form" }, fields.body.el), previewBox)
      );
      paintPreview();
    }

    root.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "s") {
        event.preventDefault();
        submitAction({ currentTarget: root.querySelector('[data-fk="save"]') });
      }
    });
    paint();
    return { el: root, isDirty: isDirty, markClean: function () { clean = true; } };
  }

  /** 续期：只改到期日，其余字段按文件现有内容原样保存。返回是否改成功 */
  function renewNotice(r, name, current) {
    let value = current && current.expires && !current.expired ? current.expires : ui.dateAfter(30);
    const expiry = expiryInput(value, function (next) { value = next; });
    expiry.input.setAttribute("data-autofocus", "");
    const fld = ui.field({ label: t("fieldExpires"), input: expiry.input, after: expiry.after, hint: t("renewHint") });
    const body = h(
      "div",
      { class: "stack" },
      h(
        "p",
        null,
        current && current.expires
          ? t(current.expired ? "renewCurrentExpired" : "renewCurrent", { date: current.expires })
          : t("renewCurrentNone")
      ),
      fld.el
    );
    const ctrl = ui.dialog({
      title: t("renewTitle", { title: (current && current.description) || name }),
      body: body,
      size: "sm",
      actions: [
        { label: t("cancel"), value: false },
        {
          label: t("renewSave"),
          kind: "primary",
          onClick: async function (dlg) {
            if (value && !DATE_RE.test(value)) {
              fld.setError(t("errDate"));
              return;
            }
            dlg.setBusy(true);
            try {
              const fresh = await ui.apiGet("file", { scope: r.scope, key: r.key, file: name });
              const res = await ui.apiPost("file/save", {
                scope: r.scope,
                key: r.key,
                file: name,
                oldFile: name,
                mode: "fields",
                type: "",
                description: fresh.description || "",
                body: fresh.body || "",
                signature: fresh.signature || "",
                expires: value || "",
                baseMtime: fresh.mtime || 0,
              });
              ui.toast(value ? t("renewed", { date: value }) : t("renewedForever"));
              dlg.setBusy(false);
              dlg.close(true);
              store.changed(r);
              return res;
            } catch (err) {
              dlg.setBusy(false);
              ui.toast(err.message, "error");
            }
          },
        },
      ],
    });
    return ctrl.done.then(function (result) { return result === true; });
  }

  // ------------------------------------------------------------ 索引

  function meter(label, text, ratio) {
    const level = ratio >= 1 ? "danger" : ratio >= 0.8 ? "warn" : "ok";
    return h(
      "div",
      { class: "meter-item" },
      h("div", { class: "meter-head" }, h("span", null, label), h("strong", null, text)),
      h("div", { class: "meter meter-" + level, "aria-hidden": "true" }, h("span", { style: { width: Math.min(100, Math.round(ratio * 100)) + "%" } }))
    );
  }

  function indexHealth(idx) {
    const out = [];
    if (idx.truncated) {
      out.push(ui.notice("warn", t("indexTruncated")));
    }
    if ((idx.missing || []).length) {
      out.push(ui.notice("warn", t("indexMissing", { n: idx.missing.length }), h("div", { class: "code-list" }, idx.missing.map(function (f) { return h("code", null, f); }))));
    }
    if ((idx.dangling || []).length) {
      out.push(ui.notice("warn", t("indexDangling", { n: idx.dangling.length }), h("div", { class: "code-list" }, idx.dangling.map(function (f) { return h("code", null, f); }))));
    }
    if (!out.length) {
      out.push(ui.notice("ok", t("indexHealthy")));
    }
    return out;
  }

  /** 索引只读（PLAN 3.12.3）：MEMORY.md 只是缓存，没有编辑入口，只能按文件重建 */
  function IndexPanel(view, r) {
    const root = h("div", { class: "detail" });
    let idx = null;
    let error = "";

    async function init() {
      try {
        const data = await store.waitScope(r);
        idx = data.index || {};
        error = "";
      } catch (err) {
        error = err.message;
      }
      paint();
      return !error;
    }

    /** 顶部刷新用：按重新读到的索引更新内容与健康提示 */
    async function reload() {
      return init();
    }

    async function rebuild() {
      const ok = await ui.confirm({
        title: t("rebuildTitle"),
        points: [t("rebuildPoint1"), t("rebuildPoint2")],
        confirmLabel: t("rebuildIndex"),
      });
      if (!ok) {
        return;
      }
      const res = await ui.apiPost("index/rebuild", { scope: r.scope, key: r.key });
      ui.toast(ui.resultMessage(res, t("done")));
      await store.changed(r);
      await openPane(view, { kind: "index" }, { skipGuard: true, fresh: true });
    }

    function paint() {
      const head = detailHead({
        view: view,
        title: t("indexTitle"),
        desc: t("indexReadonlyHint"),
        actions: idx
          ? [h("button", { type: "button", class: "btn btn-sm", onclick: action(rebuild) }, icon("refresh"), t("rebuildIndex"))]
          : null,
      });
      root.setAttribute("aria-labelledby", head.id);
      if (!idx) {
        ui.replace(root, head.el, error ? ui.errorState(error, init) : ui.spinner());
        return;
      }
      ui.replace(
        root,
        head.el,
        error ? ui.staleNotice(error, reload) : null,
        indexHealth(idx),
        h(
          "div",
          { class: "meters" },
          meter(t("indexLines"), ui.fmtNum(idx.lines) + " / " + ui.fmtNum(idx.maxLines), idx.maxLines ? idx.lines / idx.maxLines : 0),
          meter(t("indexSize"), ui.fmtBytes(idx.size) + " / " + ui.fmtBytes(idx.maxBytes), idx.maxBytes ? idx.size / idx.maxBytes : 0)
        ),
        h("pre", { class: "pre", "aria-label": t("indexTitle") }, idx.content || t("indexEmpty"))
      );
    }

    paint();
    init();
    return { el: root, reload: reload };
  }

  // ------------------------------------------------------------ 回收站与已遗忘

  function itemRow(main, meta, buttons) {
    return h(
      "li",
      { class: "item-row" },
      h("div", { class: "item-main" }, h("div", { class: "item-title" }, main), meta ? h("div", { class: "item-meta" }, meta) : null),
      h("div", { class: "item-actions" }, buttons)
    );
  }

  function smallBtn(label, iconName, fn, kind) {
    return h("button", { type: "button", class: "btn btn-sm" + (kind ? " btn-" + kind : ""), onclick: action(fn) }, iconName ? icon(iconName) : null, label);
  }

  /** 一处记忆的回收站与已遗忘；留言板、小本本、悄悄话和"管理"里共用 */
  function MaintPanel(view, r, initialTab, opts) {
    const root = h("div", { class: "detail" });
    const back = opts && opts.onBack
      ? h("button", { type: "button", class: "btn btn-ghost btn-sm back-link", onclick: opts.onBack }, icon("back"), opts.backLabel || t("back"))
      : null;
    const st = { tab: initialTab === "forgotten" ? "forgotten" : "trash", trash: null, trashError: "", data: null, dataError: "", dataStale: "" };

    async function loadTrash() {
      try {
        const res = await ui.apiGet("trash", { scope: r.scope, key: r.key });
        st.trash = res.entries || [];
        st.trashError = "";
      } catch (err) {
        st.trashError = err.message;
      }
      paint();
      return !st.trashError;
    }

    /** 读这一处的清单。force 是顶部刷新用的：先重新向服务端要；失败时旧清单留着并提示 */
    async function loadData(force) {
      try {
        if (force === true) {
          await store.loadScope(r);
        }
        st.data = await store.waitScope(r);
        const entry = store.scopeEntry(r);
        st.dataError = "";
        st.dataStale = entry && entry.error ? entry.error : "";
      } catch (err) {
        st.dataError = err.message;
        st.dataStale = "";
      }
      paint();
      return !st.dataError && !st.dataStale;
    }

    async function refresh() {
      await store.changed(r);
      st.data = scopeData(r) || st.data;
      await loadTrash();
    }

    function activeFiles() {
      return new Set(((st.data && st.data.files) || []).map(function (row) { return row.file; }));
    }

    // ---- 回收站条目（MEMORY.md 副本不会出现在列表里，见 PLAN 3.12.3）
    async function restoreTrash(item) {
      if (activeFiles().has(item.file)) {
        const ok = await ui.confirm({
          title: t("restoreTrashTitle", { file: item.file }),
          points: [t("restoreOverwritePoint", { file: item.file })],
          confirmLabel: t("restore"),
        });
        if (!ok) {
          return false;
        }
      }
      const res = await ui.apiPost("trash/restore", { scope: r.scope, key: r.key, entry: item.entry });
      ui.toast(ui.resultMessage(res, t("restored")));
      await refresh();
      return true;
    }

    async function viewTrash(item) {
      const data = await ui.apiGet("trash/file", { scope: r.scope, key: r.key, entry: item.entry });
      let current;
      if (activeFiles().has(item.file)) {
        try {
          current = (await ui.apiGet("file", { scope: r.scope, key: r.key, file: item.file })).content || "";
        } catch (err) {
          current = undefined;
        }
      }
      openViewer({
        title: item.file,
        meta: trashMeta(item),
        content: data.content || "",
        current: current,
        actions: [{ label: t("restore"), kind: "primary", icon: "restore", run: function () { return restoreTrash(item); } }],
      });
    }

    async function deleteTrash(item) {
      const ok = await confirmAndPost({
        confirm: {
          title: t("deleteTrashTitle", { file: item.file }),
          points: [t("deleteTrashPoint1"), t("deleteTrashPoint2")],
          confirmLabel: t("deleteTrash"),
          danger: true,
        },
        endpoint: "trash/delete",
        body: { scope: r.scope, key: r.key, entry: item.entry },
        done: t("deleted"),
      });
      if (ok) {
        await loadTrash();
      }
    }

    function trashMeta(item) {
      return [trashReason(item), t("trashedAt", { time: ui.fmtTime(item.ts) }), actorLabel(item.actor), ui.fmtBytes(item.size)].join(" · ");
    }

    // ---- 已遗忘
    async function restoreForgotten(row) {
      const res = await ui.apiPost("forgotten/restore", { scope: r.scope, key: r.key, file: row.file });
      ui.toast(ui.resultMessage(res, t("restored")));
      await refresh();
      return true;
    }

    async function viewForgotten(row) {
      const data = await ui.apiGet("forgotten/file", { scope: r.scope, key: r.key, file: row.file });
      openViewer({
        title: row.file,
        meta: t("forgottenAt", { time: ui.fmtTime(data.forgottenAt || row.forgottenAt) }),
        content: data.content || "",
        actions: [{ label: t("restore"), kind: "primary", icon: "restore", run: function () { return restoreForgotten(row); } }],
      });
    }

    async function deleteForgotten(row) {
      const ok = await confirmAndPost({
        confirm: {
          title: t("deleteForgottenTitle", { file: row.file }),
          points: [t("deleteForgottenPoint"), trashKeepText(r)],
          confirmLabel: t("delete"),
          danger: true,
        },
        endpoint: "forgotten/delete",
        body: { scope: r.scope, key: r.key, file: row.file },
        done: t("deleted"),
      });
      if (ok) {
        await refresh();
      }
    }

    function trashList() {
      if (st.trashError) {
        return ui.errorState(st.trashError, loadTrash);
      }
      if (!st.trash || !st.data) {
        return ui.spinner();
      }
      const intro = h("p", { class: "hint" }, t("trashHint") + " " + trashKeepText(r));
      if (!st.trash.length) {
        return [intro, ui.emptyState({ icon: "trash", title: t("noTrashTitle"), hint: t("noTrashHint"), compact: true })];
      }
      const active = activeFiles();
      return [
        intro,
        h(
          "ul",
          { class: "items" },
          st.trash.map(function (item) {
            const canPurge = !active.has(item.file);
            return itemRow(
              [h("span", { class: "mono" }, item.file), h("span", { class: "tag tag-muted" }, trashReason(item))],
              [ui.timeEl(item.ts), " · ", actorLabel(item.actor), " · ", ui.fmtBytes(item.size)],
              [
                smallBtn(t("view"), "eye", function () { return viewTrash(item); }),
                smallBtn(t("restore"), "restore", function () { return restoreTrash(item); }),
                smallBtn(t("deleteTrash"), "trash", function () { return deleteTrash(item); }, "danger-ghost"),
                canPurge
                  ? smallBtn(t("purgeFile"), null, function () { return purgeTrashFile(item); }, "danger-ghost")
                  : null,
              ]
            );
          })
        ),
      ];
    }

    /** 回收站里在册文件已经不在的条目：彻底删除这个文件的全部副本与历史 */
    async function purgeTrashFile(item) {
      const ok = await purgeFile(r, item.file);
      if (ok) {
        await refresh();
      }
    }

    function forgottenList() {
      if (st.dataError) {
        return ui.errorState(st.dataError, loadData);
      }
      if (!st.data) {
        return ui.spinner();
      }
      const rows = st.data.forgotten || [];
      const intro = st.data.forgettingEnabled
        ? h("p", { class: "hint" }, t("forgottenHint", { days: st.data.forgetAfterDays || 90 }))
        : ui.notice("info", t("forgottenOffHint"));
      if (!rows.length) {
        return [
          intro,
          ui.emptyState({
            icon: "archive",
            title: t("noForgottenTitle"),
            hint: st.data.forgettingEnabled ? t("noForgottenHint", { days: st.data.forgetAfterDays || 90 }) : null,
            compact: true,
          }),
        ];
      }
      return [
        intro,
        h(
          "ul",
          { class: "items" },
          rows.map(function (row) {
            return itemRow(
              [h("span", { class: "mono" }, row.file), r.scope === "board" ? null : typeTag(row.type)],
              [
                row.description || t("noDescription"),
                h("br"),
                t("forgottenAt", { time: ui.fmtTime(row.forgottenAt) }),
                " · ",
                row.uses ? t("usageLine", { time: ui.fmtRel(row.lastUsed), n: row.uses }) : t("neverUsed"),
              ],
              [
                smallBtn(t("view"), "eye", function () { return viewForgotten(row); }),
                smallBtn(t("restore"), "restore", function () { return restoreForgotten(row); }),
                smallBtn(t("delete"), "trash", function () { return deleteForgotten(row); }, "danger-ghost"),
              ]
            );
          })
        ),
      ];
    }

    function paint() {
      const head = detailHead({
        view: view,
        title: t("maintTitle"),
        desc: store.scopeLabel(r),
        actions: h("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: action(function () { st.trash = null; paint(); return Promise.all([refresh(), loadData()]); }) }, icon("refresh"), t("refresh")),
      });
      root.setAttribute("aria-labelledby", head.id);
      const tabId = ui.uid("maint");
      const panel = h("div", { class: "tab-panel", role: "tabpanel", id: tabId + "-panel", "aria-labelledby": tabId + "-" + st.tab });
      ui.replace(panel, st.tab === "trash" ? trashList() : forgottenList());
      const tabs = ui.tabs(
        [
          { value: "trash", label: t("tabTrash"), id: tabId + "-trash", controls: panel.id, count: st.trash ? st.trash.length : null },
          { value: "forgotten", label: t("tabForgotten"), id: tabId + "-forgotten", controls: panel.id, count: st.data ? (st.data.forgotten || []).length : null },
        ],
        st.tab,
        function (value) {
          st.tab = value;
          paint();
          const next = root.querySelector('[role="tab"][aria-selected="true"]');
          if (next) {
            next.focus();
          }
        },
        t("maintTabsLabel")
      );
      ui.replace(root, back, head.el, st.dataStale ? ui.staleNotice(st.dataStale, function () { loadData(true); }) : null, tabs, panel);
    }

    paint();
    loadData();
    loadTrash();
    return {
      el: root,
      reload: async function () {
        const results = await Promise.all([loadData(true), loadTrash()]);
        return results.every(Boolean);
      },
    };
  }

  // ------------------------------------------------------------ 对话信息

  function SessionPanel(key) {
    const root = h("div", { class: "detail" });
    const r = store.ref("whisper", key);
    let initial = (store.whisperByKey(key) || {}).remark || "";
    let remark = initial;
    let clean = false;

    /** 顶部刷新用：按重新读到的总览更新对话信息；备注输入框里有没存的修改就留着 */
    function reload() {
      const server = (store.whisperByKey(key) || {}).remark || "";
      const edited = remark !== initial;
      initial = server;
      if (!edited) {
        remark = server;
      }
      paint();
      return true;
    }

    async function saveRemark() {
      const res = await ui.apiPost("whisper/remark", { key: key, remark: remark.trim() });
      clean = true;
      ui.toast(ui.resultMessage(res, t("saved")));
      await store.loadOverview(true);
      paint();
    }

    async function purge() {
      const s = store.whisperByKey(key) || { key: key };
      const days = retention(r).trashDays;
      const ok = await ui.confirm({
        title: t("purgeSessionTitle", { name: store.sessionTitle(s) }),
        points: [
          t("purgeSessionPoint1", { n: s.fileCount || 0 }),
          days > 0 ? t("purgeSessionPoint2", { days: days }) : t("purgeSessionPoint2Forever"),
          t("purgeSessionPoint3"),
        ],
        confirmLabel: t("purgeSession"),
        danger: true,
      });
      if (!ok) {
        return;
      }
      const res = await ui.apiPost("whisper/purge", { key: key, target: "all" });
      clean = true;
      ui.toast(ui.resultMessage(res, t("deleted")));
      delete S.scopes[store.sid(r)];
      await store.loadOverview(true);
      MP.app.leaveConversation(true);
    }

    function fact(label, value) {
      return metaRow(label, value || "—");
    }

    function paint() {
      const s = store.whisperByKey(key) || { key: key };
      const head = detailHead({
        view: "whisper",
        title: store.sessionTitle(s),
        tags: h("span", { class: "tag tag-muted" }, store.kindLabel(s.kind)),
        desc: t("whisperSub"),
        actions: [
          h("button", { type: "button", class: "btn btn-sm", onclick: function () { MP.app.showContext(key); } }, icon("context"), t("viewContext")),
          newButton("whisper"),
        ],
      });
      root.setAttribute("aria-labelledby", head.id);
      const input = h("input", {
        class: "input",
        value: remark,
        maxlength: "64",
        autocomplete: "off",
        oninput: function (event) {
          remark = event.target.value;
          clean = false;
        },
      });
      const remarkField = ui.field({ label: t("remark"), input: input, hint: t("remarkHint") });
      const form = h(
        "form",
        {
          class: "inline-form",
          onsubmit: function (event) {
            event.preventDefault();
            action(saveRemark)({ currentTarget: form.querySelector("button") });
          },
        },
        remarkField.el,
        h("button", { type: "submit", class: "btn btn-sm" }, icon("check"), t("saveRemark"))
      );
      ui.replace(
        root,
        head.el,
        h(
          "dl",
          { class: "meta-grid" },
          fact(t("metaKind"), store.kindLabel(s.kind)),
          fact(t("metaName"), s.name),
          fact(t("metaPlatform"), s.platform),
          fact(t("metaTarget"), s.target),
          fact(t("metaUmo"), s.umo ? h("code", null, s.umo) : ""),
          fact(t("metaFirstSeen"), s.firstSeen ? ui.fmtTime(s.firstSeen) : ""),
          fact(t("metaLastActive"), s.lastActive ? ui.fmtTime(s.lastActive) : ""),
          fact(t("metaFiles"), ui.fmtNum(s.fileCount || 0))
        ),
        form,
        dangerZone(
          t("purgeSession"),
          t("purgeSessionHint"),
          h("button", { type: "button", class: "btn btn-sm btn-danger-ghost", onclick: action(purge) }, icon("trash"), t("purgeSession"))
        )
      );
    }

    store.ensureScope(r);
    paint();
    return {
      el: root,
      reload: reload,
      isDirty: function () { return !clean && remark !== initial && remark !== ((store.whisperByKey(key) || {}).remark || ""); },
      markClean: function () { clean = true; },
    };
  }

  // ------------------------------------------------------------ 对外：列表与详情

  function listPane(view) {
    if (view === "board") {
      return boardListPane();
    }
    if (view === "notebook") {
      return notebookListPane();
    }
    if (S.whisperKey) {
      return whisperFilesPane();
    }
    return conversationPane({
      title: t("whisperLabel"),
      sub: t("whisperListSub"),
      selectedKey: null,
      showCount: true,
      memoryFilter: true,
      onSelect: function (key) { MP.app.openConversation(key); },
    });
  }

  function staticDetail(node) {
    return function () {
      return { el: h("div", { class: "detail detail-empty" }, node) };
    };
  }

  function emptyDetail(view) {
    if (view === "board") {
      return ui.emptyState({
        icon: "board",
        title: t("boardDetailEmptyTitle"),
        hint: t("boardDetailEmptyHint"),
        action: h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: function () { openPane("board", { kind: "publish" }, { fresh: true }); } }, icon("plus"), t("publishNotice")),
      });
    }
    return ui.emptyState({
      icon: "file",
      title: t("editorEmptyTitle"),
      hint: t("editorEmptyHint"),
      action: newButton(view),
    });
  }

  /** 右侧该显示什么：{id, create}；id 不变时沿用已挂载的组件 */
  function detailFor(view) {
    const r = viewRef(view);
    if (!r) {
      const node = view === "whisper"
        ? ui.emptyState({ icon: "whisper", title: t("pickConversationTitle"), hint: t("pickConversationHint") })
        : overviewGate() || ui.emptyState({ icon: "notebook", title: t("notebookNone"), hint: t("notebookNoneHint") });
      return { id: view + "|none|" + overviewState(), create: staticDetail(node) };
    }
    const pane = S.pane[view] || { kind: "empty" };
    const id = [view, store.sid(r), pane.kind, pane.file || "", pane.tab || "", S.paneVersion].join("|");
    let create;
    switch (pane.kind) {
      case "file":
        create = function () { return FileDetail(view, r, pane.file, pane.tab === "history" ? "history" : "content"); };
        break;
      case "edit":
        create = function () { return Editor(view, r, { isNew: false, file: pane.file }); };
        break;
      case "new":
        create = function () { return Editor(view, r, { isNew: true }); };
        break;
      case "publish":
        create = PublishForm;
        break;
      case "index":
        create = function () { return IndexPanel(view, r); };
        break;
      case "maint":
        create = function () { return MaintPanel(view, r, pane.tab); };
        break;
      case "tidy":
        create = function () { return MP.manage.TidyPanel(view, r); };
        break;
      case "session":
        create = view === "whisper" ? function () { return SessionPanel(r.key); } : staticDetail(emptyDetail(view));
        break;
      default:
        create = staticDetail(emptyDetail(view));
    }
    return { id: id, create: create };
  }

  MP.memory = {
    TYPES: TYPES,
    listPane: listPane,
    detailFor: detailFor,
    conversationPane: conversationPane,
    MaintPanel: MaintPanel,
    openViewer: openViewer,
    openPane: openPane,
    openFile: openFile,
    detailHead: detailHead,
    listHead: listHead,
    dangerZone: dangerZone,
    metaRow: metaRow,
    typeTag: typeTag,
    actorLabel: actorLabel,
    action: action,
    chipGroup: chipGroup,
    viewRef: viewRef,
    overviewGate: overviewGate,
    overviewState: overviewState,
    scopeData: scopeData,
    retention: retention,
    trashKeepText: trashKeepText,
  };
})(window.MemoryPod = window.MemoryPod || {});
