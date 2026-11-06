"""管理页的 Markdown 渲染器：用户内容（记忆正文）只能变成文字和白名单里的标签。

渲染器是 pages/memory/markdown.js，没有第三方库。这里用 node 把它加载进一个空的沙箱，
换上一个只记录结构的假 h()，对各种带恶意标记的输入逐条检查生成的结构树：
标签和属性都在白名单里，所有原文里的 HTML、脚本、事件属性、javascript: 链接都以文字出现。
没有 node 时整组跳过。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MARKDOWN_JS = ROOT / "pages" / "memory" / "markdown.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")

# 在 node 里运行：读 stdin 的 JSON 用例，输出每个用例渲染出的结构树
RUNNER = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf-8");
const win = {};
vm.runInNewContext(code, { window: win });
const MP = win.MemoryPod;

function h(tag, attrs) {
  const node = { tag: tag, attrs: {}, children: [] };
  Object.keys(attrs || {}).forEach((k) => {
    const v = attrs[k];
    if (v !== null && v !== undefined && v !== false) node.attrs[k] = v;
  });
  function add(child) {
    if (child === null || child === undefined || child === false || child === true) return;
    if (Array.isArray(child)) return child.forEach(add);
    node.children.push(typeof child === "object" ? child : String(child));
  }
  for (let i = 2; i < arguments.length; i += 1) add(arguments[i]);
  return node;
}
const cases = JSON.parse(fs.readFileSync(0, "utf-8"));
const out = cases.map((c) => {
  const started = Date.now();
  const tree = MP.markdown.render(c.text, {
    h: h,
    t: (key, vars) => key + (vars ? JSON.stringify(vars) : ""),
    wiki: (target) => h("span", { class: "wiki" }, "[[" + target + "]]"),
  });
  return { ms: Date.now() - started, tree: tree };
});
process.stdout.write(JSON.stringify(out));
"""

ALLOWED_TAGS = {
    "div", "p", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "pre", "code",
    "hr", "table", "thead", "tbody", "tr", "th", "td", "strong", "em", "del", "br", "span",
}
ALLOWED_ATTRS = {"class", "title", "start", "data-lang", "style", "aria-hidden"}


