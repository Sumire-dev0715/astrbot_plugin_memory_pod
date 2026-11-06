"""用 AstrBot 4.29.4 的真实加载路径验证插件能实例化、注册 5 个工具。

仓库测试里的 ``__astrbot_llm_tool__`` 只是桩上的类属性；这里在
``--network none`` 的临时容器里导入插件并实例化，读取 AstrBot 自己的
``provider.register.llm_tools``，确认注册的正好是 5 个记忆工具（PLAN 阶段 F）。
环境里没有 docker 或镜像时跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "astrbot:4.29.4-dev"

DRIVER = r"""
import json
import sys
import types

package = types.ModuleType("astrbot_plugin_memory_pod")
package.__path__ = ["/plugin"]
sys.modules["astrbot_plugin_memory_pod"] = package

import astrbot_plugin_memory_pod.main as main_module  # noqa: E402


class FakeContext:
    def register_web_api(self, *args, **kwargs):
        pass


main_module.MemoryPodPlugin(FakeContext(), {})

from astrbot.core.provider.register import llm_tools  # noqa: E402

print("TOOLS_JSON " + json.dumps(sorted(tool.name for tool in llm_tools.func_list)))
"""

EXPECTED = [
    "memory_delete",
    "memory_edit",
    "memory_read",
    "memory_search",
    "memory_write",
]


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", IMAGE],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


@pytest.mark.skipif(
    not _docker_ready(), reason="需要 docker 与 astrbot:4.29.4-dev 镜像"
)
def test_astrbot_registers_exactly_five_memory_tools():
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{ROOT}:/plugin:ro",
            "--entrypoint",
            "python",
            IMAGE,
            "-c",
            DRIVER,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith("TOOLS_JSON ")
    ]
    assert lines, completed.stdout + completed.stderr
    assert json.loads(lines[-1][len("TOOLS_JSON ") :]) == EXPECTED
