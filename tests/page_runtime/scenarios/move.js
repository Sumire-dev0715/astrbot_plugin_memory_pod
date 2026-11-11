/* 运行时场景：文件详情里的移动（PLAN 3.12.2）。
 * 悄悄话 → 小本本默认选这段对话的机器人；小本本 → 悄悄话可按群名、昵称、备注搜索；
 * 同名冲突的三种选择（保留两份 / 覆盖 / 取消）都在这里点一遍；接口报错要有提示。 */
"use strict";
const { boot, backend, assert, until, sleep, section, summary } = require("../harness.js");

const errorToasts = (p) => p.$$("#toasts .toast.is-error").map((el) => p.text(el));

function visibleTargets(p) {
  return p.$$(".overlay .move-target").filter((button) => !button.hidden);
}

function realMoveCalls(p) {
  return p.calls.filter((call) => call.endpoint === "migrate/file" && call.body.dryRun === false);
}

/** 等一次真正的移动请求落地，核对冲突方式，再等弹窗关掉 */
async function expectMove(p, before, conflict, file) {
  await until(() => realMoveCalls(p).length === before + 1, "真的移动请求（" + conflict + "）");
  const call = realMoveCalls(p)[before];
  assert(call.body.conflict === conflict, "按 " + conflict + " 提交，实际 " + call.body.conflict);
  if (file) {
    assert(call.body.file === file, "移动的是 " + file);
  }
  await until(() => !p.$(".overlay"), "移动完成后弹窗关闭");
  await sleep(150);
}

