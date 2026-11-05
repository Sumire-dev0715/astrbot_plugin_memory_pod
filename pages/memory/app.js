(function () {
  "use strict";

  const bridge = window.AstrBotPluginPage || null;
  const I18N_PREFIX = "pages.memory.";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const TYPES = ["user", "feedback", "project", "reference", "journal"];
  const PARTS = ["system", "plugin", "tools", "history", "prompt", "recall"];
  const SCOPES = ["board", "notebook", "whisper"];
  // "记住上次填的署名"的本地存储键
  const SIGNATURE_KEY = "memoryPod.boardSignature";

  // 简体中文文案；其他语言由 .astrbot-plugin/i18n 提供，缺失时回落到这里
  const ZH = {
    title: "记忆管理",
    subtitle: "留言板、小本本与悄悄话",
    refresh: "刷新",
    filterPlaceholder: "搜索会话名称、ID、备注",
    filterAll: "全部",
    filterHasMemory: "有记忆",
    kindGroup: "群聊",
    kindPrivate: "私聊",
    kindOther: "其他",
    boardTitle: "留言板",
    boardSub: "所有会话可见，管理员发布",
    notebookLabel: "小本本",
    notebookTitle: "小本本 · {platform}",
    notebookSub: "这台机器人的所有群聊和私聊共享",
    notebookNone: "这台机器人还没有小本本",
    whisperLabel: "悄悄话 · {n}",
    whisperTitle: "悄悄话 · {name}",
    whisperSub: "只有这段对话能读写",
    whisperNone: "还没有会话",
    noSessions: "没有符合条件的会话",
    noBridge: "请在 AstrBot 管理面板的「插件页面」中打开此页面。",
    loadFailed: "加载失败",
    retry: "重试",
    requestFailed: "请求失败",
    welcomeTitle: "三层记忆",
    welcomeHint: "留言板给所有会话看，小本本是这台机器人的记忆，悄悄话只有当前这段对话能读写。",
    statBoard: "留言板",
    statNotebooks: "小本本",
    statWhispers: "悄悄话会话",
    statFiles: "记忆文件",
    fileCount: "{n} 条记忆",
    back: "返回",
    firstSeen: "首次出现 {time}",
    lastActive: "最近活跃 {time}",
    remark: "备注",
    consolidate: "整理记忆",
    consolidateAll: "整理全部记忆",
    purge: "清除数据",
    memoryOffNotice: "长期记忆已在插件配置中关闭：机器人不会读写记忆，这里只是保留在磁盘上的内容。",
    contextReadonlyNotice: "这个面板只读。上下文的截断与摘要由 AstrBot 自己的上下文设置处理，本插件不再改动对话历史。",
    userIdentifierNotice: "AstrBot 的「用户识别」开着：本插件也会标注发送者，模型会看到两次。可以关掉 AstrBot 的「用户识别」，由本插件接管。",
    tabMemories: "记忆",
    tabIndex: "索引",
    tabContext: "上下文",
    tabForgotten: "已遗忘",
    tabTrash: "回收站",
    tabRuns: "整理记录",
    searchPlaceholder: "全文搜索记忆",
    newMemory: "新建记忆",
    publishNotice: "发布告示",
    publishTitle: "发布留言板告示",
    publishHint: "留言板的每条告示都会带署名，所有机器人的所有会话都能看到。",
    fieldSignature: "署名",
    fieldSignatureHint: "会记到 frontmatter 里；下次发布自动填上次的署名",
    signatureRequired: "请填写署名",
    published: "已发布",
    typeAll: "全部",
    typeOther: "未分类",
    type_user: "人物",
    type_feedback: "指导",
    type_project: "事项",
    type_reference: "资料",
    type_journal: "日记",
    typeHint_user: "某个人是谁：身份、偏好、专长",
    typeHint_feedback: "对机器人做事方式的指导：纠正过的错误、确认过的做法",
    typeHint_project: "进行中的事、目标与约束",
    typeHint_reference: "外部资源、资料与告示",
    typeHint_journal: "每日流水账：不占单独索引行，不参与自动召回与查重",
    typeMissing: "无类型",
    noDescription: "（没有描述）",
    noMemoriesTitle: "还没有记忆",
    noMemoriesHint: "机器人会在聊天中自己记下值得长期记住的事，也可以点「新建记忆」手动添加。",
    noTypeMatch: "这个分类下没有记忆",
    noSearchResults: "没有找到包含这些词的记忆",
    editorEmptyTitle: "选择一条记忆",
    editorEmptyHint: "在左侧选择一条记忆查看或编辑，或者新建一条。",
    modeForm: "表单",
    modeRaw: "原文",
    unsaved: "有未保存的修改",
    fieldFile: "文件名",
    fieldFileHint: "记人用 user-<数字ID>.md",
    fieldType: "类型",
    fieldDescription: "描述",
    fieldDescriptionHint: "一句话说明这是什么、什么时候用得上；它会成为索引里的那一行",
    fieldBody: "正文",
    fieldBodyHint: "可以用 [[文件名]] 链接到其他记忆",
    fieldRaw: "完整内容",
    fieldRawHint: "含 frontmatter 的原始文件",
    noFrontmatterNotice: "这个文件没有标准的 frontmatter（名称、描述、类型）。切换到表单编辑并保存后会自动补上。",
    linksLabel: "链接到",
    openLink: "打开这条记忆",
    linkMissing: "这条记忆不存在",
    newHint: "新记忆会在下一次对话开始或开头块刷新后进入索引",
    savedAt: "最近写入：{time}",
    stampLine: "首次记于 {created} · 最近更新 {updated}",
    sourceLine: "来源 ID {ids}",
    delete: "删除",
    cancel: "取消",
    save: "保存",
    confirm: "确定",
    close: "关闭",
    saved: "已保存",
    deleted: "已删除",
    badFileName: "文件名只能包含字母、数字、点、下划线、连字符，并以 .md 结尾",
    needDescription: "请填写描述",
    deleteTitle: "删除记忆",
    deleteMessage: "确定删除 {file} 吗？索引里对应的行会一并移除，删除后可以在回收站里找回。",
    discardTitle: "放弃未保存的修改？",
    discardMessage: "正在编辑的内容还没有保存，离开后修改会丢失。",
    discard: "放弃修改",
    indexHealth: "索引状况",
    rebuildIndex: "按文件重建",
    indexLines: "行数",
    indexSize: "大小",
    indexTruncated: "索引超出注入上限，超出的部分不会发给模型。建议合并相关记忆、删除过时的记忆。",
    indexMissing: "{n} 个记忆文件没有出现在索引里，模型不知道它们存在：",
    indexDangling: "索引里有 {n} 行指向已不存在的文件：",
    indexHealthy: "索引与记忆文件一致。",
    indexEditorHint: "对话开始时发给模型的就是这份索引。保存记忆文件时插件会自动维护对应的行；你可以在这里调整顺序、添加分组标题。",
    chars: "{n} 字",
    lastEstimate: "最近一次估算",
    lastActual: "提供商实际用量",
    lastActualSub: "最近一次 usage 上报",
    cacheHit: "提示词缓存命中",
    cacheHitSub: "最近一次请求",
    notReported: "未上报",
    fileLong: "偏长",
    fileDangling: "悬空链接",
    lastRequest: "最近请求",
    tokens: "{n} tokens",
    partsTitle: "上下文构成（最近一次请求，估算）",
    noEstimate: "还没有请求记录",
    part_tools: "工具声明",
    part_recall: "召回记忆",
    part_history: "历史原文",
    part_prompt: "本轮输入",
    part_system: "系统提示词",
    part_plugin: "插件开头块",
    liveTag: "实时",
    liveHint: "插件内存中的当前状态",
    diskTag: "磁盘记录",
    diskHint: "插件启动后这段对话还没有新消息，显示的是上次保存的状态",
    summaryTitle: "AstrBot 压缩摘要",
    noSummary: "AstrBot 还没有压缩过这段对话",
    forgottenTitle: "已遗忘的记忆",
    forgottenHint: "长期没被召回或读取的记忆会移到这里：不进索引、不参与召回，文件保留。这个人再出现、或机器人主动读它时会自动恢复。",
    forgottenOffHint: "记忆遗忘当前未启用，这里只是之前遗忘下来的文件，不会再自动遗忘或恢复；可以手动恢复或彻底删除。",
    noForgottenTitle: "没有已遗忘的记忆",
    noForgottenHint: "超过 {days} 天没被召回或读取的记忆（指导类除外）会自动移到这里。",
    forgottenAt: "{time} 遗忘",
    usageLine: "最近用到 {time} · 共 {n} 次",
    neverUsed: "还没被召回或读取过",
    view: "查看",
    restore: "恢复",
    restored: "已恢复",
    forget: "遗忘",
    forgetTitle: "遗忘记忆",
    forgetMessage: "把 {file} 移入已遗忘：它会退出索引与召回，文件保留，随时可以恢复。",
    forgotten: "已遗忘",
    forgetDeleteTitle: "彻底删除",
    forgetDeleteMessage: "确定永久删除已遗忘的 {file} 吗？删除后无法恢复。",
    trashTitle: "回收站",
    trashHint: "被删除的记忆会先放在这里，保留期内可以查看、恢复或彻底删除。",
    noTrashTitle: "回收站是空的",
    noTrashHint: "删除记忆后旧版本会在这里保留 {days} 天。",
    restoreTrash: "恢复",
    deleteTrash: "彻底删除",
    purgeFile: "彻底删除（含历史）",
    trashPurgeTitle: "彻底删除这个文件？",
    trashPurgeMessage: "会删除 {file} 的全部历史、回收站条目与整理记录里的副本，删除后无法恢复。",
    purged: "已彻底删除",
    historyTitle: "历史版本",
    historyHint: "每次覆盖或修改前的旧内容都会留在这里，可以恢复。",
    noHistoryTitle: "还没有历史版本",
    noHistoryHint: "第一次修改这个文件时，旧内容会出现在这里。",
    historyVersion: "版本 {version}",
    restoreVersion: "恢复这一版",
    versionRestored: "已恢复这个版本",
    action_write: "写入",
    action_edit: "修改",
    action_delete: "删除",
    action_import: "导入",
    action_restore: "恢复",
    action_forget: "遗忘",
    action_rename: "改名",
    action_purge_file: "彻底删除",
    tidyHistory: "整理记录",
    historyEmptyTitle: "还没有整理记录",
    historyEmptyHint: "整理应用修改后会在这里留下记录，最近 {n} 次可以撤销。",
    trigger_manual: "手动整理",
    runOps: "查看改动",
    undo: "撤销",
    undone: "已撤销",
    undoTitle: "撤销这次整理？",
    undoMessage: "被删除的记忆会写回，被改写的恢复原文，新建的删掉。整理之后又改过的文件不会动。",
    undoDone: "撤销完成",
    consolidateTitle: "整理记忆",
    consolidateHint: "让默认对话模型复盘「{label}」里的全部记忆：合并重复、修正过时、规范格式。先在预览里勾选要应用的修改。",
    consolidateAllTitle: "整理全部记忆",
    consolidateAllHint: "依次让默认对话模型复盘每一处有记忆的地方；可以只应用其中一部分。",
    consolidateNothingTitle: "不需要整理",
    consolidateNothingHint: "模型认为这里没有需要修改的地方。",
    consolidateSkipped: "有 {n} 个文件因超出预算没有参与这次整理。",
    consolidateReview: "{n} 项修改。勾选要应用的项目：写入会整文件覆盖，删除会同时移除索引行；每处先写后删。",
    applySelected: "应用选中的修改",
    consolidateDone: "整理完成：应用 {ok} 项，失败 {failed} 项。",
    consolidateAllNone: "还没有可整理的记忆",
    opCreate: "新建",
    opRewrite: "改写",
    opDelete: "删除",
    previewContent: "正文预览",
    migrate: "备份与迁移",
    migrateHint: "备份是记忆原文与索引，不含对话历史；导入与迁移都是管理员操作。",
    tabExport: "导出",
    tabImport: "导入",
    tabScope: "迁移",
    tabPlatform: "平台改名",
    exportHint: "选择要备份的作用域；已遗忘的文件也可以一起导出。",
    includeForgotten: "包含已遗忘的文件",
    exportButton: "生成备份",
    exportNone: "选中的地方都还没有记忆",
    exported: "备份已生成",
    importHint: "选择之前导出的 JSON 备份，先预览再导入。",
    chooseFile: "选择备份文件",
    badBackup: "这个文件不是有效的记忆备份",
    policy: "同名冲突",
    policy_newer: "留较新的",
    policy_skip: "留现有的",
    policy_overwrite: "用备份覆盖",
    importButton: "导入选中的作用域",
    imported: "导入完成",
    importNothing: "备份里没有记忆",
    cntAdded: "新增 {n}",
    cntReplaced: "覆盖 {n}",
    cntSame: "相同 {n}",
    cntKept: "保留 {n}",
    cntInvalid: "无效 {n}",
    forgottenIncluded: "含 {n} 条已遗忘",
    issuesCount: "有 {n} 个文件没有导入",
    platformHint: "AstrBot 里删掉重建适配器或改了 ID 后，把旧平台 ID 下的小本本与悄悄话搬到新 ID 下。",
    platformFrom: "旧平台 ID",
    platformTo: "新平台 ID",
    includeNotebook: "同时迁移小本本",
    platformSessions: "{n} 段会话",
    modeLabel: "方式",
    modeMove: "移动",
    modeCopy: "复制",
    runMigrate: "开始迁移",
    migratePreview: "预览结果",
    migrateSkipped: "{n} 段会话缺少完整标识，已跳过",
    migrateNothing: "没有可迁移的记忆",
    migrated: "迁移完成",
    migrateTo: "目标",
    migrateScopeHint: "把一处记忆复制或移动到另一处。",
    targetLabel: "目标作用域",
    targetNewSession: "新的悄悄话会话",
    targetUmo: "会话标识",
    targetUmoHint: "格式：平台ID:消息类型:会话ID",
    search: "搜索",
    plus: "新建",
    refreshIcon: "刷新",
  };

  const ICONS = {
    search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.9-4M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.9 4M20 20v-4h-4"/>',
    group: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M16 5.2a3 3 0 0 1 0 5.6M17.5 14a5 5 0 0 1 3 5"/>',
    user: '<circle cx="12" cy="8" r="3.6"/><path d="M5 20a7 7 0 0 1 14 0"/>',
    chat: '<path d="M5 18l-1 3 4-1.5A8 8 0 1 0 5 18z"/>',
    bot: '<rect x="5" y="8" width="14" height="11" rx="2.5"/><path d="M12 4v4M9.5 13h.01M14.5 13h.01M9.5 16h5"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    edit: '<path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3z"/>',
    sparkles: '<path d="M11 3l1.8 4.7 4.7 1.8-4.7 1.8L11 16l-1.8-4.7L4.5 9.5l4.7-1.8z"/><path d="M18.5 14.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
    archive: '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 13h4"/>',
    back: '<path d="M15 18l-6-6 6-6"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
    link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    warn: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    inbox: '<path d="M4 13l2.5-7h11l2.5 7v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M4 13h5l1 2h4l1-2h5"/>',
    rotate: '<path d="M4 12a8 8 0 1 0 2.4-5.7L4 8.6"/><path d="M4 4v4.6h4.6"/>',
    transfer: '<path d="M4 8h13l-3.5-3.5M20 16H7l3.5 3.5"/>',
    history: '<path d="M4 12a8 8 0 1 0 2.4-5.7L4 8.6"/><path d="M4 4v4.6h4.6M12 8v4l3 2"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  };

  // ------------------------------------------------------------ 基础工具

  function t(key, vars) {
    const fallback = Object.prototype.hasOwnProperty.call(ZH, key) ? ZH[key] : key;
    let text = bridge ? bridge.t(I18N_PREFIX + key, fallback) : fallback;
    if (vars) {
      text = String(text).replace(/\{(\w+)\}/g, function (match, name) {
        return Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match;
      });
    }
    return text;
  }

  const PROPS = new Set(["value", "checked", "disabled", "selected"]);

  function append(el, child) {
    if (child === null || child === undefined || child === false) {
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

  function h(tag, attrs) {
    const el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (name) {
        const value = attrs[name];
        if (value === null || value === undefined || value === false) {
          return;
        }
        if (name === "class") {
          el.className = value;
        } else if (name.slice(0, 2) === "on" && typeof value === "function") {
          el.addEventListener(name.slice(2).toLowerCase(), value);
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

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = ICONS[name] || "";
    return svg;
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function fmtTime(ts) {
    if (!ts) {
      return "—";
    }
    const d = new Date(ts * 1000);
    return (
      d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      " " + pad(d.getHours()) + ":" + pad(d.getMinutes())
    );
  }

  function relTime(ts) {
    if (!ts) {
      return "—";
    }
    const diff = Math.max(0, Date.now() / 1000 - ts);
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
    return fmtTime(ts).slice(0, 10);
  }

  ZH.justNow = "刚刚";
  ZH.minutesAgo = "{n} 分钟前";
  ZH.hoursAgo = "{n} 小时前";
  ZH.daysAgo = "{n} 天前";

  function fmtNum(n) {
    return Number(n || 0).toLocaleString();
  }

  function stem(file) {
    return file.slice(-3) === ".md" ? file.slice(0, -3) : file;
  }

  function extractLinks(text) {
    const found = [];
    const re = /\[\[([^\[\]\n]{1,120})\]\]/g;
    let match;
    while ((match = re.exec(text || ""))) {
      const target = match[1].trim();
      if (target && found.indexOf(target) < 0) {
        found.push(target);
      }
    }
    return found;
  }

  function typeTag(type) {
    if (TYPES.indexOf(type) >= 0) {
      return h("span", { class: "tag " + type, title: t("typeHint_" + type) }, t("type_" + type));
    }
    return type ? h("span", { class: "tag" }, type) : h("span", { class: "tag warn" }, t("typeMissing"));
  }

  function stat(label, value, sub) {
    return h(
      "div",
      { class: "stat" },
      h("div", { class: "stat-label" }, label),
      h("div", { class: "stat-value" }, value),
      sub ? h("div", { class: "stat-sub" }, sub) : null
    );
  }

  function emptyState(iconName, title, hint) {
    return h("div", { class: "empty" }, icon(iconName), h("strong", null, title), hint ? h("span", null, hint) : null);
  }

  function spinnerBlock() {
    return h("div", { class: "center-state" }, h("div", { class: "spinner" }));
  }

  function notice(kind, content) {
    return h(
      "div",
      { class: "notice" + (kind === "info" ? " info" : "") },
      icon(kind === "info" ? "info" : "warn"),
      h("div", null, content)
    );
  }

  function errorBlock(message, retry) {
    return h(
      "div",
      { class: "empty" },
      icon("warn"),
      h("strong", null, t("loadFailed")),
      h("span", null, message),
      retry ? h("button", { class: "btn", onclick: retry }, t("retry")) : null
    );
  }

  // ------------------------------------------------------------ 接口

  function toError(err) {
    const message = err && err.message ? err.message : String(err || "");
    return new Error(message || t("requestFailed"));
  }

  async function apiGet(endpoint, params) {
    try {
      return (await bridge.apiGet(endpoint, params || {})) || {};
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

  // ------------------------------------------------------------ 提示与弹窗

  function toast(message, kind) {
    const box = document.getElementById("toasts");
    const el = h(
      "div",
      { class: "toast" + (kind === "error" ? " error" : ""), role: "status" },
      icon(kind === "error" ? "warn" : "check"),
      h("div", null, message)
    );
    box.append(el);
    setTimeout(function () {
      el.remove();
    }, kind === "error" ? 6000 : 3200);
  }

  function openDialog(opts) {
    const overlay = h("div", { class: "overlay" });
    const body = h("div", { class: "dialog-body" });
    const foot = h("div", { class: "dialog-foot" });
    let resolveDone;
    const done = new Promise(function (resolve) {
      resolveDone = resolve;
    });
    const ctrl = {
      busy: false,
      done: done,
      close: function (value) {
        if (!overlay.isConnected) {
          return;
        }
        overlay.remove();
        document.removeEventListener("keydown", onKey);
        resolveDone(value);
      },
      setBody: function (node) {
        body.replaceChildren();
        append(body, node);
      },
      setFoot: function (nodes) {
        foot.replaceChildren();
        append(foot, nodes);
      },
    };
    function onKey(event) {
      if (event.key === "Escape" && !ctrl.busy) {
        ctrl.close(null);
      }
    }
    const closeBtn = h(
      "button",
      {
        class: "btn ghost icon-only",
        title: t("close"),
        onclick: function () {
          if (!ctrl.busy) {
            ctrl.close(null);
          }
        },
      },
      icon("close")
    );
    overlay.append(
      h(
        "div",
        { class: "dialog" + (opts.wide ? " wide" : ""), role: "dialog", "aria-modal": "true" },
        h("div", { class: "dialog-head" }, h("h3", null, opts.title), closeBtn),
        body,
        foot
      )
    );
    overlay.addEventListener("mousedown", function (event) {
      if (event.target === overlay && !ctrl.busy) {
        ctrl.close(null);
      }
    });
    document.addEventListener("keydown", onKey);
    if (opts.body) {
      ctrl.setBody(opts.body);
    }
    document.body.append(overlay);
    return ctrl;
  }

  function confirmDialog(opts) {
    const ctrl = openDialog({ title: opts.title, body: h("p", null, opts.message) });
    ctrl.setFoot([
      h("button", { class: "btn", onclick: function () { ctrl.close(false); } }, t("cancel")),
      h(
        "button",
        { class: "btn " + (opts.danger ? "danger" : "primary"), onclick: function () { ctrl.close(true); } },
        opts.confirm || t("confirm")
      ),
    ]);
    return ctrl.done.then(function (value) {
      return value === true;
    });
  }

  async function withBusy(button, task) {
    if (button) {
      button.disabled = true;
    }
    try {
      return await task();
    } finally {
      if (button && button.isConnected) {
        button.disabled = false;
      }
    }
  }

  // ------------------------------------------------------------ 状态

  const state = {
    overview: null,
    overviewError: "",
    filterText: "",
    selected: null,
    detail: null,
    detailError: "",
    tab: "memories",
    typeFilter: "all",
    fileQuery: "",
    searchResults: null,
    editor: null,
    indexDraft: null,
    context: null,
    contextLoading: false,
    history: null,
    trash: null,
    trashLoading: false,
    runs: null,
  };
  const els = {};
  let detailToken = 0;
  let trashToken = 0;

  function invalidateTrash() {
    trashToken += 1;
    state.trash = null;
    state.trashLoading = false;
  }

  function scopeParams() {
    return { scope: state.selected.scope, key: state.selected.key || "" };
  }

  function sameScope(a, b) {
    return Boolean(a && b && a.scope === b.scope && (a.key || "") === (b.key || ""));
  }

  function isSelected(sel) {
    return sameScope(state.selected, sel);
  }

  function resetDetailState() {
    state.detail = null;
    state.detailError = "";
    state.tab = "memories";
    state.typeFilter = "all";
    state.fileQuery = "";
    state.searchResults = null;
    state.editor = null;
    state.indexDraft = null;
    state.context = null;
    state.contextLoading = false;
    state.history = null;
    state.runs = null;
    invalidateTrash();
  }

  function hasUnsaved() {
    const indexDirty = Boolean(
      state.detail && state.indexDraft !== null && state.indexDraft !== state.detail.index.content
    );
    return Boolean((state.editor && state.editor.dirty) || indexDirty);
  }

  async function confirmDiscard() {
    if (!hasUnsaved()) {
      return true;
    }
    const ok = await confirmDialog({
      title: t("discardTitle"),
      message: t("discardMessage"),
      confirm: t("discard"),
      danger: true,
    });
    if (ok) {
      if (state.editor) {
        state.editor.dirty = false;
      }
      state.indexDraft = null;
    }
    return ok;
  }

  function kindLabel(kind) {
    return t(kind === "group" ? "kindGroup" : kind === "private" ? "kindPrivate" : "kindOther");
  }

  function sessionTitle(s) {
    return s.remark || s.name || (s.target ? kindLabel(s.kind) + " " + s.target : s.key);
  }

  function sessionSub(s) {
    const bits = [];
    if (s.remark || s.name) {
      bits.push(s.target);
    }
    bits.push(s.platform);
    const when = relTime(s.lastActive || s.updatedAt);
    if (when !== "—") {
      bits.push(when);
    }
    return bits.filter(Boolean).join(" · ");
  }

  function scopeTitle(sel) {
    if (!sel) {
      return "";
    }
    if (sel.scope === "board") {
      return t("boardTitle");
    }
    if (sel.scope === "notebook") {
      return t("notebookTitle", { platform: sel.platform || sel.key });
    }
    return t("whisperTitle", { name: sessionTitle(sel.session || sel) });
  }

  function scopeSub(sel) {
    if (!sel) {
      return "";
    }
    if (sel.scope === "board") {
      return t("boardSub");
    }
    if (sel.scope === "notebook") {
      return t("notebookSub");
    }
    return sessionSub(sel.session || sel);
  }

  // ------------------------------------------------------------ 加载

  async function loadOverview(quiet) {
    try {
      state.overview = await apiGet("overview");
      state.overviewError = "";
    } catch (err) {
      state.overviewError = err.message;
      if (!quiet) {
        toast(err.message, "error");
      }
    }
    render();
  }

  async function loadDetail() {
    if (!state.selected) {
      return;
    }
    const token = ++detailToken;
    state.detailError = "";
    try {
      const data = await apiGet("scope", scopeParams());
      if (token !== detailToken) {
        return;
      }
      state.detail = data;
      state.indexDraft = null;
      state.searchResults = null;
      invalidateTrash();
    } catch (err) {
      if (token === detailToken) {
        state.detailError = err.message;
      }
    }
    render();
  }

  async function refreshAll() {
    await loadOverview(true);
    await loadDetail();
  }

  async function selectScope(sel) {
    if (!(await confirmDiscard())) {
      return;
    }
    state.selected = sel;
    resetDetailState();
    state.detail = null;
    render();
    await loadDetail();
  }

  function backToList() {
    state.selected = null;
    resetDetailState();
    render();
  }

  // ------------------------------------------------------------ 外壳

  function buildShell() {
    const root = document.getElementById("app");
    root.replaceChildren();
    const brand = h(
      "div",
      { class: "brand" },
      h("div", null, h("h1", null, t("title")), h("p", null, t("subtitle"))),
      h(
        "div",
        { class: "brand-actions" },
        h("button", { class: "btn ghost icon-only", title: t("refresh"), onclick: function () { refreshAll(); } }, icon("refresh"))
      )
    );
    const filter = h("input", {
      class: "input search-box",
      placeholder: t("filterPlaceholder"),
      value: state.filterText,
      oninput: function (event) {
        state.filterText = event.target.value;
        renderSidebar();
      },
    });
    const list = h("div", { class: "session-list" });
    els.sidebar = h("div", { class: "sidebar" }, h("div", { class: "sidebar-head" }, brand, filter), list);
    els.sessionList = list;
    els.main = h("div", { class: "main" });
    root.append(els.sidebar, els.main);
    renderSidebar();
    renderMain();
  }

  function filteredList(items, labelFn) {
    const text = state.filterText.trim().toLowerCase();
    if (!text) {
      return items;
    }
    return items.filter(function (item) {
      return labelFn(item).toLowerCase().indexOf(text) >= 0;
    });
  }

  function renderSidebar() {
    if (!els.sessionList) {
      return;
    }
    const list = els.sessionList;
    list.replaceChildren();
    const ov = state.overview;
    if (!ov) {
      list.append(spinnerBlock());
      return;
    }
    if (state.overviewError) {
      list.append(errorBlock(state.overviewError, function () { loadOverview(); }));
      return;
    }
    const board = ov.board || { fileCount: 0 };
    list.append(
      h(
        "div",
        {
          class: "session-item" + (isSelected({ scope: "board" }) ? " active" : ""),
          onclick: function () { selectScope({ scope: "board", key: "" }); },
        },
        h("div", { class: "avatar board" }, icon("inbox")),
        h(
          "div",
          { class: "session-text" },
          h("div", { class: "session-name" }, t("boardTitle")),
          h("div", { class: "session-sub" }, t("boardSub"))
        ),
        h("span", { class: "count" }, fmtNum(board.fileCount))
      )
    );
    const notebooks = filteredList(ov.notebooks || [], function (item) { return item.key; });
    list.append(h("div", { class: "list-label" }, t("notebookLabel")));
    if (!notebooks.length) {
      list.append(h("div", { class: "empty", style: "padding:12px" }, h("span", null, t("notebookNone"))));
    }
    notebooks.forEach(function (item) {
      list.append(
        h(
          "div",
          {
            class: "session-item" + (isSelected({ scope: "notebook", key: item.key }) ? " active" : ""),
            onclick: function () { selectScope({ scope: "notebook", key: item.key, platform: item.platform }); },
          },
          h("div", { class: "avatar notebook" }, icon("bot")),
          h(
            "div",
            { class: "session-text" },
            h("div", { class: "session-name" }, t("notebookTitle", { platform: item.platform || item.key })),
            h("div", { class: "session-sub" }, t("notebookSub"))
          ),
          h("span", { class: "count" }, fmtNum(item.fileCount))
        )
      );
    });
    const whispers = filteredList(ov.whispers || [], function (item) { return sessionTitle(item); });
    list.append(h("div", { class: "list-label" }, t("whisperLabel", { n: whispers.length })));
    if (!whispers.length) {
      list.append(h("div", { class: "empty", style: "padding:12px" }, h("span", null, t("whisperNone"))));
    }
    whispers.forEach(function (s) {
      const sel = { scope: "whisper", key: s.key, session: s };
      list.append(
        h(
          "div",
          {
            class: "session-item" + (isSelected(sel) ? " active" : ""),
            title: s.umo || s.key,
            onclick: function () { selectScope(sel); },
          },
          h("div", { class: "avatar " + (s.kind === "group" ? "group" : "private") }, icon(s.kind === "group" ? "group" : "user")),
          h(
            "div",
            { class: "session-text" },
            h("div", { class: "session-name" }, sessionTitle(s)),
            h("div", { class: "session-sub" }, sessionSub(s))
          ),
          h("span", { class: "count" }, fmtNum(s.fileCount))
        )
      );
    });
  }

  // ------------------------------------------------------------ 主区域

  function renderMain() {
    if (!els.main) {
      return;
    }
    const main = els.main;
    main.replaceChildren();
    if (!state.selected) {
      main.append(renderWelcome());
      return;
    }
    if (state.detailError) {
      main.append(errorBlock(state.detailError, function () { loadDetail(); }));
      return;
    }
    if (!state.detail) {
      main.append(spinnerBlock());
      return;
    }
    main.append(renderDetail());
  }

  function renderWelcome() {
    const ov = state.overview || {};
    const notebooks = ov.notebooks || [];
    const whispers = ov.whispers || [];
    const files =
      (ov.board ? ov.board.fileCount : 0) +
      notebooks.reduce(function (sum, item) { return sum + (item.fileCount || 0); }, 0) +
      whispers.reduce(function (sum, item) { return sum + (item.fileCount || 0); }, 0);
    const stats = h(
      "div",
      { class: "stats" },
      stat(t("statBoard"), fmtNum(ov.board ? ov.board.fileCount : 0)),
      stat(t("statNotebooks"), fmtNum(notebooks.length), t("statFiles") + " " + fmtNum(files)),
      stat(t("statWhispers"), fmtNum(whispers.length))
    );
    const box = h(
      "div",
      { class: "stack" },
      h("div", { class: "detail-head" }, h("div", { class: "detail-title" }, t("welcomeTitle"))),
      h("p", { class: "hint-line" }, t("welcomeHint")),
      stats,
      h(
        "div",
        { class: "actions" },
        h("button", { class: "btn", onclick: function () { openMigration("export"); } }, icon("archive"), t("migrate")),
        h("button", { class: "btn primary", onclick: consolidateAll }, icon("sparkles"), t("consolidateAll"))
      )
    );
    if (ov.memoryEnabled === false) {
      box.prepend(notice("warn", t("memoryOffNotice")));
    }
    if (ov.context && ov.context.userIdentifier) {
      box.append(notice("info", t("userIdentifierNotice")));
    }
    return box;
  }

  function renderDetail() {
    const d = state.detail;
    const sel = state.selected;
    const title = scopeTitle(sel);
    const sub = scopeSub(sel);
    const head = h(
      "div",
      { class: "detail-head" },
      h("button", { class: "btn ghost icon-only mobile-only", onclick: backToList }, icon("back")),
      h("div", null, h("div", { class: "detail-title" }, title), h("div", { class: "session-sub" }, sub)),
      h(
        "div",
        { class: "brand-actions" },
        d.scope !== "board"
          ? h(
              "button",
              { class: "btn", onclick: function () { consolidateScope(sel); } },
              icon("sparkles"),
              t("consolidate")
            )
          : null,
        h("button", { class: "btn", onclick: function () { refreshAll(); } }, icon("refresh"), t("refresh"))
      )
    );
    const tabs = h("div", { class: "tabs" });
    const tabDefs = [
      ["memories", "tabMemories"],
      ["index", "tabIndex"],
      ["forgotten", "tabForgotten"],
      ["trash", "tabTrash"],
      ["runs", "tabRuns"],
    ];
    if (d.scope === "whisper") {
      tabDefs.splice(2, 0, ["context", "tabContext"]);
    }
    tabDefs.forEach(function (item) {
      tabs.append(
        h(
          "button",
          {
            class: "tab" + (state.tab === item[0] ? " active" : ""),
            onclick: async function () {
              if (!(await confirmDiscard())) {
                return;
              }
              state.tab = item[0];
              render();
              activateTab();
            },
          },
          t(item[1])
        )
      );
    });
    const body = h("div", { class: "panel-body" });
    if (state.tab === "memories") {
      body.append(renderMemoriesTab());
    } else if (state.tab === "index") {
      body.append(renderIndexTab());
    } else if (state.tab === "context") {
      body.append(renderContextTab());
    } else if (state.tab === "forgotten") {
      body.append(renderForgottenTab());
    } else if (state.tab === "trash") {
      body.append(renderTrashTab());
    } else if (state.tab === "runs") {
      body.append(renderRunsTab());
    }
    return h("div", { class: "stack" }, head, tabs, h("div", { class: "panel" }, body));
  }

  function activateTab() {
    if (state.tab === "context" && !state.contextLoading && !state.context) {
      loadContext();
    }
    if (state.tab === "trash" && !state.trashLoading) {
      loadTrash();
    }
    if (state.tab === "runs" && !state.runs) {
      loadRuns();
    }
  }

  // ------------------------------------------------------------ 记忆列表与编辑器

  function renderMemoriesTab() {
    const d = state.detail;
    const box = h("div", { class: "mem-layout" });
    const listPane = h("div", { class: "list-pane" });
    const filePane = h("div", { class: "file-pane" });
    if (d.scope === "board") {
      listPane.append(
        h(
          "div",
          { class: "action-panel" },
          h(
            "div",
            { class: "action-text" },
            h("strong", null, t("publishTitle")),
            h("span", null, t("publishHint"))
          ),
          h("button", { class: "btn primary", onclick: publishNotice }, icon("plus"), t("publishNotice"))
        )
      );
    }
    const tools = h(
      "div",
      { class: "file-tools" },
      h("input", {
        class: "input search-box",
        placeholder: t("searchPlaceholder"),
        value: state.fileQuery,
        oninput: function (event) {
          state.fileQuery = event.target.value;
          state.searchResults = null;
          renderMemoriesTabInto(listPane, filePane);
        },
        onkeydown: function (event) {
          if (event.key === "Enter") {
            runSearch();
          }
        },
      }),
      h("button", { class: "btn ghost", onclick: runSearch }, icon("search"), t("search")),
      h("button", { class: "btn", onclick: newMemory }, icon("plus"), t("newMemory"))
    );
    listPane.append(tools);
    const chips = h("div", { class: "chips" });
    ["all"].concat(TYPES).forEach(function (type) {
      chips.append(
        h(
          "button",
          {
            class: "chip" + (state.typeFilter === type ? " active" : ""),
            onclick: function () {
              state.typeFilter = type;
              renderMemoriesTabInto(listPane, filePane);
            },
          },
          type === "all" ? t("typeAll") : t(type === "journal" ? "type_journal" : "type_" + type)
        )
      );
    });
    listPane.append(chips);
    const list = h("div", { class: "file-list" });
    listPane.append(list);
    filePane.append(renderEditor());
    box.append(listPane, filePane);
    renderFileList(list);
    return box;
  }

  function renderMemoriesTabInto(listPane, filePane) {
    // 简单重绘整个记忆页，避免维护两套局部刷新逻辑
    const main = els.main;
    if (!main) {
      return;
    }
    const panel = main.querySelector(".panel-body");
    if (!panel) {
      return;
    }
    panel.replaceChildren(renderMemoriesTab());
  }

  function visibleFiles() {
    const files = (state.detail && state.detail.files) || [];
    if (state.searchResults) {
      return state.searchResults;
    }
    const query = state.fileQuery.trim().toLowerCase();
    return files.filter(function (file) {
      if (state.typeFilter !== "all" && file.type !== state.typeFilter) {
        return false;
      }
      if (!query) {
        return true;
      }
      return (
        file.file.toLowerCase().indexOf(query) >= 0 ||
        (file.description || "").toLowerCase().indexOf(query) >= 0
      );
    });
  }

  function renderFileList(list) {
    const files = visibleFiles();
    if (!files.length) {
      list.append(
        emptyState(
          "file",
          state.searchResults ? t("noSearchResults") : t("noMemoriesTitle"),
          t("noMemoriesHint")
        )
      );
      return;
    }
    files.forEach(function (file) {
      const active = state.editor && state.editor.file === file.file && state.editor.scope === state.detail.scope;
      list.append(
        h(
          "div",
          { class: "file-item" + (active ? " active" : ""), onclick: function () { openFile(file.file); } },
          h(
            "div",
            { class: "file-item-top" },
            h("span", { class: "file-name" }, file.file),
            typeTag(file.type)
          ),
          h("div", { class: "file-desc" }, file.description || t("noDescription")),
          h(
            "div",
            { class: "file-age" },
            relTime(file.updated),
            file.forgottenAt ? h("span", { class: "tag warn" }, t("forgotten")) : null,
            (file.dangling || []).length ? h("span", { class: "tag warn" }, t("fileDangling")) : null,
            (file.long || []).length ? h("span", { class: "tag" }, t("fileLong")) : null
          )
        )
      );
    });
  }

  async function openFile(file) {
    if (!(await confirmDiscard())) {
      return;
    }
    try {
      const data = await apiGet("file", Object.assign(scopeParams(), { file: file }));
      state.editor = {
        scope: state.detail.scope,
        mode: "form",
        file: data.file,
        originalFile: data.file,
        type: data.type || "reference",
        description: data.description || "",
        body: data.body || "",
        signature: data.signature || "",
        raw: data.content || "",
        hasFrontmatter: data.hasFrontmatter,
        baseMtime: data.mtime || 0,
        dirty: false,
        isNew: false,
      };
      state.tab = "memories";
      render();
      activateTab();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function newMemory() {
    state.editor = {
      scope: state.detail.scope,
      mode: "form",
      file: "",
      originalFile: "",
      type: "reference",
      description: "",
      body: "",
      signature: "",
      raw: "",
      hasFrontmatter: true,
      baseMtime: 0,
      dirty: false,
      isNew: true,
    };
    render();
  }

  function editorTitle() {
    const ed = state.editor;
    if (ed.isNew) {
      return t("newMemory");
    }
    return ed.file || t("editorEmptyTitle");
  }

  function markDirty() {
    if (state.editor) {
      state.editor.dirty = true;
      const badge = document.querySelector(".dirty-dot");
      if (badge) {
        badge.classList.add("show");
      }
    }
  }

  function field(label, hint, control) {
    return h(
      "div",
      { class: "field" },
      h("label", null, label, hint ? h("small", null, hint) : null),
      control
    );
  }

  function renderEditor() {
    const ed = state.editor;
    if (!ed) {
      return emptyState("edit", t("editorEmptyTitle"), t("editorEmptyHint"));
    }
    const box = h("div", { class: "editor" });
    const head = h(
      "div",
      { class: "editor-head" },
      h("div", { class: "editor-title" }, editorTitle()),
      ed.dirty ? h("span", { class: "dirty-dot show" }, t("unsaved")) : null
    );
    box.append(head);
    const modeSwitch = h(
      "div",
      { class: "segmented" },
      h(
        "button",
        { class: "segment" + (ed.mode === "form" ? " active" : ""), onclick: function () { ed.mode = "form"; render(); } },
        t("modeForm")
      ),
      h(
        "button",
        { class: "segment" + (ed.mode === "raw" ? " active" : ""), onclick: function () { ed.mode = "raw"; render(); } },
        t("modeRaw")
      )
    );
    box.append(modeSwitch);
    if (ed.mode === "raw") {
      box.append(
        field(
          t("fieldRaw"),
          t("fieldRawHint"),
          h("textarea", {
            class: "textarea raw",
            value: ed.raw,
            oninput: function (event) {
              ed.raw = event.target.value;
              markDirty();
            },
          })
        )
      );
    } else {
      if (!ed.hasFrontmatter) {
        box.append(notice("info", t("noFrontmatterNotice")));
      }
      box.append(
        field(
          t("fieldFile"),
          t("fieldFileHint"),
          h("input", {
            class: "input",
            value: ed.file,
            disabled: !ed.isNew,
            oninput: function (event) {
              ed.file = event.target.value;
              markDirty();
            },
          })
        )
      );
      const typeOptions = TYPES.map(function (type) {
        return h("option", { value: type, selected: ed.type === type }, t("type_" + type));
      });
      box.append(
        field(
          t("fieldType"),
          null,
          h(
            "select",
            {
              class: "input",
              onchange: function (event) {
                ed.type = event.target.value;
                markDirty();
              },
            },
            typeOptions
          )
        )
      );
      box.append(
        field(
          t("fieldDescription"),
          t("fieldDescriptionHint"),
          h("input", {
            class: "input",
            value: ed.description,
            oninput: function (event) {
              ed.description = event.target.value;
              markDirty();
            },
          })
        )
      );
      if (ed.scope === "board") {
        box.append(
          field(
            t("fieldSignature"),
            t("fieldSignatureHint"),
            h("input", {
              class: "input",
              value: ed.signature,
              oninput: function (event) {
                ed.signature = event.target.value;
                markDirty();
              },
            })
          )
        );
      }
      box.append(
        field(
          t("fieldBody"),
          t("fieldBodyHint"),
          h("textarea", {
            class: "textarea",
            value: ed.body,
            oninput: function (event) {
              ed.body = event.target.value;
              markDirty();
            },
          })
        )
      );
    }
    const links = extractLinks(ed.body || ed.raw);
    if (links.length) {
      box.append(
        h(
          "div",
          { class: "links" },
          h("span", { class: "hint-line" }, t("linksLabel")),
          links.map(function (name) {
            return h(
              "button",
              {
                class: "link-chip",
                onclick: function () {
                  openFile(name.endsWith(".md") ? name : name + ".md");
                },
              },
              icon("link"),
              name
            );
          })
        )
      );
    }
    const foot = h(
      "div",
      { class: "editor-foot" },
      h(
        "button",
        {
          class: "btn primary",
          onclick: function (event) {
            saveFile(event.currentTarget);
          },
        },
        t("save")
      ),
      !ed.isNew
        ? h("button", { class: "btn danger", onclick: deleteFile }, t("delete"))
        : null,
      !ed.isNew
        ? h("button", { class: "btn ghost", onclick: forgetFile }, t("forget"))
        : null,
      h("button", { class: "btn ghost", onclick: function () { state.editor = null; render(); } }, t("cancel"))
    );
    box.append(foot);
    if (!ed.isNew) {
      box.append(
        h(
          "div",
          { class: "meta-row" },
          h("span", null, t("savedAt", { time: fmtTime(ed.baseMtime) })),
          h(
            "button",
            { class: "btn ghost", onclick: toggleHistory },
            icon("history"),
            t("historyTitle")
          )
        )
      );
      if (state.history && state.history.open) {
        box.append(renderHistoryPanel());
      }
    }
    return box;
  }

  async function saveFile(button) {
    const ed = state.editor;
    if (!ed) {
      return;
    }
    const body = {
      scope: ed.scope,
      key: (state.selected && state.selected.key) || "",
      file: ed.file,
      oldFile: ed.originalFile,
      mode: ed.mode,
      type: ed.type,
      description: ed.description,
      body: ed.body,
      signature: ed.signature,
      content: ed.raw,
      baseMtime: ed.baseMtime,
    };
    if (ed.mode === "form" && !String(ed.file || "").trim()) {
      toast(t("badFileName"), "error");
      return;
    }
    if (ed.mode === "form" && !String(ed.description || "").trim()) {
      toast(t("needDescription"), "error");
      return;
    }
    if (ed.scope === "board" && !String(ed.signature || "").trim()) {
      toast(t("signatureRequired"), "error");
      return;
    }
    await withBusy(button, async function () {
      try {
        const res = await apiPost("file/save", body);
        ed.dirty = false;
        toast(res.message || t("saved"));
        await refreshAll();
        await openFile(res.file);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  async function deleteFile() {
    const ed = state.editor;
    if (!ed) {
      return;
    }
    const ok = await confirmDialog({
      title: t("deleteTitle"),
      message: t("deleteMessage", { file: ed.file }),
      confirm: t("delete"),
      danger: true,
    });
    if (!ok) {
      return;
    }
    try {
      await apiPost("file/delete", Object.assign(scopeParams(), { file: ed.file }));
      state.editor = null;
      toast(t("deleted"));
      await refreshAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function forgetFile() {
    const ed = state.editor;
    if (!ed) {
      return;
    }
    const ok = await confirmDialog({
      title: t("forgetTitle"),
      message: t("forgetMessage", { file: ed.file }),
      confirm: t("forget"),
    });
    if (!ok) {
      return;
    }
    try {
      await apiPost("file/forget", Object.assign(scopeParams(), { file: ed.file }));
      state.editor = null;
      toast(t("forgotten"));
      await refreshAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // ------------------------------------------------------------ 留言板发布

  function savedSignature() {
    try {
      return window.localStorage.getItem(SIGNATURE_KEY) || "";
    } catch (err) {
      return "";
    }
  }

  function rememberSignature(value) {
    try {
      window.localStorage.setItem(SIGNATURE_KEY, value);
    } catch (err) {
      // 隐私模式等禁用 localStorage 时仍然可以发布，只是不记住
    }
  }

  function publishNotice() {
    const signature = h("input", {
      class: "input",
      value: savedSignature(),
      placeholder: t("fieldSignature"),
    });
    const title = h("input", { class: "input", placeholder: t("fieldDescription") });
    const body = h("textarea", { class: "textarea", placeholder: t("fieldBody") });
    const ctrl = openDialog({
      title: t("publishTitle"),
      wide: true,
      body: h(
        "div",
        { class: "stack" },
        h("p", { class: "hint-line" }, t("publishHint")),
        field(t("fieldSignature"), t("fieldSignatureHint"), signature),
        field(t("fieldDescription"), t("fieldDescriptionHint"), title),
        field(t("fieldBody"), t("fieldBodyHint"), body)
      ),
    });
    async function submit() {
      const signatureText = signature.value.trim();
      if (!signatureText) {
        toast(t("signatureRequired"), "error");
        return;
      }
      if (!title.value.trim() || !body.value.trim()) {
        toast(t("needDescription"), "error");
        return;
      }
      ctrl.busy = true;
      try {
        const res = await apiPost("board/publish", {
          signature: signatureText,
          description: title.value.trim(),
          body: body.value,
        });
        rememberSignature(signatureText);
        ctrl.busy = false;
        ctrl.close(true);
        toast(res.message || t("published"));
        await refreshAll();
      } catch (err) {
        ctrl.busy = false;
        toast(err.message, "error");
      }
    }
    ctrl.setFoot([
      h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("cancel")),
      h("button", { class: "btn primary", onclick: submit }, t("publishNotice")),
    ]);
  }

  // ------------------------------------------------------------ 搜索

  async function runSearch() {
    const query = state.fileQuery.trim();
    if (!query) {
      state.searchResults = null;
      render();
      return;
    }
    try {
      const data = await apiGet("search", Object.assign(scopeParams(), { q: query }));
      const byFile = {};
      (state.detail.files || []).forEach(function (file) {
        byFile[file.file] = file;
      });
      state.searchResults = (data.results || []).map(function (item) {
        return Object.assign({}, byFile[item.file] || { file: item.file, type: "", description: item.excerpt }, item);
      });
      render();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // ------------------------------------------------------------ 历史

  function toggleHistory() {
    if (state.history && state.history.open) {
      state.history = null;
      render();
      return;
    }
    loadHistory();
  }

  async function loadHistory() {
    const ed = state.editor;
    if (!ed) {
      return;
    }
    try {
      const data = await apiGet("history/versions", Object.assign(scopeParams(), { file: ed.file }));
      state.history = { open: true, file: ed.file, versions: data.versions || [] };
      render();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function renderHistoryPanel() {
    const box = h("div", { class: "history-panel" });
    if (!state.history.versions.length) {
      box.append(emptyState("history", t("noHistoryTitle"), t("noHistoryHint")));
      return box;
    }
    state.history.versions.forEach(function (version) {
      box.append(
        h(
          "div",
          { class: "row-wrap" },
          h("span", null, t("historyVersion", { version: version.version })),
          h("span", { class: "hint-line" }, fmtTime(version.ts) + " · " + (t("action_" + version.action) || version.action)),
          h(
            "button",
            {
              class: "btn ghost",
              onclick: async function () {
                try {
                  await apiPost("history/restore", Object.assign(scopeParams(), { file: state.history.file, version: version.version }));
                  toast(t("versionRestored"));
                  await refreshAll();
                  await openFile(state.history.file);
                } catch (err) {
                  toast(err.message, "error");
                }
              },
            },
            t("restoreVersion")
          )
        )
      );
    });
    return box;
  }

  // ------------------------------------------------------------ 索引

  function renderIndexTab() {
    const d = state.detail;
    const idx = d.index || {};
    if (state.indexDraft === null) {
      state.indexDraft = idx.content || "";
    }
    const box = h("div", { class: "stack" });
    if (idx.truncated) {
      box.append(notice("warn", t("indexTruncated")));
    }
    if ((idx.missing || []).length) {
      box.append(notice("warn", t("indexMissing", { n: idx.missing.length }) + " " + idx.missing.join("、")));
    }
    if ((idx.dangling || []).length) {
      box.append(notice("warn", t("indexDangling", { n: idx.dangling.length }) + " " + idx.dangling.join("、")));
    }
    if (!idx.truncated && !(idx.missing || []).length && !(idx.dangling || []).length) {
      box.append(notice("info", t("indexHealthy")));
    }
    box.append(
      h(
        "div",
        { class: "stats" },
        stat(t("indexLines"), fmtNum(idx.lines), null),
        stat(t("indexSize"), fmtBytes(idx.size), t("chars", { n: idx.size }))
      )
    );
    box.append(
      h("textarea", {
        class: "textarea index",
        value: state.indexDraft,
        oninput: function (event) {
          state.indexDraft = event.target.value;
        },
      })
    );
    box.append(
      h(
        "div",
        { class: "actions" },
        h("button", { class: "btn primary", onclick: saveIndex }, t("save")),
        h("button", { class: "btn", onclick: rebuildIndex }, t("rebuildIndex"))
      )
    );
    box.append(h("p", { class: "hint-line" }, t("indexEditorHint")));
    return box;
  }

  async function saveIndex(button) {
    await withBusy(button, async function () {
      try {
        await apiPost("index/save", Object.assign(scopeParams(), { content: state.indexDraft }));
        toast(t("saved"));
        state.indexDraft = null;
        await loadDetail();
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  async function rebuildIndex(button) {
    await withBusy(button, async function () {
      try {
        const res = await apiPost("index/rebuild", scopeParams());
        toast(res.message || t("saved"));
        state.indexDraft = null;
        await loadDetail();
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  // ------------------------------------------------------------ 上下文

  async function loadContext() {
    if (!state.selected || state.selected.scope !== "whisper") {
      return;
    }
    state.contextLoading = true;
    try {
      state.context = await apiGet("whisper/context", { key: state.selected.key });
    } catch (err) {
      toast(err.message, "error");
    }
    state.contextLoading = false;
    render();
  }

  function renderContextTab() {
    if (state.contextLoading) {
      return spinnerBlock();
    }
    if (!state.context) {
      return emptyState("clock", t("lastEstimate"), t("noEstimate"));
    }
    const data = state.context;
    const box = h("div", { class: "stack" });
    box.append(notice("info", t("contextReadonlyNotice")));
    box.append(
      h(
        "div",
        { class: "stats" },
        stat(t("lastEstimate"), data.estimate ? t("tokens", { n: data.estimate }) : "—", data.live ? t("liveTag") + " · " + t("liveHint") : t("diskTag") + " · " + t("diskHint")),
        stat(t("lastActual"), data.actual > 0 ? t("tokens", { n: data.actual }) : t("notReported"), t("lastActualSub")),
        stat(t("cacheHit"), data.cacheHit >= 0 ? t("tokens", { n: data.cacheHit }) : t("notReported"), t("cacheHitSub"))
      )
    );
    if (data.context) {
      box.append(
        h(
          "div",
          { class: "kv-list" },
          h("div", null, "上下文窗口：" + (data.context.window || "—") + "（" + (data.context.windowNote || "") + "）"),
          h("div", null, "AstrBot 轮数上限：" + (data.context.turnLimit > 0 ? data.context.turnLimit : "未设置"))
        )
      );
    }
    const parts = data.parts || {};
    const rows = PARTS.filter(function (key) { return parts[key] !== undefined; }).map(function (key) {
      return h("div", { class: "row-wrap" }, h("span", null, t("part_" + key) || key), h("span", null, fmtNum(parts[key])));
    });
    if (rows.length) {
      box.append(h("div", { class: "kv-list" }, h("strong", null, t("partsTitle")), rows));
    }
    box.append(
      h(
        "div",
        { class: "kv-list" },
        h("strong", null, t("summaryTitle")),
        h("div", { class: data.hasSummary ? "pre" : "hint-line" }, data.hasSummary ? data.summary : t("noSummary"))
      )
    );
    if (state.selected.scope === "whisper") {
      box.append(
        h(
          "div",
          { class: "actions" },
          h("button", { class: "btn", onclick: editRemark }, t("remark")),
          h("button", { class: "btn danger", onclick: purgeWhisper }, t("purge"))
        )
      );
    }
    return box;
  }

  async function editRemark() {
    const sel = state.selected;
    if (!sel || sel.scope !== "whisper") {
      return;
    }
    const value = window.prompt(t("remark"), (sel.session && sel.session.remark) || "");
    if (value === null) {
      return;
    }
    try {
      await apiPost("whisper/remark", { key: sel.key, remark: value });
      toast(t("saved"));
      await refreshAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function purgeWhisper() {
    const sel = state.selected;
    if (!sel || sel.scope !== "whisper") {
      return;
    }
    const ok = await confirmDialog({
      title: t("purge"),
      message: "确定清除这段对话的全部悄悄话吗？文件会移入作用域归档，无法在页面里恢复。",
      confirm: t("purge"),
      danger: true,
    });
    if (!ok) {
      return;
    }
    try {
      const res = await apiPost("whisper/purge", { key: sel.key, target: "all" });
      toast(res.message || t("deleted"));
      backToList();
      await refreshAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // ------------------------------------------------------------ 已遗忘

  function renderForgottenTab() {
    const d = state.detail;
    const box = h("div", { class: "stack" });
    box.append(
      d.forgettingEnabled
        ? h("p", { class: "hint-line" }, t("forgottenHint"))
        : notice("info", t("forgottenOffHint"))
    );
    if (!(d.forgotten || []).length) {
      box.append(
        emptyState(
          "archive",
          t("noForgottenTitle"),
          t("noForgottenHint", { days: d.forgetAfterDays || 90 })
        )
      );
      return box;
    }
    d.forgotten.forEach(function (file) {
      box.append(
        h(
          "div",
          { class: "forgotten-item" },
          h("div", null, h("div", { class: "file-name" }, file.file), h("div", { class: "file-desc" }, file.description || t("noDescription"))),
          h("span", { class: "hint-line" }, t("forgottenAt", { time: fmtTime(file.forgottenAt) })),
          h("button", { class: "btn ghost", onclick: function () { viewForgotten(file.file); } }, t("view")),
          h("button", { class: "btn", onclick: function () { restoreForgotten(file.file); } }, t("restore")),
          h("button", { class: "btn danger", onclick: function () { deleteForgotten(file.file); } }, t("delete"))
        )
      );
    });
    return box;
  }

  async function viewForgotten(file) {
    try {
      const data = await apiGet("forgotten/file", Object.assign(scopeParams(), { file: file }));
      const ctrl = openDialog({ title: file, wide: true, body: h("pre", { class: "pre" }, data.content || "") });
      ctrl.setFoot([h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("close"))]);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function restoreForgotten(file) {
    try {
      await apiPost("forgotten/restore", Object.assign(scopeParams(), { file: file }));
      toast(t("restored"));
      await refreshAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function deleteForgotten(file) {
    const ok = await confirmDialog({
      title: t("forgetDeleteTitle"),
      message: t("forgetDeleteMessage", { file: file }),
      confirm: t("delete"),
      danger: true,
    });
    if (!ok) {
      return;
    }
    try {
      await apiPost("forgotten/delete", Object.assign(scopeParams(), { file: file }));
      toast(t("deleted"));
      await refreshAll();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // ------------------------------------------------------------ 回收站

  async function loadTrash() {
    const token = ++trashToken;
    state.trashLoading = true;
    try {
      const data = await apiGet("trash", scopeParams());
      if (token !== trashToken) {
        return;
      }
      state.trash = data.entries || [];
    } catch (err) {
      toast(err.message, "error");
    }
    state.trashLoading = false;
    render();
  }

  function trashMeta(item) {
    return [fmtTime(item.ts), item.action, t("action_" + item.action) || item.action, item.size + " B"].filter(Boolean).join(" · ");
  }

  function renderTrashTab() {
    const box = h("div", { class: "stack" });
    box.append(h("p", { class: "hint-line" }, t("trashHint")));
    if (state.trashLoading || !state.trash) {
      box.append(spinnerBlock());
      return box;
    }
    if (!state.trash.length) {
      box.append(emptyState("trash", t("noTrashTitle"), t("noTrashHint", { days: 90 })));
      return box;
    }
    const active = new Set(((state.detail && state.detail.files) || []).map(function (file) { return file.file; }));
    state.trash.forEach(function (item) {
      const row = h(
        "div",
        { class: "row-wrap" },
        h("div", null, h("div", { class: "file-name" }, item.file), h("div", { class: "hint-line" }, trashMeta(item))),
        h("button", { class: "btn ghost", onclick: function () { viewTrashEntry(item); } }, t("view")),
        h("button", { class: "btn", onclick: function () { restoreTrashEntry(item); } }, t("restoreTrash")),
        h("button", { class: "btn danger", onclick: function () { deleteTrashEntry(item); } }, t("deleteTrash"))
      );
      if (!active.has(item.file)) {
        row.append(h("button", { class: "btn danger", onclick: function () { purgeTrashFile(item); } }, t("purgeFile")));
      }
      box.append(row);
    });
    return box;
  }

  async function viewTrashEntry(item) {
    try {
      const data = await apiGet("trash/file", Object.assign(scopeParams(), { entry: item.entry }));
      const ctrl = openDialog({ title: item.file, wide: true, body: h("pre", { class: "pre" }, data.content || "") });
      ctrl.setFoot([h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("close"))]);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function restoreTrashEntry(item) {
    try {
      await apiPost("trash/restore", Object.assign(scopeParams(), { entry: item.entry }));
      toast(t("restored"));
      await loadDetail();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function deleteTrashEntry(item) {
    const ok = await confirmDialog({
      title: t("deleteTrash"),
      message: t("forgetDeleteMessage", { file: item.file }),
      confirm: t("deleteTrash"),
      danger: true,
    });
    if (!ok) {
      return;
    }
    try {
      await apiPost("trash/delete", Object.assign(scopeParams(), { entry: item.entry }));
      toast(t("deleted"));
      await loadDetail();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function purgeTrashFile(item) {
    const ok = await confirmDialog({
      title: t("trashPurgeTitle"),
      message: t("trashPurgeMessage", { file: item.file }),
      confirm: t("purgeFile"),
      danger: true,
    });
    if (!ok) {
      return;
    }
    try {
      await apiPost("file/purge", Object.assign(scopeParams(), { file: item.file }));
      toast(t("purged"));
      await loadDetail();
      await loadOverview(true);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // ------------------------------------------------------------ 整理

  function consolidateTargets() {
    const ov = state.overview || {};
    const targets = [];
    if (ov.board && ov.board.fileCount > 0) {
      targets.push({ scope: "board", key: "", label: t("boardTitle"), sub: t("boardSub") });
    }
    (ov.notebooks || []).forEach(function (item) {
      if (item.fileCount > 0) {
        targets.push({
          scope: "notebook",
          key: item.key,
          label: t("notebookTitle", { platform: item.platform || item.key }),
          sub: t("notebookSub"),
        });
      }
    });
    (ov.whispers || []).forEach(function (item) {
      if (item.fileCount > 0) {
        targets.push({
          scope: "whisper",
          key: item.key,
          label: sessionTitle(item),
          sub: t("whisperSub"),
        });
      }
    });
    return targets;
  }

  async function consolidateScope(sel) {
    const label = scopeTitle(sel);
    const ctrl = openDialog({
      title: t("consolidateTitle"),
      wide: true,
      body: h("p", null, t("consolidateHint", { label: label })),
    });
    const runBtn = h(
      "button",
      { class: "btn primary", onclick: function () { runTarget(ctrl, runBtn, sel, label); } },
      icon("sparkles"),
      t("consolidate")
    );
    ctrl.setFoot([h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("cancel")), runBtn]);
  }

  async function runTarget(ctrl, button, sel, label) {
    await withBusy(button, async function () {
      try {
        ctrl.setBody(spinnerBlock());
        const plan = await apiPost("consolidate/preview", { scope: sel.scope, key: sel.key || "" });
        showPlan(ctrl, sel, plan, plan.files || [], label);
      } catch (err) {
        ctrl.setBody(errorBlock(err.message, null));
      }
    });
  }

  function showPlan(ctrl, sel, plan, existing, label) {
    const ops = plan.operations || [];
    if (!ops.length) {
      ctrl.setBody(
        h(
          "div",
          { class: "stack" },
          emptyState("check", t("consolidateNothingTitle"), t("consolidateNothingHint")),
          (plan.skipped || []).length ? notice("info", t("consolidateSkipped", { n: plan.skipped.length })) : null
        )
      );
      ctrl.setFoot([h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("close"))]);
      return;
    }
    const selected = new Set(ops.map(function (_, index) { return index; }));
    const box = h("div", { class: "stack" });
    box.append(h("p", { class: "hint-line" }, t("consolidateReview", { n: ops.length })));
    if ((plan.skipped || []).length) {
      box.append(notice("info", t("consolidateSkipped", { n: plan.skipped.length })));
    }
    ops.forEach(function (op, index) {
      const check = h("input", {
        type: "checkbox",
        checked: true,
        onchange: function (event) {
          if (event.target.checked) {
            selected.add(index);
          } else {
            selected.delete(index);
          }
        },
      });
      box.append(
        h(
          "div",
          { class: "op" },
          h("div", { class: "op-head" }, check, h("strong", null, op.op === "delete" ? t("opDelete") : (existing.indexOf(op.file) >= 0 ? t("opRewrite") : t("opCreate"))), h("span", { class: "file-name" }, op.file)),
          op.reason ? h("div", { class: "op-reason" }, op.reason) : null,
          op.content ? h("details", null, h("summary", null, t("previewContent")), h("pre", { class: "pre" }, op.content)) : null
        )
      );
    });
    ctrl.setBody(box);
    const apply = h(
      "button",
      {
        class: "btn primary",
        onclick: async function () {
          const chosen = ops.filter(function (_, index) { return selected.has(index); });
          if (!chosen.length) {
            return;
          }
          await withBusy(apply, async function () {
            try {
              const res = await apiPost("consolidate/apply", {
                scope: sel.scope,
                key: sel.key || "",
                operations: chosen,
              });
              toast(res.message || t("consolidateDone"));
              ctrl.close(true);
              await refreshAll();
            } catch (err) {
              toast(err.message, "error");
            }
          });
        },
      },
      t("applySelected")
    );
    ctrl.setFoot([h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("cancel")), apply]);
  }

  async function consolidateAll() {
    const targets = consolidateTargets();
    if (!targets.length) {
      toast(t("consolidateAllNone"), "error");
      return;
    }
    const ctrl = openDialog({
      title: t("consolidateAllTitle"),
      wide: true,
      body: h("p", { class: "hint-line" }, t("consolidateAllHint")),
    });
    const box = h("div", { class: "stack" });
    ctrl.setBody(box);
    const results = [];
    const run = h("button", { class: "btn primary" }, t("consolidate"));
    ctrl.setFoot([h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("cancel")), run]);
    run.addEventListener("click", async function () {
      await withBusy(run, async function () {
        for (const target of targets) {
          try {
            const plan = await apiPost("consolidate/preview", { scope: target.scope, key: target.key || "" });
            if ((plan.operations || []).length) {
              results.push({ target: target, plan: plan });
            }
          } catch (err) {
            results.push({ target: target, error: err.message });
          }
        }
        if (!results.length) {
          ctrl.setBody(emptyState("check", t("consolidateNothingTitle"), t("consolidateAllNone")));
          return;
        }
        ctrl.setBody(renderConsolidateAll(results));
      });
    });
  }

  function renderConsolidateAll(results) {
    const box = h("div", { class: "stack" });
    results.forEach(function (item) {
      const label = item.target.label;
      if (item.error) {
        box.append(notice("warn", label + "：" + item.error));
        return;
      }
      const ops = item.plan.operations || [];
      const group = h("div", { class: "batch-group" }, h("div", { class: "batch-group-head" }, h("strong", null, label), h("span", { class: "hint-line" }, t("consolidateReview", { n: ops.length }))));
      const selected = new Set(ops.map(function (_, index) { return index; }));
      ops.forEach(function (op, index) {
        group.append(
          h(
            "div",
            { class: "batch-row" },
            h("input", {
              type: "checkbox",
              checked: true,
              onchange: function (event) {
                if (event.target.checked) {
                  selected.add(index);
                } else {
                  selected.delete(index);
                }
              },
            }),
            h("span", { class: "batch-name" }, op.file),
            h("span", { class: "batch-sub" }, op.op === "delete" ? t("opDelete") : op.reason || "")
          )
        );
      });
      group.append(
        h(
          "button",
          {
            class: "btn primary",
            onclick: async function () {
              const chosen = ops.filter(function (_, index) { return selected.has(index); });
              if (!chosen.length) {
                return;
              }
              try {
                await apiPost("consolidate/apply", {
                  scope: item.target.scope,
                  key: item.target.key || "",
                  operations: chosen,
                });
                toast(t("consolidateDone"));
                await refreshAll();
              } catch (err) {
                toast(err.message, "error");
              }
            },
          },
          t("applySelected")
        )
      );
      box.append(group);
    });
    return box;
  }

  // ------------------------------------------------------------ 整理记录

  async function loadRuns() {
    try {
      const data = await apiGet("tidy/runs", scopeParams());
      state.runs = data.runs || [];
    } catch (err) {
      toast(err.message, "error");
    }
    render();
  }

  function renderRunsTab() {
    const box = h("div", { class: "stack" });
    if (!state.runs) {
      box.append(spinnerBlock());
      return box;
    }
    if (!state.runs.length) {
      box.append(emptyState("history", t("historyEmptyTitle"), t("historyEmptyHint", { n: 5 })));
      return box;
    }
    state.runs.forEach(function (run) {
      const card = h(
        "div",
        { class: "run-card" },
        h(
          "div",
          { class: "run-head" },
          h("strong", null, fmtTime(run.at)),
          h("span", { class: "tag" }, t("trigger_" + run.trigger) || run.trigger),
          run.undoneAt ? h("span", { class: "tag warn" }, t("undone")) : null
        ),
        (run.ops || []).map(function (op) {
          return h("div", { class: "run-op" }, (op.op === "delete" ? t("opDelete") : op.op === "write" ? (op.created ? t("opCreate") : t("opRewrite")) : op.op) + " · " + op.file);
        }),
        !run.undoneAt
          ? h(
              "button",
              {
                class: "btn",
                onclick: async function () {
                  const ok = await confirmDialog({
                    title: t("undoTitle"),
                    message: t("undoMessage"),
                    confirm: t("undo"),
                  });
                  if (!ok) {
                    return;
                  }
                  try {
                    await apiPost("tidy/undo", Object.assign(scopeParams(), { id: run.id }));
                    toast(t("undoDone"));
                    state.runs = null;
                    await refreshAll();
                    await loadRuns();
                  } catch (err) {
                    toast(err.message, "error");
                  }
                },
              },
              t("undo")
            )
          : null
      );
      box.append(card);
    });
    return box;
  }

  // ------------------------------------------------------------ 备份与迁移

  function allScopes() {
    const ov = state.overview || {};
    const items = [];
    if (ov.board && ov.board.fileCount > 0) {
      items.push({ scope: "board", key: "", label: t("boardTitle"), fileCount: ov.board.fileCount });
    }
    (ov.notebooks || []).forEach(function (item) {
      if (item.fileCount > 0) {
        items.push({ scope: "notebook", key: item.key, label: t("notebookTitle", { platform: item.key }), fileCount: item.fileCount });
      }
    });
    (ov.whispers || []).forEach(function (item) {
      if (item.fileCount > 0) {
        items.push(Object.assign({ scope: "whisper", label: sessionTitle(item) }, item));
      }
    });
    return items;
  }

  function openMigration(tab) {
    const ctrl = openDialog({ title: t("migrate"), wide: true, body: h("p", { class: "hint-line" }, t("migrateHint")) });
    const content = h("div", { class: "stack" });
    const tabs = h("div", { class: "migrate-tabs" });
    const defs = [["export", "tabExport"], ["import", "tabImport"], ["scope", "tabScope"], ["platform", "tabPlatform"]];
    function show(name) {
      tabs.replaceChildren();
      defs.forEach(function (item) {
        tabs.append(
          h("button", { class: "tab" + (item[0] === name ? " active" : ""), onclick: function () { show(item[0]); } }, t(item[1]))
        );
      });
      content.replaceChildren();
      if (name === "export") {
        exportTab(content);
      } else if (name === "import") {
        importTab(content);
      } else if (name === "scope") {
        scopeMigrateTab(content);
      } else {
        platformTab(content);
      }
    }
    ctrl.setBody(h("div", { class: "stack" }, tabs, content));
    ctrl.setFoot([h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("close"))]);
    show(tab || "export");
  }

  function exportTab(box) {
    const scopes = allScopes();
    const selected = new Set(scopes.map(function (item) { return item.scope + ":" + (item.key || ""); }));
    const include = h("input", { type: "checkbox", checked: true });
    box.append(h("p", { class: "hint-line" }, t("exportHint")));
    scopes.forEach(function (item) {
      box.append(
        h(
          "label",
          { class: "check" },
          h("input", {
            type: "checkbox",
            checked: true,
            onchange: function (event) {
              if (event.target.checked) {
                selected.add(item.scope + ":" + (item.key || ""));
              } else {
                selected.delete(item.scope + ":" + (item.key || ""));
              }
            },
          }),
          item.label
        )
      );
    });
    box.append(h("label", { class: "check" }, include, t("includeForgotten")));
    const btn = h("button", { class: "btn primary" }, t("exportButton"));
    btn.addEventListener("click", async function () {
      await withBusy(btn, async function () {
        try {
          const res = await apiPost("export/prepare", {
            scopes: scopes
              .filter(function (item) { return selected.has(item.scope + ":" + (item.key || "")); })
              .map(function (item) { return { scope: item.scope, key: item.key || "" }; }),
            includeForgotten: include.checked,
          });
          const bundle = await apiGet("export/file", { token: res.token });
          const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
          const link = document.createElement("a");
          link.href = URL.createObjectURL(blob);
          link.download = res.filename;
          link.click();
          URL.revokeObjectURL(link.href);
          toast(t("exported"));
        } catch (err) {
          toast(err.message, "error");
        }
      });
    });
    box.append(btn);
  }

  function importTab(box) {
    const fileInput = h("input", { type: "file", accept: ".json,application/json" });
    const preview = h("div", { class: "stack" });
    const policy = h(
      "select",
      { class: "input" },
      ["newer", "skip", "overwrite"].map(function (value) {
        return h("option", { value: value }, t("policy_" + value));
      })
    );
    const selected = new Set();
    let token = "";
    const apply = h("button", { class: "btn primary" }, t("importButton"));
    apply.disabled = true;
    fileInput.addEventListener("change", async function () {
      const file = fileInput.files && fileInput.files[0];
      if (!file) {
        return;
      }
      try {
        const bundle = JSON.parse(await file.text());
        const res = await apiPost("import/preview", { bundle: bundle, policy: policy.value });
        token = res.token;
        selected.clear();
        (res.scopes || []).forEach(function (item) { selected.add(item.scope + ":" + (item.key || "")); });
        preview.replaceChildren(
          ...(res.scopes || []).map(function (item) {
            return h(
              "label",
              { class: "check" },
              h("input", {
                type: "checkbox",
                checked: true,
                onchange: function (event) {
                  if (event.target.checked) {
                    selected.add(item.scope + ":" + (item.key || ""));
                  } else {
                    selected.delete(item.scope + ":" + (item.key || ""));
                  }
                },
              }),
              (item.label || item.key || item.scope) + " · " + t("cntAdded", { n: item.counts.added }) + " / " + t("cntReplaced", { n: item.counts.replaced })
            );
          })
        );
        apply.disabled = false;
      } catch (err) {
        toast(t("badBackup"), "error");
      }
    });
    apply.addEventListener("click", async function () {
      await withBusy(apply, async function () {
        try {
          const res = await apiPost("import/apply", {
            token: token,
            policy: policy.value,
            selection: Array.from(selected).map(function (id) {
              const parts = id.split(":");
              return { scope: parts[0], key: parts.slice(1).join(":") };
            }),
          });
          toast(res.message || t("imported"));
          await refreshAll();
        } catch (err) {
          toast(err.message, "error");
        }
      });
    });
    box.append(h("p", { class: "hint-line" }, t("importHint")));
    box.append(fileInput, h("label", { class: "field-label" }, t("policy")), policy, preview, apply);
  }

  function scopeMigrateTab(box) {
    const scopes = allScopes();
    const options = scopes.map(function (item) {
      return h("option", { value: item.scope + ":" + (item.key || "") }, item.label);
    });
    const src = h("select", { class: "input" }, options);
    const dst = h("select", { class: "input" }, options);
    const mode = h(
      "select",
      { class: "input" },
      [h("option", { value: "copy" }, t("modeCopy")), h("option", { value: "move" }, t("modeMove"))]
    );
    const dry = h("input", { type: "checkbox", checked: true });
    const result = h("pre", { class: "pre" });
    const btn = h("button", { class: "btn primary" }, t("runMigrate"));
    btn.addEventListener("click", async function () {
      const from = src.value.split(":");
      const to = dst.value.split(":");
      try {
        const res = await apiPost("migrate/scope", {
          from: { scope: from[0], key: from.slice(1).join(":") },
          to: { scope: to[0], key: to.slice(1).join(":") },
          mode: mode.value,
          dryRun: dry.checked,
        });
        result.textContent = JSON.stringify(res, null, 2);
        if (!dry.checked) {
          await refreshAll();
        }
      } catch (err) {
        toast(err.message, "error");
      }
    });
    box.append(h("p", { class: "hint-line" }, t("migrateScopeHint")));
    box.append(h("label", { class: "field-label" }, t("modeLabel")), mode);
    box.append(h("label", { class: "field-label" }, t("platformFrom")), src);
    box.append(h("label", { class: "field-label" }, t("migrateTo")), dst);
    box.append(h("label", { class: "check" }, dry, t("migratePreview")), btn, result);
  }

  function platformTab(box) {
    const from = h("input", { class: "input", placeholder: "aiocqhttp" });
    const to = h("input", { class: "input", placeholder: "aiocqhttp-new" });
    const mode = h(
      "select",
      { class: "input" },
      [h("option", { value: "move" }, t("modeMove")), h("option", { value: "copy" }, t("modeCopy"))]
    );
    const includeNotebook = h("input", { type: "checkbox", checked: true });
    const dry = h("input", { type: "checkbox", checked: true });
    const result = h("pre", { class: "pre" });
    const btn = h("button", { class: "btn primary" }, t("runMigrate"));
    btn.addEventListener("click", async function () {
      try {
        const res = await apiPost("migrate/platform", {
          from: from.value.trim(),
          to: to.value.trim(),
          mode: mode.value,
          includeNotebook: includeNotebook.checked,
          dryRun: dry.checked,
        });
        result.textContent = JSON.stringify(res, null, 2);
        if (!dry.checked) {
          await refreshAll();
        }
      } catch (err) {
        toast(err.message, "error");
      }
    });
    box.append(h("p", { class: "hint-line" }, t("platformHint")));
    box.append(h("label", { class: "field-label" }, t("platformFrom")), from);
    box.append(h("label", { class: "field-label" }, t("platformTo")), to);
    box.append(h("label", { class: "field-label" }, t("modeLabel")), mode);
    box.append(h("label", { class: "check" }, includeNotebook, t("includeNotebook")));
    box.append(h("label", { class: "check" }, dry, t("migratePreview")), btn, result);
  }

  // ------------------------------------------------------------ 启动

  function render() {
    if (!els.main) {
      buildShell();
      return;
    }
    renderSidebar();
    renderMain();
  }

  async function init() {
    if (!bridge || !bridge.apiGet) {
      document.getElementById("app").replaceChildren(
        emptyState("warn", t("loadFailed"), t("noBridge"))
      );
      return;
    }
    buildShell();
    await loadOverview();
  }

  init();
})();
