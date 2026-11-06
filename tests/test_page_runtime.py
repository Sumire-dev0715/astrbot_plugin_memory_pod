"""管理页的运行时测试：在迷你 DOM 里加载真实的页面脚本，连真实的 MemoryDashboard 后端，按场景操作并断言。

test_page_source.py 只对源码做字符串断言，看不出"点了刷新数字没变""失败被吞掉"这类运行时问题。
这里把 pages/memory/ 下的 8 个脚本按 index.html 的顺序放进 tests/page_runtime/dom.js（一个没有任何依赖的
迷你 DOM，innerHTML 一类的写法会直接抛错），桥接对象换成连本机后端的假桥接，然后按
tests/page_runtime/scenarios/*.js 里的场景点击、输入、断言。后端用合成数据（backend.py），不碰任何真实记忆。
没有 node 时整组跳过。每个场景一个用例，失败时把场景的输出带出来。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "tests" / "page_runtime"
SCENARIOS = sorted((RUNTIME / "scenarios").glob("*.js"))
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="没有 node")


@pytest.fixture(scope="module")
def backend():
    sys.path.insert(0, str(RUNTIME))
    try:
        import backend as module

        server = module.start()
    finally:
        sys.path.remove(str(RUNTIME))
    yield server
    server.stop()


def _run(path: Path, port: int, timeout: int = 180) -> subprocess.CompletedProcess:
    env = dict(os.environ, MP_PORT=str(port))
    return subprocess.run(
        [NODE, str(path)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def test_scenarios_exist():
    names = {path.stem for path in SCENARIOS}
    assert {"boot", "board", "notebook", "whisper_manage", "states", "locale", "more"} <= names


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda path: path.stem)
def test_page_scenario(scenario, backend):
    backend.reset()
    result = _run(scenario, backend.port)
    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, "场景 %s 失败（退出码 %s）：\n%s" % (
        scenario.name,
        result.returncode,
        output[-6000:],
    )
