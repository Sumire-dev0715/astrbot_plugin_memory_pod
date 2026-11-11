/* 运行时场景：悄悄话、上下文面板，以及管理页的概览、整理、备份与迁移。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, assert, until, sleep, section, summary } = require("../harness.js");

(async () => {
  await (await boot()).reset();
  const p = await boot();
  const { $, $$, text, find, click, input, change } = p;
  await until(() => $(".nav-item"), "导航出现");

  section("C1 悄悄话：对话列表");
  click(find(".nav-item", "悄悄话"));
  await until(() => $$(".row-conv").length >= 3, "对话行出现");
  const convs = $$(".row-conv");
  console.log("  对话:", convs.map((r) => text(r)));
  assert(find(".row-conv", "产品群"), "有备注的对话显示备注『产品群』");
  assert(find(".row-conv", "小红"), "私聊显示昵称");
  const group = find(".row-conv", "产品群");
  assert(/合成测试群/.test(text(group)), "有备注时副标题仍显示群名: " + text(group));
  // 筛选
  const search = $("[data-search]");
  input(search, "合成测试群");
  await sleep(20);
  assert($$(".row-conv").length === 1, "按群名搜索只剩 1 个");
  input(search, "");
  click(find(".chips button", "私聊"));
  await sleep(20);
  assert($$(".row-conv").length === 1 && /小红/.test(text($(".row-conv"))), "按私聊筛选");
  click(find(".chips button", "全部"));
  click(find(".chip", "有悄悄话"));
  await sleep(20);
  assert($$(".row-conv").length === 2, "只看有悄悄话的: " + $$(".row-conv").length);
  click(find(".chip", "有悄悄话"));

  section("C2 打开一段对话");
  click(find(".row-conv", "产品群"));
  await until(() => find(".list-head", "全部对话"), "进入这段对话的文件列表");
  await until(() => find(".row", "group-secret.md"), "文件行");
  assert(/产品群/.test(text($(".list-head"))), "标题显示备注");
  console.log("  详情:", text($(".pane-detail")).slice(0, 200));
  assert($(".pane-detail") && /对话信息|产品群/.test(text($(".pane-detail"))), "右侧默认是对话信息");
  // 备注
  const remark = $$(".pane-detail input").find((i) => i.getAttribute("maxlength") === "64");
  assert(remark && remark.value === "产品群", "备注输入框带现有值");
  input(remark, "产品讨论群");
  click(find(".pane-detail button", "保存备注"));
  await until(() => p.lastCall("whisper/remark"), "whisper/remark");
  assert(p.lastCall("whisper/remark").body.remark === "产品讨论群" && p.lastCall("whisper/remark").body.key, "备注 payload");
  await sleep(150);
  assert(/产品讨论群/.test(text($(".list-head"))), "改备注后标题更新: " + text($(".list-head")));
  // 离开未保存的备注会提示
  input($$(".pane-detail input").find((i) => i.getAttribute("maxlength") === "64"), "改了没存");
  click(find(".back-link", "全部对话"));
  await until(() => $(".overlay"), "未保存备注离开时确认");
  click(find(".overlay button", "取消"));
  await until(() => !$(".overlay"), "取消离开");
  // 清除数据的确认
  click(find(".danger-zone button", "清除"));
  await until(() => $(".overlay"), "清除确认");
  const pt = text($(".overlay"));
  console.log("  清除确认:", pt.slice(0, 300));
  assert(/不能再查看或恢复|无法/.test(pt) && /\.trash-scopes/.test(pt), "清除确认写清后果与取回位置");
  click(find(".overlay button", "取消"));
  await until(() => !$(".overlay"), "取消清除");
  // 新建悄悄话
  click(find(".list-head button", "新建"));
  await until(() => $(".overlay"), "备注有未保存修改，新建前要确认");
  click(find(".overlay button", "放弃修改"));
  await until(() => $(".editor input"), "编辑器");
  click(find(".detail-head button", "取消"));
  await sleep(50);

  section("D 上下文面板");
  // 悄悄话详情里的「查看上下文」按钮已去掉，改从顶栏「上下文」进
  click(find(".nav-item", "上下文"));
  await sleep(60);
  // 上下文列表按备注优先显示（"产品讨论群"），这里只要匹配到这一段对话
  click(find(".row-conv", "产品"));
  await until(() => $(".stackbar"), "上下文面板的比例横条");
  await sleep(100);
  const ctxText = text($(".pane-detail"));
  console.log("  上下文:", ctxText.slice(0, 500));
  assert($$(".stackbar .seg").length === 6, "横条 6 段");
  assert(/8,?140/.test(ctxText), "总估算 8140");
  assert(/缓存命中/.test(ctxText) && /命中率 7\d(\.\d)?%/.test(ctxText), "缓存命中率 6000/8400≈71%: " + (ctxText.match(/命中率[^ ]*/) || [])[0]);
  assert(/用户小明计划下周评审/.test(ctxText), "显示 AstrBot 压缩摘要");
  assert(/只读/.test(ctxText), "只读提示");
  assert(!find(".pane-detail button", "清除") && !find(".pane-detail button", "重置"), "上下文面板没有任何改动上下文的按钮");
  const widths = $$(".stackbar .seg").map((s) => s.style.width);
  console.log("  横条宽度:", widths);
  assert(Math.abs(widths.reduce((a, w) => a + parseFloat(w), 0) - 100) < 0.01, "横条总宽 100%");
  // 另一段没有统计
  click(find(".nav-item", "上下文"));
  await sleep(60);
  click(find(".row-conv", "小红"));
  await until(() => /还没有请求记录/.test(text($(".pane-detail"))), "无记录时的空状态");

  section("E1 管理：概览");
  click(find(".nav-item", "管理"));
  await sleep(60);
  const sections = $$(".row-section").map((r) => text(r));
  console.log("  管理分区:", sections);
  assert(sections.length === 4, "管理四个分区");
  click(find(".row-section", "概览"));
  await sleep(60);
  console.log("  概览:", text($(".pane-detail")).slice(0, 300));
  assert($$(".stat").length === 3, "三个统计卡");

  section("E2 管理：整理一处（预览→勾选→确认→应用→撤销）");
  click(find(".row-section", "整理"));
  await until(() => find(".pane-detail button", "整理全部记忆"), "整理主页");
  const scopeRowsEl = $$(".row-scope");
  console.log("  可整理的处:", scopeRowsEl.map((r) => text(r)));
  click(find(".row-scope", "小本本 · Bot-A"));
  await until(() => find(".pane-detail button", "开始整理"), "整理一处面板");
  click(find(".pane-detail button", "开始整理"));
  await until(() => $(".plan"), "方案预览出现", 6000);
  const ops = $$(".ops .op");
  console.log("  方案项:", ops.map((o) => text(o).slice(0, 60)));
  assert(ops.length === 3, "三项方案: " + ops.length);
  // 展开一项看对比
  click(find(".ops button", "查看改动"));
  await until(() => $(".op-change .diff") || $(".op-change .md"), "展开改动", 4000);
  // 取消删除项（方案顺序由合并逻辑决定，按类型找更稳）
  const deleteBox = $(".ops .op-delete input[type=checkbox]");
  assert(deleteBox, "方案里有删除项");
  click(deleteBox);
  await sleep(20);
  assert(/应用选中的 2 项/.test(text($(".sticky-actions"))), "按钮文案随勾选变化: " + text($(".sticky-actions")));
  // 离开会提示
  click(find(".row-section", "概览"));
  await until(() => $(".overlay"), "有方案时离开要确认");
  click(find(".overlay button", "取消"));
  await until(() => !$(".overlay"), "留下");
  click(find(".sticky-actions button", "应用选中的 2 项"));
  await until(() => $(".overlay"), "应用确认");
  const at = text($(".overlay"));
  console.log("  应用确认:", at.slice(0, 260));
  assert(/新建 1 个、改写 1 个、自动修复 0 个、删除 0 个/.test(at) && /撤销/.test(at), "应用确认写清数量和可撤销");
  click(find(".overlay button", "应用"));
  await until(() => p.lastCall("consolidate/apply"), "consolidate/apply");
  const ap = p.lastCall("consolidate/apply");
  assert(ap.body.operations.length === 2 && ap.body.operations[0].baseMtime !== undefined, "原样回传方案项: " + JSON.stringify(ap.body.operations[0]).slice(0, 160));
  await until(() => /整理已应用/.test(text($(".pane-detail"))), "应用完成", 6000);
  await sleep(150);
  assert(find(".run", "撤销") || find(".runs button", "撤销"), "整理记录里出现可撤销的记录");
  click(find(".runs button", "撤销"));
  await until(() => $(".overlay"), "撤销确认");
  click(find(".overlay button", "撤销"));
  await until(() => p.lastCall("tidy/undo"), "tidy/undo");
  await sleep(200);

  section("E3 管理：整理全部");
  click(find(".row-section", "整理"));
  await sleep(40);
  click(find(".pane-detail button", "整理全部记忆"));
  await until(() => find(".pane-detail button", "整理全部记忆") && /共 \d+ 处/.test(text($(".pane-detail"))), "整理全部页");
  click(find(".pane-detail .actions button", "整理全部记忆"));
  await until(() => $(".sticky-actions") || /不需要整理|每一处都不需要/.test(text($(".pane-detail"))), "全部预览完成", 10000);
  console.log("  整理全部:", text($(".pane-detail")).slice(0, 300));
  const groups = $$(".pane-detail .card").length;
  assert(groups >= 1, "有分组卡片");

  section("E4 管理：回收站与已遗忘总入口");
  click(find(".row-section", "回收站与已遗忘"));
  await until(() => $(".overlay"), "整理全部的方案还没应用，离开要确认");
  click(find(".overlay button", "放弃修改"));
  await sleep(60);
  assert($$(".row-scope").length >= 3, "列出各处: " + $$(".row-scope").length);
  click(find(".row-scope", "小本本 · Bot-A"));
  await until(() => find("[role=tab]", "回收站"), "维护面板");
  assert(find(".back-link", "返回"), "有返回按钮");

  section("E5 管理：备份与迁移");
  click(find(".row-section", "备份与迁移"));
  await until(() => find("[role=tab]", "导出"), "备份页");
  await sleep(40);
  console.log("  导出页:", text($(".pane-detail")).slice(0, 300));
  const boxes = $$(".checklist input[type=checkbox]");
  assert(boxes.length >= 5, "导出可选各处: " + boxes.length);
  click(find(".pane-detail button", "导出备份"));
  await until(() => p.saved.length === 1, "触发保存");
  const exportCall = p.lastCall("export/file");
  assert(exportCall && exportCall.params.token && /^memory-backup-/.test(p.saved[0].filename), "保存参数: " + JSON.stringify([exportCall, p.saved[0].filename]));
  assert(p.downloads.length === 0, "先经 apiGet 取回再在页面里保存，没有走面板代下载");
  // 取到备份内容，用于导入
  const exp = p.lastCall("export/prepare");
  assert(exp.body.scopes.length >= 5 && exp.body.includeForgotten === true, "export/prepare payload");

  click(find("[role=tab]", "导入"));
  await sleep(30);
  const bundle = JSON.parse(await p.saved[0].blob.text());
  assert(bundle.format === "memory-pod-backup", "保存下来的是备份内容，格式正确");
  const fileInput = $("input[type=file]");
  p.setFile(fileInput, "b.json", JSON.stringify(bundle));
  change(fileInput);
  await until(() => $(".checklist"), "导入预览", 6000);
  console.log("  导入预览:", text($(".pane-detail")).slice(0, 360));
  assert(p.lastCall("import/preview").body.bundle, "import/preview 带 bundle");
  // 改冲突策略要重新预览
  const policySel = $(".pane-detail select");
  change(policySel, "overwrite");
  await until(() => p.lastCall("import/preview").body.token, "改策略后重新预览（只传 token）");
  assert(p.lastCall("import/preview").body.policy === "overwrite", "重新预览用新策略");
  click(find(".pane-detail button", "导入选中的地方"));
  await until(() => $(".overlay"), "导入确认");
  console.log("  导入确认:", text($(".overlay")).slice(0, 300));
  assert(/覆盖/.test(text($(".overlay"))), "导入确认写清覆盖");
  click(find(".overlay button", "取消"));
  await until(() => !$(".overlay"), "取消导入");
  // 坏文件
  p.setFile(fileInput, "bad.json", "不是 JSON");
  change(fileInput);
  await until(() => /不是有效的记忆备份/.test(text($("#toasts"))), "坏备份提示");

  click(find("[role=tab]", "迁移一处"));
  await sleep(40);
  const selects = $$(".pane-detail select");
  console.log("  迁移页 select 数:", selects.length);
  click(find(".pane-detail button", "预览"));
  await until(() => $(".report"), "迁移预览", 6000);
  console.log("  迁移预览:", text($(".pane-detail .report")).slice(0, 200));

  click(find("[role=tab]", "平台改名"));
  await sleep(100);
  const platInputs = $$(".pane-detail input.mono");
  input(platInputs[0], "Bot-A");
  input(platInputs[1], "Bot-A2");
  click(find(".pane-detail button", "预览"));
  await until(() => $(".report"), "平台改名预览", 6000);
  console.log("  平台改名预览:", text($(".pane-detail")).slice(0, 360));
  click(find(".pane-detail button", "开始迁移"));
  await until(() => $(".overlay"), "平台改名确认");
  console.log("  平台改名确认:", text($(".overlay")).slice(0, 300));
  click(find(".overlay button", "取消"));
  await until(() => !$(".overlay"), "取消");

  console.log("\nuncaught:", p.uncaught().map((e) => String(e && e.stack || e).slice(0, 300)));
  assert(p.uncaught().length === 0, "没有未捕获错误");
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});
