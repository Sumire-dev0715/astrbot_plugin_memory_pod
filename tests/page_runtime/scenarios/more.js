/* 运行时场景：其余的写操作：改名保存、笔记本选择器、已遗忘 / 回收站的删除、迁移与平台改名、清除对话数据、导入覆盖。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, assert, until, sleep, section, summary } = require("../harness.js");

(async () => {
  await (await boot()).reset();
  const p = await boot();
  const { $, $$, text, find, click, input, change } = p;
  await until(() => $(".nav-item"), "导航出现");

  section("K1 改名保存");
  click(find(".nav-item", "小本本"));
  await until(() => find(".row", "plans.md"), "plans.md");
  click(find(".row", "plans.md"));
  await until(() => /plans\.md/.test(text($("[data-detail-heading]"))) && $(".pane-detail .md"), "详情");
  click(find(".detail-actions button", "编辑"));
  await until(() => $(".editor input"), "编辑器");
  const nameInput = $$(".editor input").find((e) => e.value === "plans.md");
  input(nameInput, "plans-renamed");
  click(find(".detail-head button", "保存"));
  await until(() => $(".overlay .diff"), "保存前对比");
  assert(/改名/.test(text($(".overlay"))) && /plans\.md/.test(text($(".overlay"))), "改名时对比框里有改名提示: " + text($(".overlay")).slice(0, 120));
  click(find(".overlay button", "保存"));
  await until(() => p.lastCall("file/save"), "file/save");
  const rn = p.lastCall("file/save");
  assert(rn.body.file === "plans-renamed.md" && rn.body.oldFile === "plans.md", "改名 payload: " + JSON.stringify({ f: rn.body.file, o: rn.body.oldFile }));
  await until(() => /plans-renamed\.md/.test(text($("[data-detail-heading]"))), "改名后打开新文件");
  const old = await p.peek("notebook", "Bot-A", "plans.md");
  const neu = await p.peek("notebook", "Bot-A", "plans-renamed.md");
  assert(!old.content && neu.content, "磁盘上旧名已无、新名在");
  // 历史（非留言板）：改名后历史跟着走
  click(find("[role=tab]", "历史版本"));
  await until(() => $$(".timeline .tl-item").length >= 1, "改名后历史版本还在");

  section("K2 笔记本选择器");
  const picker = $("select[data-fk=notebook-picker]");
  change(picker, "Bot-B");
  await until(() => find(".row", "hello.md"), "切到 Bot-B 看到 hello.md");
  assert(!find(".row", "plans-renamed.md"), "Bot-A 的文件不在 Bot-B 的列表里");
  change($("select[data-fk=notebook-picker]"), "Bot-A");
  await until(() => find(".row", "plans-renamed.md"), "切回 Bot-A");

  section("K3 已遗忘删除 / 回收站删除 / 回收站里的彻底删除");
  click(find(".foot-link", "已遗忘"));
  await until(() => find("[role=tab]", "已遗忘"), "维护面板");
  click(find("[role=tab]", "已遗忘"));
  await sleep(80);
  const stale = find(".item-row", "stale.md");
  assert(stale, "已遗忘里有 stale.md");
  click(find("button", "删除", stale));
  await until(() => $(".overlay"), "删除已遗忘的确认");
  assert(/回收站/.test(text($(".overlay"))), "确认里写明进回收站");
  click(find(".overlay button", "删除"));
  await until(() => p.lastCall("forgotten/delete"), "forgotten/delete");
  await sleep(150);
  assert(!find(".item-row", "stale.md"), "删除后从已遗忘消失");
  click(find("[role=tab]", "回收站"));
  await sleep(100);
  const trashedStale = find(".item-row", "stale.md");
  assert(trashedStale, "刚删的 stale.md 进了回收站");
  click(find("button", "彻底删除（含历史）", trashedStale));
  await until(() => $(".overlay"), "彻底删除确认");
  click(find(".overlay button", "彻底删除（含历史）"));
  await until(() => p.lastCall("file/purge"), "file/purge");
  await sleep(150);
  assert(!find(".item-row", "stale.md"), "彻底删除后回收站里也没有了");
  const trashedOther = find(".item-row", "trashed.md");
  assert(trashedOther, "trashed.md 还在回收站");
  click(find("button", "删除这份副本", trashedOther));
  await until(() => $(".overlay"), "回收站条目删除确认");
  click(find(".overlay button", "删除这份副本"));
  await until(() => p.lastCall("trash/delete"), "trash/delete");

  section("K4 迁移一处（真的写）与平台改名（真的搬）");
  click(find(".nav-item", "管理"));
  await sleep(60);
  click(find(".row-section", "备份与迁移"));
  await until(() => find("[role=tab]", "迁移一处"), "迁移页");
  click(find("[role=tab]", "迁移一处"));
  await sleep(60);
  const sels = $$(".pane-detail select");
  // 从 小本本 Bot-B 到 小本本 Bot-A
  const src = sels[0];
  const dst = sels[1];
  const srcOpt = Array.from(src.querySelectorAll("option")).find((o) => /Bot-B/.test(o.textContent));
  const dstOpt = Array.from(dst.querySelectorAll("option")).find((o) => /Bot-A/.test(o.textContent) && /小本本/.test(o.textContent));
  change(src, srcOpt.value);
  change(dst, dstOpt.value);
  click(find(".pane-detail button", "开始迁移"));
  await until(() => $(".overlay"), "迁移确认");
  click(find(".overlay button", "开始迁移"));
  await until(() => p.calls.filter((c) => c.endpoint === "migrate/scope").length >= 2 && p.lastCall("migrate/scope").body.dryRun === false, "真正迁移的请求");
  await sleep(200);
  const moved = await p.peek("notebook", "Bot-A", "hello.md");
  assert(moved.content && /你好/.test(moved.content), "复制后 Bot-A 里有 hello.md");
  const stillThere = await p.peek("notebook", "Bot-B", "hello.md");
  assert(stillThere.content, "复制模式下来源还在");

  click(find("[role=tab]", "平台改名"));
  await sleep(100);
  const plat = $$(".pane-detail input.mono");
  input(plat[0], "Bot-B");
  input(plat[1], "Bot-C");
  click(find(".pane-detail button", "开始迁移"));
  await until(() => $(".overlay"), "平台改名确认");
  click(find(".overlay button", "开始迁移"));
  await until(() => p.lastCall("migrate/platform").body.dryRun === false, "平台改名真实请求");
  await sleep(200);
  const bc = await p.peek("notebook", "Bot-C", "hello.md");
  assert(bc.content, "平台改名后 Bot-C 小本本里有 hello.md");
  assert(/迁移结果|Move result/.test(text($(".pane-detail"))), "显示迁移结果");

  section("K5 清除整段对话的数据");
  click(find(".nav-item", "悄悄话"));
  await sleep(100);
  click(find(".row-conv", "小红"));
  await until(() => find(".danger-zone button", "清除"), "对话信息里的危险区");
  click(find(".danger-zone button", "清除"));
  await until(() => $(".overlay"), "清除确认");
  click(find(".overlay button", "清除这段对话的数据"));
  await until(() => p.lastCall("whisper/purge"), "whisper/purge");
  assert(p.lastCall("whisper/purge").body.target === "all", "目标是 all");
  await until(() => !find(".row-conv", "小红") && $$(".row-conv").length >= 1, "清除后回到对话列表且小红不见了");

  section("K6 导入真的写入（覆盖）");
  click(find(".nav-item", "管理"));
  await sleep(60);
  click(find(".row-section", "备份与迁移"));
  await until(() => find("[role=tab]", "导入"), "迁移页");
  // 先导出再改一条再导入
  click(find("[role=tab]", "导出"));
  await sleep(60);
  click(find(".pane-detail button", "导出备份"));
  await until(() => p.downloads.length >= 1, "导出");
  const bundle = await p.getJson(`/export/file?token=${p.downloads[0].params.token}`);
  click(find("[role=tab]", "导入"));
  await sleep(40);
  const fi = $("input[type=file]");
  p.setFile(fi, "b.json", JSON.stringify(bundle));
  change(fi);
  await until(() => $(".checklist"), "预览");
  change($(".pane-detail select"), "overwrite");
  await sleep(200);
  click(find(".pane-detail button", "导入选中的地方"));
  await until(() => $(".overlay"), "导入确认");
  click(find(".overlay button", "导入选中的地方"));
  await until(() => p.lastCall("import/apply"), "import/apply");
  const ia = p.lastCall("import/apply");
  assert(ia.body.policy === "overwrite" && ia.body.selection.length >= 1 && ia.body.token, "import/apply payload: " + JSON.stringify(ia.body).slice(0, 160));
  await until(() => /导入结果/.test(text($(".pane-detail"))), "显示导入结果");

  console.log("\nuncaught:", p.uncaught().map((e) => String(e && e.stack || e).slice(0, 300)));
  assert(p.uncaught().length === 0, "没有未捕获错误");
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});
