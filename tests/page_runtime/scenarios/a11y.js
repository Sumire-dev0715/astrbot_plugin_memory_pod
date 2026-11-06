/* 运行时场景：读屏软件能读到的东西。
 * 没有角色的 span / div 上挂 aria-label，ARIA 不允许（generic 不能命名），读屏软件通常忽略，
 * 「9 项」「3 个文件」会读成一个裸数字。数量要用「看得见但对读屏隐藏的数字 + 只给读屏读的整句」。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, assert, until, section, summary } = require("../harness.js");

// 可以带 aria-label 的元素：有名字的原生控件和地标，或者显式写了 role
const NAMEABLE = new Set(["button", "input", "select", "textarea", "a", "nav", "section", "form", "fieldset", "table", "details", "summary", "dialog", "main", "aside", "header", "footer", "article", "ul", "ol", "img"]);

function unnamable(p) {
  return p.$$("[aria-label]").filter((el) => !NAMEABLE.has(el.localName) && !el.hasAttribute("role"));
}

async function visit(p, nav) {
  p.click(p.find(".nav-item", nav));
  await until(() => p.$(".pane-list"), "列表栏");
}

(async () => {
  section("A1 导航角标：数字对读屏隐藏，整句只给读屏读");
  const p = await boot();
  await until(() => p.$(".nav-count"), "导航角标");
  const badge = p.$(".nav-count");
  assert(badge.querySelector("[aria-hidden]") && badge.querySelector(".sr-only"), "角标里有隐藏的数字和只给读屏读的整句");
  assert(/\d+ 项/.test(p.text(badge.querySelector(".sr-only"))), "整句是「n 项」: " + p.text(badge.querySelector(".sr-only")));
  assert(!badge.hasAttribute("aria-label"), "角标本身不再挂 aria-label");

  section("A2 各个视图里，aria-label 只出现在能命名的元素上");
  const views = [["留言板", null], ["小本本", () => p.find(".row", "plans.md")], ["悄悄话", () => p.$(".row-conv")], ["上下文", () => p.$(".row-conv")], ["管理", null]];
  for (const [nav, waitFor] of views) {
    await visit(p, nav);
    if (waitFor) {
      await until(waitFor, nav + " 的列表内容");
    }
    const bad = unnamable(p);
    assert(bad.length === 0, `${nav}：这些元素带了 aria-label 但没有角色: ` + bad.map((el) => el.localName + "." + el.className).join(", "));
  }

  section("A3 对话列表与管理列表里的文件数也是同样的写法");
  await visit(p, "悄悄话");
  await until(() => p.$(".row-conv .count"), "对话行里的文件数");
  const count = p.$(".row-conv .count");
  assert(count.querySelector("[aria-hidden]") && /个文件/.test(p.text(count.querySelector(".sr-only"))), "文件数有整句: " + p.text(count));
  await visit(p, "管理");
  p.click(p.find(".row-section", "整理"));
  await until(() => p.$(".pane-detail .row-scope .count"), "整理入口里各处的文件数");
  const scopeCount = p.$(".pane-detail .row-scope .count");
  assert(scopeCount.querySelector("[aria-hidden]") && /个文件/.test(p.text(scopeCount.querySelector(".sr-only"))), "整理入口里的文件数有整句");

  assert(p.uncaught().length === 0, "没有未捕获错误: " + p.uncaught().map(String));
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});
