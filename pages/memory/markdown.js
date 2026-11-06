/* 鲸群记忆管理页：只支持记忆文件用得到的 Markdown 子集，自己解析、自己建 DOM。
 *
 * 支持：标题、段落（单个换行保留为换行）、无序 / 有序 / 任务列表（可嵌套）、
 * 引用、围栏代码块、分隔线、简单表格；行内的粗体、斜体、删除线、行内代码、
 * 链接、图片占位与 [[记忆链接]]。
 *
 * 安全：parse() 只产出普通对象组成的语法树，render() 只用固定白名单里的标签，
 * 所有文字经文本节点写入。原文里的 HTML 标签按字面显示；链接不生成可点的
 * href（页面跑在沙箱 iframe 里，点外链只会把管理页本身导航走），图片不加载，
 * 只显示替代文字。整个文件不使用 innerHTML。
 */
(function (MP) {
  "use strict";

  const MAX_DEPTH = 6; // 列表、引用的嵌套层数
  const MAX_INLINE = 20000; // 更长的段落不做行内解析，按纯文本显示
  const MAX_TABLE_COLS = 30;

  const RE = {
    fence: /^ {0,3}(`{3,}|~{3,})[ \t]*([^`\s]*)[^`]*$/,
    heading: /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*#*[ \t]*$/,
    hr: /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/,
    quote: /^ {0,3}> ?/,
    bullet: /^( *)([-*+])[ \t]+(.*)$/,
    ordered: /^( *)(\d{1,9})([.)])[ \t]+(.*)$/,
    tableSep: /^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/,
    task: /^\[([ xX])\][ \t]+/,
    lang: /^[A-Za-z0-9_+.#-]{1,20}$/,
  };

  function normalize(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(/\r\n?/g, "\n")
      .replace(/\t/g, "    ");
  }

  /** 去掉开头的 frontmatter（--- … ---），返回正文 */
  function stripFrontmatter(text) {
    const value = normalize(text);
    const match = /^---[ \t]*\n[\s\S]*?\n---[ \t]*(?:\n|$)/.exec(value);
    return match ? value.slice(match[0].length).replace(/^\n+/, "") : value;
  }

  function leading(line) {
    const match = /^ */.exec(line);
    return match ? match[0].length : 0;
  }

  function itemMatch(line) {
    let match = RE.bullet.exec(line);
    if (match) {
      return { indent: match[1].length, ordered: false, num: 0, content: match[3] };
    }
    match = RE.ordered.exec(line);
    if (match) {
      return { indent: match[1].length, ordered: true, num: parseInt(match[2], 10), content: match[4] };
    }
    return null;
  }

  function startsBlock(line, next) {
    return (
      RE.fence.test(line) ||
      RE.heading.test(line) ||
      RE.hr.test(line) ||
      RE.quote.test(line) ||
      Boolean(itemMatch(line)) ||
      isTableStart(line, next)
    );
  }

  function isTableStart(line, next) {
    return (
      typeof next === "string" &&
      line.indexOf("|") >= 0 &&
      next.indexOf("-") >= 0 &&
      RE.tableSep.test(next) &&
      (next.indexOf("|") >= 0 || line.trim().charAt(0) === "|")
    );
  }

  function splitRow(line) {
    let text = line.trim();
    if (text.charAt(0) === "|") {
      text = text.slice(1);
    }
    if (text.slice(-1) === "|" && text.slice(-2) !== "\\|") {
      text = text.slice(0, -1);
    }
    const cells = [];
    let current = "";
    for (let i = 0; i < text.length; i += 1) {
      const ch = text.charAt(i);
      if (ch === "\\" && text.charAt(i + 1) === "|") {
        current += "|";
        i += 1;
      } else if (ch === "|") {
        cells.push(current.trim());
        current = "";
      } else {
        current += ch;
      }
    }
    cells.push(current.trim());
    return cells;
  }

  function parseTable(lines, start) {
    const head = splitRow(lines[start]);
    const sep = splitRow(lines[start + 1]);
    if (head.length !== sep.length || head.length > MAX_TABLE_COLS) {
      return null;
    }
    if (!sep.every(function (cell) { return /^:?-+:?$/.test(cell); })) {
      return null;
    }
    const align = sep.map(function (cell) {
      const left = cell.charAt(0) === ":";
      const right = cell.slice(-1) === ":";
      return left && right ? "center" : right ? "right" : left ? "left" : "";
    });
    const rows = [];
    let i = start + 2;
    while (i < lines.length && lines[i].trim() && lines[i].indexOf("|") >= 0) {
      const cells = splitRow(lines[i]);
      rows.push(head.map(function (_, k) { return cells[k] || ""; }));
      i += 1;
    }
    return { block: { t: "table", align: align, head: head, rows: rows }, next: i };
  }

  function parseList(lines, start, depth) {
    const first = itemMatch(lines[start]);
    const base = first.indent;
    const ordered = first.ordered;
    const items = [];
    let i = start;
    while (i < lines.length) {
      const m = itemMatch(lines[i]);
      if (!m || m.ordered !== ordered || m.indent < base || m.indent >= base + 2) {
        break;
      }
      const content = [m.content];
      i += 1;
      while (i < lines.length) {
        const line = lines[i];
        if (!line.trim()) {
          // 空行后面还是本项的缩进内容，就算同一项（松散列表）
          let j = i + 1;
          while (j < lines.length && !lines[j].trim()) {
            j += 1;
          }
          if (j < lines.length && leading(lines[j]) >= base + 2 && !isSibling(lines[j], base)) {
            for (let k = i; k < j; k += 1) {
              content.push("");
            }
            i = j;
            continue;
          }
          break;
        }
        const sub = itemMatch(line);
        if (sub && sub.indent < base + 2) {
          break;
        }
        const indent = leading(line);
        if (indent > base) {
          content.push(line.slice(Math.min(indent, base + 2)));
          i += 1;
          continue;
        }
        if (!sub && !startsBlock(line, lines[i + 1])) {
          // 懒惰续行：紧跟着的不缩进文字仍算这一项的段落
          content.push(line.trim());
          i += 1;
          continue;
        }
        break;
      }
      let task = null;
      const taskMatch = RE.task.exec(content[0]);
      if (taskMatch) {
        task = taskMatch[1] !== " ";
        content[0] = content[0].slice(taskMatch[0].length);
      }
      items.push({
        task: task,
        c: depth < MAX_DEPTH ? parseBlocks(content, depth + 1) : [{ t: "p", text: content.join("\n") }],
      });
      // 项与项之间的空行
      let j = i;
      while (j < lines.length && !lines[j].trim()) {
        j += 1;
      }
      if (j === i) {
        continue;
      }
      const nextItem = j < lines.length ? itemMatch(lines[j]) : null;
      if (nextItem && nextItem.ordered === ordered && nextItem.indent >= base && nextItem.indent < base + 2) {
        i = j;
        continue;
      }
      break;
    }
    return {
      block: { t: ordered ? "ol" : "ul", start: ordered ? first.num : 0, items: items },
      next: i,
    };
  }

  function isSibling(line, base) {
    const m = itemMatch(line);
    return Boolean(m && m.indent < base + 2);
  }

  function parseBlocks(lines, depth) {
    const blocks = [];
    let para = [];
    function flush() {
      if (para.length) {
        blocks.push({ t: "p", text: para.join("\n") });
        para = [];
      }
    }
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) {
        flush();
        i += 1;
        continue;
      }
      let match = RE.fence.exec(line);
      if (match) {
        flush();
        const marker = match[1];
        const close = new RegExp("^ {0,3}" + (marker.charAt(0) === "`" ? "`" : "~") + "{" + marker.length + ",}[ \\t]*$");
        const body = [];
        i += 1;
        while (i < lines.length && !close.test(lines[i])) {
          body.push(lines[i]);
          i += 1;
        }
        i += 1; // 跳过结束围栏（没有结束围栏时到文末为止）
        blocks.push({ t: "code", lang: RE.lang.test(match[2] || "") ? match[2] : "", text: body.join("\n") });
        continue;
      }
      match = RE.heading.exec(line);
      if (match) {
        flush();
        blocks.push({ t: "h", level: match[1].length, text: match[2] || "" });
        i += 1;
        continue;
      }
      if (RE.hr.test(line)) {
        flush();
        blocks.push({ t: "hr" });
        i += 1;
        continue;
      }
      if (RE.quote.test(line)) {
        flush();
        const inner = [];
        while (i < lines.length && RE.quote.test(lines[i])) {
          inner.push(lines[i].replace(RE.quote, ""));
          i += 1;
        }
        blocks.push(
          depth < MAX_DEPTH ? { t: "quote", c: parseBlocks(inner, depth + 1) } : { t: "p", text: inner.join("\n") }
        );
        continue;
      }
      if (itemMatch(line)) {
        flush();
        const list = parseList(lines, i, depth);
        blocks.push(list.block);
        i = list.next;
        continue;
      }
      if (isTableStart(line, lines[i + 1])) {
        const table = parseTable(lines, i);
        if (table) {
          flush();
          blocks.push(table.block);
          i = table.next;
          continue;
        }
      }
      para.push(line.trim());
      i += 1;
    }
    flush();
    return blocks;
  }

  /** 文本 → 块级语法树（普通对象，不含任何 DOM） */
  function parse(text) {
    return parseBlocks(normalize(text).split("\n"), 0);
  }

  // ------------------------------------------------------------ 行内

  const WORD = /[A-Za-z0-9]/;

  function intraword(text, match) {
    const before = text.charAt(match.index - 1);
    const after = text.charAt(match.index + match[0].length);
    return (before && WORD.test(before)) || (after && WORD.test(after));
  }

  /** 行内代码：一串反引号开头，找长度相同的另一串结束（不用后行断言，兼容旧浏览器） */
  function findCode(source, pos) {
    let start = source.indexOf("`", pos);
    while (start >= 0) {
      let open = start;
      while (source.charAt(open) === "`") {
        open += 1;
      }
      const run = open - start;
      let close = source.indexOf("`", open);
      while (close >= 0 && close - open <= 2000) {
        let end = close;
        while (source.charAt(end) === "`") {
          end += 1;
        }
        if (end - close === run && close > open) {
          const match = [source.slice(start, end), source.slice(open, close)];
          match.index = start;
          return match;
        }
        close = source.indexOf("`", end);
      }
      // 没有配对的反引号串按普通文字处理，从串尾继续找
      start = source.indexOf("`", open);
    }
    return null;
  }

  // 同一位置能匹配多种时，排在前面的优先
  const INLINE = [
    { re: /\\([!-/:-@[-`{-~])/g, build: function (m) { return { t: "text", v: m[1] }; } },
    {
      find: findCode,
      build: function (m) {
        let value = m[1].replace(/\n/g, " ");
        if (/^ [\s\S]*[^ ][\s\S]* $/.test(value)) {
          value = value.slice(1, -1);
        }
        return { t: "code", v: value };
      },
    },
    { re: /\[\[([^[\]\n]{1,120})\]\]/g, build: function (m) { return m[1].trim() ? { t: "wiki", target: m[1].trim() } : null; } },
    {
      re: /!\[([^\]\n]{0,300})\]\(([^()\s]{0,1000})(?:[ \t]+"[^"\n]{0,300}")?\)/g,
      build: function (m) { return { t: "img", alt: m[1], src: m[2] }; },
    },
    {
      re: /\[((?:[^[\]\n]|\[[^[\]\n]*\]){1,300})\]\(([^()\s]{0,1000})(?:[ \t]+"([^"\n]{0,300})")?\)/g,
      build: function (m, depth) {
        return { t: "link", href: m[2], title: m[3] || "", c: parseInline(m[1], depth + 1) };
      },
    },
    {
      re: /<(https?:\/\/[^\s<>]{1,1000})>/g,
      build: function (m) { return { t: "link", href: m[1], title: "", c: [{ t: "text", v: m[1] }] }; },
    },
    { re: /\*\*(?=\S)([\s\S]{0,500}?\S)\*\*/g, build: function (m, depth) { return { t: "strong", c: parseInline(m[1], depth + 1) }; } },
    {
      re: /__(?=\S)([\s\S]{0,500}?\S)__/g,
      intraword: true,
      build: function (m, depth) { return { t: "strong", c: parseInline(m[1], depth + 1) }; },
    },
    { re: /~~(?=\S)([\s\S]{0,500}?\S)~~/g, build: function (m, depth) { return { t: "del", c: parseInline(m[1], depth + 1) }; } },
    { re: /\*(?=[^\s*])([\s\S]{0,500}?[^\s*])\*/g, build: function (m, depth) { return { t: "em", c: parseInline(m[1], depth + 1) }; } },
    {
      re: /_(?=[^\s_])([\s\S]{0,500}?[^\s_])_/g,
      intraword: true,
      build: function (m, depth) { return { t: "em", c: parseInline(m[1], depth + 1) }; },
    },
    { re: /\n/g, build: function () { return { t: "br" }; } },
  ];

  function pushText(out, value) {
    if (!value) {
      return;
    }
    const last = out[out.length - 1];
    if (last && last.t === "text") {
      last.v += value;
    } else {
      out.push({ t: "text", v: value });
    }
  }

  /** 行内文本 → 行内节点数组 */
  function parseInline(text, depth) {
    const source = String(text || "");
    depth = depth || 0;
    if (!source || source.length > MAX_INLINE || depth > 8) {
      return source ? [{ t: "text", v: source }] : [];
    }
    const out = [];
    // 每种模式记下从当前位置往后的第一个匹配；位置越过它才重新找
    const cache = INLINE.map(function () { return undefined; });
    let pos = 0;
    while (pos < source.length) {
      let best = null;
      let bestIndex = -1;
      for (let k = 0; k < INLINE.length; k += 1) {
        let found = cache[k];
        if (found === undefined || (found !== null && found.index < pos)) {
          const pattern = INLINE[k];
          if (pattern.find) {
            found = pattern.find(source, pos);
          } else {
            pattern.re.lastIndex = pos;
            found = pattern.re.exec(source);
          }
          cache[k] = found;
        }
        if (found && (best === null || found.index < best.index)) {
          best = found;
          bestIndex = k;
        }
      }
      if (!best) {
        break;
      }
      pushText(out, source.slice(pos, best.index));
      const pattern = INLINE[bestIndex];
      const node = pattern.intraword && intraword(source, best) ? null : pattern.build(best, depth);
      if (!node) {
        pushText(out, source.charAt(best.index));
        pos = best.index + 1;
        continue;
      }
      if (node.t === "text") {
        pushText(out, node.v);
      } else {
        out.push(node);
      }
      pos = best.index + best[0].length;
    }
    pushText(out, source.slice(pos));
    return out;
  }

  // ------------------------------------------------------------ 渲染

  function inlineText(nodes) {
    return nodes
      .map(function (node) {
        if (node.t === "text" || node.t === "code") {
          return node.v;
        }
        if (node.c) {
          return inlineText(node.c);
        }
        if (node.t === "wiki") {
          return node.target;
        }
        return "";
      })
      .join("");
  }

  function renderInline(nodes, opts) {
    const h = opts.h;
    const out = [];
    nodes.forEach(function (node) {
      switch (node.t) {
        case "text":
          out.push(node.v);
          break;
        case "br":
          out.push(h("br"));
          break;
        case "code":
          out.push(h("code", { class: "md-code" }, node.v));
          break;
        case "strong":
          out.push(h("strong", null, renderInline(node.c, opts)));
          break;
        case "em":
          out.push(h("em", null, renderInline(node.c, opts)));
          break;
        case "del":
          out.push(h("del", null, renderInline(node.c, opts)));
          break;
        case "link": {
          const label = inlineText(node.c);
          out.push(h("span", { class: "md-link", title: node.title || node.href || null }, renderInline(node.c, opts)));
          if (node.href && node.href !== label) {
            out.push(h("span", { class: "md-url" }, " (" + node.href + ")"));
          }
          break;
        }
        case "img":
          out.push(h("span", { class: "md-img" }, opts.t("mdImage", { alt: node.alt || node.src || "" })));
          break;
        case "wiki":
          out.push(opts.wiki ? opts.wiki(node.target) : h("span", { class: "md-wiki" }, "[[" + node.target + "]]"));
          break;
        default:
          break;
      }
    });
    return out;
  }

  // 正文里的 # 一级标题在页面里排在文件标题之下：整体降三级
  function headingTag(level) {
    return "h" + Math.min(6, level + 2);
  }

  function renderBlocks(blocks, opts) {
    const h = opts.h;
    return blocks.map(function (block) {
      switch (block.t) {
        case "h":
          return h(headingTag(block.level), { class: "md-h md-h" + block.level }, renderInline(parseInline(block.text), opts));
        case "p":
          return h("p", null, renderInline(parseInline(block.text), opts));
        case "ul":
        case "ol": {
          const hasTask = block.items.some(function (item) { return item.task !== null; });
          return h(
            block.t,
            {
              class: hasTask ? "md-tasks" : null,
              start: block.t === "ol" && block.start > 1 ? String(block.start) : null,
            },
            block.items.map(function (item) {
              const only = item.c.length === 1 && item.c[0].t === "p" ? item.c[0] : null;
              return h(
                "li",
                { class: item.task === null ? null : "md-task-item" + (item.task ? " is-done" : "") },
                item.task === null
                  ? null
                  : [
                      h("span", { class: "md-task", "aria-hidden": "true" }, item.task ? "☑" : "☐"),
                      h("span", { class: "sr-only" }, opts.t(item.task ? "mdTaskDone" : "mdTaskTodo")),
                    ],
                only ? renderInline(parseInline(only.text), opts) : renderBlocks(item.c, opts)
              );
            })
          );
        }
        case "quote":
          return h("blockquote", null, renderBlocks(block.c, opts));
        case "code":
          return h("pre", { class: "md-pre", "data-lang": block.lang || null }, h("code", null, block.text));
        case "hr":
          return h("hr");
        case "table":
          return h(
            "div",
            { class: "md-table-wrap" },
            h(
              "table",
              null,
              h(
                "thead",
                null,
                h(
                  "tr",
                  null,
                  block.head.map(function (cell, k) {
                    return h("th", { style: { textAlign: block.align[k] || "" } }, renderInline(parseInline(cell), opts));
                  })
                )
              ),
              h(
                "tbody",
                null,
                block.rows.map(function (row) {
                  return h(
                    "tr",
                    null,
                    row.map(function (cell, k) {
                      return h("td", { style: { textAlign: block.align[k] || "" } }, renderInline(parseInline(cell), opts));
                    })
                  );
                })
              )
            )
          );
        default:
          return null;
      }
    });
  }

  /**
   * 把 Markdown 文本渲染成 DOM。opts.wiki(target) 返回 [[链接]] 的节点；
   * opts.h / opts.t 默认用页面的 h() 与文案函数（测试里可以替换）。
   */
  function render(text, opts) {
    const options = Object.assign(
      {
        h: MP.ui ? MP.ui.h : null,
        t: MP.t || function (key) { return key; },
      },
      opts || {}
    );
    const blocks = parse(text);
    return options.h("div", { class: "md" }, renderBlocks(blocks, options));
  }

  MP.markdown = {
    parse: parse,
    parseInline: parseInline,
    render: render,
    stripFrontmatter: stripFrontmatter,
    inlineText: inlineText,
  };
})(window.MemoryPod = window.MemoryPod || {});
