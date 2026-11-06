/* 鲸群记忆管理页：按行比较两段文字，渲染成带行号的改动对比。
 *
 * 先去掉首尾相同的行，中间部分用最长公共子序列求改动；中间部分太大时
 * （两边行数乘积超过上限）不再细算，整段显示为"删去旧的、加上新的"。
 */
(function (MP) {
  "use strict";

  const MAX_CELLS = 2500000;

  function splitLines(text) {
    const value = String(text === null || text === undefined ? "" : text).replace(/\r\n?/g, "\n");
    if (!value) {
      return [];
    }
    const lines = value.split("\n");
    // 末尾的换行不算一行
    if (lines.length && lines[lines.length - 1] === "") {
      lines.pop();
    }
    return lines;
  }

  /** 返回 [{op: " " | "-" | "+", text, a, b}]，a / b 是旧、新文件里的行号（从 1 起） */
  function diffLines(before, after) {
    const a = splitLines(before);
    const b = splitLines(after);
    let head = 0;
    while (head < a.length && head < b.length && a[head] === b[head]) {
      head += 1;
    }
    let tail = 0;
    while (
      tail < a.length - head &&
      tail < b.length - head &&
      a[a.length - 1 - tail] === b[b.length - 1 - tail]
    ) {
      tail += 1;
    }
    const rows = [];
    for (let i = 0; i < head; i += 1) {
      rows.push({ op: " ", text: a[i], a: i + 1, b: i + 1 });
    }
    const midA = a.slice(head, a.length - tail);
    const midB = b.slice(head, b.length - tail);
    middle(midA, midB, head, rows);
    for (let i = tail; i > 0; i -= 1) {
      const ia = a.length - i;
      const ib = b.length - i;
      rows.push({ op: " ", text: a[ia], a: ia + 1, b: ib + 1 });
    }
    return rows;
  }

  function middle(a, b, offset, rows) {
    const n = a.length;
    const m = b.length;
    if (!n || !m || n * m > MAX_CELLS) {
      a.forEach(function (text, i) {
        rows.push({ op: "-", text: text, a: offset + i + 1, b: 0 });
      });
      b.forEach(function (text, j) {
        rows.push({ op: "+", text: text, a: 0, b: offset + j + 1 });
      });
      return;
    }
    // lcs[i][j]：a[i:] 与 b[j:] 的最长公共子序列长度
    const width = m + 1;
    const lcs = new Uint32Array((n + 1) * width);
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        lcs[i * width + j] =
          a[i] === b[j]
            ? lcs[(i + 1) * width + j + 1] + 1
            : Math.max(lcs[(i + 1) * width + j], lcs[i * width + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) {
        rows.push({ op: " ", text: a[i], a: offset + i + 1, b: offset + j + 1 });
        i += 1;
        j += 1;
      } else if (lcs[(i + 1) * width + j] >= lcs[i * width + j + 1]) {
        rows.push({ op: "-", text: a[i], a: offset + i + 1, b: 0 });
        i += 1;
      } else {
        rows.push({ op: "+", text: b[j], a: 0, b: offset + j + 1 });
        j += 1;
      }
    }
    for (; i < n; i += 1) {
      rows.push({ op: "-", text: a[i], a: offset + i + 1, b: 0 });
    }
    for (; j < m; j += 1) {
      rows.push({ op: "+", text: b[j], a: 0, b: offset + j + 1 });
    }
  }

  function stats(rows) {
    let added = 0;
    let removed = 0;
    rows.forEach(function (row) {
      if (row.op === "+") {
        added += 1;
      } else if (row.op === "-") {
        removed += 1;
      }
    });
    return { added: added, removed: removed, changed: added + removed > 0 };
  }

  /** 渲染对比；context 是改动前后各保留几行原样的上下文 */
  function render(rows, opts) {
    const ui = MP.ui;
    const h = ui.h;
    const t = MP.t;
    const context = opts && opts.context !== undefined ? opts.context : 3;
    const s = stats(rows);
    if (!s.changed) {
      return h("div", { class: "diff-empty" }, t("diffNoChange"));
    }
    // 标出要显示的行：改动行及其前后 context 行
    const keep = new Array(rows.length).fill(false);
    rows.forEach(function (row, index) {
      if (row.op !== " ") {
        for (let k = Math.max(0, index - context); k <= Math.min(rows.length - 1, index + context); k += 1) {
          keep[k] = true;
        }
      }
    });
    const body = h("tbody");
    let skipped = 0;
    function flushSkip() {
      if (skipped) {
        body.append(
          h(
            "tr",
            { class: "diff-skip" },
            h("td", { colspan: "3" }, t("diffSkipped", { n: skipped }))
          )
        );
        skipped = 0;
      }
    }
    rows.forEach(function (row, index) {
      if (!keep[index]) {
        skipped += 1;
        return;
      }
      flushSkip();
      const cls = row.op === "+" ? "is-add" : row.op === "-" ? "is-del" : "";
      body.append(
        h(
          "tr",
          { class: cls || null },
          h("td", { class: "diff-num", "aria-hidden": "true" }, row.a ? String(row.a) : ""),
          h("td", { class: "diff-num", "aria-hidden": "true" }, row.b ? String(row.b) : ""),
          h(
            "td",
            { class: "diff-text" },
            h("span", { class: "diff-mark", "aria-hidden": "true" }, row.op === " " ? " " : row.op === "+" ? "+" : "−"),
            row.op === "+" ? ui.srOnly(t("diffAddedLine")) : row.op === "-" ? ui.srOnly(t("diffRemovedLine")) : null,
            row.text || "\u00a0"
          )
        )
      );
    });
    flushSkip();
    return h(
      "div",
      { class: "diff" },
      h(
        "div",
        { class: "diff-summary" },
        h("span", { class: "diff-added" }, "+" + s.added),
        h("span", { class: "diff-removed" }, "−" + s.removed),
        h("span", null, t("diffLines"))
      ),
      h("div", { class: "diff-scroll" }, h("table", { class: "diff-table" }, body))
    );
  }

  MP.diff = { diffLines: diffLines, stats: stats, render: render, splitLines: splitLines };
})(window.MemoryPod = window.MemoryPod || {});