def render_all(cases: list[str]) -> list[dict]:
    result = subprocess.run(
        ["node", "-e", RUNNER, str(MARKDOWN_JS)],
        input=json.dumps([{"text": text} for text in cases]),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def walk(node):
    yield node
    for child in node.get("children", []):
        if isinstance(child, dict):
            yield from walk(child)


def text_of(node) -> str:
    return "".join(
        child if isinstance(child, str) else text_of(child) for child in node.get("children", [])
    )


def assert_safe(tree: dict) -> None:
    for node in walk(tree):
        assert node["tag"] in ALLOWED_TAGS, node["tag"]
        assert set(node["attrs"]) <= ALLOWED_ATTRS, node["attrs"]
        for name, value in node["attrs"].items():
            if name == "style":
                # 只有表格列对齐一种样式
                assert set(value) <= {"textAlign"}, value
                assert value.get("textAlign", "") in ("", "left", "right", "center")
            elif name == "title":
                # 链接的悬停提示：只是文字，不会导航也不会执行
                assert isinstance(value, str)
            else:
                # 类名、起始序号、代码语言：只能是简单的词，不带任何用户内容里的标点
                assert re.fullmatch(r"[A-Za-z0-9 _.+#-]*", str(value)), (name, value)


HOSTILE = [
    "<script>window.pwned = 1</script>",
    "<img src=x onerror=alert(1)>",
    '<a href="javascript:alert(1)">点我</a>',
    "<iframe src=//evil.example></iframe><svg onload=alert(1)>",
    "[x](javascript:evil) [y](data:text/html;base64,PHNjcmlwdD4=) [z](  javascript:evil  )",
    "![x](http://evil.example/track.png) ![y](javascript:evil)",
    "<http://evil.example/> https://evil.example/ <javascript:alert(1)>",
    "`<script>x</script>` ```\n<script>y</script>\n```",
    "# <img src=x onerror=1>\n## `<b>` heading",
    "> <b>quote</b>\n> - <i>item</i>",
    "| <b>a</b> | b |\n|---|---|\n| <img src=x> | `<u>` |",
    "- [ ] <input onfocus=1 autofocus>\n- [x] <button onclick=1>go</button>",
    "[[<script>alert(1)</script>]] [[x\" onclick=\"alert(1)]]",
    '**<span style="x" onclick="y">bold</span>** _<em onmouseover=1>em</em>_ ~~<del>d</del>~~',
    "&lt;script&gt; &#60;script&#62; &amp;lt;",
    "\\<script\\> \\[x\\](javascript:1)",
    "---\nname: <script>\ndescription: x\n---\n正文",
]


def test_hostile_markup_only_ever_becomes_text_and_allowed_tags():
    for case, item in zip(HOSTILE, render_all(HOSTILE)):
        assert_safe(item["tree"])
        assert item["tree"]["tag"] == "div" and item["tree"]["attrs"]["class"] == "md"


def test_raw_html_is_shown_literally():
    cases = ["<script>window.pwned = 1</script>", "<img src=x onerror=alert(1)>", "一段<b>粗体</b>标签"]
    for case, item in zip(cases, render_all(cases)):
        shown = text_of(item["tree"])
        assert case in shown, (case, shown)


def test_links_are_not_clickable_and_images_are_not_loaded():
    [item] = render_all(["[点我](javascript:evil) ![图](http://evil.example/x.png) [官网](https://example.org)"])
    tree = item["tree"]
    tags = {node["tag"] for node in walk(tree)}
    assert not tags & {"a", "img", "iframe", "script", "svg", "video", "audio", "object", "embed"}
    classes = [node["attrs"].get("class") for node in walk(tree)]
    assert classes.count("md-link") == 2
    assert classes.count("md-img") == 1
    # 链接地址写在文字里给人看，不放进任何 href
    shown = text_of(tree)
    assert "javascript:evil" in shown and "https://example.org" in shown
    assert not any("href" in node["attrs"] or "src" in node["attrs"] for node in walk(tree))


def test_supported_markdown_subset_renders_to_the_right_structure():
    text = "\n".join(
        [
            "# 标题",
            "",
            "第一行",
            "第二行 **粗** *斜* ~~删~~ `码`",
            "",
            "- 甲",
            "- 乙",
            "  - 乙一",
            "",
            "1. 一",
            "2. 二",
            "",
            "- [x] 完成",
            "- [ ] 待办",
            "",
            "> 引用",
            "",
            "```py",
            "print(1)",
            "```",
            "",
            "---",
            "",
            "| 列一 | 列二 |",
            "|:---|---:|",
            "| a | b |",
            "",
            "见 [[plans]]。",
        ]
    )
    [item] = render_all([text])
    tree = item["tree"]
    tags = [node["tag"] for node in walk(tree)]
    for expected in ("h3", "p", "strong", "em", "del", "code", "ul", "ol", "li", "blockquote", "pre", "hr", "table", "th", "td", "br"):
        assert expected in tags, expected
    # 正文里的一级标题降到 h3，排在文件标题之下
    assert next(node for node in walk(tree) if node["tag"] in {"h3", "h4", "h5", "h6"})["tag"] == "h3"
    tables = [node for node in walk(tree) if node["tag"] == "table"]
    assert len(tables) == 1
    aligns = [node["attrs"].get("style", {}).get("textAlign") for node in walk(tables[0]) if node["tag"] == "th"]
    assert aligns == ["left", "right"]
    assert any(node["attrs"].get("class") == "wiki" for node in walk(tree))
    pre = next(node for node in walk(tree) if node["tag"] == "pre")
    assert pre["attrs"].get("data-lang") == "py"
    assert text_of(pre) == "print(1)"
    # 任务列表带无障碍文字
    assert "mdTaskDone" in text_of(tree) and "mdTaskTodo" in text_of(tree)


def test_front_matter_is_not_part_of_the_rendered_body():
    script = r"""
const fs = require("fs"); const vm = require("vm");
const win = {}; vm.runInNewContext(fs.readFileSync(process.argv[1], "utf-8"), { window: win });
const md = win.MemoryPod.markdown;
process.stdout.write(JSON.stringify([
  md.stripFrontmatter("---\nname: a\n---\n\n正文"),
  md.stripFrontmatter("没有 frontmatter\n---\n不是分隔"),
  md.stripFrontmatter("---\n没有结束的 frontmatter"),
]));
"""
    result = subprocess.run(["node", "-e", script, str(MARKDOWN_JS)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["正文", "没有 frontmatter\n---\n不是分隔", "---\n没有结束的 frontmatter"]


def test_pathological_inputs_finish_quickly_and_stay_bounded():
    """很长、嵌套很深、标记符号重复很多次的输入不能把页面卡死。"""
    cases = [
        "*" * 40000,
        "_a" * 20000,
        "[" * 30000,
        "[](" * 8000,
        "`" * 40000,
        "~" * 40000,
        "**a " * 8000,
        "> " * 2000 + "x",
        "- " * 2000 + "x",
        "\n".join("  " * i + "- x" for i in range(300)),
        "|a|" * 3000 + "\n" + "|-|" * 3000,
        ("# " + "x" * 2000 + "\n") * 200,
        "<" * 50000,
        "x" * 300000,
        "\n".join(["段落 " * 200] * 500),
    ]
    for case, item in zip(cases, render_all(cases)):
        assert_safe(item["tree"])
        assert item["ms"] < 4000, (case[:30], item["ms"])
    # 嵌套的引用 / 列表层数有上限
    deep = next(
        node
        for node, case in zip(render_all(cases), cases)
        if case == "> " * 2000 + "x"
    )["tree"]
    depth = 0
    node = deep
    while True:
        quote = next((c for c in node.get("children", []) if isinstance(c, dict) and c["tag"] == "blockquote"), None)
        if quote is None:
            break
        depth += 1
        node = quote
    assert depth <= 10
