/* 运行时场景：文件详情里的移动（PLAN 3.12.2）。
 * 悄悄话 → 小本本默认选这段对话的机器人；小本本 → 悄悄话可按群名、昵称、备注搜索；
 * 同名冲突的三种选择（保留两份 / 覆盖 / 取消）都在这里点一遍。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, assert, until, sleep, section, summary } = require("../harness.js");

function visibleTargets(p) {
  return p.$$(".overlay .move-target").filter((button) => !button.hidden);
}

function realMoveCalls(p) {
  return p.calls.filter((call) => call.endpoint === "migrate/file" && call.body.dryRun === false);
}

async function openFile(p, view, conversation, file) {
  p.click(p.find(".nav-item", view));
  await until(() => p.$(".list-body"), "列表");
  if (conversation) {
    p.click(p.find(".row-conv", conversation));
    await until(() => p.find(".row", file), "对话里的文件行");
  }
  await until(() => p.find(".row", file), "文件行");
  p.click(p.find(".row", file));
  await until(() => p.find(".detail-head button", "编辑"), "文件详情的操作按钮");
}

async function openMoveDialog(p, buttonText) {
  p.click(p.find(".detail-head button", buttonText));
  await until(() => p.$(".overlay .move-targets"), "移动目标弹窗");
}

(async () => {
  await (await boot()).reset();
  let p = await boot();
  const { $, $$, text, find, click, input } = p;
  await until(() => $(".nav-item"), "导航出现");
  const overview = (await p.getJson("/overview")).data;
  const privateKey = overview.whispers.find((item) => item.name === "小红").key;

  section("M1 悄悄话 → 小本本：默认选中这段对话的机器人，确认框写明可见范围");
  await openFile(p, "悄悄话", "小红", "health.md");
  assert(!find(".detail-head button", "移动到悄悄话"), "悄悄话的文件没有『移动到悄悄话』");
  await openMoveDialog(p, "移动到小本本");
  const dialogText = text($(".overlay"));
  console.log("  移动弹窗:", dialogText.slice(0, 260));
  assert(/所有群聊和私聊都能读到/.test(dialogText), "弹窗写明这个机器人的所有群聊和私聊都能读到");
  const chosen = $$(".overlay .move-target").find((button) => button.getAttribute("aria-pressed") === "true");
  assert(chosen && /Bot-A/.test(text(chosen)), "默认选中这段对话所属的机器人 Bot-A: " + (chosen ? text(chosen) : "无"));
  click(find(".overlay button", "移动"));
  await until(() => $(".overlay .confirm-points"), "移动确认框");
  const confirmText = text($(".overlay"));
  assert(/所有群聊和私聊都能读到/.test(confirmText), "确认框写明可见范围");
  assert(/回收站/.test(confirmText), "确认框写明原文件进回收站");
  click(find(".overlay button", "移动"));
  await until(() => realMoveCalls(p).length === 1, "真的移动请求");
  assert(realMoveCalls(p)[0].body.to.platform === "Bot-A", "目标是 Bot-A 的小本本");
  assert(realMoveCalls(p)[0].body.conflict === "overwrite", "没有冲突时按覆盖提交");
  await sleep(200);
  const moved = await p.peek("notebook", "Bot-A", "health.md");
  assert(moved.content && /睡得不好/.test(moved.content), "小本本里有搬过去的内容");
  const gone = await p.peek("whisper", privateKey, "health.md");
  assert(!gone.content, "悄悄话里原文件已经移走");

  section("M2 小本本 → 悄悄话：按昵称、群名、备注搜索目标");
  const seeded = await p.bridge.apiPost("file/save", {
    scope: "whisper",
    key: privateKey,
    file: "plans.md",
    mode: "fields",
    type: "note",
    description: "目标里的旧计划",
    body: "目标里的旧内容",
  });
  assert(seeded && seeded.file === "plans.md", "悄悄话里先放一份同名文件");
  await openFile(p, "小本本", null, "plans.md");
  await openMoveDialog(p, "移动到悄悄话");
  const search = $(".overlay input[type=search]");
  assert(search, "有目标搜索框");
  input(search, "小红");
  await sleep(30);
  assert(visibleTargets(p).length === 1 && /小红/.test(text(visibleTargets(p)[0])), "按昵称搜索");
  input(search, "合成测试群");
  await sleep(30);
  assert(visibleTargets(p).length === 1 && /产品群|合成测试群/.test(text(visibleTargets(p)[0])), "按群名搜索");
  input(search, "产品群");
  await sleep(30);
  assert(visibleTargets(p).length === 1 && /产品群/.test(text(visibleTargets(p)[0])), "按备注搜索");
  input(search, "小红");
  await sleep(30);
  click(visibleTargets(p)[0]);
  assert(/只有这段对话能读到/.test(text($(".overlay"))), "弹窗写明只有这段对话能读到");
  click(find(".overlay button", "移动"));
  await until(() => find(".overlay button", "保留两份"), "同名冲突的三种选择");
  const conflictText = text($(".overlay"));
  console.log("  冲突提示:", conflictText.slice(0, 320));
  assert(/目标里已经有 plans\.md/.test(conflictText), "写明冲突的文件名");
  assert(find(".overlay button", "取消") && find(".overlay button", "保留两份") && find(".overlay button", "覆盖"), "三种选择都在");
  assert(/plans-2\.md/.test(conflictText), "保留两份写明新文件名 plans-2.md");
  assert(/历史版本/.test(conflictText), "覆盖写明旧版进历史");
  assert(/只有这段对话能读到/.test(conflictText), "冲突框也写明可见范围");
  const beforeCancel = realMoveCalls(p).length;
  click(find(".overlay button", "取消"));
  await until(() => !$(".overlay"), "取消后关闭");
  assert(realMoveCalls(p).length === beforeCancel, "取消没有发真的移动请求");
  assert((await p.peek("notebook", "Bot-A", "plans.md")).content, "取消后原文件还在");

  section("M3 同名冲突：保留两份");
  await openFile(p, "小本本", null, "plans.md");
  await openMoveDialog(p, "移动到悄悄话");
  input($(".overlay input[type=search]"), "小红");
  await sleep(30);
  click(visibleTargets(p)[0]);
  click(find(".overlay button", "移动"));
  await until(() => find(".overlay button", "保留两份"), "冲突框");
  click(find(".overlay button", "保留两份"));
  await until(() => realMoveCalls(p).length === beforeCancel + 1, "保留两份的真实请求");
  assert(realMoveCalls(p)[beforeCancel].body.conflict === "keep", "按保留两份提交");
  await sleep(200);
  const kept = await p.peek("whisper", privateKey, "plans-2.md");
  assert(kept.content && /本周计划/.test(kept.content), "新名字下是搬过去的内容");
  const original = await p.peek("whisper", privateKey, "plans.md");
  assert(original.content && /目标里的旧内容/.test(original.content), "目标里的原文件不动");
  assert(!(await p.peek("notebook", "Bot-A", "plans.md")).content, "来源已经移走");

  section("M4 同名冲突：覆盖（旧版进历史）");
  await p.bridge.apiPost("file/save", {
    scope: "whisper",
    key: privateKey,
    file: "keep-calm.md",
    mode: "fields",
    type: "agreement",
    description: "目标里的旧约定",
    body: "目标里的旧约定内容",
  });
  await openFile(p, "小本本", null, "keep-calm.md");
  await openMoveDialog(p, "移动到悄悄话");
  input($(".overlay input[type=search]"), "小红");
  await sleep(30);
  click(visibleTargets(p)[0]);
  click(find(".overlay button", "移动"));
  await until(() => find(".overlay button", "覆盖"), "冲突框");
  click(find(".overlay button", "覆盖"));
  await until(() => realMoveCalls(p).length === beforeCancel + 2, "覆盖的真实请求");
  assert(realMoveCalls(p)[beforeCancel + 1].body.conflict === "overwrite", "按覆盖提交");
  await sleep(250);
  const replaced = await p.peek("whisper", privateKey, "keep-calm.md");
  assert(replaced.content && /回答不超过三句/.test(replaced.content), "目标里换成了搬来的内容");
  const versions = (await p.getJson("/history/versions?scope=whisper&key=" + encodeURIComponent(privateKey) + "&file=keep-calm.md")).data.versions;
  assert(versions.length >= 1, "覆盖留下了历史版本");
  const historyText = (await p.getJson("/history/file?scope=whisper&key=" + encodeURIComponent(privateKey) + "&file=keep-calm.md&version=" + encodeURIComponent(versions[0].version))).data.content;
  assert(/目标里的旧约定内容/.test(historyText), "历史版本里是覆盖前的内容");

  section("M5 留言板不参与移动");
  p.click(p.find(".nav-item", "留言板"));
  await until(() => p.find(".row", "welcome.md") || p.find(".notice-card", "欢迎"), "留言板告示");
  const notice = p.find(".notice-card", "欢迎") || p.find(".row", "welcome.md");
  click(notice);
  await until(() => p.find(".detail-head button", "编辑"), "告示详情");
  assert(!find(".detail-head button", "移动到小本本") && !find(".detail-head button", "移动到悄悄话"), "留言板没有移动入口");

  console.log("\nuncaught:", p.uncaught().map((e) => String((e && e.stack) || e).slice(0, 300)));
  assert(p.uncaught().length === 0, "没有未捕获错误");
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});
