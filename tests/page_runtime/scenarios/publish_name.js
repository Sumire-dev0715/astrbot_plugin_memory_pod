/* 运行时场景：发布留言板告示时自己起文件名（旧页面在留言板也能「新建记忆」并起名）。
 * 表单里「文件名」留空时由服务端按时间生成 notice-<时间>.md；填了就按填的存，
 * 不合规的名字在表单里就拦下来；重名由服务端拒绝，页面弹出原因。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, assert, until, sleep, section, summary } = require("../harness.js");

const toastTexts = (p) => p.$$("#toasts .toast").map((el) => p.text(el));

async function openPublish(p) {
  await until(() => p.$(".notice-card"), "留言板首屏");
  p.click(p.find(".list-head button", "发布告示"));
  await until(() => p.$(".editor textarea"), "发布表单");
}

function fill(p, fields) {
  const inputs = p.$$(".editor input");
  const byLength = (n) => inputs.find((el) => el.getAttribute("maxlength") === n);
  p.input(byLength("100"), fields.signature || "堇");
  p.input(byLength("120"), fields.title);
  if (fields.file !== undefined) {
    p.input(byLength("121"), fields.file);
  }
  p.input(p.$(".editor textarea"), fields.body || "正文");
}

(async () => {
  section("P1 自己起文件名：按名字保存，列表里出现");
  const p = await boot();
  await openPublish(p);
  assert(p.$(".editor input.mono"), "表单里有文件名的输入框（等宽字体）");
  fill(p, { title: "自定文件名的告示", file: "custom-note" });
  p.click(p.find(".detail-head button", "发布告示"));
  await until(() => p.lastCall("board/publish"), "发出 board/publish");
  const call = p.lastCall("board/publish");
  assert(call.body.file === "custom-note.md", "没写 .md 时页面补上: " + JSON.stringify(call.body));
  await until(() => !p.$(".editor textarea"), "发布后离开表单");
  const peek = await p.peek("board", "", "custom-note.md");
  assert(peek.files.includes("custom-note.md"), "文件按自己起的名字保存: " + JSON.stringify(peek.files.filter((f) => /custom|notice/.test(f))));
  assert(/自定文件名的告示/.test(peek.content), "内容对得上");
  await until(() => p.find(".notice-card", "自定文件名的告示"), "列表里出现新告示");

  section("P2 文件名不合规：在表单里拦下，不发给服务端");
  await openPublish(p);
  fill(p, { title: "不合规的名字", file: "a b.md" });
  const before = p.$$("#toasts .toast").length;
  p.click(p.find(".detail-head button", "发布告示"));
  await sleep(60);
  const errors = p.$$(".editor .field-error").filter((el) => !el.hidden).map((el) => p.text(el));
  assert(errors.some((x) => /文件名/.test(x)), "表单里报文件名不合规: " + errors.join("|"));
  assert(p.$$("#toasts .toast").length === before, "没有新提示（没发请求）");
  assert(!p.lastCall("board/publish").body.file.endsWith("a b.md"), "没有把不合规的名字发出去");
  // memory.md 是索引，也不能拿来当告示的文件名
  fill(p, { title: "索引", file: "memory.md" });
  p.click(p.find(".detail-head button", "发布告示"));
  await sleep(60);
  const errors2 = p.$$(".editor .field-error").filter((el) => !el.hidden).map((el) => p.text(el));
  assert(errors2.some((x) => /MEMORY\.md/.test(x)), "memory.md 被拦住: " + errors2.join("|"));
  p.click(p.find(".detail-head button", "取消"));

  section("P3 留空：服务端按时间生成 notice-<时间>.md");
  await openPublish(p);
  fill(p, { title: "自动命名的告示", file: "" });
  p.click(p.find(".detail-head button", "发布告示"));
  await until(() => p.lastCall("board/publish").body.description === "自动命名的告示", "发出 board/publish");
  assert(p.lastCall("board/publish").body.file === "", "留空时把空串交给服务端");
  await until(() => !p.$(".editor textarea"), "发布后离开表单");
  const files = (await p.peek("board", "", "welcome.md")).files;
  assert(files.some((f) => /^notice-\d{8}-\d{6}\.md$/.test(f)), "生成了 notice-<时间>.md: " + JSON.stringify(files.filter((f) => /notice/.test(f))));

  section("P4 名字已被占用：表单里拦下（旧页面也是拒绝同名新建），不覆盖已有告示");
  await openPublish(p);
  fill(p, { title: "重名的告示", file: "custom-note.md" });
  const callsBefore = p.$$("#toasts .toast").length;
  p.click(p.find(".detail-head button", "发布告示"));
  await sleep(60);
  const errors3 = p.$$(".editor .field-error").filter((el) => !el.hidden).map((el) => p.text(el));
  assert(errors3.some((x) => /已经有 custom-note\.md/.test(x)), "提示这个名字已经被占用: " + errors3.join("|"));
  assert(p.$$("#toasts .toast").length === callsBefore, "没有发请求");
  const again = await p.peek("board", "", "custom-note.md");
  assert(/自定文件名的告示/.test(again.content), "原来的告示没有被覆盖");
  assert(p.$(".editor textarea"), "仍停在表单上，可以改个名字再发");

  assert(p.uncaught().length === 0, "没有未捕获错误: " + p.uncaught().map(String));
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});
