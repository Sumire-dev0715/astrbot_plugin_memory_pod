/* 运行时场景：留言板：Markdown 渲染与恶意内容、历史版本与恢复、续期、编辑、发布、删除、搜索、遗忘。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, assert, until, sleep, section, summary } = require("../harness.js");

(async () => {
  const p = await boot();
  await p.reset();
  const p2 = await boot();
  const { $, $$, text, find, click, input } = p2;
  const q = (sel, t) => find(sel, t);
  await until(() => $(".notice-card"), "留言板告示卡片");

  section("A1 打开普通告示（Markdown 渲染）");
  click(find(".notice-card", "欢迎新同学"));
  await until(() => $(".detail .md"), "详情里渲染出 Markdown");
  const detail = $(".pane-detail");
  assert($$("strong", detail).some((e) => e.textContent === "teto"), "粗体渲染成 strong");
  assert($$("ul li", detail).length >= 3, "列表渲染成 ul/li");
  const wiki = $$("button.md-wiki", detail);
  assert(wiki.length === 1 && wiki[0].textContent.includes("rules"), "[[rules]] 渲染成可点按钮（rules.md 存在）");
  console.log("  标题:", text($("[data-detail-heading]")), "| 元信息:", text($(".meta-grid", detail)).slice(0, 160));
  assert(/堇/.test(text($(".meta-grid", detail))), "元信息里显示署名");

  section("A2 恶意内容全部当文字");
  click(find(".notice-card", "恶意标题"));
  await until(() => /window\.__pwned/.test(text($(".pane-detail"))), "恶意告示详情渲染完成");
  const d2 = $(".pane-detail");
  for (const tag of ["script", "img", "iframe", "a", "b", "i", "u", "object", "embed", "form"]) {
    assert($$(tag, d2).length === 0, `详情里不应有 <${tag}>`);
  }
  assert($$(".md-link", d2).length === 1, "[点我](javascript:evil) 渲染成不可点的 span.md-link: " + $$(".md-link", d2).length);
  assert(/javascript:evil/.test(text(d2)), "链接地址以文字形式显示出来");
  const onAttrs = [];
  Array.from(p2.document.querySelectorAll("*")).forEach((e) => { for (const a of Array.from(e.attributes)) if (a.name.startsWith("on")) onAttrs.push(e.localName + "@" + a.name); });
  assert(onAttrs.length === 0, "页面里没有任何 on* 属性: " + onAttrs);
  assert($$(".md-img", d2).length === 1, "图片只显示替代文字，不加载");
  assert(/<b>粗体标签<\/b>/.test(text(d2)), "原文里的 <b> 标签按字面显示");
  assert($$("table", d2).length === 1, "表格渲染");
  assert(!("__pwned" in p2.window), "脚本没有执行");
  const head = text($(".detail-head", d2));
  assert(head.includes("<img src=x onerror=alert(1)>"), "标题里的 HTML 以文字显示：" + head.slice(0, 80));

  section("A3 历史版本时间线、对比、恢复");
  click(find(".notice-card", "欢迎新同学"));
  await until(() => $(".detail .md") && /欢迎新同学/.test(text($("[data-detail-heading]"))), "回到欢迎告示");
  const historyTab = find("[role=tab]", "历史版本");
  assert(Boolean(historyTab), "有『历史版本』标签");
  click(historyTab);
  await until(() => $$(".timeline .tl-item").length >= 2, "时间线出现至少两个版本");
  console.log("  时间线条数:", $$(".timeline .tl-item").length);
  click(find(".timeline button", "查看与对比"));
  await until(() => $(".overlay .diff"), "对比对话框");
  assert($$(".overlay .diff .is-add, .overlay .diff .is-del").length > 0, "对比里有增删行");
  click(find(".overlay button", "关闭"));
  await until(() => !$(".overlay"), "对话框关闭");
  click(find(".timeline button", "恢复这一版"));
  await until(() => $(".overlay [role], .overlay .dialog"), "恢复确认框");
  assert(/先存进历史/.test(text($(".overlay"))), "恢复确认框写清现在的内容会先存进历史");
  click(find(".overlay button", "恢复这一版"));
  await until(() => !$(".overlay"), "确认后对话框关闭");
  await until(() => p2.lastCall("history/restore"), "发出 history/restore");
  const rc = p2.lastCall("history/restore");
  assert(rc.body.scope === "board" && rc.body.file === "welcome.md" && rc.body.version, "history/restore 参数: " + JSON.stringify(rc.body));

  section("A4 续期");
  click(find(".notice-card", "国庆活动"));
  await until(() => /国庆活动/.test(text($("[data-detail-heading]"))), "打开过期告示");
  await until(() => $(".pane-detail .md"), "详情读完");
  assert(/已过期/.test(text($(".pane-detail"))), "详情里有已过期提示: " + text($(".pane-detail")).slice(0, 200) + " | calls=" + p2.calls.slice(-4).map((c) => c.endpoint));
  click(find(".detail-actions button", "续期"));
  await until(() => $(".overlay input[type=date]"), "续期对话框");
  click(find(".overlay .chip", "一个月后"));
  const dateVal = $(".overlay input[type=date]").value;
  assert(/^\d{4}-\d{2}-\d{2}$/.test(dateVal), "快速选择填入日期: " + dateVal);
  click(find(".overlay button", "保存到期日"));
  await until(() => !$(".overlay"), "续期后对话框关闭");
  const sc = p2.lastCall("file/save");
  assert(sc && sc.body.mode === "fields" && sc.body.expires === dateVal && sc.body.oldFile === "old-event.md" && sc.body.signature === "管理员 B", "续期走 file/save fields 且带上原署名: " + JSON.stringify(sc && sc.body));
  const onDisk = await p2.peek("board", "", "old-event.md");
  assert(onDisk.content && onDisk.content.includes("expires: " + dateVal), "磁盘上的告示有新到期日");
  await sleep(250);
  assert(!/已过期/.test(text(find(".notice-card", "国庆活动"))), "续期后卡片不再标已过期: " + text(find(".notice-card", "国庆活动")));

  section("A5 编辑 + 保存前对比");
  click(find(".notice-card", "群公约"));
  await until(() => /群公约/.test(text($("[data-detail-heading]"))), "打开群公约");
  click(find(".detail-actions button", "编辑"));
  await until(() => $(".editor textarea"), "编辑器出现");
  const body = $$(".editor textarea")[0];
  input(body, body.value + "\n3. 守时");
  await sleep(10);
  assert($(".dirty") && !$(".dirty").hidden, "出现『有未保存的修改』标记");
  // 离开会提示
  click(find(".nav-item", "小本本"));
  await until(() => $(".overlay"), "有未保存修改时离开要确认");
  assert(/未保存|丢失/.test(text($(".overlay"))), "放弃确认框文案: " + text($(".overlay")).slice(0, 80));
  click(find(".overlay button", "取消"));
  await until(() => !$(".overlay"), "取消后留在编辑器");
  click(find(".editor-head button, .detail-head button", "保存") || find("button", "保存"));
  await until(() => $(".overlay .diff"), "保存前显示改动对比");
  assert($$(".overlay .diff .is-add").length >= 1, "对比里有新增行");
  click(find(".overlay button", "保存"));
  await until(() => p2.lastCall("file/save") && p2.lastCall("file/save").body.file === "rules.md", "发出 file/save");
  const es = p2.lastCall("file/save");
  assert(es.body.mode === "fields" && es.body.oldFile === "rules.md" && es.body.signature === "堇" && es.body.expires === "2099-12-31" && es.body.type === "", "编辑告示的 payload 完整: " + JSON.stringify(es.body).slice(0, 300));
  await until(() => $(".pane-detail .md") && /守时/.test(text($(".pane-detail"))), "保存后回到详情并显示新正文");

  section("A6 发布告示（署名记忆）");
  click(find("button", "发布告示"));
  await until(() => $(".editor input"), "发布表单");
  const inputs = $$(".editor input");
  console.log("  表单输入框数:", inputs.length);
  const sig = $$(".editor input").find((e) => (e.getAttribute("maxlength") === "100"));
  assert(sig && sig.value === "", "第一次发布时署名为空（服务端还没记住署名）");
  click(find(".detail-head button", "发布告示"));
  await until(() => $$(".editor .field-error").some((e) => !e.hidden), "空表单提示必填");
  console.log("  校验提示:", $$(".editor .field-error").filter((e) => !e.hidden).map((e) => e.textContent));
  await sleep(30);
  input(sig, "  测试   署名 ");
  const title = $$(".editor input").find((e) => e.getAttribute("maxlength") === "120");
  input(title, "新员工入职");
  const area = $(".editor textarea");
  input(area, "**下周一**入职");
  click(find(".editor .chip", "一周后"));
  click(find(".detail-head button", "发布告示"));
  await until(() => p2.lastCall("board/publish"), "发出 board/publish");
  const pub = p2.lastCall("board/publish");
  assert(pub.body.signature === "测试   署名" && pub.body.description === "新员工入职" && /^\d{4}-/.test(pub.body.expires), "publish payload: " + JSON.stringify(pub.body));
  await until(() => find(".notice-card", "新员工入职"), "列表里出现新告示");
  // 再开一次发布表单：署名预填
  click(find(".list-head button", "发布告示"));
  await until(() => $(".editor input"), "第二次发布表单");
  const sig2 = $$(".editor input").find((e) => (e.getAttribute("maxlength") === "100"));
  assert(sig2.value === "测试 署名", "第二次发布预填上次署名（服务端记的，折叠空白）: " + sig2.value);
  click(find(".detail-head button", "取消"));

  section("A7 删除告示（确认写清后果）");
  click(find(".notice-card", "新员工入职"));
  await until(() => /新员工入职/.test(text($("[data-detail-heading]"))), "打开新告示");
  click(find(".detail-actions button", "删除"));
  await until(() => $(".overlay"), "删除确认框");
  const ot = text($(".overlay"));
  assert(/回收站/.test(ot) && /天/.test(ot), "删除确认框写清回收站与保留天数: " + ot.slice(0, 160));
  click(find(".overlay button", "删除"));
  await until(() => !find(".notice-card", "新员工入职"), "删除后卡片消失");
  assert(p2.lastCall("file/delete").body.scope === "board", "file/delete 参数");

  section("A8 搜索与筛选");
  const search = $("[data-search]");
  input(search, "国庆");
  await sleep(20);
  assert($$(".notice-card").length === 1, "输入即筛选：只剩 1 张卡片，实际 " + $$(".notice-card").length);
  const bodyBtn = find(".list-note button", "在正文里搜索");
  assert(bodyBtn && /国庆/.test(text(bodyBtn)), "输入筛选词后列表顶上有『在正文里搜索』按钮（鼠标也能触发正文搜索）");
  click(bodyBtn);
  await until(() => p2.lastCall("search"), "点按钮触发正文搜索");
  assert(p2.lastCall("search").params.q === "国庆" && p2.lastCall("search").params.scope === "board", "search 参数: " + JSON.stringify(p2.lastCall("search").params));
  await until(() => find(".list-note", "正文搜索到"), "显示正文搜索结果条数");
  click(find(".list-note button", "清除搜索"));
  await sleep(40);
  p2.key(search, "Enter");
  await until(() => p2.calls.filter((c) => c.endpoint === "search").length >= 2, "回车也触发正文搜索");
  input(search, "");
  await sleep(20);
  click(find(".chips button", "已过期"));
  await sleep(20);
  console.log("  已过期筛选后卡片:", $$(".notice-card").length);

  section("A9 遗忘留言板告示（旧页面就有）并从已遗忘恢复");
  input(search, "");
  click(find(".chips button", "全部"));
  await sleep(40);
  click(find(".notice-card", "欢迎新同学"));
  await until(() => /欢迎新同学/.test(text($("[data-detail-heading]"))), "打开欢迎告示");
  click(find(".detail-actions button", "遗忘"));
  await until(() => $(".overlay"), "遗忘确认");
  assert(/已遗忘/.test(text($(".overlay"))) && !/自动恢复/.test(text($(".overlay"))), "留言板的遗忘确认只说手动恢复: " + text($(".overlay")).slice(0, 160));
  click(find(".overlay button", "遗忘"));
  await until(() => p2.lastCall("file/forget"), "file/forget");
  assert(p2.lastCall("file/forget").body.scope === "board" && p2.lastCall("file/forget").body.file === "welcome.md", "forget payload");
  await until(() => !find(".notice-card", "欢迎新同学"), "遗忘后卡片消失");
  click(find(".foot-link", "已遗忘"));
  await until(() => find(".item-row", "welcome.md"), "已遗忘里出现 welcome.md");
  click(find("button", "恢复", find(".item-row", "welcome.md")));
  await until(() => p2.lastCall("forgotten/restore"), "forgotten/restore");
  await sleep(200);
  click(find(".nav-item", "留言板"));
  await sleep(100);

  console.log("\nuncaught:", p2.uncaught().map((e) => String(e && e.stack || e).slice(0, 300)));
  assert(p2.uncaught().length === 0, "没有未捕获错误");
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});
