/* 运行时场景：整理模型下拉框、后台任务状态、自动修复项，以及离开页面再回来不丢方案。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, backend, assert, until, sleep, section, summary } = require("../harness.js");

(async () => {
  const p = await boot();
  const { $, $$, text, find, click } = p;
  await until(() => $(".notice-card"), "留言板列表");

  section("T1 整理模型下拉框与后台任务");
  // 造一个带悬空链接的文件，整理方案里应该出现单独的「自动修复」项
  const saved = await backend("/file/save", {
    scope: "notebook",
    key: "Bot-A",
    file: "dangling.md",
    mode: "fields",
    type: "note",
    description: "悬空链接的合成文件",
    body: "见 [[missing-file]]",
  });
  assert(saved && saved.status === "ok", "造悬空文件: " + JSON.stringify(saved).slice(0, 200));

  const store = p.MP.store;
  const ref = store.ref("notebook", "Bot-A");
  await p.MP.app.openManage("tidy", { tidySid: store.sid(ref) });
  await until(() => find(".pane-detail button", "开始整理"), "整理一处面板");
  const select = $(".pane-detail select");
  assert(select, "有整理模型下拉框");
  console.log("  模型下拉框:", text(select));
  assert(/provider-a/.test(text(select)), "下拉框列出提供商标识与模型名");
  assert(/provider-a · model-a/.test(text($(".pane-detail"))), "显示当前整理模型");

  click(find(".pane-detail button", "开始整理"));
  await until(() => $(".plan"), "方案出现", 6000);
  assert(/方案已就绪/.test(text($(".pane-detail"))), "任务状态显示方案已就绪");
  assert(/共 \d+ 个文件/.test(text($(".pane-detail"))) && /分 \d+ 批/.test(text($(".pane-detail"))), "显示文件与批次");
  const fixOps = $$(".ops .op-fix");
  console.log("  自动修复项:", fixOps.map((el) => text(el)));
  assert(fixOps.length >= 1, "方案里有自动修复项");
  assert(/自动修复悬空链接/.test(text(fixOps[0])), "自动修复项写明原因");
  assert(/自动修复/.test(text($(".plan"))), "方案里有自动修复的说明");

  const before = text($(".sticky-actions button.btn-primary"));
  click($$(".ops input[type=checkbox]").find((box) => box.closest(".op-fix")));
  await sleep(20);
  const after = text($(".sticky-actions button.btn-primary"));
  assert(before !== after, "取消勾选自动修复后按钮数量变化: " + before + " -> " + after);

  section("T2 离开再回来，任务状态和方案还在");
  click(find(".nav-item", "留言板"));
  await until(() => $(".overlay"), "有方案时离开要确认");
  click(find(".overlay button", "取消"));
  await until(() => !$(".overlay"), "取消离开");
  click(find(".nav-item", "留言板"));
  await until(() => $(".overlay"), "再次离开要确认");
  click(find(".overlay button", "放弃修改"));
  await sleep(100);
  click(find(".nav-item", "管理"));
  await sleep(60);
  click(find(".row-section", "整理"));
  // 管理页回到上次的整理分区，直接显示原来那处的任务与方案
  await until(() => $(".plan"), "回来以后方案还在", 4000);
  assert(/方案已就绪/.test(text($(".pane-detail"))), "回来以后仍显示方案已就绪");
  assert(/小本本 · Bot-A/.test(text($(".pane-detail"))), "回来以后还是原来那一处");
  assert($$(".ops .op-fix").length >= 1, "回来以后自动修复项还在");

  // 相当于刷新浏览器：新开一个页面实例，从服务端重新读任务状态
  const p2 = await boot();
  await until(() => p2.$(".notice-card"), "新页面留言板列表");
  const ref2 = p2.MP.store.ref("notebook", "Bot-A");
  await p2.MP.app.openManage("tidy", { tidySid: p2.MP.store.sid(ref2) });
  await until(() => p2.$(".plan"), "刷新页面后方案还在", 4000);
  assert(/方案已就绪/.test(p2.text(p2.$(".pane-detail"))), "刷新页面后仍显示方案已就绪");
  assert(p2.$$(".ops .op-fix").length >= 1, "刷新页面后自动修复项还在");
  section("T3 没选整理模型时点整理会提示，不发起请求");
  await backend("/__clear_model");
  const p3 = await boot();
  await until(() => p3.$(".notice-card"), "清掉模型后的留言板列表");
  const ref3 = p3.MP.store.ref("notebook", "Bot-B");
  await p3.MP.app.openManage("tidy", { tidySid: p3.MP.store.sid(ref3) });
  await until(() => p3.find(".pane-detail button", "开始整理"), "清掉模型后的整理面板");
  p3.click(p3.find(".pane-detail button", "开始整理"));
  await until(() => /请先选择整理模型/.test(p3.text(p3.$("#toasts"))), "提示先选择整理模型");
  assert(!p3.lastCall("tidy/start"), "没选模型时不发起整理请求");

  assert(p.uncaught().length === 0 && p2.uncaught().length === 0 && p3.uncaught().length === 0, "无未捕获错误");
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});
