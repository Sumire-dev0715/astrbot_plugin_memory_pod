/* 鲸群记忆管理页：页面状态、作用域定位与数据加载。 */
(function (MP) {
  "use strict";

  const t = MP.t;
  const ui = MP.ui;

  const TYPES = ["person", "agreement", "note", "journal"];
  const VIEWS = ["board", "notebook", "whisper", "context", "manage"];

  // 页面的全部状态；视图按它渲染
  const S = {
    view: "board",
    narrow: false,
    // 窄屏时当前视图显示列表还是详情
    showDetail: {},
    ov: null,
    ovError: "",
    ovLoading: false,
    // sid -> {data, error, loading}
    scopes: {},
    // sid -> 列表筛选：{query, type, sort, results, searching, journalsOpen, status}
    lists: {},
    notebookKey: "",
    whisperKey: "",
    contextKey: "",
    conv: { query: "", kind: "all", withMemory: false },
    // 每个记忆视图右侧显示什么：{kind, file, tab, version}
    pane: {
      board: { kind: "empty" },
      notebook: { kind: "empty" },
      whisper: { kind: "session" },
    },
    manage: { section: "overview", tidySid: "", maintSid: "", transferTab: "export" },
    paneVersion: 0,
  };

  // ------------------------------------------------------------ 作用域

  function ref(scope, key) {
    return { scope: scope, key: scope === "board" ? "" : String(key || "") };
  }

  function sid(r) {
    return r.scope === "board" ? "board" : r.scope + ":" + r.key;
  }

  function parseSid(value) {
    const text = String(value || "");
    if (text === "board") {
      return ref("board", "");
    }
    const index = text.indexOf(":");
    if (index < 0) {
      return null;
    }
    return ref(text.slice(0, index), text.slice(index + 1));
  }

  function sameRef(a, b) {
    return Boolean(a && b && a.scope === b.scope && (a.key || "") === (b.key || ""));
  }

  function kindLabel(kind) {
    return t(kind === "group" ? "kindGroup" : kind === "private" ? "kindPrivate" : "kindOther");
  }

  function whisperByKey(key) {
    const list = (S.ov && S.ov.whispers) || [];
    for (let i = 0; i < list.length; i += 1) {
      if (list[i].key === key) {
        return list[i];
      }
    }
    return null;
  }

  /** 会话的显示名：备注 > 群名或昵称 > 类型 + 会话 ID > 目录名 */
  function sessionTitle(s) {
    if (!s) {
      return "";
    }
    return s.remark || s.name || (s.target ? kindLabel(s.kind) + " " + s.target : s.key);
  }

  /** 会话副标题：有备注时补上群名或昵称，再加会话 ID 与平台 */
  function sessionSub(s) {
    if (!s) {
      return "";
    }
    const bits = [];
    if (s.remark && s.name) {
      bits.push(s.name);
    }
    if ((s.remark || s.name) && s.target) {
      bits.push(s.target);
    }
    if (s.platform) {
      bits.push(s.platform);
    }
    return bits.join(" · ");
  }

  /** 作用域的完整名称，如"小本本 · Bot-A" */
  function scopeLabel(r) {
    if (!r) {
      return "";
    }
    if (r.scope === "board") {
      return t("boardTitle");
    }
    if (r.scope === "notebook") {
      return t("notebookTitle", { platform: r.key });
    }
    const s = whisperByKey(r.key);
    return t("whisperTitle", { name: s ? sessionTitle(s) : r.key });
  }

  /** 迁移报告里的作用域（transfer.describe 的结构） */
  function describeLabel(info) {
    if (!info) {
      return "";
    }
    if (info.scope === "board") {
      return t("boardTitle");
    }
    if (info.scope === "notebook") {
      return t("notebookTitle", { platform: info.platform || info.key });
    }
    const known = whisperByKey(info.key);
    return t("whisperTitle", { name: sessionTitle(known || info) || info.key });
  }

  /** 有记忆的全部作用域（整理、导出、迁移的候选） */
  function scopesWithFiles() {
    const ov = S.ov || {};
    const items = [];
    if (ov.board && ov.board.fileCount > 0) {
      items.push({ ref: ref("board", ""), label: t("boardTitle"), count: ov.board.fileCount });
    }
    (ov.notebooks || []).forEach(function (item) {
      if (item.fileCount > 0) {
        items.push({ ref: ref("notebook", item.key), label: t("notebookTitle", { platform: item.platform || item.key }), count: item.fileCount });
      }
    });
    (ov.whispers || []).forEach(function (item) {
      if (item.fileCount > 0) {
        items.push({ ref: ref("whisper", item.key), label: t("whisperTitle", { name: sessionTitle(item) }), count: item.fileCount });
      }
    });
    return items;
  }

  /** 全部作用域（包括还没有在册记忆的），给回收站与已遗忘的选择框用 */
  function allScopes() {
    const ov = S.ov || {};
    const items = [{ ref: ref("board", ""), label: t("boardTitle"), count: ov.board ? ov.board.fileCount : 0 }];
    (ov.notebooks || []).forEach(function (item) {
      items.push({ ref: ref("notebook", item.key), label: t("notebookTitle", { platform: item.platform || item.key }), count: item.fileCount });
    });
    (ov.whispers || []).forEach(function (item) {
      items.push({ ref: ref("whisper", item.key), label: t("whisperTitle", { name: sessionTitle(item) }), count: item.fileCount });
    });
    return items;
  }

  function listState(r) {
    const key = sid(r);
    if (!S.lists[key]) {
      S.lists[key] = { query: "", type: "all", sort: "updated", results: null, searching: false, journalsOpen: false, status: "all" };
    }
    return S.lists[key];
  }

  // ------------------------------------------------------------ 加载

  let ovToken = 0;

  async function loadOverview(quiet) {
    const token = ++ovToken;
    S.ovLoading = true;
    try {
      const data = await ui.apiGet("overview");
      if (token !== ovToken) {
        return;
      }
      S.ov = data;
      S.ovError = "";
      const notebooks = data.notebooks || [];
      if (!notebooks.some(function (item) { return item.key === S.notebookKey; })) {
        S.notebookKey = notebooks.length ? notebooks[0].key : "";
      }
    } catch (err) {
      if (token !== ovToken) {
        return;
      }
      S.ovError = err.message;
      if (!quiet || !S.ov) {
        ui.toast(err.message, "error");
      }
    } finally {
      if (token === ovToken) {
        S.ovLoading = false;
      }
    }
    MP.render();
  }

  const scopeTokens = {};
  // sid -> 最近一次加载的 Promise，等数据的组件跟着它走
  const pending = {};

  function scopeEntry(r) {
    return S.scopes[sid(r)] || null;
  }

  function loadScope(r) {
    const key = sid(r);
    const promise = fetchScope(r);
    pending[key] = promise;
    return promise;
  }

  async function fetchScope(r) {
    const key = sid(r);
    const token = (scopeTokens[key] || 0) + 1;
    scopeTokens[key] = token;
    const entry = S.scopes[key] || { data: null, error: "", loading: false };
    entry.loading = true;
    S.scopes[key] = entry;
    MP.render();
    try {
      const data = await ui.apiGet("scope", { scope: r.scope, key: r.key });
      if (scopeTokens[key] !== token) {
        return;
      }
      entry.data = data;
      entry.error = "";
    } catch (err) {
      if (scopeTokens[key] !== token) {
        return;
      }
      entry.error = err.message;
    } finally {
      if (scopeTokens[key] === token) {
        entry.loading = false;
      }
    }
    MP.render();
  }

  /** 视图需要某个作用域的数据时调用：没加载过才去拉 */
  function ensureScope(r) {
    const entry = scopeEntry(r);
    if (!entry) {
      loadScope(r);
    }
    return scopeEntry(r);
  }

  /** 拿到某个作用域的数据（需要时加载）；加载失败抛出错误 */
  async function waitScope(r) {
    const key = sid(r);
    let entry = scopeEntry(r);
    if (!entry || (!entry.data && !entry.loading)) {
      await loadScope(r);
    }
    // 期间又发起了新的加载（比如刚改过记忆）就等最新那次
    while ((entry = scopeEntry(r)) && entry.loading && pending[key]) {
      await pending[key];
    }
    entry = scopeEntry(r);
    if (entry && entry.data) {
      return entry.data;
    }
    throw new Error((entry && entry.error) || t("requestFailed"));
  }

  /** 改动记忆之后：刷新总览与这个作用域，列表里的数字、标记跟着更新 */
  async function changed(r) {
    if (r) {
      const list = S.lists[sid(r)];
      if (list) {
        list.results = null;
      }
    }
    await Promise.all([loadOverview(true), r ? loadScope(r) : Promise.resolve()]);
  }

  // ------------------------------------------------------------ 未保存修改

  // 当前右侧组件：{id, el, isDirty(), markClean()}
  const current = { detail: null };

  function isDirty() {
    const d = current.detail;
    return Boolean(d && typeof d.isDirty === "function" && d.isDirty());
  }

  /** 离开前确认是否放弃未保存的修改；返回 true 表示可以离开 */
  async function guard() {
    if (!isDirty()) {
      return true;
    }
    const ok = await ui.confirm({
      title: t("discardTitle"),
      message: t("discardMessage"),
      confirmLabel: t("discard"),
      danger: true,
    });
    if (ok && current.detail && typeof current.detail.markClean === "function") {
      current.detail.markClean();
    }
    return ok;
  }

  MP.store = {
    TYPES: TYPES,
    VIEWS: VIEWS,
    S: S,
    ref: ref,
    sid: sid,
    parseSid: parseSid,
    sameRef: sameRef,
    kindLabel: kindLabel,
    whisperByKey: whisperByKey,
    sessionTitle: sessionTitle,
    sessionSub: sessionSub,
    scopeLabel: scopeLabel,
    describeLabel: describeLabel,
    scopesWithFiles: scopesWithFiles,
    allScopes: allScopes,
    listState: listState,
    loadOverview: loadOverview,
    loadScope: loadScope,
    ensureScope: ensureScope,
    waitScope: waitScope,
    scopeEntry: scopeEntry,
    changed: changed,
    current: current,
    isDirty: isDirty,
    guard: guard,
  };
})(window.MemoryPod = window.MemoryPod || {});
