/* 运行时场景：窄屏下点开一条记忆或告示后，键盘和读屏的焦点要进到详情的标题上。
 * 详情要先向服务端读文件，读的时候还没有标题；焦点得等标题出现再移过去，
 * 否则列表被隐藏后焦点会掉回页面开头。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, assert, until, sleep, section, summary } = require("../harness.js");

(async () => {
  section("N1 窄屏点开告示：详情读完后焦点在标题上");
  let p = await boot({ narrow: true });
  await until(() => p.$(".notice-card"), "留言板首屏");
  p.click(p.find(".notice-card", "欢迎新同学"));
  await until(() => p.$("[data-detail-heading]"), "详情标题出现");
  await until(() => p.document.activeElement === p.$("[data-detail-heading]"), "焦点移到详情标题");
  assert(p.document.activeElement === p.$("[data-detail-heading]"), "焦点在详情标题上");

  section("N2 详情读得慢：读的时候就有标题和返回按钮，焦点已经在标题上；读完后焦点跟到新标题");
  p = await boot({ narrow: true });
  await until(() => p.$(".notice-card"), "留言板首屏");
  const release = p.hold("file");
  p.click(p.find(".notice-card", "欢迎新同学"));
  await until(() => p.$(".pane-detail .state-loading"), "详情在转圈");
  assert(p.$("[data-detail-heading]"), "读的时候已经有标题");
  assert(p.$(".pane-detail .only-narrow"), "读的时候窄屏就有返回按钮");
  assert(p.document.activeElement === p.$("[data-detail-heading]"), "读的时候焦点已经在标题上");
  assert(p.$(".pane-detail .detail").getAttribute("aria-busy") === "true", "读的时候详情标着正在加载（aria-busy）");
  const loadingHeading = p.$("[data-detail-heading]");
  release();
  await until(() => p.$(".pane-detail .md"), "详情读完");
  assert(p.$("[data-detail-heading]") !== loadingHeading, "读完后标题重画成了新元素（带内容的）");
  assert(p.document.activeElement === p.$("[data-detail-heading]"), "读完后焦点跟到新标题上: " + p.document.activeElement.tagName);
  assert(!p.$(".pane-detail .detail").hasAttribute("aria-busy"), "读完后不再标正在加载");

  section("N3 读文件失败：错误态里也有标题和返回按钮，焦点在标题上");
  p = await boot({ narrow: true });
  await until(() => p.$(".notice-card"), "留言板首屏");
  await p.fail("file", 1);
  p.click(p.find(".notice-card", "欢迎新同学"));
  await until(() => p.$(".pane-detail .state-error"), "详情读取失败");
  assert(p.$("[data-detail-heading]") && p.$(".pane-detail .only-narrow"), "错误态有标题和返回按钮");
  assert(p.document.activeElement === p.$("[data-detail-heading]"), "焦点在标题上");
  p.click(p.$(".pane-detail .only-narrow"));
  await sleep(60);
  assert(p.$(".pane-list") && p.$(".split").getAttribute("data-show") === "list", "返回按钮回到了列表");

  section("N3b 窄屏点「编辑」：编辑器读文件时也有标题，焦点跟到编辑器标题");
  p = await boot({ narrow: true });
  await until(() => p.$(".notice-card"), "留言板首屏");
  p.click(p.find(".notice-card", "欢迎新同学"));
  await until(() => p.$('.pane-detail [data-fk="edit"]'), "详情读完，有编辑按钮");
  const releaseEdit = p.hold("file");
  p.click(p.$('.pane-detail [data-fk="edit"]'));
  await until(() => p.$(".pane-detail .state-loading"), "编辑器在读文件");
  assert(p.document.activeElement === p.$("[data-detail-heading]"), "编辑器读文件时焦点在标题上");
  releaseEdit();
  await until(() => p.$(".pane-detail .editor-tools"), "编辑器出现");
  assert(p.document.activeElement === p.$("[data-detail-heading]"), "编辑器出现后焦点跟到编辑器的标题");

  section("N4 宽屏不受影响：点开告示不会强行移动焦点");
  p = await boot();
  await until(() => p.$(".notice-card"), "留言板首屏");
  const card = p.find(".notice-card", "欢迎新同学");
  card.focus();
  p.click(card);
  await until(() => p.$(".pane-detail .md"), "详情读完");
  const focused = p.document.activeElement;
  assert(focused && focused.getAttribute("data-fk") === card.getAttribute("data-fk"), "焦点还在被点的那张卡片上（列表重画后按 data-fk 还原）: " + (focused && focused.tagName));
  assert(!focused.hasAttribute("data-detail-heading"), "没有被移进详情标题");

  assert(p.uncaught().length === 0, "没有未捕获错误: " + p.uncaught().map(String));
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});
