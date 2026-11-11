/* 鲸群记忆管理页：「上下文」与「管理」两个视图。
 *
 * 上下文：按对话看最近一次请求的构成（按比例分段的横条与数字）、提供商上报
 * 的用量与缓存命中、AstrBot 的压缩摘要。面板只读，不提供任何改动上下文的操作。
 * 管理：概览、整理（一处或全部，预览后勾选应用，可撤销）、回收站与已遗忘、
 * 备份与迁移。
 */
(function (MP) {
  "use strict";

  const t = MP.t;
  const ui = MP.ui;
  const h = ui.h;
  const icon = ui.icon;
  const store = MP.store;
  const S = store.S;
  const M = MP.memory;
  const PARTS = ["system", "plugin", "tools", "history", "prompt", "recall"];

  function backLink(label, onBack) {
    return h(
      "button",
      { type: "button", class: "btn btn-ghost btn-sm back-link", onclick: onBack },
      icon("back"),
      label
    );
  }

  function card(title, body, extra) {
    return h(
      "section",
      { class: "card", "aria-label": title || null },
      title ? h("div", { class: "card-head" }, h("h3", { class: "card-title" }, title), extra || null) : null,
      body
    );
  }

  function statCard(label, value, sub) {
    return h(
      "div",
      { class: "stat" },
      h("div", { class: "stat-label" }, label),
      h("div", { class: "stat-value" }, value),
      sub ? h("div", { class: "stat-sub" }, sub) : null
    );
  }

  // ------------------------------------------------------------ 上下文

  /** 按比例分段的横条；每段的数字在下面的表里（读屏软件读表格） */
  function stackBar(parts, total) {
    return h(
      "div",
      { class: "stackbar", "aria-hidden": "true" },
      PARTS.filter(function (k) { return parts[k] > 0; }).map(function (k) {
        return h("span", {
          class: "seg seg-" + k,
          style: { width: (parts[k] / total) * 100 + "%" },
          title: t("part_" + k) + " · " + ui.fmtNum(parts[k]) + " · " + ui.fmtPct(parts[k] / total),
        });
      })
    );
  }

  function partsTable(parts, total) {
    return h(
      "table",
      { class: "legend-table" },
      h(
        "thead",
        null,
        h("tr", null, h("th", { scope: "col" }, t("partCol")), h("th", { scope: "col", class: "num" }, t("tokensCol")), h("th", { scope: "col", class: "num" }, t("shareCol")))
      ),
      h(
        "tbody",
        null,
        PARTS.map(function (k) {
          const value = parts[k] || 0;
          return h(
            "tr",
            { class: value ? null : "is-zero" },
            h(
              "th",
              { scope: "row" },
              h("span", { class: "swatch seg-" + k, "aria-hidden": "true" }),
              h("span", null, t("part_" + k)),
              h("span", { class: "legend-note" }, t("partNote_" + k))
            ),
            h("td", { class: "num" }, ui.fmtNum(value)),
            h("td", { class: "num" }, total ? ui.fmtPct(value / total) : "—")
          );
        })
      )
    );
  }

  /** 上下文窗口的来源说明：后端给稳定的来源代码，页面按界面语言写；认不出来才用后端的原文 */
  function windowNote(ctx) {
    if (ctx.windowSource) {
      return t("windowSource_" + ctx.windowSource, { model: ctx.windowModel || "" });
    }
    return ctx.windowNote || "";
  }

  function contextStats(data) {
    const ctx = data.context || {};
    const windowSize = ctx.window || 0;
    const ratio = windowSize > 0 ? data.estimate / windowSize : 0;
    const level = ratio >= 0.82 ? "danger" : ratio >= 0.7 ? "warn" : "ok";
    return h(
      "div",
      { class: "stats" },
      statCard(
        t("windowLabel"),
        windowSize > 0 ? ui.fmtNum(windowSize) : t("windowUnknown"),
        [
          windowNote(ctx) ? h("span", null, windowNote(ctx)) : null,
          windowSize > 0 && data.estimate
            ? h("div", { class: "meter meter-" + level, "aria-hidden": "true" }, h("span", { style: { width: Math.min(100, ratio * 100) + "%" } }))
            : null,
          windowSize > 0 && data.estimate ? h("span", { class: "stat-note" }, t("windowUsed", { pct: ui.fmtPct(ratio) })) : null,
        ]
      ),
      statCard(t("lastActual"), data.actual > 0 ? t("tokens", { n: ui.fmtNum(data.actual) }) : t("notReported"), t("lastActualSub")),
      statCard(
        t("cacheHit"),
        data.cacheHit >= 0 ? t("tokens", { n: ui.fmtNum(data.cacheHit) }) : t("notReported"),
        data.cacheHit >= 0 && data.actual > 0 ? t("cacheRate", { pct: ui.fmtPct(data.cacheHit / data.actual) }) : t("cacheHitSub")
      ),
      statCard(t("turnLimit"), ctx.turnLimit > 0 ? ui.fmtNum(ctx.turnLimit) : t("turnLimitNone"), t("turnLimitSub"))
    );
  }

  /** 一段对话的上下文面板（只读）：whisper/context */
  function ContextPanel(key) {
    const root = h("div", { class: "detail" });
    let data = null;
    let error = "";

    /** 读上下文。成功返回 true；失败返回 false，已有数据时留着旧数据并在上面提示 */
    async function load() {
      try {
        data = await ui.apiGet("whisper/context", { key: key });
        error = "";
      } catch (err) {
        error = err.message;
      }
      paint();
      return !error;
    }

    function composition() {
      const parts = data.parts || {};
      const total = PARTS.reduce(function (sum, k) { return sum + (parts[k] || 0); }, 0);
      const badge = h("span", { class: "tag " + (data.live ? "tag-ok" : "tag-muted"), title: data.live ? t("liveHint") : t("diskHint") }, data.live ? t("liveTag") : t("diskTag"));
      if (!total) {
        return card(t("partsTitle"), ui.emptyState({ icon: "context", title: t("noEstimate"), hint: t("noEstimateHint"), compact: true }), badge);
      }
      return card(
        t("partsTitle"),
        [
          h(
            "p",
            { class: "big-number" },
            h("strong", null, ui.fmtNum(data.estimate || total)),
            h("span", null, " tokens"),
            data.requestAt ? h("span", { class: "big-sub" }, t("lastRequestAt", { time: ui.fmtRel(data.requestAt) })) : null
          ),
          stackBar(parts, total),
          partsTable(parts, total),
          h("p", { class: "hint" }, data.live ? t("liveHint") : t("diskHint")),
        ],
        badge
      );
    }

    function summary() {
      return card(
        t("summaryTitle"),
        data.hasSummary
          ? h("div", { class: "summary-text", tabindex: "0", role: "region", "aria-label": t("summaryTitle") }, data.summary)
          : ui.emptyState({ icon: "info", title: t("noSummary"), hint: t("noSummaryHint"), compact: true })
      );
    }

    function paint() {
      const s = store.whisperByKey(key) || { key: key };
      const head = M.detailHead({
        view: "context",
        title: store.sessionTitle(s),
        tags: h("span", { class: "tag tag-muted" }, store.kindLabel(s.kind)),
        desc: store.sessionSub(s) || t("contextSub"),
        actions: [
          h("button", { type: "button", class: "btn btn-sm", onclick: M.action(load) }, icon("refresh"), t("refresh")),
        ],
      });
      root.setAttribute("aria-labelledby", head.id);
      if (!data) {
        ui.replace(root, head.el, error ? ui.errorState(error, load) : ui.spinner());
        return;
      }
      ui.replace(
        root,
        head.el,
        error ? ui.staleNotice(error, load) : null,
        ui.notice("info", t("contextReadonlyNotice")),
        data.context && data.context.userIdentifier ? ui.notice("warn", t("userIdentifierNotice")) : null,
        composition(),
        contextStats(data),
        summary()
      );
    }

    paint();
    load();
    return { el: root, reload: load };
  }

  // ------------------------------------------------------------ 整理：模型与任务状态

  const TASK_STATE_KEYS = {
    running: "taskRunning",
    ready: "taskReady",
    failed: "taskFailed",
    cancelled: "taskCancelled",
    interrupted: "taskInterrupted",
  };

  function taskStateText(state) {
    return t(TASK_STATE_KEYS[state] || "taskRunning");
  }

  function taskProgressText(task) {
    if (task.state === "running" && !task.batchCount) {
      return task.queued ? t("taskQueued") : t("taskPreparing");
    }
    return t("taskProgress", {
      done: task.batchesDone || 0,
      total: task.batchCount || 0,
      files: task.fileCount || 0,
    });
  }

  function taskModelText(task) {
    const model = task.model || {};
    return model.model ? model.providerId + " · " + model.model : model.providerId || "";
  }

  function taskProgressBar(task) {
    const total = task.batchCount || 0;
    const done = Math.min(task.batchesDone || 0, total);
    const percent = total ? Math.round((done / total) * 100) : 0;
    return h(
      "div",
      { class: "progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(total), "aria-valuenow": String(done) },
      h("span", { class: "progress-fill", style: "width:" + percent + "%" }),
      h("span", { class: "progress-text" }, taskProgressText(task))
    );
  }

  /** 任务状态的一行摘要：状态、模型、文件与批次进度、需要先拆分的文件 */
  function taskSummary(task) {
    const bits = [h("span", { class: "tag tag-task-" + task.state }, taskStateText(task.state))];
    if (task.queued) {
      bits.push(h("span", { class: "tag tag-muted" }, t("taskQueued")));
    }
    if (taskModelText(task)) {
      bits.push(h("span", { class: "hint" }, t("taskModelLabel", { model: taskModelText(task) })));
    }
    if (task.fileCount) {
      bits.push(h("span", { class: "hint" }, t("taskFiles", { n: task.fileCount }) + (task.batchCount ? " · " + t("taskBatches", { n: task.batchCount }) : "")));
    }
    return h("div", { class: "task-summary" }, bits);
  }

  function needsSplitNotice(task) {
    const names = (task && task.needsSplit) || [];
    if (!names.length) {
      return null;
    }
    return ui.notice(
      "warn",
      t("needsSplitHint", { n: names.length }) + " " + names.join(t("listSeparator"))
    );
  }

  /** 整理模型下拉框（PLAN 3.12.1）：选择存在服务端，下次打开还在。 */
  function TidyModelCard(onChange) {
    const root = h("div", { class: "model-picker" });
    const st = { loading: true, error: "", models: [], selected: null, saving: false, timeout: null, choices: [], customRange: null, draftSeconds: "", pendingCustom: false };

    async function load() {
      st.loading = true;
      paint();
      try {
        const data = await ui.apiGet("tidy/models");
        st.models = data.models || [];
        st.selected = data.selected || null;
        st.timeout = data.timeout || { mode: "auto" };
        st.choices = data.timeoutChoices || [];
        st.customRange = data.timeoutCustomRange || null;
        st.pendingCustom = false;
        if (st.timeout && st.timeout.mode === "custom") {
          st.draftSeconds = String(st.timeout.seconds || "");
        }
        st.error = "";
      } catch (err) {
        st.error = err.message;
      }
      st.loading = false;
      paint();
      return !st.error;
    }

    function ready() {
      return Boolean(st.selected && st.selected.available);
    }

    function requireReady() {
      if (ready()) {
        return true;
      }
      ui.toast(t(st.selected ? "tidyPickUnavailable" : "tidyPickNeed"), "warn");
      return false;
    }

    async function choose(providerId) {
      if (!providerId || st.saving) {
        return;
      }
      st.saving = true;
      paint();
      try {
        const data = await ui.apiPost("tidy/model", { providerId: providerId });
        st.models = data.models || st.models;
        st.selected = data.selected || null;
        ui.toast(t("tidyPickSaved"));
        if (onChange) {
          onChange();
        }
      } catch (err) {
        ui.toast(err.message, "error");
        await load();
        return;
      }
      st.saving = false;
      paint();
    }

    function paint() {
      if (st.loading) {
        ui.replace(root, ui.spinner(t("loading")));
        return;
      }
      if (st.error) {
        ui.replace(root, ui.errorState(st.error, load));
        return;
      }
      if (!st.models.length) {
        ui.replace(root, ui.notice("warn", t("tidyPickEmpty")));
        return;
      }
      const options = [h("option", { value: "" }, t("tidyPickPlaceholder"))];
      if (st.selected && !st.selected.available) {
        options.push(
          h(
            "option",
            { value: st.selected.providerId, selected: true },
            t("tidyPickOptionUnavailable", { provider: st.selected.providerId })
          )
        );
      }
      st.models.forEach(function (item) {
        const label = item.model ? item.providerId + " · " + item.model : item.providerId;
        options.push(
          h(
            "option",
            {
              value: item.providerId,
              selected: Boolean(st.selected && st.selected.providerId === item.providerId && st.selected.available),
            },
            label
          )
        );
      });
      const selectId = ui.uid("tidy-model");
      ui.replace(
        root,
        h("p", { class: "hint" }, t("tidyPickHint")),
        h(
          "div",
          { class: "field-row" },
          h(
            "label",
            { class: "field-label", for: selectId },
            t("tidyPickTitle")
          ),
          h(
            "select",
            {
              class: "input",
              id: selectId,
              disabled: st.saving,
              onchange: function (event) {
                choose(event.target.value);
              },
            },
            options
          )
        ),
        st.selected && !st.selected.available
          ? ui.notice("warn", t("tidyPickUnavailable"))
          : st.selected && st.selected.model
            ? h("p", { class: "hint" }, t("tidyPickCurrent", { model: st.selected.providerId + " · " + st.selected.model }))
            : null,
        timeoutRows()
      );
    }

    /** 超时下拉框：默认（自动推算）、不限时、3 分钟、5 分钟、自定义。 */
    function timeoutRows() {
      // 选了「自定义」但还没保存时，下拉框要停在这一项并露出输入框；
      // 只看 st.timeout 的话，重画会弹回已保存的值，看上去像选不中。
      const mode = st.pendingCustom ? "custom" : (st.timeout && st.timeout.mode) || "auto";
      const fixed = st.timeout && st.timeout.seconds ? st.timeout.seconds : null;
      const selectId = ui.uid("tidy-timeout");
      const options = [
        h("option", { value: "auto", selected: mode === "auto" }, t("tidyTimeoutAuto")),
        h("option", { value: "none", selected: mode === "none" }, t("tidyTimeoutNone")),
      ];
      (st.choices || []).forEach(function (seconds) {
        options.push(
          h(
            "option",
            {
              value: "fixed:" + seconds,
              selected: mode === "fixed" && fixed === seconds,
            },
            t("tidyTimeoutMinutes", { n: Math.round(seconds / 60) })
          )
        );
      });
      options.push(
        h("option", { value: "custom", selected: mode === "custom" }, t("tidyTimeoutCustom"))
      );
      const rows = [
        h("p", { class: "hint" }, t("tidyTimeoutHint")),
        h(
          "div",
          { class: "field-row" },
          h("label", { class: "field-label", for: selectId }, t("tidyTimeoutTitle")),
          h(
            "select",
            {
              class: "input",
              id: selectId,
              disabled: st.saving,
              onchange: function (event) {
                const value = event.target.value;
                if (value.indexOf("fixed:") === 0) {
                  st.pendingCustom = false;
                  saveTimeout({ mode: "fixed", seconds: Number(value.slice(6)) });
                } else if (value === "custom") {
                  st.pendingCustom = true;
                  st.draftSeconds = st.draftSeconds || String(fixed || 300);
                  paint();
                } else {
                  st.pendingCustom = false;
                  saveTimeout({ mode: value });
                }
              },
            },
            options
          )
        ),
      ];
      if (mode === "custom") {
        const range = st.customRange || [30, 3600];
        rows.push(
          h(
            "div",
            { class: "field-row" },
            h(
              "label",
              { class: "field-label" },
              t("tidyTimeoutSecondsLabel", { min: range[0], max: range[1] })
            ),
            h("input", {
              class: "input",
              type: "number",
              min: String(range[0]),
              max: String(range[1]),
              value: st.draftSeconds || String(fixed || 300),
              disabled: st.saving,
              oninput: function (event) {
                st.draftSeconds = event.target.value;
              },
            }),
            h(
              "button",
              {
                class: "btn",
                disabled: st.saving,
                onclick: function () {
                  saveTimeout({ mode: "custom", seconds: Number(st.draftSeconds) });
                },
              },
              t("tidyTimeoutSave")
            )
          )
        );
      }
      return rows;
    }

    async function saveTimeout(timeout) {
      if (st.saving) {
        return;
      }
      st.saving = true;
      paint();
      try {
        const data = await ui.apiPost("tidy/timeout", { timeout: timeout });
        st.timeout = data.timeout || st.timeout;
        st.pendingCustom = false;
        if (st.timeout && st.timeout.mode === "custom") {
          st.draftSeconds = String(st.timeout.seconds || "");
        }
        ui.toast(t("tidyTimeoutSaved"));
        if (onChange) {
          onChange();
        }
      } catch (err) {
        ui.toast(err.message, "error");
        await load();
        return;
      }
      st.saving = false;
      paint();
    }

    paint();
    load();
    return { el: root, load: load, ready: ready, requireReady: requireReady, state: st };
  }

  // ------------------------------------------------------------ 整理：方案审阅

  function opKind(op, existing) {
    if (op.op === "delete") {
      return "delete";
    }
    if (op.op === "fix") {
      return "fix";
    }
    return existing.has(op.file) ? "rewrite" : "create";
  }

  function countOps(ops, existing) {
    const counts = { create: 0, rewrite: 0, delete: 0, fix: 0 };
    ops.forEach(function (op) {
      counts[opKind(op, existing)] += 1;
    });
    return counts;
  }

  /** 整理方案里的字段行（「署名：堇」/「Signature: 堇」）：冒号跟着界面语言走 */
  function fieldLine(label, value) {
    return t("fieldLine", { label: label, value: value });
  }

  function planText(r, v) {
    const board = r.scope === "board";
    const lines = [fieldLine(t("fieldDescription"), v.description || "")];
    if (!board) {
      lines.push(fieldLine(t("fieldType"), v.type ? t("type_" + v.type) : "—"));
    } else {
      lines.push(fieldLine(t("fieldSignature"), v.signature || ""));
    }
    return lines.join("\n") + "\n\n" + (v.body || "");
  }

  /** 展开一项改动：改写看对比、新建看正文、删除看现在的内容（都在点开时才去读） */
  async function changeView(r, op, kind, existing) {
    if (kind === "create" || (kind === "fix" && !existing.has(op.file))) {
      return h("div", { class: "doc" }, MP.markdown.render(op.content || ""));
    }
    const current = await ui.apiGet("file", { scope: r.scope, key: r.key, file: op.file });
    if (kind === "delete") {
      return h("div", { class: "doc" }, MP.markdown.render(current.body || ""));
    }
    const before = planText(r, { description: current.description, type: current.type, signature: current.signature, body: current.body });
    const after = planText(r, { description: op.description, type: op.type, signature: op.signature, body: op.content });
    return MP.diff.render(MP.diff.diffLines(before, after));
  }

  /** 一份整理方案：每项可勾选、可展开看改动。onChange() 在勾选变化时调用 */
  function PlanReview(r, plan, onChange) {
    const ops = plan.operations || [];
    const existing = new Set(plan.files || []);
    const selected = new Set(ops.map(function (_, i) { return i; }));
    const autoCount = ops.filter(function (op) { return op.op === "fix"; }).length;
    const list = h("ul", { class: "ops" });
    ops.forEach(function (op, index) {
      const kind = opKind(op, existing);
      const checkId = ui.uid("op");
      const area = h("div", { class: "op-change", hidden: true });
      const toggle = h(
        "button",
        {
          type: "button",
          class: "btn btn-ghost btn-sm",
          "aria-expanded": "false",
          onclick: M.action(async function (btn) {
            const open = btn.getAttribute("aria-expanded") !== "true";
            btn.setAttribute("aria-expanded", open ? "true" : "false");
            area.hidden = !open;
            if (open && !area.firstChild) {
              ui.replace(area, ui.spinner());
              try {
                ui.replace(area, await changeView(r, op, kind, existing));
              } catch (err) {
                ui.replace(area, ui.errorState(err.message));
              }
            }
          }),
        },
        icon("eye"),
        t("viewChange")
      );
      const check = h("input", {
        type: "checkbox",
        id: checkId,
        checked: true,
        onchange: function (event) {
          if (event.target.checked) {
            selected.add(index);
          } else {
            selected.delete(index);
          }
          onChange();
        },
      });
      list.append(
        h(
          "li",
          { class: "op op-" + kind },
          h(
            "div",
            { class: "op-head" },
            check,
            h("label", { for: checkId, class: "op-label" }, h("span", { class: "tag tag-op-" + kind }, t("op_" + kind)), h("span", { class: "mono" }, op.file)),
            toggle
          ),
          op.reason ? h("p", { class: "op-reason" }, op.reason) : null,
          // 留言板的写入带署名（插件从原告示继承，新建的用默认值）：审阅时要看得到
          op.op === "write" && op.signature ? h("p", { class: "op-reason" }, fieldLine(t("fieldSignature"), op.signature)) : null,
          area
        )
      );
    });
    function setAll(on) {
      list.querySelectorAll('input[type="checkbox"]').forEach(function (box, i) {
        box.checked = on;
        if (on) {
          selected.add(i);
        } else {
          selected.delete(i);
        }
      });
      onChange();
    }
    const el = h(
      "div",
      { class: "plan" },
      h(
        "div",
        { class: "plan-head" },
        h("span", null, t("planSummary", { n: ops.length })),
        h(
          "div",
          { class: "plan-select" },
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { setAll(true); } }, t("selectAll")),
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { setAll(false); } }, t("selectNone"))
        )
      ),
      autoCount ? ui.notice("info", t("autoFixCount", { n: autoCount })) : null,
      (plan.needsSplit || []).length ? ui.notice("warn", t("needsSplitHint", { n: plan.needsSplit.length }) + " " + plan.needsSplit.join(t("listSeparator"))) : null,
      // 批外操作、读不出的文件等说明由后端给（管理员排错用，保留中文原因）
      (plan.notices || [])
        .filter(function (item) { return item && item.kind !== "needs_split" && item.message; })
        .map(function (item) { return ui.notice("warn", item.message); }),
      (plan.skipped || []).length ? ui.notice("info", t("consolidateSkipped", { n: plan.skipped.length })) : null,
      list
    );
    return {
      el: el,
      existing: existing,
      chosen: function () { return ops.filter(function (_, i) { return selected.has(i); }); },
      size: function () { return selected.size; },
      total: ops.length,
    };
  }

  /** 应用整理方案前的确认：写清会新建、改写、自动修复、删除几个文件，以及怎么撤销 */
  function confirmApply(counts, label) {
    return ui.confirm({
      title: t("applyTitle", { label: label }),
      points: [
        t("applyCounts", { create: counts.create, rewrite: counts.rewrite, fix: counts.fix || 0, del: counts.delete }),
        counts.fix ? t("applyPointAutoFix") : null,
        counts.rewrite || counts.delete ? t("applyPointBackup") : null,
        counts.delete ? t("applyPointDelete") : null,
        t("applyPointUndo"),
      ],
      confirmLabel: t("applySelected"),
      danger: counts.rewrite + counts.delete > 0,
    });
  }

  /**
   * 应用整理 / 撤销整理的逐项结果。成功项用页面自己的译文；失败项的原因是后端写的
   * （只有中文），原样显示。kind: "apply" 或 "undo"（撤销里 delete 是写回、write 是回退）
   */
  function resultList(results, kind) {
    function okText(item) {
      if (kind === "undo") {
        return t(item.op === "delete" ? "resultRestored" : "resultRolledBack");
      }
      if (item.op === "fix") {
        return t("resultFixed");
      }
      return t(item.op === "delete" ? "resultDeleted" : "resultWritten");
    }
    return h(
      "ul",
      { class: "results" },
      (results || []).map(function (item) {
        return h(
          "li",
          { class: item.ok ? "is-ok" : "is-fail" },
          icon(item.ok ? "check" : "warn"),
          h("span", { class: "mono" }, item.file || "—"),
          h("span", { class: "result-msg" }, item.ok ? okText(item) : item.message || t("requestFailed"))
        );
      })
    );
  }

  function runOpLabel(op) {
    if (op.op === "delete") {
      return t("op_delete");
    }
    if (op.op === "fix") {
      return t("op_fix");
    }
    return op.created ? t("op_create") : t("op_rewrite");
  }

  /** 整理记录：最近 5 次应用的改动，没撤销过的可以撤销 */
  function RunsSection(r, onUndone) {
    const box = h("div", { class: "runs" });
    let runs = null;
    let meta = null;
    let error = "";

    async function load() {
      try {
        const data = await ui.apiGet("tidy/runs", { scope: r.scope, key: r.key });
        runs = data.runs || [];
        meta = data.tidy || null;
        error = "";
      } catch (err) {
        error = err.message;
      }
      paint();
      return !error;
    }

    async function undo(run) {
      const ok = await ui.confirm({
        title: t("undoTitle"),
        points: [t("undoPoint1"), t("undoPoint2"), t("undoPoint3")],
        confirmLabel: t("undo"),
      });
      if (!ok) {
        return;
      }
      const res = await ui.apiPost("tidy/undo", { scope: r.scope, key: r.key, id: run.id });
      ui.toast(ui.resultMessage(res, t("undoDone")));
      if ((res.results || []).some(function (item) { return !item.ok; })) {
        ui.dialog({ title: t("undoResultTitle"), body: resultList(res.results, "undo"), actions: [{ label: t("close"), autofocus: true }] });
      }
      await store.changed(r);
      await load();
      if (onUndone) {
        onUndone();
      }
    }

    function paint() {
      const title = h("h3", { class: "card-title" }, t("tidyHistory"));
      if (error) {
        ui.replace(box, title, ui.errorState(error, load));
        return;
      }
      if (!runs) {
        ui.replace(box, title, ui.spinner());
        return;
      }
      const last = meta && meta.lastRun ? h("p", { class: "hint" }, t("lastTidy", { time: ui.fmtTime(meta.lastRun) })) : null;
      if (!runs.length) {
        ui.replace(box, title, last, ui.emptyState({ icon: "history", title: t("historyEmptyTitle"), hint: t("historyEmptyHint", { n: 5 }), compact: true }));
        return;
      }
      ui.replace(
        box,
        title,
        last,
        h(
          "ul",
          { class: "items" },
          runs.map(function (run) {
            return h(
              "li",
              { class: "run" + (run.undoneAt ? " is-undone" : "") },
              h(
                "div",
                { class: "run-head" },
                h("strong", null, ui.fmtTime(run.at)),
                h("span", { class: "tag tag-muted" }, t("trigger_" + (run.trigger || "manual"))),
                run.undoneAt ? h("span", { class: "tag tag-warn" }, t("undoneAt", { time: ui.fmtTime(run.undoneAt) })) : null,
                run.undoneAt ? null : h("button", { type: "button", class: "btn btn-sm", onclick: M.action(function () { return undo(run); }) }, icon("restore"), t("undo"))
              ),
              h(
                "ul",
                { class: "run-ops" },
                (run.ops || []).map(function (op) {
                  return h("li", null, h("span", { class: "tag tag-op-" + (op.op === "delete" ? "delete" : op.op === "fix" ? "fix" : op.created ? "create" : "rewrite") }, runOpLabel(op)), h("span", { class: "mono" }, op.file), op.reason ? h("span", { class: "op-reason" }, op.reason) : null);
                })
              )
            );
          })
        )
      );
    }

    paint();
    load();
    return { el: box, reload: load };
  }

  // ------------------------------------------------------------ 整理一处

  /** 整理一处记忆：选模型、后台分批复盘，刷新或离开再回来都能看到任务状态；下面是整理记录 */
  function TidyPanel(view, r, opts) {
    const root = h("div", { class: "detail" });
    const label = store.scopeLabel(r);
    const st = { phase: "loading", task: null, error: "", review: null, results: null };
    const modelCard = TidyModelCard();
    const runs = RunsSection(r);
    const TERMINAL = ["ready", "failed", "cancelled", "interrupted"];
    let applyBtn = null;
    let pollTimer = null;
    let pollToken = 0;

    function stopPolling() {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function isTerminal(task) {
      return Boolean(task && TERMINAL.indexOf(task.state) >= 0);
    }

    function updateApply() {
      if (applyBtn && st.review) {
        applyBtn.disabled = st.review.size() === 0;
        applyBtn.querySelector(".btn-label").textContent = t("applyN", { n: st.review.size() });
      }
    }

    function setTask(task) {
      st.task = task || null;
      st.review = null;
      if (!task) {
        st.phase = st.results ? "done" : "idle";
        return;
      }
      if (isTerminal(task)) {
        const plan = task.plan || {};
        const operations = plan.operations || [];
        st.review = operations.length ? PlanReview(r, plan, updateApply) : null;
        st.phase = "review";
      } else {
        st.phase = "running";
      }
    }

    function schedulePoll() {
      stopPolling();
      if (!st.task || st.task.state !== "running") {
        return;
      }
      const token = ++pollToken;
      pollTimer = setTimeout(async function () {
        pollTimer = null;
        if (token !== pollToken || !root.isConnected) {
          return;
        }
        await loadTask(true);
      }, 800);
    }

    async function loadTask(quiet) {
      try {
        const data = await ui.apiGet("tidy/task", { scope: r.scope, key: r.key });
        setTask(data.task);
        if (!quiet) {
          st.error = "";
        }
      } catch (err) {
        st.error = err.message;
        if (st.phase === "loading") {
          // 任务状态读不到时给错误态和重试入口，不能一直转圈
          st.phase = "idle";
        }
      }
      paint();
      schedulePoll();
    }

    async function start() {
      if (!modelCard.requireReady()) {
        return;
      }
      if (st.review && st.review.size() > 0) {
        const ok = await ui.confirm({
          title: t("discardTitle"),
          message: t("discardTaskPlan"),
          confirmLabel: t("discard"),
          danger: true,
        });
        if (!ok) {
          return;
        }
      }
      st.phase = "starting";
      st.task = null;
      st.review = null;
      st.results = null;
      st.error = "";
      paint();
      try {
        const data = await ui.apiPost("tidy/start", { scope: r.scope, key: r.key });
        setTask(data.task);
      } catch (err) {
        st.error = err.message;
        st.phase = "idle";
      }
      paint();
      schedulePoll();
    }

    async function cancel() {
      const ok = await ui.confirm({
        title: t("taskCancelConfirmTitle"),
        message: t("taskCancelConfirmPoint"),
        confirmLabel: t("taskCancel"),
        danger: true,
      });
      if (!ok) {
        return;
      }
      try {
        const res = await ui.apiPost("tidy/cancel", { scope: r.scope, key: r.key });
        setTask(res.task);
        ui.toast(t("taskCancelledNotice"), "info");
      } catch (err) {
        ui.toast(err.message, "error");
      }
      stopPolling();
      paint();
    }

    async function apply() {
      const ops = st.review.chosen();
      if (!ops.length) {
        return;
      }
      if (!(await confirmApply(countOps(ops, st.review.existing), label))) {
        return;
      }
      st.phase = "applying";
      paint();
      try {
        const res = await ui.apiPost("consolidate/apply", { scope: r.scope, key: r.key, operations: ops });
        st.results = res.results || [];
        ui.toast(ui.resultMessage(res, t("applied")));
        st.phase = "done";
        st.task = null;
        st.review = null;
        await store.changed(r);
        runs.reload();
      } catch (err) {
        ui.toast(err.message, "error");
        st.phase = "review";
      }
      paint();
    }

    function startButton(text) {
      return h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: M.action(start) }, icon("sparkles"), text);
    }

    function runningBody() {
      return [
        taskSummary(st.task),
        st.task.queued ? ui.notice("info", t("taskQueuedHint")) : null,
        taskProgressBar(st.task),
        needsSplitNotice(st.task),
        h(
          "div",
          { class: "actions" },
          h(
            "button",
            { type: "button", class: "btn btn-sm", onclick: M.action(cancel) },
            icon("stop"),
            t("taskCancel")
          )
        ),
      ];
    }

    function terminalBody() {
      const task = st.task;
      const notice = task.state === "ready"
        ? null
        : task.state === "failed"
          ? ui.notice("danger", t("taskFailedNotice", { message: task.message || t("requestFailed") }))
          : task.state === "cancelled"
            ? ui.notice("info", t("taskCancelledNotice"))
            : task.state === "interrupted"
              ? ui.notice("warn", t("taskInterruptedNotice"))
              : null;
      if (!st.review || !st.review.total) {
        return [
          taskSummary(task),
          notice,
          needsSplitNotice(task),
          task.state === "ready" && !(task.needsSplit || []).length
            ? ui.emptyState({ icon: "check", title: t("consolidateNothingTitle"), hint: t("consolidateNothingHint"), compact: true })
            : null,
          h("div", { class: "actions" }, startButton(t("consolidateAgain"))),
        ];
      }
      applyBtn = h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: M.action(apply) }, icon("check"), h("span", { class: "btn-label" }, t("applyN", { n: st.review.size() })));
      const discard = h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { st.review = null; st.phase = "idle"; paint(); } }, t("discardPlan"));
      return [
        taskSummary(task),
        notice,
        needsSplitNotice(task),
        st.review.el,
        h("div", { class: "actions sticky-actions" }, applyBtn, discard),
      ];
    }

    function body() {
      if (st.phase === "loading") {
        return ui.spinner(t("loading"));
      }
      if (st.phase === "starting") {
        return ui.spinner(t("consolidateRunning", { label: label }));
      }
      if (st.phase === "running") {
        return runningBody();
      }
      if (st.phase === "applying") {
        return ui.spinner(t("applying"));
      }
      if (st.phase === "done") {
        return [ui.notice("ok", t("consolidateDoneNotice")), resultList(st.results, "apply"), h("div", { class: "actions" }, startButton(t("consolidateAgain")))];
      }
      if (st.phase === "review") {
        return terminalBody();
      }
      return [
        h("p", null, t("consolidateHint", { label: label })),
        h("ul", { class: "confirm-points" }, h("li", null, t("consolidatePoint1")), h("li", null, t("consolidatePoint2"))),
        st.error ? ui.notice("danger", st.error) : null,
        h("div", { class: "actions" }, startButton(t("consolidate"))),
      ];
    }

    function paint() {
      const head = M.detailHead({ view: view, title: t("tidyTitle"), desc: label });
      root.setAttribute("aria-labelledby", head.id);
      ui.replace(
        root,
        opts && opts.onBack ? backLink(opts.backLabel || t("back"), opts.onBack) : null,
        head.el,
        card(t("tidyPickTitle"), modelCard.el),
        card(null, body()),
        card(null, runs.el)
      );
      updateApply();
    }

    paint();
    loadTask();
    return {
      el: root,
      // 刷新时重读模型与任务状态；正在审阅的方案原样留着
      reload: async function () {
        const modelOk = await modelCard.load();
        if (st.phase === "running" || st.phase === "loading" || !st.review) {
          await loadTask(true);
        }
        const runsOk = await runs.reload();
        return modelOk && runsOk !== false;
      },
      isDirty: function () { return Boolean(st.review && st.review.size() > 0); },
      markClean: function () { st.review = null; st.phase = "idle"; paint(); },
    };
  }

  // ------------------------------------------------------------ 整理全部

  /** "整理全部记忆"覆盖三层：留言板、各机器人的小本本、各段对话的悄悄话 */
  function consolidateTargets() {
    const ov = S.ov || {};
    const targets = [];
    if (ov.board && ov.board.fileCount > 0) {
      targets.push({ ref: store.ref("board", ""), label: t("boardTitle") });
    }
    (ov.notebooks || []).forEach(function (item) {
      if (item.fileCount > 0) {
        targets.push({ ref: store.ref("notebook", item.key), label: t("notebookTitle", { platform: item.platform || item.key }) });
      }
    });
    (ov.whispers || []).forEach(function (item) {
      if (item.fileCount > 0) {
        targets.push({ ref: store.ref("whisper", item.key), label: store.whisperLabel(item, item.key) });
      }
    });
    return targets;
  }

  function TidyAllPanel(opts) {
    const root = h("div", { class: "detail" });
    const st = { phase: "idle", targets: [], tasks: {}, errors: {}, groups: [], results: [] };
    const modelCard = TidyModelCard();
    let applyBtn = null;
    let pollTimer = null;
    let pollToken = 0;

    function sidOf(ref) {
      return store.sid(ref);
    }

    function taskOf(target) {
      return st.tasks[sidOf(target.ref)] || null;
    }

    function anyRunning() {
      return st.targets.some(function (target) {
        const task = taskOf(target);
        return task && task.state === "running";
      });
    }

    function stopPolling() {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function chosenTotal() {
      return st.groups.reduce(function (sum, g) { return sum + (g.review ? g.review.size() : 0); }, 0);
    }

    function updateApply() {
      if (applyBtn) {
        const n = chosenTotal();
        applyBtn.disabled = n === 0;
        applyBtn.querySelector(".btn-label").textContent = t("applyN", { n: n });
      }
    }

    async function loadTasks() {
      const data = await ui.apiGet("tidy/tasks");
      (data.tasks || []).forEach(function (task) {
        const ref = store.ref(task.scope, task.key);
        if (st.targets.some(function (item) { return store.sameRef(item.ref, ref); })) {
          st.tasks[sidOf(ref)] = task;
        }
      });
    }

    function finish() {
      st.groups = st.targets.map(function (target) {
        const sid = sidOf(target.ref);
        if (st.errors[sid]) {
          return { target: target, error: st.errors[sid] };
        }
        const task = st.tasks[sid];
        if (!task) {
          return { target: target, error: t("taskMissing") };
        }
        const plan = task.plan || {};
        const review = (plan.operations || []).length ? PlanReview(target.ref, plan, updateApply) : null;
        return { target: target, task: task, review: review, nothing: !review && task.state === "ready" };
      });
      st.phase = "review";
      paint();
    }

    function poll() {
      stopPolling();
      if (st.phase !== "running") {
        return;
      }
      const token = ++pollToken;
      pollTimer = setTimeout(async function () {
        pollTimer = null;
        if (token !== pollToken || !root.isConnected) {
          return;
        }
        try {
          await loadTasks();
        } catch (err) {
          st.errors.poll = err.message;
        }
        if (anyRunning()) {
          paint();
          poll();
        } else {
          finish();
        }
      }, 800);
    }

    async function start() {
      if (!modelCard.requireReady()) {
        return;
      }
      st.targets = consolidateTargets();
      if (!st.targets.length) {
        ui.toast(t("consolidateAllNone"), "info");
        return;
      }
      if (st.review && chosenTotal() > 0) {
        const ok = await ui.confirm({
          title: t("discardTitle"),
          message: t("discardTaskPlan"),
          confirmLabel: t("discard"),
          danger: true,
        });
        if (!ok) {
          return;
        }
      }
      st.phase = "starting";
      st.tasks = {};
      st.errors = {};
      st.groups = [];
      st.results = [];
      paint();
      for (const target of st.targets) {
        const sid = sidOf(target.ref);
        try {
          const data = await ui.apiPost("tidy/start", { scope: target.ref.scope, key: target.ref.key });
          st.tasks[sid] = data.task;
        } catch (err) {
          st.errors[sid] = err.message;
        }
      }
      st.phase = "running";
      paint();
      poll();
    }

    async function cancelAll() {
      const ok = await ui.confirm({
        title: t("taskCancelAllTitle"),
        message: t("taskCancelConfirmPoint"),
        confirmLabel: t("taskCancel"),
        danger: true,
      });
      if (!ok) {
        return;
      }
      for (const target of st.targets) {
        const task = taskOf(target);
        if (!task || task.state !== "running") {
          continue;
        }
        try {
          const res = await ui.apiPost("tidy/cancel", { scope: target.ref.scope, key: target.ref.key });
          st.tasks[sidOf(target.ref)] = res.task;
        } catch (err) {
          st.errors[sidOf(target.ref)] = err.message;
        }
      }
      stopPolling();
      finish();
    }

    async function apply() {
      const counts = { create: 0, rewrite: 0, delete: 0, fix: 0 };
      const work = st.groups.filter(function (g) { return g.review && g.review.size(); });
      work.forEach(function (g) {
        const c = countOps(g.review.chosen(), g.review.existing);
        counts.create += c.create;
        counts.rewrite += c.rewrite;
        counts.delete += c.delete;
        counts.fix += c.fix;
      });
      if (!work.length || !(await confirmApply(counts, t("consolidateAllScope", { n: work.length })))) {
        return;
      }
      st.phase = "applying";
      st.results = [];
      paint();
      for (const g of work) {
        try {
          const res = await ui.apiPost("consolidate/apply", { scope: g.target.ref.scope, key: g.target.ref.key, operations: g.review.chosen() });
          st.results.push({ target: g.target, results: res.results || [] });
        } catch (err) {
          st.results.push({ target: g.target, error: err.message });
        }
        await store.changed(g.target.ref);
      }
      st.phase = "done";
      ui.toast(t("consolidateAllDone", { n: work.length }));
      paint();
    }

    function taskBox(target) {
      const sid = sidOf(target.ref);
      const error = st.errors[sid];
      const task = st.tasks[sid];
      const head = h("div", { class: "group-head" }, h("h3", { class: "card-title" }, target.label));
      if (error) {
        return card(null, [head, ui.notice("danger", error)]);
      }
      if (!task) {
        return card(null, [head, ui.spinner(t("taskPreparing"))]);
      }
      return card(null, [
        head,
        taskSummary(task),
        task.state === "running"
          ? taskProgressBar(task)
          : task.state === "failed"
            ? ui.notice("danger", t("taskFailedNotice", { message: task.message || t("requestFailed") }))
            : task.state === "cancelled"
              ? ui.notice("info", t("taskCancelledNotice"))
              : task.state === "interrupted"
                ? ui.notice("warn", t("taskInterruptedNotice"))
                : null,
        needsSplitNotice(task),
      ]);
    }

    function groupBox(g) {
      const headLine = h("div", { class: "group-head" }, h("h3", { class: "card-title" }, g.target.label));
      if (g.error) {
        return card(null, [headLine, ui.notice("danger", g.error)]);
      }
      if (g.nothing) {
        return card(null, [headLine, h("p", { class: "hint" }, t("consolidateNothingHint"))]);
      }
      const stateNotice = g.task && g.task.state === "failed"
        ? ui.notice("danger", t("taskFailedNotice", { message: g.task.message || t("requestFailed") }))
        : g.task && g.task.state === "cancelled"
          ? ui.notice("info", t("taskCancelledNotice"))
          : g.task && g.task.state === "interrupted"
            ? ui.notice("warn", t("taskInterruptedNotice"))
            : null;
      return card(null, [headLine, stateNotice, needsSplitNotice(g.task), g.review ? g.review.el : null]);
    }

    function body() {
      if (st.phase === "starting" || st.phase === "running") {
        return [
          h("p", { class: "hint" }, t("consolidateAllRunningHint", { n: st.targets.length })),
          st.errors.poll ? ui.notice("danger", st.errors.poll) : null,
          st.targets.map(taskBox),
          h(
            "div",
            { class: "actions" },
            h("button", { type: "button", class: "btn btn-sm", onclick: M.action(cancelAll) }, icon("stop"), t("taskCancelAll"))
          ),
        ];
      }
      if (st.phase === "applying") {
        return ui.spinner(t("applying"));
      }
      if (st.phase === "done") {
        return [
          ui.notice("ok", t("consolidateDoneNotice")),
          st.results.map(function (item) {
            return card(null, [h("h3", { class: "card-title" }, item.target.label), item.error ? ui.notice("danger", item.error) : resultList(item.results, "apply")]);
          }),
        ];
      }
      if (st.phase === "review") {
        const reviewable = st.groups.filter(function (g) { return g.review; }).length;
        applyBtn = h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: M.action(apply) }, icon("check"), h("span", { class: "btn-label" }, t("applyN", { n: chosenTotal() })));
        return [
          h("p", { class: "hint" }, reviewable ? t("consolidateAllReview", { n: reviewable }) : t("consolidateAllNothing")),
          st.groups.map(groupBox),
          reviewable ? h("div", { class: "actions sticky-actions" }, applyBtn) : null,
        ];
      }
      const gate = M.overviewGate();
      if (gate) {
        // 总览没读到，不知道有多少处可整理：不能显示「0 处」
        return [h("p", null, t("consolidateAllHint")), gate];
      }
      return [
        h("p", null, t("consolidateAllHint")),
        h("p", { class: "hint" }, t("consolidateAllCount", { n: consolidateTargets().length })),
        st.errors.poll ? ui.notice("danger", st.errors.poll) : null,
        h("div", { class: "actions" }, h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: M.action(start) }, icon("sparkles"), t("consolidateAll"))),
      ];
    }

    function paint() {
      const head = M.detailHead({ view: "manage", title: t("consolidateAllTitle") });
      root.setAttribute("aria-labelledby", head.id);
      ui.replace(
        root,
        opts && opts.onBack ? backLink(t("backToTidy"), opts.onBack) : null,
        head.el,
        card(t("tidyPickTitle"), modelCard.el),
        card(null, body())
      );
      updateApply();
    }

    paint();
    return {
      el: root,
      // 刷新时重读模型；正在跑的任务继续轮询，审阅中的方案原样留着
      reload: async function () {
        const modelOk = await modelCard.load();
        if (st.phase === "running") {
          await loadTasks();
          paint();
          poll();
        } else if (st.phase === "idle") {
          paint();
        }
        return modelOk;
      },
      isDirty: function () { return st.phase === "review" && chosenTotal() > 0; },
      markClean: function () { st.phase = "idle"; st.groups = []; paint(); },
    };
  }

  // ------------------------------------------------------------ 管理：概览

  function OverviewSection() {
    const root = h("div", { class: "detail" });
    const ov = S.ov || {};
    const notebooks = ov.notebooks || [];
    const whispers = ov.whispers || [];
    const sum = function (list) { return list.reduce(function (acc, item) { return acc + (item.fileCount || 0); }, 0); };
    const head = M.detailHead({ view: "manage", title: t("manageOverview"), desc: t("welcomeHint") });
    root.setAttribute("aria-labelledby", head.id);
    // 总览没读到：显示加载中或错误，不能拿一堆 0 当真数据
    const gate = M.overviewGate();
    if (gate) {
      ui.replace(root, head.el, gate);
      return { el: root };
    }
    ui.replace(
      root,
      head.el,
      ov.memoryEnabled === false ? ui.notice("warn", t("memoryOffNotice")) : null,
      ov.context && ov.context.userIdentifier ? ui.notice("warn", t("userIdentifierNotice")) : null,
      h(
        "div",
        { class: "stats" },
        statCard(t("statBoard"), ui.fmtNum(ov.board ? ov.board.fileCount : 0), t("statBoardSub")),
        statCard(t("statNotebooks"), ui.fmtNum(notebooks.length), t("statFiles", { n: ui.fmtNum(sum(notebooks)) })),
        statCard(
          t("statWhispers"),
          ui.fmtNum(whispers.filter(function (s) { return s.fileCount > 0; }).length),
          t("statWhispersSub", { n: ui.fmtNum(whispers.length), files: ui.fmtNum(sum(whispers)) })
        )
      ),
      card(
        t("quickActions"),
        h(
          "div",
          { class: "actions" },
          h("button", { type: "button", class: "btn btn-sm", onclick: function () { MP.app.openManage("tidy", { tidySid: "all" }); } }, icon("sparkles"), t("consolidateAll")),
          h("button", { type: "button", class: "btn btn-sm", onclick: function () { MP.app.openManage("transfer", { transferTab: "export" }); } }, icon("download"), t("exportButton"))
        )
      )
    );
    return { el: root };
  }

  // ------------------------------------------------------------ 管理：整理、回收站与已遗忘

  /** 作用域清单（点一行进入那一处）；items 来自 store.scopesWithFiles / allScopes */
  function scopeRows(items, onPick, emptyTitle) {
    if (!items.length) {
      return ui.emptyState({ icon: "file", title: emptyTitle, compact: true });
    }
    const list = h(
      "div",
      { class: "rows", role: "list" },
      items.map(function (item) {
        return h(
          "div",
          { role: "listitem" },
          h(
            "button",
            { type: "button", class: "row row-scope", "data-nav-item": "", onclick: function () { onPick(item.ref); } },
            h("span", { class: "row-main" }, h("span", { class: "row-name" }, item.label)),
            ui.countBadge("count", ui.fmtNum(item.count || 0), t("fileCount", { n: item.count || 0 })),
            icon("chevronRight")
          )
        );
      })
    );
    ui.listNav(list);
    return list;
  }

  /** 所有作用域的整理任务：打开管理页就能看到刷新前留下的整理中、方案已就绪等状态 */
  function TidyTasksCard() {
    const root = h("div");
    let tasks = null;
    let error = "";

    function taskRow(task) {
      const ref = store.ref(task.scope, task.key);
      return h(
        "li",
        { class: "item" },
        h("div", { class: "item-title" }, store.scopeLabel(ref)),
        taskSummary(task),
        task.state === "running" ? taskProgressBar(task) : null,
        task.state === "failed"
          ? h("p", { class: "hint" }, t("taskFailedNotice", { message: task.message || t("requestFailed") }))
          : null,
        h(
          "div",
          { class: "actions" },
          h(
            "button",
            {
              type: "button",
              class: "btn btn-sm",
              onclick: function () { MP.app.openManage("tidy", { tidySid: store.sid(ref) }); },
            },
            icon("chevronRight"),
            t("viewTask")
          )
        )
      );
    }

    function paint() {
      const title = h("h3", { class: "card-title" }, t("tasksTitle"));
      if (error) {
        ui.replace(root, card(null, [title, ui.errorState(error, load)]));
        return;
      }
      if (!tasks) {
        ui.replace(root, card(null, [title, ui.spinner()]));
        return;
      }
      if (!tasks.length) {
        ui.replace(root, card(null, [title, h("p", { class: "hint" }, t("tasksNone"))]));
        return;
      }
      ui.replace(root, card(null, [title, h("ul", { class: "items" }, tasks.map(taskRow))]));
    }

    async function load() {
      try {
        const data = await ui.apiGet("tidy/tasks");
        tasks = data.tasks || [];
        error = "";
      } catch (err) {
        error = err.message;
      }
      paint();
      return !error;
    }

    paint();
    load();
    return { el: root, reload: load };
  }

  function TidyHome() {
    const root = h("div", { class: "detail" });
    const head = M.detailHead({ view: "manage", title: t("manageTidy"), desc: t("manageTidyHint") });
    root.setAttribute("aria-labelledby", head.id);
    const gate = M.overviewGate();
    const modelCard = TidyModelCard();
    const tasksCard = TidyTasksCard();
    ui.replace(
      root,
      head.el,
      gate
        ? card(null, gate)
        : [
            card(t("tidyPickTitle"), modelCard.el),
            tasksCard.el,
            card(
              t("consolidateAllTitle"),
              [
                h("p", { class: "hint" }, t("consolidateAllHint")),
                h("div", { class: "actions" }, h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: function () { MP.app.openManage("tidy", { tidySid: "all" }); } }, icon("sparkles"), t("consolidateAll"))),
              ]
            ),
            card(
              t("consolidateOneTitle"),
              [
                h("p", { class: "hint" }, t("consolidateOneHint")),
                scopeRows(store.scopesWithFiles(), function (r) { MP.app.openManage("tidy", { tidySid: store.sid(r) }); }, t("consolidateAllNone")),
              ]
            ),
          ]
    );
    return {
      el: root,
      reload: async function () {
        const modelOk = await modelCard.load();
        const tasksOk = await tasksCard.reload();
        return modelOk && tasksOk !== false;
      },
    };
  }

  function MaintHome() {
    const root = h("div", { class: "detail" });
    const head = M.detailHead({ view: "manage", title: t("manageMaint"), desc: t("manageMaintHint") });
    root.setAttribute("aria-labelledby", head.id);
    ui.replace(
      root,
      head.el,
      card(
        null,
        M.overviewGate() || scopeRows(store.allScopes(), function (r) { MP.app.openManage("maint", { maintSid: store.sid(r) }); }, t("noScopes"))
      )
    );
    return { el: root };
  }

  // ------------------------------------------------------------ 管理：备份与迁移

  /** 多选框清单；返回 {el, values()} */
  function checklist(items, idPrefix) {
    const boxes = [];
    const el = h(
      "div",
      { class: "checklist" },
      items.map(function (item) {
        const id = ui.uid(idPrefix || "chk");
        const box = h("input", { type: "checkbox", id: id, checked: item.checked !== false, value: item.value });
        boxes.push(box);
        return h(
          "div",
          { class: "check-row" },
          box,
          h("label", { for: id }, h("span", null, item.label), item.meta ? h("span", { class: "check-meta" }, item.meta) : null)
        );
      })
    );
    function setAll(on) {
      boxes.forEach(function (box) { box.checked = on; });
    }
    return {
      el: el,
      setAll: setAll,
      values: function () { return boxes.filter(function (box) { return box.checked; }).map(function (box) { return box.value; }); },
    };
  }

  function policySelect(id, value) {
    return h(
      "select",
      { class: "input", id: id },
      ["newer", "skip", "overwrite"].map(function (p) {
        return h("option", { value: p, selected: p === value }, t("policy_" + p));
      })
    );
  }

  function countsLine(counts) {
    const c = counts || {};
    return [
      t("cntAdded", { n: c.added || 0 }),
      t("cntReplaced", { n: c.replaced || 0 }),
      t("cntSame", { n: c.same || 0 }),
      t("cntKept", { n: c.kept || 0 }),
      t("cntInvalid", { n: c.invalid || 0 }),
    ].join(" · ");
  }

  function issuesBox(issues) {
    if (!(issues || []).length) {
      return null;
    }
    return h(
      "details",
      { class: "issues" },
      h("summary", null, t("issuesCount", { n: issues.length })),
      h(
        "ul",
        null,
        issues.map(function (item) {
          return h("li", null, h("span", { class: "mono" }, item.name), " · ", t("outcome_" + item.outcome), ui.zhOnly(item.reason) ? " · " + item.reason : "");
        })
      )
    );
  }

  /** 迁移报告（作用域之间、平台改名都用）：每处 来源 → 目标 与计数 */
  function reportList(items) {
    return h(
      "ul",
      { class: "items" },
      (items || []).map(function (item) {
        return h(
          "li",
          { class: "report" },
          h("div", { class: "item-title" }, store.describeLabel(item.from), " → ", store.describeLabel(item.to)),
          h("div", { class: "item-meta" }, countsLine(item.counts), item.moved ? " · " + t("cntMoved", { n: item.moved }) : ""),
          issuesBox(item.issues)
        );
      })
    );
  }

  function exportTab() {
    const gate = M.overviewGate();
    if (gate) {
      return gate;
    }
    const scopes = store.scopesWithFiles();
    if (!scopes.length) {
      return ui.emptyState({ icon: "download", title: t("exportNone"), compact: true });
    }
    const list = checklist(
      scopes.map(function (item) { return { value: store.sid(item.ref), label: item.label, meta: t("fileCount", { n: item.count }) }; }),
      "exp"
    );
    const includeId = ui.uid("inc");
    const include = h("input", { type: "checkbox", id: includeId, checked: true });
    async function run() {
      const chosen = list.values().map(store.parseSid).filter(Boolean);
      if (!chosen.length) {
        ui.toast(t("pickScopes"), "info");
        return;
      }
      const res = await ui.apiPost("export/prepare", {
        scopes: chosen.map(function (r) { return { scope: r.scope, key: r.key }; }),
        includeForgotten: include.checked,
      });
      await ui.download("export/file", { token: res.token }, res.filename);
      ui.toast(t("exported", { scopes: res.scopeCount, files: res.fileCount }));
    }
    return [
      h("p", { class: "hint" }, t("exportHint")),
      h(
        "div",
        { class: "plan-select" },
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { list.setAll(true); } }, t("selectAll")),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: function () { list.setAll(false); } }, t("selectNone"))
      ),
      list.el,
      h("div", { class: "check-row" }, include, h("label", { for: includeId }, t("includeForgotten"))),
      h("div", { class: "actions" }, h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: M.action(run) }, icon("download"), t("exportButton"))),
    ];
  }

  /** 导入、迁移之后很多处都可能变了：清掉缓存，按需重新加载 */
  async function afterBulkChange() {
    Object.keys(S.scopes).forEach(function (k) { delete S.scopes[k]; });
    await store.loadOverview(true);
  }

  function importTab() {
    const st = { token: "", scopes: null, policy: "newer" };
    const box = h("div", { class: "stack" });
    const fileId = ui.uid("imp");
    const policyId = ui.uid("pol");
    const result = h("div", { class: "stack" });
    const policy = policySelect(policyId, st.policy);
    let list = null;

    async function preview(extra) {
      const res = await ui.apiPost("import/preview", Object.assign({ policy: st.policy }, extra));
      st.token = res.token;
      st.scopes = res.scopes || [];
      paint();
    }

    const fileInput = h("input", {
      type: "file",
      id: fileId,
      class: "file-input",
      accept: ".json,application/json",
      onchange: async function () {
        const file = fileInput.files && fileInput.files[0];
        if (!file) {
          return;
        }
        let bundle;
        try {
          bundle = JSON.parse(await file.text());
        } catch (err) {
          ui.toast(t("badBackup"), "error");
          return;
        }
        try {
          await preview({ bundle: bundle });
        } catch (err) {
          ui.toast(err.message, "error");
        }
      },
    });
    policy.addEventListener("change", function () {
      st.policy = policy.value;
      if (st.token) {
        preview({ token: st.token }).catch(function (err) { ui.toast(err.message, "error"); });
      }
    });

    async function apply() {
      const chosen = list ? list.values() : [];
      const picked = (st.scopes || []).filter(function (item) { return chosen.indexOf(itemId(item)) >= 0; });
      if (!picked.length) {
        ui.toast(t("pickScopes"), "info");
        return;
      }
      const sum = function (k) { return picked.reduce(function (acc, item) { return acc + ((item.counts || {})[k] || 0); }, 0); };
      const ok = await ui.confirm({
        title: t("importTitle", { n: picked.length }),
        points: [
          t("importPointAdd", { n: sum("added") }),
          sum("replaced") ? t("importPointReplace", { n: sum("replaced") }) : null,
          t("importPointPolicy", { policy: t("policy_" + st.policy) }),
        ],
        confirmLabel: t("importButton"),
        danger: sum("replaced") > 0,
      });
      if (!ok) {
        return;
      }
      const res = await ui.apiPost("import/apply", {
        token: st.token,
        policy: st.policy,
        selection: picked.map(function (item) { return { scope: item.scope, key: item.key || "" }; }),
      });
      ui.toast(ui.resultMessage(res, t("imported")));
      await afterBulkChange();
      ui.replace(
        result,
        h("h3", { class: "card-title" }, t("importResult")),
        h(
          "ul",
          { class: "items" },
          (res.reports || []).map(function (rep) {
            return h("li", { class: "report" }, h("div", { class: "item-title" }, store.describeLabel(rep.to || rep.from)), h("div", { class: "item-meta" }, countsLine(rep.counts)), issuesBox(rep.issues));
          })
        )
      );
    }

    function itemId(item) {
      return item.scope === "board" ? "board" : item.scope + ":" + (item.key || "");
    }

    function paint() {
      const controls = h(
        "div",
        { class: "form-grid two" },
        h("div", { class: "field" }, h("label", { class: "field-label", for: fileId }, t("chooseFile")), fileInput, h("p", { class: "field-hint" }, t("importHint"))),
        h("div", { class: "field" }, h("label", { class: "field-label", for: policyId }, t("policy")), policy, h("p", { class: "field-hint" }, t("policyHint")))
      );
      if (!st.scopes) {
        ui.replace(box, controls, result);
        return;
      }
      if (!st.scopes.length) {
        ui.replace(box, controls, ui.emptyState({ title: t("importNothing"), compact: true }), result);
        return;
      }
      list = checklist(
        st.scopes.map(function (item) {
          return {
            value: itemId(item),
            label: store.describeLabel(item),
            meta: [
              t("fileCount", { n: item.fileCount || 0 }),
              item.forgottenCount ? t("forgottenIncluded", { n: item.forgottenCount }) : null,
              countsLine(item.counts),
            ].filter(Boolean).join(" · "),
          };
        }),
        "impsel"
      );
      ui.replace(
        box,
        controls,
        h("h3", { class: "card-title" }, t("importPreview")),
        h("p", { class: "hint" }, t("importPreviewHint")),
        list.el,
        st.scopes.map(function (item) { return issuesBox(item.issues); }),
        h("div", { class: "actions" }, h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: M.action(apply) }, icon("upload"), t("importButton"))),
        result
      );
    }

    paint();
    return box;
  }

  function labeled(label, control, hint) {
    const id = control.id || ui.uid("ctl");
    control.id = id;
    return h("div", { class: "field" }, h("label", { class: "field-label", for: id }, label), control, hint ? h("p", { class: "field-hint" }, hint) : null);
  }

  function modeSelect(value) {
    return h(
      "select",
      { class: "input" },
      h("option", { value: "copy", selected: value === "copy" }, t("modeCopy")),
      h("option", { value: "move", selected: value === "move" }, t("modeMove"))
    );
  }

  function migrateTab() {
    const gate = M.overviewGate();
    if (gate) {
      return gate;
    }
    const sources = store.scopesWithFiles();
    if (!sources.length) {
      return ui.emptyState({ icon: "transfer", title: t("migrateNothing"), compact: true });
    }
    const src = h("select", { class: "input" }, sources.map(function (item) { return h("option", { value: store.sid(item.ref) }, item.label); }));
    const dst = h(
      "select",
      { class: "input" },
      store.allScopes().map(function (item) { return h("option", { value: store.sid(item.ref) }, item.label); }),
      h("option", { value: "@umo" }, t("targetNewSession")),
      h("option", { value: "@platform" }, t("targetPlatformNotebook"))
    );
    const extra = h("input", { class: "input mono", autocomplete: "off", spellcheck: "false" });
    const extraField = labeled(t("targetUmo"), extra, t("targetUmoHint"));
    extraField.hidden = true;
    const mode = modeSelect("copy");
    const policy = policySelect(ui.uid("pol"), "newer");
    const includeId = ui.uid("inc");
    const include = h("input", { type: "checkbox", id: includeId, checked: true });
    const result = h("div", { class: "stack" });
    // 目标默认选和来源不同的一处：两个下拉框都从第一项开始，直接点预览只会得到"同一处"的报错
    function pickOtherTarget() {
      if (dst.value !== src.value) {
        return;
      }
      const other = Array.prototype.find.call(dst.querySelectorAll("option"), function (option) {
        return option.value !== src.value;
      });
      if (other) {
        dst.value = other.value;
      }
    }
    pickOtherTarget();
    src.addEventListener("change", pickOtherTarget);
    dst.addEventListener("change", function () {
      const special = dst.value.charAt(0) === "@";
      extraField.hidden = !special;
      extraField.querySelector(".field-label").textContent = dst.value === "@platform" ? t("targetPlatform") : t("targetUmo");
      extraField.querySelector(".field-hint").textContent = dst.value === "@platform" ? t("targetPlatformHint") : t("targetUmoHint");
    });
    function payload(dryRun) {
      if (dst.value === src.value) {
        throw new Error(t("errSameScope"));
      }
      const from = store.parseSid(src.value);
      let to;
      if (dst.value === "@umo") {
        to = { scope: "whisper", umo: extra.value.trim() };
      } else if (dst.value === "@platform") {
        to = { scope: "notebook", platform: extra.value.trim() };
      } else {
        const target = store.parseSid(dst.value);
        to = { scope: target.scope, key: target.key };
      }
      return {
        from: { scope: from.scope, key: from.key },
        to: to,
        mode: mode.value,
        policy: policy.value,
        includeForgotten: include.checked,
        dryRun: dryRun,
      };
    }

    async function previewRun() {
      const res = await ui.apiPost("migrate/scope", payload(true));
      ui.replace(result, h("h3", { class: "card-title" }, t("migratePreview")), reportList([res]));
      return res;
    }

    async function run() {
      const res = await ui.apiPost("migrate/scope", payload(true));
      const counts = res.counts || {};
      const from = res.from || {};
      const to = res.to || {};
      // 悄悄话搬到别处会改变可见范围：搬到小本本能被这个机器人的所有对话
      // 读到，搬到另一段对话则是换一群人能读（PLAN 3.12.2 的可见范围提示）
      const visibilityPoint =
        from.scope === "whisper" && to.scope === "notebook"
          ? t("migratePointNotebookVisible", { platform: to.platform || to.key })
          : from.scope === "whisper" && to.scope === "whisper" && to.key !== from.key
            ? t("migratePointWhisperVisible")
            : null;
      const ok = await ui.confirm({
        title: t("migrateTitle"),
        message: store.describeLabel(res.from) + " → " + store.describeLabel(res.to),
        points: [
          t("migrateWillWrite", { n: (counts.added || 0) + (counts.replaced || 0) }),
          counts.replaced ? t("migratePointReplace", { n: counts.replaced }) : null,
          mode.value === "move" ? t("migratePointMove") : t("migratePointCopy"),
          visibilityPoint,
        ],
        confirmLabel: t("runMigrate"),
        danger: mode.value === "move" || Boolean(counts.replaced),
      });
      if (!ok) {
        ui.replace(result, h("h3", { class: "card-title" }, t("migratePreview")), reportList([res]));
        return;
      }
      const done = await ui.apiPost("migrate/scope", payload(false));
      ui.toast(ui.resultMessage(done, t("migrated")));
      await afterBulkChange();
      ui.replace(result, h("h3", { class: "card-title" }, t("migrateResult")), reportList([done]));
    }

    return [
      h("p", { class: "hint" }, t("migrateScopeHint")),
      h("div", { class: "form-grid two" }, labeled(t("migrateFrom"), src), labeled(t("migrateTo"), dst)),
      extraField,
      h("div", { class: "form-grid two" }, labeled(t("modeLabel"), mode, t("modeHint")), labeled(t("policy"), policy, t("policyHint"))),
      h("div", { class: "check-row" }, include, h("label", { for: includeId }, t("includeForgotten"))),
      h(
        "div",
        { class: "actions" },
        h("button", { type: "button", class: "btn btn-sm", onclick: M.action(previewRun) }, icon("eye"), t("migratePreviewButton")),
        h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: M.action(run) }, icon("transfer"), t("runMigrate"))
      ),
      result,
    ];
  }

  function platformTab() {
    const listId = ui.uid("plat");
    const datalist = h("datalist", { id: listId });
    const from = h("input", { class: "input mono", list: listId, autocomplete: "off", spellcheck: "false", placeholder: "aiocqhttp" });
    const to = h("input", { class: "input mono", list: listId, autocomplete: "off", spellcheck: "false", placeholder: "aiocqhttp-new" });
    const mode = modeSelect("move");
    const policy = policySelect(ui.uid("pol"), "newer");
    const nbId = ui.uid("nb");
    const includeNotebook = h("input", { type: "checkbox", id: nbId, checked: true });
    const result = h("div", { class: "stack" });
    const known = h("p", { class: "hint" });

    ui.apiGet("migrate/info").then(function (info) {
      const names = [];
      (info.platforms || []).forEach(function (p) {
        if (names.indexOf(p.platform) < 0) {
          names.push(p.platform);
        }
      });
      (info.adapters || []).forEach(function (p) {
        if (names.indexOf(p) < 0) {
          names.push(p);
        }
      });
      names.forEach(function (name) { datalist.append(h("option", { value: name })); });
      const parts = (info.platforms || []).map(function (p) {
        return p.platform + t("paren", { text: t("platformSessions", { n: p.sessions }) + (p.notebook ? " · " + t("notebookLabel") : "") });
      });
      known.textContent = parts.length ? t("platformKnown", { list: parts.join(t("listSep")) }) : "";
      if ((info.adapters || []).length) {
        known.textContent += (known.textContent ? " " : "") + t("platformAdapters", { list: info.adapters.join(t("listSep")) });
      }
    }).catch(function () {
      known.textContent = "";
    });

    function body(dryRun) {
      return { from: from.value.trim(), to: to.value.trim(), mode: mode.value, policy: policy.value, includeNotebook: includeNotebook.checked, dryRun: dryRun };
    }

    function report(res, title) {
      return [
        h("h3", { class: "card-title" }, title),
        h("p", { class: "hint" }, t("platformTotals", { items: (res.items || []).length, counts: countsLine(res.totals) })),
        reportList(res.items),
        (res.skipped || []).length ? ui.notice("warn", t("migrateSkipped", { n: res.skipped.length }), h("ul", null, res.skipped.map(function (s) { return h("li", null, store.describeLabel(s), ui.zhOnly(s.reason) ? " · " + s.reason : ""); }))) : null,
      ];
    }

    async function previewRun() {
      const res = await ui.apiPost("migrate/platform", body(true));
      ui.replace(result, report(res, t("migratePreview")));
    }

    async function run() {
      const res = await ui.apiPost("migrate/platform", body(true));
      const totals = res.totals || {};
      const ok = await ui.confirm({
        title: t("platformTitle", { from: from.value.trim(), to: to.value.trim() }),
        points: [
          t("platformPointItems", { n: (res.items || []).length }),
          totals.replaced ? t("migratePointReplace", { n: totals.replaced }) : null,
          mode.value === "move" ? t("platformPointMove") : t("migratePointCopy"),
          (res.skipped || []).length ? t("migrateSkipped", { n: res.skipped.length }) : null,
        ],
        confirmLabel: t("runMigrate"),
        danger: mode.value === "move" || Boolean(totals.replaced),
      });
      if (!ok) {
        ui.replace(result, report(res, t("migratePreview")));
        return;
      }
      const done = await ui.apiPost("migrate/platform", body(false));
      ui.toast(ui.resultMessage(done, t("migrated")));
      await afterBulkChange();
      ui.replace(result, report(done, t("migrateResult")));
    }

    return [
      h("p", { class: "hint" }, t("platformHint")),
      datalist,
      h("div", { class: "form-grid two" }, labeled(t("platformFrom"), from), labeled(t("platformTo"), to)),
      known,
      h("div", { class: "form-grid two" }, labeled(t("modeLabel"), mode, t("modeHint")), labeled(t("policy"), policy, t("policyHint"))),
      h("div", { class: "check-row" }, includeNotebook, h("label", { for: nbId }, t("includeNotebook"))),
      h(
        "div",
        { class: "actions" },
        h("button", { type: "button", class: "btn btn-sm", onclick: M.action(previewRun) }, icon("eye"), t("migratePreviewButton")),
        h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: M.action(run) }, icon("transfer"), t("runMigrate"))
      ),
      result,
    ];
  }

  const TRANSFER_TABS = [
    ["export", "tabExport", exportTab],
    ["import", "tabImport", importTab],
    ["scope", "tabScope", migrateTab],
    ["platform", "tabPlatform", platformTab],
  ];

  function TransferSection() {
    const root = h("div", { class: "detail" });
    const head = M.detailHead({ view: "manage", title: t("migrate"), desc: t("migrateHint") });
    root.setAttribute("aria-labelledby", head.id);
    const panel = h("div", { class: "tab-panel", role: "tabpanel" });
    const tabId = ui.uid("xfer");
    panel.id = tabId + "-panel";

    function paint() {
      const current = TRANSFER_TABS.find(function (item) { return item[0] === S.manage.transferTab; }) || TRANSFER_TABS[0];
      panel.setAttribute("aria-labelledby", tabId + "-" + current[0]);
      ui.replace(panel, card(null, current[2]()));
      const tabs = ui.tabs(
        TRANSFER_TABS.map(function (item) { return { value: item[0], label: t(item[1]), id: tabId + "-" + item[0], controls: panel.id }; }),
        current[0],
        function (value) {
          S.manage.transferTab = value;
          paint();
          const next = root.querySelector('[role="tab"][aria-selected="true"]');
          if (next) {
            next.focus();
          }
        },
        t("migrate")
      );
      ui.replace(root, head.el, tabs, panel);
    }

    paint();
    return { el: root };
  }

  // ------------------------------------------------------------ 管理：列表与详情

  const SECTIONS = [
    ["overview", "manageOverview", "manageOverviewHint", "manage"],
    ["tidy", "manageTidy", "manageTidyHint", "sparkles"],
    ["maint", "manageMaint", "manageMaintHint", "trash"],
    ["transfer", "migrate", "migrateHint", "transfer"],
  ];

  function manageListPane() {
    const list = h(
      "div",
      { class: "rows", role: "list" },
      SECTIONS.map(function (item) {
        return h(
          "div",
          { role: "listitem" },
          h(
            "button",
            {
              type: "button",
              class: "row row-section",
              "data-nav-item": "",
              "data-fk": "section:" + item[0],
              "aria-current": S.manage.section === item[0] ? "true" : null,
              // 再点一次当前分区，回到这个分区的首页（从某一处的整理 / 回收站里退出来）
              onclick: function () { MP.app.openManage(item[0], { tidySid: "", maintSid: "" }); },
            },
            h("span", { class: "avatar" }, icon(item[3])),
            h("span", { class: "row-main" }, h("span", { class: "row-name" }, t(item[1])), h("span", { class: "row-meta" }, t(item[2])))
          )
        );
      })
    );
    ui.listNav(list);
    return [M.listHead({ title: t("tabManage"), sub: t("manageSub") }), h("div", { class: "list-body" }, list)];
  }

  function manageDetailFor() {
    const m = S.manage;
    const back = function () { MP.app.openManage(m.section, m.section === "tidy" ? { tidySid: "" } : { maintSid: "" }); };
    // 总览没读到（加载中、失败）时这些组件显示的是占位；总览到了以后 id 变了，组件重建
    const ready = M.overviewState();
    if (m.section === "tidy") {
      if (m.tidySid === "all") {
        return { id: "manage|tidy|all|" + S.paneVersion + "|" + ready, create: function () { return TidyAllPanel({ onBack: back }); } };
      }
      const r = m.tidySid ? store.parseSid(m.tidySid) : null;
      if (r) {
        return { id: "manage|tidy|" + m.tidySid + "|" + S.paneVersion, create: function () { return TidyPanel("manage", r, { onBack: back, backLabel: t("backToTidy") }); } };
      }
      return { id: "manage|tidy|home|" + S.paneVersion + "|" + ready, create: TidyHome };
    }
    if (m.section === "maint") {
      const r = m.maintSid ? store.parseSid(m.maintSid) : null;
      if (r) {
        return { id: "manage|maint|" + m.maintSid + "|" + S.paneVersion, create: function () { return M.MaintPanel("manage", r, "trash", { onBack: back, backLabel: t("backToMaint") }); } };
      }
      return { id: "manage|maint|home|" + S.paneVersion + "|" + ready, create: MaintHome };
    }
    if (m.section === "transfer") {
      return { id: "manage|transfer|" + S.paneVersion + "|" + ready, create: TransferSection };
    }
    return { id: "manage|overview|" + S.paneVersion + "|" + ready, create: OverviewSection };
  }

  // ------------------------------------------------------------ 上下文：列表与详情

  function contextListPane() {
    return M.conversationPane({
      title: t("tabContext"),
      sub: t("contextListSub"),
      selectedKey: S.contextKey,
      showCount: false,
      memoryFilter: false,
      onSelect: function (key) { MP.app.showContext(key); },
    });
  }

  function contextDetailFor() {
    if (!S.contextKey) {
      return {
        id: "context|none",
        create: function () {
          return {
            el: h(
              "div",
              { class: "detail detail-empty" },
              ui.emptyState({ icon: "context", title: t("pickConversationTitle"), hint: t("contextPickHint") })
            ),
          };
        },
      };
    }
    const key = S.contextKey;
    return { id: "context|" + key + "|" + S.paneVersion, create: function () { return ContextPanel(key); } };
  }

  MP.manage = {
    TidyPanel: TidyPanel,
    TidyAllPanel: TidyAllPanel,
    ContextPanel: ContextPanel,
    consolidateTargets: consolidateTargets,
    manageListPane: manageListPane,
    manageDetailFor: manageDetailFor,
    contextListPane: contextListPane,
    contextDetailFor: contextDetailFor,
  };
})(window.MemoryPod = window.MemoryPod || {});
