/* 运行时场景：顶部「刷新」要真的刷新，失败要看得见，总览没读到时不能当成真的空数据。
 * 这些场景对应一次代码审查提出的问题：刷新后右侧的概览统计、对话信息、索引页还是旧的；
 * 重新加载失败被吞掉、照样提示"已刷新"；总览读取失败时小本本写「还没有小本本」、概览全是 0。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, backend, assert, until, sleep, section, summary } = require("../harness.js");

const toasts = (p) => p.$$("#toasts .toast").map((el) => p.text(el));
const refreshButton = (p) => p.$(".topbar-actions button");

/** 点一下；找不到就记一次失败而不是整个场景崩掉，后面的检查还能接着跑 */
function tap(p, el, label) {
  if (assert(el, "找得到：" + label)) {
    p.click(el);
  }
}

async function ready(p) {
  await until(() => p.$(".nav-item") && p.$(".notice-card"), "留言板首屏");
  return p;
}

async function whisperKey(remark) {
  const ov = (await backend("/overview")).data;
  return ov.whispers.find((item) => item.remark === remark).key;
}

(async () => {
  // ------------------------------------------------------------ 刷新要真的刷新
  section("R1 管理 → 概览：服务端多了告示，点刷新后统计跟着变");
  let p = await ready(await boot());
  p.click(p.find(".nav-item", "管理"));
  await until(() => p.$(".pane-detail .stat"), "概览统计");
  const stats = () => p.$$(".pane-detail .stat .stat-value").map((el) => p.text(el));
  const before = stats();
  const published = await backend("/board/publish", { signature: "测试", description: "刷新测试告示", body: "正文" });
  assert(published.status === "ok", "后端发布成功: " + JSON.stringify(published).slice(0, 120));
  p.click(refreshButton(p));
  await until(() => toasts(p).some((x) => /已刷新/.test(x)), "刷新完成的提示");
  const after = stats();
  assert(Number(after[0]) === Number(before[0]) + 1, `概览里的留言板告示数 ${before[0]} → ${after[0]}（应当 +1）`);
  assert(p.text(p.$(".nav-count [aria-hidden]")) === after[0], "导航角标和概览数字一致");

  section("R2 悄悄话 → 对话信息：服务端多了一个文件，点刷新后文件数跟着变");
  p = await ready(await boot());
  const gkey = await whisperKey("产品群");
  p.click(p.find(".nav-item", "悄悄话"));
  await until(() => p.find(".row-conv", "产品群"), "对话列表");
  p.click(p.find(".row-conv", "产品群"));
  await until(() => p.find(".meta-item dt", "悄悄话"), "对话信息");
  const filesFact = () => p.text(p.find(".meta-item", "悄悄话").querySelector("dd"));
  const factBefore = Number(filesFact());
  const saved = await backend("/file/save", { scope: "whisper", key: gkey, file: "refresh-extra.md", mode: "fields", type: "note", description: "刷新测试", body: "正文" });
  assert(saved.status === "ok", "后端写入悄悄话成功: " + JSON.stringify(saved).slice(0, 120));
  p.click(refreshButton(p));
  await until(() => toasts(p).some((x) => /已刷新/.test(x)), "刷新完成的提示");
  assert(Number(filesFact()) === factBefore + 1, `对话信息里的文件数 ${factBefore} → ${filesFact()}（应当 +1）`);

  section("R3 小本本 → 索引：服务端把索引修好后，点刷新后健康提示跟着变；有未保存的草稿时不动草稿");
  // 先把索引改成缺项，再打开页面（页面读到的就是缺项的索引）
  const ruined = await backend("/__ruin_index?scope=notebook&key=Bot-A");
  assert(ruined.ok, "后端改坏索引: " + JSON.stringify(ruined).slice(0, 120));
  p = await ready(await boot());
  p.click(p.find(".nav-item", "小本本"));
  await until(() => p.find(".row", "plans.md"), "小本本文件列表");
  p.click(p.find(".foot-link", "索引"));
  await until(() => p.find(".pane-detail .notice", "没有出现在索引里"), "索引页提示缺项");
  await backend("/index/rebuild", { scope: "notebook", key: "Bot-A" });
  p.click(refreshButton(p));
  await until(() => p.find(".pane-detail .notice", "索引和记忆文件一致"), "刷新后索引页变成一致");
  assert(!p.find(".pane-detail .notice", "没有出现在索引里"), "缺项提示消失");
  const area = p.$(".pane-detail pre");
  assert(area && /plans\.md/.test(p.text(area)), "只读文本里是重建后的索引");
  assert(!p.$(".pane-detail textarea") && !p.find(".detail-head button", "保存"), "索引没有编辑入口");

  // ------------------------------------------------------------ 失败要看得见
  section("R4 总览重新加载失败：提示失败，横幅说明数字可能过期，重试后恢复");
  p = await ready(await boot());
  await p.fail("overview", 1);
  p.click(refreshButton(p));
  await until(() => p.$$("#toasts .toast.is-error").length > 0, "刷新失败的提示");
  assert(!toasts(p).some((x) => /已刷新/.test(x)), "失败时不再说「已刷新」: " + toasts(p).join("|"));
  await until(() => p.find(".banner-area .notice", "总览刷新失败"), "横幅说明总览刷新失败");
  assert(/模拟的后端错误/.test(p.text(p.$(".banner-area"))), "横幅带着失败原因");
  assert(p.$(".notice-card"), "页面上仍是上次读到的内容");
  tap(p, p.find(".banner-area button", "重试"), "横幅里的重试按钮");
  await until(() => p.$(".banner-area").hidden, "重试成功后横幅消失");

  section("R5 当前作用域重新加载失败：提示失败，列表上方说明显示的是旧内容");
  p = await ready(await boot());
  await p.fail("scope", 1);
  p.click(refreshButton(p));
  await until(() => p.$$("#toasts .toast.is-error").length > 0, "刷新失败的提示");
  assert(!toasts(p).some((x) => /已刷新/.test(x)), "失败时不再说「已刷新」");
  await until(() => p.find(".banner-area .notice", "上次读到的内容"), "横幅说明是上次读到的内容");
  assert(p.$(".notice-card"), "列表仍显示旧告示");
  tap(p, p.find(".banner-area button", "重试"), "横幅里的重试按钮");
  await until(() => p.$(".banner-area").hidden, "重试成功后横幅消失");

  section("R6 文件详情重新加载失败：详情里说明显示的是旧内容");
  p = await ready(await boot());
  p.click(p.find(".notice-card", "欢迎新同学"));
  await until(() => p.$(".pane-detail .md"), "详情已加载");
  await p.fail("file", 1);
  p.click(refreshButton(p));
  await until(() => p.$$("#toasts .toast.is-error").length > 0, "刷新失败的提示");
  await until(() => p.find(".pane-detail .notice", "上次读到的内容"), "详情里的过期提示");
  assert(p.$(".pane-detail .md"), "详情里仍是旧正文");
  p.click(refreshButton(p));
  await until(() => toasts(p).some((x) => /已刷新/.test(x)), "再刷新一次成功");
  assert(!p.find(".pane-detail .notice", "上次读到的内容"), "过期提示消失");

  section("R7 上下文面板重新加载失败：面板里说明显示的是旧内容");
  p = await ready(await boot());
  p.click(p.find(".nav-item", "上下文"));
  await until(() => p.find(".row-conv", "产品群"), "对话列表");
  p.click(p.find(".row-conv", "产品群"));
  await until(() => p.$(".pane-detail .big-number"), "上下文面板已加载");
  await p.fail("whisper/context", 1);
  p.click(refreshButton(p));
  await until(() => p.$$("#toasts .toast.is-error").length > 0, "刷新失败的提示");
  await until(() => p.find(".pane-detail .notice", "上次读到的内容"), "面板里的过期提示");
  assert(p.$(".pane-detail .big-number"), "面板里仍是旧数据");

  // ------------------------------------------------------------ 总览没读到时
  section("R8 总览还在加载：小本本与概览显示加载中，不是「还没有小本本」/ 全 0");
  p = await boot({ hold: ["overview"] });
  await until(() => p.$(".nav-item"), "导航已画出来");
  p.click(p.find(".nav-item", "小本本"));
  await sleep(60);
  assert(p.$(".pane-list .state-loading"), "小本本列表转圈: " + p.text(p.$(".pane-list")).slice(0, 80));
  assert(!/还没有小本本/.test(p.text(p.$(".pane-list"))), "列表里没有「还没有小本本」");
  assert(!/还没有小本本/.test(p.text(p.$(".pane-detail"))), "详情里没有「还没有小本本」: " + p.text(p.$(".pane-detail")).slice(0, 80));
  p.click(p.find(".nav-item", "管理"));
  await sleep(60);
  assert(p.$(".pane-detail .state-loading"), "概览转圈: " + p.text(p.$(".pane-detail")).slice(0, 80));
  assert(p.$$(".pane-detail .stat").length === 0, "概览里没有 0 值统计卡");
  p.release("overview");
  await until(() => p.$$(".pane-detail .stat").length > 0, "总览到了以后概览出现统计");
  assert(p.text(p.$(".pane-detail .stat .stat-value")) !== "0", "统计是真数字");
  p.click(p.find(".nav-item", "小本本"));
  await until(() => p.find(".row", "plans.md"), "总览到了以后小本本有文件");

  section("R9 总览读取失败：小本本与概览显示错误和重试，重试后恢复");
  p = await boot();
  await p.reset(false);
  await p.fail("overview", 1);
  p = await boot();
  await until(() => p.find(".banner-area .notice", "读取记忆总览失败"), "横幅");
  p.click(p.find(".nav-item", "小本本"));
  await sleep(60);
  assert(p.$(".pane-list .state-error"), "小本本列表是错误态: " + p.text(p.$(".pane-list")).slice(0, 80));
  assert(!/还没有小本本/.test(p.text(p.$(".pane-list")) + p.text(p.$(".pane-detail"))), "没有「还没有小本本」");
  p.click(p.find(".nav-item", "管理"));
  await sleep(60);
  assert(p.$(".pane-detail .state-error"), "概览是错误态: " + p.text(p.$(".pane-detail")).slice(0, 80));
  assert(p.$$(".pane-detail .stat").length === 0, "概览里没有 0 值统计卡");
  tap(p, p.find(".pane-detail .state-error button", "重试"), "概览错误态里的重试按钮");
  await until(() => p.$$(".pane-detail .stat").length > 0, "重试后概览出现统计");

  assert(p.uncaught().length === 0, "没有未捕获错误: " + p.uncaught().map(String));
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});
