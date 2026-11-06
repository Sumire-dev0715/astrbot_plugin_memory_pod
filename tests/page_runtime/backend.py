"""管理页运行时测试的后端：真实的 MemoryDashboard + 合成数据，经本机 HTTP 暴露成桥接接口的形状。

tests/test_page_runtime.py 用 start() 在后台线程里起它，再让 node 跑 scenarios/ 下的场景；
手动调试时也可以直接运行：.venv/bin/python tests/page_runtime/backend.py
（stdout 打印一行 PORT=<端口>）。

控制用的路径（页面不会调用）：
  GET /__reset[?empty=1]            重新生成合成数据（empty=1 是什么都没有的新装状态）
  GET /__fail?endpoint=&count=      让某个接口接下来的 count 次请求返回错误
  GET /__peek?scope=&key=&file=     读磁盘上的文件原文，给断言用
所有数据都是代码里造出来的合成内容，不读任何真实的记忆。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT, ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from support import (  # noqa: E402
    pkg_context_stats,
    pkg_history,
    pkg_memstore,
    pkg_origin,
    thing,
)
import test_web_api as tw  # noqa: E402

UMO_GROUP = "Bot-A:GroupMessage:987654321"
UMO_PRIVATE = "Bot-A:FriendMessage:10001"
UMO_OTHER = "Bot-B:GroupMessage:555"
GROUP_NAME = "合成测试群"

STATE: dict = {}


def run(coro):
    return asyncio.run(coro)


def seed(base: Path, empty: bool = False):
    plugin, dashboard = tw.setup(base)
    if empty:
        return plugin, dashboard
    plugin.config.update(enable_forgetting=True, forget_after_days=60)
    now = time.time()
    board = plugin._store.board_scope()
    nb = plugin._store.notebook_scope("Bot-A")
    nb2 = plugin._store.notebook_scope("Bot-B")

    # ---- 留言板
    run(board.write("welcome.md", thing("welcome", description="欢迎新同学 teto 入职", body="11 月 11 日新员工 **teto** 入职。\n\n- 带好工牌\n- 准备自我介绍\n\n详见 [[rules]]。", signature="堇"), actor=pkg_history.ACTOR_WEBUI))
    run(board.write("rules.md", thing("rules", description="群公约", body="1. 友善\n2. 不刷屏\n\n> 违者禁言\n\n```\nprint('hi')\n```", signature="堇", expires="2099-12-31")))
    run(board.write("old-event.md", thing("old-event", description="国庆活动", body="十月一日线下活动", signature="管理员 B", expires="2020-01-01")))
    run(board.write("evil.md", thing(
        "evil",
        description="<img src=x onerror=alert(1)> 恶意标题",
        body="<script>window.__pwned = 1</script>\n\n[点我](javascript:evil) ![图](http://evil.example/x.png)\n\n<b>粗体标签</b> **真粗体**\n\n| a | b |\n|---|---|\n| <i>x</i> | `<u>y</u>` |",
        signature="<svg onload=alert(3)>",
    )))
    # 改过几次：产生历史版本
    run(board.write("welcome.md", thing("welcome", description="欢迎新同学 teto 入职", body="11 月 11 日新员工 **teto** 入职，欢迎！\n\n- 带好工牌\n- 准备自我介绍\n\n详见 [[rules]]。", signature="堇"), actor=pkg_history.ACTOR_WEBUI))
    run(board.write("welcome.md", thing("welcome", description="欢迎新同学 teto 入职", body="11 月 11 日新员工 **teto** 入职，欢迎！\n\n- 带好工牌\n- 准备自我介绍\n- 周五聚餐\n\n详见 [[rules]]。", signature="堇"), actor=pkg_history.ACTOR_WEBUI))

    # ---- 小本本 Bot-A
    rec = pkg_origin.Origin("group", "123", "小明", now - 3 * 86400, pkg_origin.session_hash(UMO_GROUP))
    run(nb.write("user-10001.md", thing("user-10001", description="小红：后端工程师，喜欢猫", body="称呼：小红\n\n爱好：猫、咖啡", mem_type="person"), source="10001", actor="Bot-A", origin_record=pkg_origin.Origin("private", "10001", "小红", now - 86400, pkg_origin.session_hash(UMO_PRIVATE)), audit=pkg_history.AuditContext(session=UMO_PRIVATE)))
    run(nb.write("user-10001-hobby.md", thing("user-10001-hobby", description="小红的爱好细节", body="喜欢橘猫。", mem_type="person")))
    run(nb.write("keep-calm.md", thing("keep-calm", description="对机器人的要求：说话简短", body="回答不超过三句。", mem_type="agreement")))
    run(nb.write("plans.md", thing("plans", description="本周计划", body="# 计划\n\n- [x] 周一例会\n- [ ] 周三评审\n\n见 [[user-10001]] 与 [[missing-file]]。", mem_type="note"), source="123", actor="Bot-A", origin_record=rec, audit=pkg_history.AuditContext(session=UMO_GROUP, group=GROUP_NAME)))
    run(nb.write("plans.md", thing("plans", description="本周计划", body="# 计划\n\n- [x] 周一例会\n- [ ] 周三评审\n- [ ] 周五复盘\n\n见 [[user-10001]] 与 [[missing-file]]。", mem_type="note")))
    run(nb.write("journal-20261101.md", thing("journal-20261101", description="11 月 1 日的日记", body="08:30 小明（群聊）\n今天聊了新员工。", mem_type="journal")))
    run(nb.write("big.md", thing("big", description="很长的资料", body="长长的资料。\n" * 600, mem_type="note")))
    run(nb.write("stale.md", thing("stale", description="很久没用的笔记", body="一直没人看。", mem_type="note")))
    run(nb.write("trashed.md", thing("trashed", description="要删掉的笔记", body="再见。", mem_type="note")))
    run(nb.delete("trashed.md", actor=pkg_history.ACTOR_WEBUI))
    run(nb.forget("stale.md", actor=pkg_history.ACTOR_WEBUI))
    run(nb2.write("hello.md", thing("hello", description="另一台机器人", body="你好", mem_type="note")))

    # ---- 悄悄话
    for umo, name, remark in ((UMO_GROUP, GROUP_NAME, "产品群"), (UMO_PRIVATE, "小红", ""), (UMO_OTHER, "", "")):
        key = pkg_memstore.safe_key(umo)
        plugin._registry.touch(key, umo, name=name, now=now - 3600)
        if remark:
            plugin._registry.set_remark(key, remark)
    gkey = pkg_memstore.safe_key(UMO_GROUP)
    pkey = pkg_memstore.safe_key(UMO_PRIVATE)
    run(plugin._store.whisper_scope_by_key(gkey).write("group-secret.md", thing("group-secret", description="群里的小秘密", body="只在这个群里说。", mem_type="note")))
    run(plugin._store.whisper_scope_by_key(pkey).write("health.md", thing("health", description="小红的健康情况", body="最近睡得不好。", mem_type="note")))

    # ---- 上下文
    class Runtime:
        pass

    rt = Runtime()
    rt.stats = pkg_context_stats.ContextStats(
        parts={"system": 800, "plugin": 1200, "tools": 600, "history": 5200, "prompt": 40, "recall": 300},
        estimate=8140,
        actual=8400,
        cache_hit=6000,
        request_at=now - 300,
    )
    plugin._sessions[UMO_GROUP] = rt

    async def summary(umo):
        return "用户小明计划下周评审，机器人已提醒。" if umo == UMO_GROUP else ""

    plugin.astrbot_summary = summary

    async def status(umo=None):
        return {"window": 128000, "windowNote": "跟随 AstrBot 的内置模型表（synthetic-model-1）", "turnLimit": 20, "userIdentifier": True}

    plugin.context_status = status
    plugin.reply = json.dumps(
        [
            {"op": "write", "file": "plans.md", "type": "note", "description": "本周计划（整理后）", "content": "- 周一例会\n- 周三评审\n- 周五复盘", "reason": "合并重复"},
            {"op": "delete", "file": "keep-calm.md", "reason": "已并入别处"},
            {"op": "write", "file": "summary-2026.md", "type": "note", "description": "年度总结", "content": "新建的总结", "reason": "新增"},
        ]
    )
    return plugin, dashboard


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _dispatch(self, method):
        url = urlparse(self.path)
        endpoint = url.path.lstrip("/")
        params = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        if endpoint == "__reset":
            reset(empty=params.get("empty") == "1")
            return self._send({"ok": True})
        if endpoint == "__fail":
            STATE.setdefault("fail", {})[params["endpoint"]] = int(params.get("count", "1"))
            return self._send({"ok": True})
        failing = STATE.get("fail", {})
        if failing.get(endpoint, 0) > 0:
            failing[endpoint] -= 1
            return self._send({"status": "error", "message": "模拟的后端错误：" + endpoint, "data": {}})
        if endpoint == "__peek":
            plugin = STATE["plugin"]
            scope = params.get("scope")
            store = {
                "board": lambda: plugin._store.board_scope(),
                "notebook": lambda: plugin._store.notebook_scope_by_key(params.get("key", "")),
                "whisper": lambda: plugin._store.whisper_scope_by_key(params.get("key", "")),
            }[scope]()
            return self._send({"content": run(store.read(params["file"])), "files": store.list_files()})
        dash = STATE["dashboard"]
        found = [r for r in dash._routes() if r[0] == endpoint]
        if not found:
            return self._send({"status": "error", "message": "未找到该路由", "data": {}})
        if found[0][1] != method:
            return self._send({"status": "error", "message": "方法不对：" + method + " vs " + found[0][1], "data": {}})
        tw.REQUEST.query = params
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            tw.REQUEST.body = json.loads(self.rfile.read(length) or b"{}")
        else:
            tw.REQUEST.body = None
        result = run(dash._wrap(found[0][2])())
        self._send(result)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def reset(empty: bool = False):
    old = STATE.get("tmp")
    tmp = Path(tempfile.mkdtemp(prefix="mp-page-runtime-"))
    STATE["tmp"] = tmp
    STATE["fail"] = {}
    STATE["plugin"], STATE["dashboard"] = seed(tmp, empty)
    if old:
        shutil.rmtree(old, ignore_errors=True)


class Backend:
    """在后台线程里跑的本机 HTTP 后端（一次只处理一个请求，所以共用的假请求对象不会串）。"""

    def __init__(self):
        reset()
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reset(self, empty: bool = False):
        reset(empty=empty)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(STATE.get("tmp"), ignore_errors=True)


def start() -> Backend:
    return Backend()


if __name__ == "__main__":
    backend = start()
    print("PORT=%d" % backend.port, flush=True)
    try:
        backend.thread.join()
    except KeyboardInterrupt:
        backend.stop()
