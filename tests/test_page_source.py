"""管理页的源码层面断言：锁住关键接线与已删除功能的痕迹。

页面是几个原生 JS 文件（pages/memory/*.js，由 index.html 按顺序加载）。
下面按文件和函数把源码切出来再断言：删除、彻底删除这类危险操作必须先确认，
页面里的接口路径必须和 web_api 注册的路由对得上，用户内容不能当 HTML 插进页面。
这些只是字符串层面的检查；点击、刷新、失败这类运行时行为由 test_page_runtime.py
在迷你 DOM 里跑真实脚本来测。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from support import pkg_memstore
from test_web_api import setup

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "memory"

SCRIPT_ORDER = [
    "i18n.js",
    "ui.js",
    "markdown.js",
    "diff.js",
    "store.js",
    "memory.js",
    "manage.js",
    "app.js",
]


def _page(name: str) -> str:
    return (PAGE / name).read_text("utf-8")


def _all_js() -> dict[str, str]:
    return {path.name: path.read_text("utf-8") for path in sorted(PAGE.glob("*.js"))}


def _between(source: str, start: str, end: str) -> str:
    assert start in source, start
    tail = source.split(start, 1)[1]
    assert end in tail, end
    return tail.split(end, 1)[0]


def _strip_comments(source: str) -> str:
    """去掉 /* */ 与 // 注释（只为查"代码里有没有某个写法"，不求完美的词法分析）。"""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)(^|[^:\"'\\])//.*$", r"\1", source)


# ------------------------------------------------------------ 页面骨架


def test_index_html_loads_scripts_in_dependency_order_without_external_resources():
    html = _page("index.html")
    sources = re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)
    # 桥接脚本必须是这个固定路径：AstrBot 按它改写成带令牌与主题的地址
    assert sources[0] == "/api/plugin/page/bridge-sdk.js"
    assert sources[1:] == ["./" + name for name in SCRIPT_ORDER]
    for name in SCRIPT_ORDER:
        assert (PAGE / name).is_file(), name
    # 没有内联脚本、内联事件，也没有外链字体、CDN
    assert not re.search(r"<script(?![^>]*\ssrc=)", html)
    assert not re.search(r"\son[a-z]+\s*=", html)
    assert "<style" not in html
    for name in ["index.html", "style.css", *SCRIPT_ORDER]:
        code = _strip_comments(_page(name))
        urls = set(re.findall(r"(?:https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s\"')]*", code))
        # 唯一的网址是 SVG 命名空间（createElementNS 要用），不是网络请求
        assert urls <= {"http://www.w3.org/2000/svg"}, (name, urls)
    css = _page("style.css")
    assert "@import" not in css and "url(" not in css
    # 骨架里要有页面脚本取用的容器
    for element_id in ("app", "dialogs", "toasts"):
        assert f'id="{element_id}"' in html


