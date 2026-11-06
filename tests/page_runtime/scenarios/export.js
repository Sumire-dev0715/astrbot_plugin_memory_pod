/* 运行时场景：导出备份。
 * 后端出错时返回 HTTP 200 加 {status:"error"}；AstrBot 面板的代下载（files:download）只存响应体、
 * 不看 status，会把错误 JSON 存成备份文件，页面还提示「备份已生成」。所以页面先经 apiGet
 * 取回内容（出错会抛），再在页面里保存；页面内保存不可用时才退回面板代下载。
 * 由 tests/test_page_runtime.py 起后端后运行；单独运行需要环境变量 MP_PORT。 */
"use strict";
const { boot, assert, until, sleep, section, summary } = require("../harness.js");

async function openExport(p) {
  await until(() => p.$(".nav-item"), "导航");
  p.click(p.find(".nav-item", "管理"));
  await until(() => p.find(".row-section", "备份与迁移"), "管理列表");
  p.click(p.find(".row-section", "备份与迁移"));
  await until(() => p.$$(".checklist input[type=checkbox]").length > 0, "导出页的勾选清单");
}

const toastTexts = (p) => p.$$("#toasts .toast").map((el) => p.text(el));

(async () => {
  section("X1 正常导出：保存下来的是完整备份，没有走面板代下载");
  let p = await boot();
  await openExport(p);
  p.click(p.find(".pane-detail button", "导出备份"));
  await until(() => p.saved.length === 1, "保存了一个文件");
  const file = p.saved[0];
  assert(file && /^memory-backup-.*\.json$/.test(file.filename), "文件名: " + (file && file.filename));
  if (file) {
    const bundle = JSON.parse(await file.blob.text());
    assert(bundle.format === "memory-pod-backup" && Array.isArray(bundle.scopes) && bundle.scopes.length >= 5, "内容是备份: " + JSON.stringify(Object.keys(bundle)));
  }
  assert(p.downloads.length === 0, "没有走面板代下载");
  await until(() => toastTexts(p).some((x) => /备份已生成/.test(x)), "提示备份已生成");

  section("X2 备份令牌失效（被新备份挤掉或过期）：提示错误，不保存任何东西，也不说备份已生成");
  p = await boot();
  await openExport(p);
  await p.fail("export/file", 1);
  p.click(p.find(".pane-detail button", "导出备份"));
  await until(() => p.$$("#toasts .toast.is-error").length > 0, "错误提示");
  assert(/模拟的后端错误/.test(toastTexts(p).join("|")), "错误提示带着原因: " + toastTexts(p).join("|"));
  assert(!toastTexts(p).some((x) => /备份已生成/.test(x)), "没有说备份已生成");
  await sleep(80);
  assert(p.saved.length === 0, "页面内没有保存任何文件");
  assert(p.downloads.length === 0, "面板代下载也没有触发，不会把错误 JSON 存成备份文件");
  // 再点一次就好了
  p.click(p.find(".pane-detail button", "导出备份"));
  await until(() => p.saved.length === 1, "重试后保存成功");
  if (p.saved[0]) {
    const again = JSON.parse(await p.saved[0].blob.text());
    assert(again.format === "memory-pod-backup", "重试保存的是备份");
  }

  section("X3 页面内保存不可用：退回面板代下载，取回的内容先经过状态检查");
  p = await boot({ blobFails: true });
  await openExport(p);
  p.click(p.find(".pane-detail button", "导出备份"));
  await until(() => p.downloads.length === 1, "退回面板代下载");
  assert(p.downloads[0] && p.downloads[0].endpoint === "export/file" && /^memory-backup-/.test(p.downloads[0].filename), "面板代下载的参数: " + JSON.stringify(p.downloads[0]).slice(0, 120));
  assert(p.downloads[0] && JSON.parse(p.downloads[0].body).format === "memory-pod-backup", "面板存下的是备份内容");
  assert(p.saved.length === 0, "页面内没有保存");

  assert(p.uncaught().length === 0, "没有未捕获错误: " + p.uncaught().map(String));
  process.exit(summary() ? 0 : 1);
})().catch((err) => {
  console.error("FATAL", err);
  process.exit(2);
});
