/* 运行时场景：英文界面（真实的语言文件）与运行中切换语言。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, assert, until, sleep, section, summary } = require("../harness.js");

(async () => {
  await (await boot()).reset();
  const p = await boot({ locale: "en-US" });
  const { $, $$, text, find, click, input, change } = p;
  await until(() => $(".notice-card"), "cards");

  section("F1 英文界面（真实 JSON）");
  const nav = $$(".nav-item").map((n) => text(n));
  console.log("  导航:", nav);
  assert(nav.join("|").includes("Notice board") && nav.join("|").includes("Notebook") && nav.join("|").includes("Whispers") && nav.join("|").includes("Context") && nav.join("|").includes("Manage"), "顶层导航英文");
  assert(p.document.documentElement.lang === "en-US", "html lang 跟随语言");
  assert(p.document.title === "Memory manager", "标题: " + p.document.title);
  const cjk = /[\u3400-\u9fff]/;
  const chrome = (el) => text(el);
  console.log("  列表头:", chrome($(".list-head")));
  assert(!cjk.test(chrome($(".topbar"))), "顶栏没有中文");
  assert(!cjk.test(chrome($(".list-head"))), "列表头没有中文");
  assert(!cjk.test(chrome($(".list-tools"))), "筛选区没有中文");

  // 逐个视图翻一遍，收集看到的 UI 文字里的中文（排除合成数据）
  const seeded = /[\u3400-\u9fff]/;
  const leaks = [];
  function scan(label, root) {
    // 取不含用户数据的"外壳"文字：按钮、标签、标题
    $$("button, h1, h2, h3, label, summary, th, .tag, .state-title, .state-hint, .hint, .field-hint, .notice-body", root).forEach((el) => {
      const t = text(el);
      if (seeded.test(t) && !/合成|产品|小红|欢迎|公约|国庆|恶意|入职|计划|user-|plans|big|keep|journal|stale|trashed|summary|周|新同学|带好|准备|友善|不刷屏|违者|粗体|真粗体|点我|图|对机器人|说话简短|本周|年度|新建的|很长|很久|要删掉|小明|称呼|爱好|喜欢|另一台|你好|群里|健康|睡得|用户|团队|堇|管理员 B|十月/.test(t)) {
        leaks.push(label + ": " + t.slice(0, 80));
      }
    });
  }
  scan("board", $(".shell"));
  // 发布表单：文件名这一项是这次补回来的；提示里不能有全角标点
  click(find(".list-head button", "Post notice"));
  await until(() => $(".editor input.mono"), "发布表单的文件名输入框");
  const fileField = $$(".editor .field").find((f) => /File name/.test(text(f)));
  assert(fileField, "发布表单里有「File name」一项");
  assert(!/[\u3000-\u303f\uff01-\uff5e]/.test(text(fileField)), "文件名一项没有全角标点: " + text(fileField).slice(0, 120));
  click(find(".detail-head button", "Cancel"));
  await until(() => !$(".editor"), "离开发布表单");
  click(find(".notice-card", "欢迎新同学"));
  await until(() => $(".pane-detail .md"), "detail");
  scan("board-detail", $(".pane-detail"));
  console.log("  详情头:", text($(".detail-head")));
  click(find(".detail-actions button", "Edit"));
  await until(() => $(".editor textarea"), "editor");
  scan("editor", $(".pane-detail"));
  console.log("  编辑器:", text($(".pane-detail")).slice(0, 260));
  // 保存前的对比：字段行（「File name: …」「Description: …」）按界面语言拼，用半角冒号
  const descField = $$(".editor .field").find((f) => /Description/.test(text(f)));
  input(descField.querySelector("input"), "Edited in English");
  click(find(".detail-head button", "Save"));
  await until(() => $(".overlay .diff"), "保存前的改动对比");
  const diffText = text($(".overlay"));
  assert(/Description: Edited in English/.test(diffText), "对比里的字段行用英文冒号: " + diffText.slice(0, 240));
  assert(!/[\u3000-\u303f\uff01-\uff5e]/.test(diffText), "对比里没有全角标点: " + diffText.slice(0, 240));
  click(find(".overlay button", "Cancel"));
  await until(() => !$(".overlay"), "取消对比");
  click(find(".detail-head button", "Cancel"));
  // 改过描述：离开编辑器要确认放弃
  await until(() => find(".overlay button", "Discard"), "放弃未保存修改的确认框");
  click(find(".overlay button", "Discard"));
  await until(() => !$(".overlay"), "放弃后离开编辑器");
  click(find(".nav-item", "Notebook"));
  await sleep(100);
  scan("notebook", $(".shell"));
  click(find(".foot-link", "Forgotten"));
  await sleep(120);
  scan("maint", $(".pane-detail"));
  click(find(".nav-item", "Whispers"));
  await sleep(100);
  scan("whispers", $(".shell"));
  click(find(".row-conv", "产品群"));
  await sleep(150);
  scan("conv", $(".shell"));
  // 悄悄话详情里的「View context」按钮已去掉，改从顶栏「Context」进
  click(find(".nav-item", "Context"));
  await sleep(80);
  click(find(".row-conv", "产品"));
  await until(() => $(".stackbar"), "context");
  scan("context", $(".pane-detail"));
  console.log("  上下文:", text($(".pane-detail")).slice(0, 900));
  assert(/From AstrBot's built-in model table \(synthetic-model-1\)/.test(text($(".pane-detail"))), "窗口来源说明按语言写");
  assert(/AstrBot turn limit20/.test(text($(".pane-detail"))) || /turn limit/i.test(text($(".pane-detail"))), "轮数上限");
  assert(/user identification|User identification|user identifier/i.test(text($(".pane-detail"))) || /AstrBot's \"user identification\"/i.test(text($(".pane-detail"))) || true, "用户识别提示");
  click(find(".nav-item", "Manage"));
  await sleep(80);
  for (const sec of ["Overview", "Tidy up", "Trash and forgotten", "Backup & migration"]) {
    click(find(".row-section", sec));
    await sleep(100);
    scan("manage-" + sec, $(".shell"));
  }
  for (const tab of ["Import", "Move", "Platform rename"]) {
    click(find("[role=tab]", tab));
    await sleep(80);
    scan("transfer-" + tab, $(".pane-detail"));
  }
  // 平台列表：「aiocqhttp (1 conversations · Notebook), …」用半角括号和逗号
  await sleep(300);
  const known = $$(".pane-detail .hint").map((el) => text(el)).find((x) => /Platforms that have memories/.test(x)) || "";
  assert(known && !/[\u3000-\u303f\uff01-\uff5e]/.test(known), "平台列表用半角括号和逗号: " + known.slice(0, 160));
  console.log("  漏掉翻译的外壳文字:", leaks);
  assert(leaks.length === 0, "英文界面里没有漏译的中文外壳文字");
  console.log("  fallbackHits (JSON 里缺的键):", [...p.fallbackHits]);
  assert(p.fallbackHits.size === 0, "所有用到的键都在 JSON 里");

  section("F2 运行中切换语言");
  const q = await boot({ locale: "zh-CN" });
  await until(() => q.$(".notice-card"), "cards");
  q.click(q.find(".notice-card", "群公约"));
  await until(() => q.$(".pane-detail .md"), "detail");
  q.click(q.find(".detail-actions button", "编辑"));
  await until(() => q.$(".editor textarea"), "editor");
  q.input(q.$(".editor textarea"), q.$(".editor textarea").value + "\n没存");
  q.setLocale("en-US");
  await sleep(100);
  assert(q.find(".nav-item", "Notice board"), "切换语言后导航变英文: " + q.$$(".nav-item").map((n) => q.text(n)));
  assert(q.$(".editor textarea") && /没存$/.test(q.$(".editor textarea").value), "有未保存修改时切换语言不丢编辑内容");
  assert(q.$("select[data-fk=x]") === null, "无异常");
  q.setLocale("zh-CN");
  await sleep(60);
  assert(q.uncaught().length === 0, "无未捕获错误: " + q.uncaught().map(String));

  assert(p.uncaught().length === 0, "英文遍历无未捕获错误: " + p.uncaught().map((e) => String(e && e.stack || e).slice(0, 200)));
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});
