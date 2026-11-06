/* 运行时场景：各种状态：空数据、后端报错、键盘操作、窄屏、深色主题。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, assert, until, sleep, section, summary } = require("../harness.js");

(async () => {
  // ------------------------------------------------------------ 空状态
  section("I1 空数据");
  await (await boot()).reset(true);
  let p = await boot();
  let { $, $$, text, find, click, input } = p;
  await until(() => $(".state-empty"), "留言板空状态");
  console.log("  留言板:", text($(".pane-list")).slice(0, 160));
  assert(find(".state-empty button", "发布告示"), "留言板空状态里有发布入口");
  assert(/选择一条告示/.test(text($(".pane-detail"))), "右侧空提示");
  click(find(".nav-item", "小本本"));
  await sleep(60);
  assert(/还没有小本本/.test(text($(".pane-list"))), "小本本空状态: " + text($(".pane-list")).slice(0, 80));
  click(find(".nav-item", "悄悄话"));
  await sleep(60);
  assert(/还没有对话/.test(text($(".pane-list"))), "悄悄话空状态");
  click(find(".nav-item", "上下文"));
  await sleep(60);
  assert(/还没有对话/.test(text($(".pane-list"))), "上下文空状态");
  click(find(".nav-item", "管理"));
  await sleep(60);
  click(find(".row-section", "备份与迁移"));
  await sleep(60);
  assert(/还没有可以导出的记忆/.test(text($(".pane-detail"))), "导出空状态");
  click(find("[role=tab]", "迁移一处"));
  await sleep(40);
  assert(/还没有可迁移的记忆/.test(text($(".pane-detail"))), "迁移空状态");
  click(find(".row-section", "整理"));
  await sleep(60);
  assert(/还没有可整理的记忆/.test(text($(".pane-detail"))), "整理空状态: " + text($(".pane-detail")).slice(0, 120));
  assert(p.uncaught().length === 0, "空数据无未捕获错误: " + p.uncaught().map(String));

  // ------------------------------------------------------------ 错误状态
  section("I2 后端报错");
  await p.reset(false);
  await p.fail("overview", 1);
  p = await boot();
  ({ $, $$, text, find, click, input } = p);
  await until(() => $(".banner-area .notice"), "总览失败的横幅");
  console.log("  横幅:", text($(".banner-area")));
  assert(/读取记忆总览失败/.test(text($(".banner-area"))), "横幅说明总览读取失败");
  assert(/模拟的后端错误/.test(text($("#toasts"))), "同时弹出错误提示");
  click(find(".banner-area button", "重试"));
  await until(() => $(".notice-card"), "重试后恢复");
  await until(() => $(".banner-area").hidden, "恢复后横幅消失");

  await p.fail("scope", 1);
  click(find(".nav-item", "小本本"));
  await until(() => $(".state-error"), "作用域加载失败的错误态");
  assert(/加载失败/.test(text($(".state-error"))), "错误态标题");
  click(find(".state-error button", "重试"));
  await until(() => $$(".row").length > 0, "重试后文件列表出现");

  await p.fail("file", 1);
  click(find(".row", "plans.md"));
  await until(() => $(".pane-detail .state-error"), "文件读取失败的错误态");
  click(find(".pane-detail .state-error button", "重试"));
  await until(() => $(".pane-detail .md"), "重试后文件详情出现");

  await p.fail("history/versions", 1);
  click(find("[role=tab]", "历史版本"));
  await until(() => $(".pane-detail .state-error"), "历史版本失败的错误态");
  click(find(".pane-detail .state-error button", "重试"));
  await until(() => $(".timeline") || $(".state-empty"), "重试后出现历史或空状态");

  await p.fail("file/save", 1);
  click(find(".detail-actions button", "编辑"));
  await until(() => $(".editor textarea"), "编辑器");
  input($(".editor textarea"), $(".editor textarea").value + "\n新增");
  click(find(".detail-head button", "保存"));
  await until(() => $(".overlay .diff"), "对比");
  click(find(".overlay button", "保存"));
  await until(() => /模拟的后端错误/.test(text($("#toasts"))), "保存失败提示");
  assert($(".dirty") && !$(".dirty").hidden, "保存失败后仍保持未保存标记");
  assert($(".editor textarea").value.endsWith("新增"), "保存失败后输入没有丢");
  assert(p.uncaught().length === 0, "错误态无未捕获错误: " + p.uncaught().map(String));

  // ------------------------------------------------------------ 键盘
  section("H 键盘");
  await p.reset(false);
  p = await boot();
  ({ $, $$, text, find, click, input } = p);
  await until(() => $(".notice-card"), "列表");
  p.document.body.focus && p.document.body.focus();
  const ev = p.key(p.document.body, "/");
  const act = p.document.activeElement;
  assert(act && act.hasAttribute("data-search"), "按 / 聚焦到列表搜索框: " + (act && act.localName));
  // 列表方向键
  const first = $$(".notice-card")[0];
  first.focus();
  p.key(first, "ArrowDown");
  assert(p.document.activeElement === $$(".notice-card")[1], "方向键下移一项");
  p.key(p.document.activeElement, "End");
  assert(p.document.activeElement === $$(".notice-card").slice(-1)[0], "End 到最后一项");
  p.key(p.document.activeElement, "Home");
  assert(p.document.activeElement === $$(".notice-card")[0], "Home 回到第一项");
  // roving tabindex
  const idx = $$(".notice-card").map((c) => c.tabIndex);
  assert(idx.filter((v) => v === 0).length === 1, "列表只有一项在 Tab 序列里: " + idx);
  // 导航栏
  assert($$(".nav-item")[0].getAttribute("aria-current") === "page", "当前导航项 aria-current=page");

  // 对话框：Esc 关闭、Tab 循环、焦点归还
  click(find(".notice-card", "群公约"));
  await until(() => /群公约/.test(text($("[data-detail-heading]"))), "打开群公约");
  const delBtn = find(".detail-actions button", "删除");
  delBtn.focus();
  click(delBtn);
  await until(() => $(".overlay"), "删除确认");
  const dlg = $(".dialog");
  assert(dlg.getAttribute("role") === "alertdialog" && dlg.getAttribute("aria-modal") === "true", "危险确认用 alertdialog + aria-modal");
  assert($("#app").inert === true, "弹窗打开时背景 inert");
  assert(/取消/.test(p.document.activeElement.textContent), "默认焦点在取消");
  const btns = $$(".dialog button").filter((b) => !b.disabled);
  // Shift+Tab 在第一个按钮上 -> 跳到最后一个
  btns[0].focus();
  const sh = p.key(btns[0], "Tab", { shiftKey: true });
  assert(sh.defaultPrevented && p.document.activeElement === btns[btns.length - 1], "Shift+Tab 在第一项时循环到最后一项");
  const tb = p.key(btns[btns.length - 1], "Tab");
  assert(tb.defaultPrevented && p.document.activeElement === btns[0], "Tab 在最后一项时循环到第一项");
  p.key(btns[0], "Escape");
  await until(() => !$(".overlay"), "Esc 关闭对话框");
  assert($("#app").inert === false, "关闭后背景恢复");
  assert(p.document.activeElement === delBtn, "焦点回到触发按钮");

  // 编辑器 Ctrl+S
  click(find(".detail-actions button", "编辑"));
  await until(() => $(".editor textarea"), "编辑器");
  input($(".editor textarea"), $(".editor textarea").value + "\n键盘保存");
  p.key($(".editor textarea"), "s", { ctrlKey: true });
  await until(() => $(".overlay .diff"), "Ctrl+S 触发保存前对比");
  p.key($(".dialog button"), "Escape");
  await until(() => !$(".overlay"), "关闭对比");

  // 分段控件 / 选项卡方向键
  const seg = $(".segmented");
  const segBtns = $$(".segment", seg);
  segBtns[0].focus();
  p.key(segBtns[0], "ArrowRight");
  await until(() => $(".overlay"), "表单里有未保存的修改，切到原文要确认");
  click(find(".overlay button", "放弃并切换"));
  await sleep(80);
  assert(/原文/.test(text($(".segmented [aria-checked=true]"))), "分段控件方向键切换到『原文』: " + text($(".segmented [aria-checked=true]")));

  // ------------------------------------------------------------ 窄屏
  section("G 窄屏");
  await p.reset(false);
  p = await boot({ narrow: true });
  ({ $, $$, text, find, click, input } = p);
  await until(() => $(".notice-card"), "窄屏列表");
  const split = $(".split");
  assert(p.document.documentElement.classList.contains("is-narrow"), "窄屏时 html 带 is-narrow");
  assert(split.getAttribute("data-show") === "list", "窄屏默认显示列表: " + split.getAttribute("data-show"));
  click(find(".notice-card", "群公约"));
  await until(() => split.getAttribute("data-show") === "detail", "点卡片后显示详情");
  await until(() => $(".pane-detail .md"), "详情加载");
  const back = $(".pane-detail .only-narrow");
  assert(back && back.getAttribute("aria-label") === "返回", "详情顶部有返回按钮");
  click(back);
  await until(() => split.getAttribute("data-show") === "list", "返回后显示列表");
  assert(p.document.activeElement && p.document.activeElement.getAttribute("aria-current") === "true", "返回后焦点回到选中项");
  click(find(".nav-item", "管理"));
  await sleep(40);
  assert(split.getAttribute("data-show") === "list", "切视图时先显示列表");
  click(find(".row-section", "概览"));
  await until(() => split.getAttribute("data-show") === "detail", "管理分区点击后显示详情");

  // ------------------------------------------------------------ 深色主题
  section("J 深色主题");
  p = await boot({ theme: "dark" });
  assert(p.document.documentElement.getAttribute("data-theme") === "dark", "初始 data-theme=dark（由宿主写入，页面不覆盖）");
  await until(() => p.$(".notice-card"), "深色下正常渲染");
  assert(p.uncaught().length === 0, "无未捕获错误: " + p.uncaught().map(String));
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});
