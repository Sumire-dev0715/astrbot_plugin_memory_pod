/* 运行时场景：页面启动：外壳、导航、留言板首屏。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, assert, until, section, summary } = require("../harness.js");

(async () => {
  const p = await boot();
  const { $, $$, text, find, click } = p;
  section("启动与留言板");
  await until(() => $(".notice-card"), "留言板告示卡片出现");
  const cards = $$(".notice-card");
  console.log("告示卡片数:", cards.length);
  console.log("顶栏:", text($(".topbar")));
  console.log("列表头:", text($(".list-head")));
  console.log("卡片文字:", cards.map((c) => text(c)));
  assert(cards.length === 4, "留言板应有 4 条告示（含恶意与过期）");
  assert(!document_has(p, "#app img"), "页面里不应出现 <img>（恶意标题里的 img 必须是文字）");
  assert(!document_has(p, "#app script"), "页面里不应出现 <script>");
  assert(!document_has(p, "svg[onload]") , "不应出现 svg onload");
  const expired = find(".notice-card", "国庆活动");
  assert(expired && /已过期/.test(text(expired)), "过期告示带『已过期』标记");
  console.log("uncaught:", p.uncaught().length);
  console.log("fallbackHits:", [...p.fallbackHits].slice(0, 20), p.fallbackHits.size);
  assert(p.uncaught().length === 0, "没有未捕获错误");
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});

function document_has(p, sel) {
  return p.document.querySelectorAll(sel).length > 0;
}