async function openFile(p, view, conversation, file) {
  p.click(p.find(".nav-item", view));
  await until(() => p.$(".list-body"), "列表");
  if (conversation) {
    // 悄悄话视图选了某段对话后，左栏是文件列表；先回「全部对话」再挑
    const back = p.$(".pane-list .back-link");
    if (back) {
      p.click(back);
      await until(() => p.$(".row-conv"), "全部对话列表");
    }
    await until(() => p.find(".row-conv", conversation), "对话行");
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

async function pickTarget(p, query) {
  const search = p.$(".overlay input[type=search]");
  assert(search, "有目标搜索框");
  p.input(search, query);
  await sleep(30);
  assert(visibleTargets(p).length === 1, "搜索 " + query + " 只剩一个目标: " + visibleTargets(p).length);
  p.click(visibleTargets(p)[0]);
}

(async () => {
  await (await boot()).reset();
  const initial = (await backend("/overview")).data;
  const privateKey = initial.whispers.find((item) => item.name === "小红").key;
  // 造一段 Bot-C 的对话（没有小本本目录），给"拿不到机器人清单"的场景用
  const seededSession = await backend("/migrate/scope", {
    from: { scope: "whisper", key: privateKey },
    to: { scope: "whisper", umo: "Bot-C:FriendMessage:30003" },
    files: ["health.md"],
    mode: "copy",
    dryRun: false,
  });
  assert(seededSession.status === "ok", "造一段 Bot-C 的对话: " + JSON.stringify(seededSession).slice(0, 160));
  let p = await boot();
  const { $, $$, text, find, click, input } = p;
  await until(() => $(".nav-item"), "导航出现");

  section("M0 拿不到机器人清单时，这段对话自己的机器人仍是默认目标");
  await openFile(p, "悄悄话", "30003", "health.md");
  await p.fail("migrate/info", 1);
  await openMoveDialog(p, "移动到小本本");
  const fallback = $$(".overlay .move-target");
  assert(fallback.length >= 1, "列表里至少有一个目标");
  const botC = fallback.find((button) => /Bot-C/.test(text(button)));
  assert(botC, "这段对话所属的 Bot-C 出现在目标里（它还没有小本本目录）");
  assert(botC && botC.getAttribute("aria-pressed") === "true", "Bot-C 是默认选中");
  click(find(".overlay button", "取消"));
  await until(() => !$(".overlay"), "取消 M0");

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
  const beforeM1 = realMoveCalls(p).length;
  click(find(".overlay button", "移动"));
  await expectMove(p, beforeM1, "overwrite", "health.md");
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
  await pickTarget(p, "小红");
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

  section("M3 同名冲突：保留两份（第一次真实请求失败，重试成功）");
  await openFile(p, "小本本", null, "plans.md");
  await openMoveDialog(p, "移动到悄悄话");
  await pickTarget(p, "小红");
  click(find(".overlay button", "移动"));
  await until(() => find(".overlay button", "保留两份"), "冲突框");
  // 真正的请求失败时：弹出错误提示，冲突框留着可以重试，不会变成未处理的拒绝
  const beforeFail = realMoveCalls(p).length;
  const errorsBefore = errorToasts(p).length;
  await p.fail("migrate/file", 1);
  click(find(".overlay button", "保留两份"));
  await until(() => errorToasts(p).length > errorsBefore, "保留两份失败时的提示");
  assert(errorToasts(p).some((x) => /模拟的后端错误/.test(x)), "提示带着失败原因");
  assert(find(".overlay button", "保留两份") && find(".overlay button", "覆盖"), "失败后冲突框还在");
  assert(realMoveCalls(p).length === beforeFail + 1, "失败的那次请求记在调用里");
  assert((await p.peek("notebook", "Bot-A", "plans.md")).content, "失败后原文件还在");
  const beforeRetry = realMoveCalls(p).length;
  click(find(".overlay button", "保留两份"));
  await expectMove(p, beforeRetry, "keep", "plans.md");
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
  await pickTarget(p, "小红");
  click(find(".overlay button", "移动"));
  await until(() => find(".overlay button", "覆盖"), "冲突框");
  const beforeM4 = realMoveCalls(p).length;
  click(find(".overlay button", "覆盖"));
  await expectMove(p, beforeM4, "overwrite", "keep-calm.md");
  const replaced = await p.peek("whisper", privateKey, "keep-calm.md");
  assert(replaced.content && /回答不超过三句/.test(replaced.content), "目标里换成了搬来的内容");
  const versions = (await p.getJson("/history/versions?scope=whisper&key=" + encodeURIComponent(privateKey) + "&file=keep-calm.md")).data.versions;
  assert(versions.length >= 1, "覆盖留下了历史版本");
  const historyText = (await p.getJson("/history/file?scope=whisper&key=" + encodeURIComponent(privateKey) + "&file=keep-calm.md&version=" + encodeURIComponent(versions[0].version))).data.content;
  assert(/目标里的旧约定内容/.test(historyText), "历史版本里是覆盖前的内容");

  section("M5 目标里只有已遗忘的同名副本：覆盖走回收站，不进历史版本");
  await p.bridge.apiPost("file/save", {
    scope: "whisper",
    key: privateKey,
    file: "health.md",
    mode: "fields",
    type: "note",
    description: "目标里已遗忘的旧健康",
    body: "目标里已遗忘的旧内容",
  });
  const forgotten = await p.bridge.apiPost("file/forget", { scope: "whisper", key: privateKey, file: "health.md" });
  assert(forgotten && forgotten.file === "health.md", "目标里的同名副本已经遗忘");
  await openFile(p, "小本本", null, "health.md");
  await openMoveDialog(p, "移动到悄悄话");
  await pickTarget(p, "小红");
  click(find(".overlay button", "移动"));
  await until(() => find(".overlay button", "覆盖"), "冲突框");
  const forgottenText = text($(".overlay"));
  console.log("  遗忘区冲突提示:", forgottenText.slice(0, 320));
  assert(/已遗忘/.test(forgottenText) && /回收站/.test(forgottenText), "写明已遗忘的旧版进回收站");
  const beforeM5 = realMoveCalls(p).length;
  click(find(".overlay button", "覆盖"));
  await expectMove(p, beforeM5, "overwrite", "health.md");
  const forgottenTarget = await p.peek("whisper", privateKey, "health.md");
  assert(forgottenTarget.content && /小红的健康情况|睡得不好/.test(forgottenTarget.content), "目标里是在册的新内容");
  const forgottenVersions = (await p.getJson("/history/versions?scope=whisper&key=" + encodeURIComponent(privateKey) + "&file=health.md")).data.versions;
  assert(forgottenVersions.length === 0, "已遗忘的旧版没有历史版本");
  const trashList = (await p.getJson("/trash?scope=whisper&key=" + encodeURIComponent(privateKey))).data.entries;
  const oldCopy = trashList.find((item) => item.file === "health.md");
  assert(oldCopy, "已遗忘的旧版进了回收站");
  const oldCopyText = (await p.getJson("/trash/file?scope=whisper&key=" + encodeURIComponent(privateKey) + "&entry=" + encodeURIComponent(oldCopy.entry))).data.content;
  assert(/目标里已遗忘的旧内容/.test(oldCopyText), "回收站里是覆盖前的旧内容");

  section("M6 预演失败：提示原因，目标弹窗留着");
  await openFile(p, "小本本", null, "user-10001-hobby.md");
  await openMoveDialog(p, "移动到悄悄话");
  await pickTarget(p, "小红");
  const beforeDry = realMoveCalls(p).length;
  // 先清掉之前的错误提示，免得把旧提示当成这次失败
  p.$$("#toasts .toast-close").forEach((button) => button.click());
  await until(() => !p.$("#toasts .toast"), "清空旧提示");
  const cleanErrors = errorToasts(p).length;
  await p.fail("migrate/file", 2);
  click(find(".overlay button", "移动"));
  await until(() => errorToasts(p).length > cleanErrors, "预演失败时的提示");
  assert($(".overlay .move-targets"), "预演失败后目标弹窗还在");
  assert(realMoveCalls(p).length === beforeDry, "预演失败没有发真的移动请求");
  click(find(".overlay button", "取消"));
  await until(() => !$(".overlay"), "取消目标弹窗");

  section("M7 留言板不参与移动");
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