def test_page_never_builds_html_from_strings():
    """记忆正文、文件名、群名、署名、摘要都是用户内容：只能当文字放进页面。"""
    forbidden = [
        r"\.innerHTML",
        r"outerHTML",
        r"insertAdjacentHTML",
        r"document\.write",
        r"\beval\s*\(",
        r"new\s+Function",
        r"srcdoc",
        r"DOMParser",
        r"createContextualFragment",
        # 沙箱 iframe 没有 allow-same-origin，访问会抛 SecurityError
        r"localStorage",
        r"sessionStorage",
        # 沙箱 iframe 没有 allow-modals，这些调用被浏览器直接忽略
        r"window\.(?:prompt|alert|confirm)\b",
        r"(?<![\w.])(?:prompt|alert)\s*\(",
        r"javascript:",
        r"setAttribute\(\s*[\"']on",
    ]
    for name, source in _all_js().items():
        code = _strip_comments(source)
        for pattern in forbidden:
            assert not re.search(pattern, code), (name, pattern)
    # 唯一创建链接的地方是下载备份的 Blob 链接
    for name, source in _all_js().items():
        count = len(re.findall(r'h\(\s*"a"\s*[,)]', _strip_comments(source)))
        assert count == (1 if name == "ui.js" else 0), name
    # 事件只接受函数：字符串形式的内联事件在 h() 里直接丢掉
    assert 'typeof value === "function"' in _page("ui.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_every_js_file_passes_node_check():
    for name in SCRIPT_ORDER:
        result = subprocess.run(
            ["node", "--check", str(PAGE / name)], capture_output=True, text=True
        )
        assert result.returncode == 0, (name, result.stderr)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_page_file_name_rule_is_not_looser_than_the_backend():
    """表单里放行的文件名，后端必须也认：否则要到保存时才被服务端报"文件名不合法"。"""
    names = ["a" * (total - 3) + ".md" for total in range(4, 131)]
    names += [
        "a.md", ".md", "md", "-a.md", "_a.md", "a b.md", "a/b.md", "../a.md", "ä.md", "A-b_c.d.md",
        "a.md.md", "a.MD", "a.md\n", " a.md", "a.md ", "MEMORY.md", "user-10001.md",
    ]
    runner = (
        "const fs = require('fs');"
        "const m = /const NAME_RE = (\\/.*\\/);/.exec(fs.readFileSync(process.argv[1], 'utf8'));"
        "const re = eval(m[1]);"
        "const names = JSON.parse(fs.readFileSync(0, 'utf8'));"
        "console.log(JSON.stringify(names.map((n) => re.test(n))));"
    )
    result = subprocess.run(
        ["node", "-e", runner, str(PAGE / "memory.js")],
        input=json.dumps(names),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    page_says = json.loads(result.stdout)
    backend_says = [pkg_memstore.ScopeStore.is_valid_name(name) for name in names]
    differing = [(len(n), n[:12]) for n, a, b in zip(names, page_says, backend_says) if a != b]
    assert not differing, differing
    # 两边都按"整个名字（含 .md）最多 121 个字符"算：这一条防的是两边一起错
    longest_ok = max(len(n) for n, ok in zip(names, backend_says) if ok and n.startswith("aaa"))
    assert longest_ok == 121


# ------------------------------------------------------------ 接口对得上


def _used_endpoints() -> set[tuple[str, str]]:
    used = set()
    for name, source in _all_js().items():
        code = _strip_comments(source)
        for endpoint in re.findall(r'apiGet\(\s*"([^"]+)"', code):
            used.add((endpoint, "GET"))
        for endpoint in re.findall(r'apiPost\(\s*"([^"]+)"', code):
            used.add((endpoint, "POST"))
        # 备份下载：先 POST export/prepare，再 GET export/file
        for endpoint in re.findall(r'download\(\s*"([^"]+)"', code):
            used.add((endpoint, "GET"))
        # confirmAndPost({ endpoint: "file/delete", ... }) 走 POST
        for endpoint in re.findall(r'endpoint:\s*"([^"]+)"', code):
            used.add((endpoint, "POST"))
    return used


def test_every_endpoint_the_page_calls_is_a_registered_route(tmp_path):
    _, dashboard = setup(tmp_path)
    routes = {(path, method) for path, method, _handler, _desc in dashboard._routes()}
    used = _used_endpoints()
    assert used, "没有从页面源码里读到任何接口调用"
    assert used <= routes, sorted(used - routes)


def test_every_registered_route_has_an_entry_in_the_page(tmp_path):
    """原有功能一项不丢：后端注册的每个管理页接口，新页面里都有调用。"""
    _, dashboard = setup(tmp_path)
    routes = {(path, method) for path, method, _handler, _desc in dashboard._routes()}
    assert routes <= _used_endpoints(), sorted(routes - _used_endpoints())


# ------------------------------------------------------------ 危险操作必须确认


def _call_context(source: str, needle: str) -> list[str]:
    """needle 每次出现时，所在函数（从最近一个 function 声明起）到它为止的代码。"""
    code = _strip_comments(source)
    contexts = []
    for match in re.finditer(re.escape(needle), code):
        starts = [m.start() for m in re.finditer(r"\bfunction\s+\w+\s*\(", code[: match.start()])]
        assert starts, needle
        contexts.append(code[starts[-1] : match.start()])
    return contexts


# 端点 → 调用它之前必须出现的确认写法；每一组里出现任意一种就算
CONFIRMED = {
    "file/delete": (("confirm:",),),
    "file/forget": (("confirm:",),),
    "file/purge": (("confirm:",),),
    "forgotten/delete": (("confirm:",),),
    "trash/delete": (("confirm:",),),
    "history/restore": (("confirm:",),),
    "whisper/purge": (("ui.confirm(",),),
    "tidy/undo": (("ui.confirm(",),),
    "import/apply": (("ui.confirm(",),),
    "consolidate/apply": (("confirmApply(",),),
    "index/rebuild": (("ui.confirm(",),),
    # 编辑器保存先显示改动对比；续期的对话框就是确认（写着现在的到期日）
    "file/save": (("confirmSave(", "ui.dialog("),),
}
# 删了就找不回来（或影响面大）的操作，确认框必须用 danger
DANGER = ("file/delete", "file/purge", "forgotten/delete", "trash/delete", "whisper/purge")


@pytest.mark.parametrize("endpoint", sorted(CONFIRMED))
def test_destructive_endpoints_are_called_only_after_a_confirmation(endpoint):
    found = 0
    for name, source in _all_js().items():
        # 带 endpoint: 的写法是 confirmAndPost 的参数，确认框在同一个对象里
        for needle in (f'apiPost("{endpoint}"', f'endpoint: "{endpoint}"'):
            for context in _call_context(source, needle):
                found += 1
                for group in CONFIRMED[endpoint]:
                    assert any(token in context for token in group), (name, endpoint, group)
                if endpoint in DANGER:
                    assert "danger: true" in context, (name, endpoint)
    assert found, endpoint


def test_confirm_and_post_asks_before_it_posts():
    memory = _page("memory.js")
    body = _between(memory, "async function confirmAndPost(opts) {", "function deleteFile(")
    assert body.index("ui.confirm(opts.confirm)") < body.index("ui.apiPost(opts.endpoint")


def test_real_migrations_run_only_after_a_confirmation_built_from_the_dry_run():
    manage = _page("manage.js")
    for body_name, endpoint in (("payload", "migrate/scope"), ("body", "migrate/platform")):
        real = f'apiPost("{endpoint}", {body_name}(false))'
        assert real in manage
        for context in _call_context(manage, real):
            assert "ui.confirm(" in context, endpoint
            assert "danger:" in context, endpoint
            # 真正迁移之前先用预演的数字写确认框
            assert f'apiPost("{endpoint}", {body_name}(true))' in context, endpoint


def test_trash_restore_confirms_before_overwriting():
    memory = _page("memory.js")
    body = _between(memory, "async function restoreTrash(item) {", "async function viewTrash(item) {")
    assert "if (activeFiles().has(item.file)) {" in body
    assert body.index("ui.confirm(") < body.index('ui.apiPost("trash/restore"')
    assert 't("restoreOverwritePoint"' in body
    # 索引副本不再出现在回收站，也没有"恢复旧索引"的确认与分支
    assert "isIndexFile" not in memory and "restoreIndexPoint" not in memory


def test_manual_index_editing_is_gone():
    memory = _page("memory.js")
    panel = _between(memory, "function IndexPanel(view, r) {", "// ------------------------------------------------------------ 回收站与已遗忘")
    # 只读：没有编辑框、没有保存入口，也没有旧索引备份的说明；重建与健康提示保留
    assert "textarea" not in panel
    assert "index/save" not in panel and "index/save" not in memory
    assert 't("indexReadonlyHint")' in panel
    assert 'apiPost("index/rebuild"' in panel
    assert "indexHealth(idx)" in panel
    api_source = (ROOT / "web_api.py").read_text("utf-8")
    assert "index/save" not in api_source
    assert "def index_save" not in api_source and "write_index" not in api_source
    memstore_source = (ROOT / "core" / "memstore.py").read_text("utf-8")
    assert "_backup_index_to_trash" not in memstore_source
    assert "def write_index" not in memstore_source
    assert "copy_to_trash" not in memstore_source


def test_move_buttons_open_a_target_dialog_and_ask_about_conflicts():
    memory = _page("memory.js")
    actions = _between(memory, "function headActions(d) {", "function paint() {")
    assert 't("moveToNotebook")' in actions and 't("moveToWhisper")' in actions
    assert "moveFile(view, r, name)" in actions
    move = _between(memory, "async function moveFile(view, r, name) {", "function FileDetail(")
    # 目标里有同名文件时三种选择；取消不发写请求
    assert 't("conflictKeep")' in move
    assert 't("conflictOverwrite")' in move
    assert 't("cancel")' in move
    # 目标里只有已遗忘的同名副本时，覆盖的说法换成"进回收站"，不谎称进历史版本
    assert 'keepPreview.forgotten ? "conflictOverwriteForgottenPoint" : "conflictOverwritePoint"' in move
    # 从悄悄话移出去时写清"这个机器人的所有群聊和私聊都能读到"
    assert 't("moveNotebookConsequence", { platform: picked.platform })' in move
    assert 't("moveWhisperConsequence")' in move
    # 先预演（改盘前），再写；取消关掉弹窗就结束
    assert move.count('apiPost("migrate/file"') >= 2
    assert "dryRun: true" in move and "dryRun: false" in move
    assert 't("moveToTrashPoint")' in move
    # 预演与真正的写都有 catch：接口报错时提示原因，不留下未处理的拒绝
    assert move.count("catch (err)") >= 2
    assert move.count('ui.toast(err.message, "error")') >= 2
    # 这段对话自己的机器人先列进目标，migrate/info 取不到时默认目标也不会缺
    targets = _between(memory, "function moveTargets(r, toNotebook, info) {", "  /**\n   * 移动一个文件")
    assert targets.index("add(platform);") < targets.index("((info && info.platforms) || [])")


def test_move_posts_only_from_a_confirmed_dialog():
    """migrate/file 会移走来源：真正的写只在三选一弹窗或无冲突的确认框之后发出。"""
    memory = _page("memory.js")
    move = _between(memory, "async function moveFile(view, r, name) {", "function FileDetail(")
    send = _between(move, "async function sendMove(", "/** 同名冲突")
    assert send.count('apiPost("migrate/file"') == 1
    assert "dryRun: false" in send
    conflict = _between(move, "function askConflict(", "async function confirmNoConflict(")
    assert 'sendMove(dlg, "keep"' in conflict and 'sendMove(dlg, "overwrite"' in conflict
    assert 't("cancel")' in conflict
    assert 't("conflictKeep")' in conflict and 't("conflictOverwrite")' in conflict
    confirm = _between(move, "async function confirmNoConflict(", "const ctrl = ui.dialog(")
    assert confirm.index("ui.confirm(") < confirm.index('sendMove(null, "overwrite"')
    # 除了定义，只有这两个确认/选择入口会发真正的移动
    assert move.count("sendMove(") == 4


# ------------------------------------------------------------ 具体功能的接线


def test_trash_tab_purge_button_only_for_missing_active_files():
    memory = _page("memory.js")
    render = _between(memory, "function trashList() {", "function forgottenList() {")
    assert "const active = activeFiles();" in render
    assert "!active.has(item.file)" in render
    assert "purgeTrashFile(item)" in render
    assert 't("purgeFile")' in render
    # MEMORY.md 副本不会出现在列表里，不需要特判
    assert "isIndexFile" not in render


def test_trash_purge_uses_file_purge_with_confirmation():
    memory = _page("memory.js")
    purge = _between(memory, "function purgeFile(r, name) {", "/** 按钮点击")
    assert "confirmAndPost(" in purge
    assert 't("purgeTitle"' in purge
    assert 't("purgeMessage")' in purge
    assert "danger: true" in purge
    assert 'endpoint: "file/purge"' in purge
    # 后果写清：历史版本、回收站副本、整理记录里的原文、审计只留元数据
    for key in ("purgePoint1", "purgePoint2", "purgePoint3"):
        assert f't("{key}")' in purge
    trash = _between(memory, "async function purgeTrashFile(item) {", "function forgottenList() {")
    assert "purgeFile(r, item.file)" in trash
    assert "await refresh()" in trash


def test_overview_shows_user_identifier_notice():
    manage = _page("manage.js")
    overview = _between(manage, "function OverviewSection() {", "function scopeRows(")
    assert 't("userIdentifierNotice")' in overview
    assert "turnLimitNotice" not in overview
    # 上下文面板里同样提示
    assert 't("userIdentifierNotice")' in _between(
        manage, "function ContextPanel(key) {", "整理：模型与任务状态"
    )


def test_context_panel_is_read_only():
    app = _page("app.js")
    manage = _page("manage.js")
    assert '"tabContext"' in app
    panel = _between(manage, "function ContextPanel(key) {", "整理：模型与任务状态")
    assert 'apiGet("whisper/context"' in panel
    assert 't("contextReadonlyNotice")' in panel
    assert 't("summaryTitle")' in panel
    assert 't("noSummary")' in panel
    # 面板里没有任何写接口
    assert "apiPost(" not in panel
    for name, source in _all_js().items():
        for needle in ("session/context", "session/reset", "resetCompression"):
            assert needle not in source, (name, needle)


def test_context_panel_shows_composition_bar_cache_rate_and_summary():
    manage = _page("manage.js")
    bar = _between(manage, "/** 按比例分段的横条", "/** 一段对话的上下文面板")
    assert "function stackBar(parts, total)" in bar
    assert "(parts[k] / total) * 100" in bar
    assert 't("cacheRate"' in bar
    assert "data.cacheHit / data.actual" in bar
    panel = _between(manage, "function ContextPanel(key) {", "整理：模型与任务状态")
    assert "data.hasSummary" in panel and "data.summary" in panel


def test_context_panel_leaves_no_dead_ui():
    for name, source in _all_js().items():
        assert "fmtDuration" not in source, name
        assert "pendingRounds" not in source, name
        assert "下一次压缩" not in source, name
    api_source = (ROOT / "web_api.py").read_text("utf-8")
    assert "hasContext" not in api_source


def test_removed_features_are_gone_from_page():
    sources = {name: _page(name) for name in ("index.html", "style.css", *SCRIPT_ORDER)}
    for name, source in sources.items():
        for needle in (
            "autotidy",
            "autoTidy",
            "tidyMode",
            "pendingScopes",
            "renderPendingPanel",
            "SHARED_KEY",
            "globalTitle",
            "peopleTitle",
            "renderArchiveTab",
        ):
            assert needle not in source, (name, needle)


def test_consolidate_all_covers_three_layers_only():
    manage = _page("manage.js")
    body = _between(manage, "function consolidateTargets() {", "function TidyAllPanel(")
    assert "ov.board" in body
    assert "ov.notebooks" in body
    assert "ov.whispers" in body
    assert "people.platforms" not in body
    assert "sessions" not in body


def test_memory_type_options_use_the_four_new_types():
    expected = 'const TYPES = ["person", "agreement", "note", "journal"];'
    assert expected in _page("memory.js")
    assert expected in _page("store.js")
    i18n = _page("i18n.js")
    for key in ("type_person", "type_agreement", "type_note", "type_journal"):
        assert f'"{key}"' in i18n, key
    for key in ("typeHint_person", "typeHint_agreement", "typeHint_note", "typeHint_journal"):
        assert f'"{key}"' in i18n, key


def test_board_editor_and_publish_form_have_no_type_option():
    memory = _page("memory.js")
    editor = _between(
        memory,
        "function Editor(view, r, opts) {",
        "// ------------------------------------------------------------ 发布告示",
    )
    # 类型下拉只在非留言板时渲染
    assert "board ? null : typeField()" in editor
    assert 't("fieldType")' in _between(editor, "function typeField() {", "function formFields() {")
    # 保存时留言板的类型一律给空
    assert 'type: board ? "" : cur.type' in editor
    # 发布告示表单没有类型；字段是署名、到期日、标题、可选的自己起的文件名和正文
    publish = _between(memory, "function PublishForm() {", "/** 续期")
    assert "fieldType" not in publish
    assert "cur.type" not in publish
    payload = publish.split('apiPost("board/publish"', 1)[1].split("});", 1)[0]
    assert "type:" not in payload
    # 旧页面在留言板也能自己起文件名；留空时由服务端按时间生成
    assert 't("publishFile")' in publish and "cur.file.trim()" in publish
    assert "file: cur.file.trim()" in payload
    # 告示卡片与详情标题不显示类型标签
    card = _between(memory, "function noticeCard(row) {", "function boardRows(")
    assert "typeTag" not in card
    assert "tags: board ? null : typeTag(d.type)" in memory


def test_publish_form_prefills_last_signature_from_server():
    """记住上次填的署名放在服务端（沙箱 iframe 里没有 localStorage）。"""
    memory = _page("memory.js")
    publish = _between(memory, "function PublishForm() {", "/** 续期")
    assert "S.ov.board.lastSignature" in publish
    # 刚发布过就用页面里记着的署名，总览还在重新加载时也不会取到旧值
    assert "S.lastSignature" in publish
    assert "signature: last" in publish
    assert 't("signatureRequired")' in publish
    assert "signature: cur.signature.trim()" in publish


# ------------------------------------------------------------ 键盘、焦点、状态


def test_replacing_content_keeps_keyboard_focus_inside_the_area():
    """重绘一块区域时，焦点原来在里面就留在这块区域，而不是掉回页面开头。"""
    ui = _page("ui.js")
    body = _between(ui, "function replace(el) {", "let idSeq")
    assert "hadFocus" in body
    assert 'setAttribute("tabindex", "-1")' in body
    assert 'setAttribute("data-focus-anchor", "")' in body
    assert "el.focus()" in body
    # 落脚点是脚本临时放的容器，不画焦点环
    assert "[data-focus-anchor]:focus" in _page("style.css")


def test_busy_buttons_keep_focus_and_ignore_repeat_clicks():
    ui = _page("ui.js")
    body = _between(
        ui,
        "async function withBusy(button, task) {",
        "// ------------------------------------------------------------ 表单件",
    )
    assert "disabled" not in body
    assert 'getAttribute("aria-busy") === "true"' in body
    dialog = _between(ui, "setBusy: function (value) {", "setBody: function (node) {")
    assert "aria-disabled" in dialog and "disabled =" not in dialog


def test_dialogs_are_modal_trap_focus_and_default_to_cancel_when_dangerous():
    ui = _page("ui.js")
    assert '"aria-modal": "true"' in ui
    assert 'opts.alert ? "alertdialog" : "dialog"' in ui
    assert 'event.key === "Escape"' in ui
    assert 'event.key !== "Tab"' in ui
    confirm = _between(ui, "function confirm(opts) {", "/** 按钮执行异步任务期间")
    # 危险确认：默认焦点放在"取消"上
    assert "autofocus: Boolean(opts.danger)" in confirm
    assert "autofocus: !opts.danger" in confirm
    # 弹窗打开时背景不可操作，关闭后把焦点还给触发它的元素
    assert "setAppInert(true)" in ui and "previous.focus()" in ui


def test_page_follows_host_theme_and_language():
    html = _page("index.html")
    css = _page("style.css")
    app = _page("app.js")
    assert 'data-theme="light"' in html
    # 桥接在 <html data-theme> 上写 light / dark；样式只认这个属性
    assert ':root[data-theme="dark"]' in css
    assert "prefers-color-scheme" not in css
    assert "bridge.onContext(onContext)" in app
    assert "window.AstrBotPluginView || window.AstrBotPluginPage" in app
    assert "prefers-reduced-motion" in css and "max-width: 900px" in css


def test_lists_have_loading_empty_and_error_states():
    ui = _page("ui.js")
    for helper in ("function spinner(", "function emptyState(", "function errorState("):
        assert helper in ui
    memory = _page("memory.js")
    for body in (
        _between(memory, "function fillFileList(body, view, r, st) {", "/** 全文搜索"),
        _between(memory, "function fillBoard(body, r, st) {", "function boardListPane() {"),
    ):
        assert "ui.spinner()" in body
        assert "ui.errorState(" in body
        assert "ui.emptyState(" in body


# ------------------------------------------------------------ 后端文案与窄屏


def test_backend_written_chinese_is_not_shown_in_other_languages():
    """后端的回执、理由只有中文：成功提示在非中文界面里改用页面自己的译文。"""
    ui = _page("ui.js")
    assert "function resultMessage(res, fallback)" in ui
    assert "function zhOnly(text)" in ui
    for name in ("memory.js", "manage.js"):
        code = _strip_comments(_page(name))
        # 成功提示一律经 resultMessage，不再直接用 res.message / done.message
        assert not re.search(r"\b(?:res|done)\.message\b", code), name
    manage = _page("manage.js")
    # 导入问题的理由、迁移跳过的理由是后端写的中文句子
    assert "ui.zhOnly(item.reason)" in manage
    assert "ui.zhOnly(s.reason)" in manage
    memory = _page("memory.js")
    assert "ui.zhOnly(row.long.join" in memory and 't("longNoticePlain")' in memory
    # 上下文窗口的来源说明由后端给稳定的代码，页面按语言写
    assert 't("windowSource_" + ctx.windowSource' in manage


def test_dialog_cover_hides_the_busy_spinner_of_the_trigger_button():
    """弹窗盖在页面上时，触发它的按钮不转圈：那时是在等人确认，不是在等网络。"""
    ui = _page("ui.js")
    assert 'app.classList.toggle("is-covered", on)' in ui
    css = _page("style.css")
    assert ".app:not(.is-covered) .btn.is-busy" in css
    assert ".app:not(.is-covered) .btn.is-busy::after" in css


def test_narrow_layout_rules_exist():
    css = _page("style.css")
    tablet = css.split("@media (max-width: 900px) {", 1)[1].split("@media (max-width: 640px) {", 1)[0]
    # 五个导航项等宽、独占一行：图标在上、文字在下，数量变成角标。
    # 这组规则必须从 900px（单栏布局）起生效：一行放不下英文的导航，横向滚动条又是
    # 藏起来的，最后几项会被裁掉（在真 Chrome 里 700 / 780px 量到过）
    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in tablet
    assert "flex-direction: column" in tablet
    assert ".nav-count" in tablet and "position: absolute" in tablet
    assert "flex-wrap: wrap" in tablet and "flex-basis: 100%" in tablet
    narrow = css.split("@media (max-width: 640px) {", 1)[1].split("@media (prefers-reduced-motion", 1)[0]
    assert "repeat(5" not in narrow
    # 元信息两列排；弹窗在手机上贴底
    assert "repeat(2, minmax(0, 1fr))" in narrow
    assert "border-radius: var(--r-lg) var(--r-lg) 0 0" in narrow
    # 两栏在 900px 以下变成一次只看一栏，并有返回按钮
    assert '.split[data-show="list"] .pane-detail' in css and ".only-narrow" in css
    assert ".meta-item {\n  display: grid;\n  align-content: start;" in css
    assert "justify-self: start" in css.split(".back-link {", 1)[1].split("}", 1)[0]


def test_ui_strings_do_not_hard_code_full_width_punctuation():
    """英文界面里的冒号、括号、分隔符要跟着语言走：写死的全角标点会在英文里露出来。

    后端数据里的固定字符串（比如搜索只命中文件头时的片段占位）不属于界面文字，单独放行。
    """
    fullwidth = re.compile(r"[\u3001\u3002\uff01\uff0c\uff1a\uff1b\uff08\uff09\u201c\u201d]")
    allowed = "（正文匹配）"  # web_api.py 里搜索结果的占位，只用于比较
    offenders = []
    for name, source in _all_js().items():
        if name == "i18n.js":
            continue
        for number, line in enumerate(_strip_comments(source).splitlines(), 1):
            if allowed in line:
                continue
            if fullwidth.search(line):
                offenders.append((name, number, line.strip()[:80]))
    assert not offenders, offenders


def test_i18n_carries_the_punctuation_separators():
    """冒号、括号、顿号、分号都从文案里取：中文全角，英文半角。"""
    i18n = _page("i18n.js")
    for key in ("fieldLine", "paren", "listSep", "semiSep"):
        assert f'"{key}"' in i18n, key


def test_every_file_detail_offers_edit_forget_and_delete_including_board_notices():
    """旧页面里留言板的告示也能遗忘；新页面的文件详情对三层都保留「遗忘」。"""
    memory = _page("memory.js")
    actions = _between(memory, "function headActions(d) {", "function paint() {")
    assert 'openPane(view, { kind: "edit", file: name }' in actions
    assert "forgetFile(view, r, name)" in actions
    assert "deleteFile(view, r, name)" in actions
    # 续期只属于留言板；遗忘不再按作用域区分
    assert actions.index("renewNotice(r, name, d)") < actions.index("forgetFile(view, r, name)")
    assert "board\n          ? h(" in actions and ": null," in actions


def test_body_search_is_reachable_by_mouse_and_by_enter():
    """正文搜索（服务端全文）原来有个「搜索」按钮；新页面输入筛选词后列表顶上给按钮，回车也行。"""
    memory = _page("memory.js")
    note = _between(memory, "function bodySearchNote(r, st) {", "/** 全文搜索")
    assert 'onclick: function () { runSearch(r, st, st.query); }' in note
    assert 't("searchBody"' in note
    assert memory.count("bodySearchNote(r, st)") == 3  # 定义 + 文件列表 + 告示列表
    assert 'apiGet("search"' in memory
    assert memory.count('event.key === "Enter"') >= 1 and "onEnter" in memory
