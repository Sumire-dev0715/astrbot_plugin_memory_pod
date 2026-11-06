"""鲸群记忆评测入口（PLAN 3.8.9，命令行与输出沿用 3.7.7）。

用法：
    python scripts/eval/run_eval.py --env-file <路径> --runs 3 --json
    python scripts/eval/run_eval.py --only <场景名>

标准输出只打印一个 JSON 汇总；退出码：0 通过，1 没通过，2 配置、网络或
脚本自身的问题。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.eval.harness import (  # noqa: E402
    ScenarioRun,
    patched_time,
    run_scenario,
)
from scripts.eval.model_client import (  # noqa: E402
    CallBudget,
    ConfigError,
    EvalError,
    ModelError,
    OpenAICompatClient,
)
from scripts.eval.scenarios import all_scenarios  # noqa: E402

DEFAULT_ENV_FILE = Path("~/.config/memory-pod/eval.env").expanduser()
DEFAULT_RUNS = 3
DEFAULT_CONCURRENCY = 6
# 整次评测的时间上限（秒）；超时按脚本问题退出码 2。
EVAL_DEADLINE_SECONDS = 3600
PASS_RATE_FLOOR = 0.9
REQUIRED_ENV = ("MB_EVAL_BASE_URL", "MB_EVAL_MODEL", "MB_EVAL_API_KEY")
# 本次评测读到的密钥；所有写到标准输出的文本都先抹掉它。异常信息、第三方
# 报错都可能把请求头或请求内容带出来，不能依赖每条路径自己记得脱敏。
_SECRETS: tuple[str, ...] = ()


def _redact(text: str) -> str:
    """抹掉本次评测的密钥；输出前统一调用。"""
    for secret in _SECRETS:
        if secret:
            text = text.replace(secret, "***")
    return text


@dataclass
class RunSpec:
    scenario: object
    index: int


@dataclass
class RunResult:
    spec: RunSpec
    run: ScenarioRun | None = None
    error: str = ""


@dataclass
class Aggregate:
    checks: dict = field(default_factory=dict)
    critical_failed: list = field(default_factory=list)
    pass_rate: float = 0.0
    noncritical_pass_rate: float = 0.0
    passed: bool = False
    prompt_chars: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    background_calls: int = 0
    errors: list = field(default_factory=list)


def load_env_file(path: Path) -> dict:
    """读 KEY=VALUE 环境文件；只返回键值，不打印文件内容。"""
    global _SECRETS
    _SECRETS = ()
    if not path.is_file():
        raise ConfigError(
            f"找不到评测环境文件：{path}"
            "（需要 MB_EVAL_BASE_URL、MB_EVAL_MODEL、MB_EVAL_API_KEY）"
        )
    values: dict[str, str] = {}
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError as exc:
        raise ConfigError(f"无法读取评测环境文件：{path}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    missing = [name for name in REQUIRED_ENV if not values.get(name)]
    if missing:
        raise ConfigError(
            "评测环境文件缺少配置项：" + "、".join(missing) + f"（文件：{path}）"
        )
    # load_env_file 也可能被测试或库直接调用；密钥从这里开始全局生效
    _SECRETS = (values["MB_EVAL_API_KEY"],)
    return values


def _check_passed(passes: int, runs: int) -> bool:
    """一个检查项至少半数运行通过（默认 3 次里 2 次）。"""
    if runs <= 0:
        return False
    return passes * 2 >= runs


def aggregate(
    scenarios: list,
    runs: int,
    results: list[RunResult],
) -> Aggregate:
    checks: dict[str, dict] = {}
    for scenario in scenarios:
        for check in scenario.checks:
            name = f"{scenario.name}.{check.name}"
            checks[name] = {
                "critical": bool(check.critical),
                "category": check.category,
                "runs": runs,
                "passes": 0,
                "passed": False,
                "failure_sample": "",
            }
    prompt_chars: list[int] = []
    tool_calls: list[int] = []
    background_calls = 0
    errors: list[str] = []
    for result in results:
        if result.error:
            errors.append(
                f"{result.spec.scenario.name}#{result.spec.index + 1}: "
                f"{result.error}"
            )
            continue
        run = result.run
        if run is None:
            continue
        background_calls += run.background_calls
        prompt_chars.extend(run.prompt_chars)
        tool_calls.extend(run.tool_calls_per_reply)
        for outcome in run.outcomes:
            item = checks.get(outcome.name)
            if item is None:
                continue
            if outcome.ok:
                item["passes"] += 1
            elif not item["failure_sample"]:
                item["failure_sample"] = outcome.detail

    passed_checks = 0
    critical_failed: list[str] = []
    noncritical_passed = 0
    noncritical_total = 0
    for name, item in checks.items():
        item["passed"] = _check_passed(item["passes"], item["runs"])
        if item["critical"]:
            if not item["passed"]:
                critical_failed.append(name)
        else:
            noncritical_total += 1
            if item["passed"]:
                noncritical_passed += 1
        if item["passed"]:
            passed_checks += 1
    total = len(checks)
    # pass_rate 按规格统计全部检查项；passed 的判定另按“其余检查项”
    # （非关键项）计算通过率，避免关键项抬高分母掩盖非关键项不达标。
    pass_rate = (passed_checks / total) if total else 0.0
    noncritical_pass_rate = (
        noncritical_passed / noncritical_total if noncritical_total else 1.0
    )
    return Aggregate(
        checks=checks,
        critical_failed=sorted(critical_failed),
        pass_rate=round(pass_rate, 4),
        noncritical_pass_rate=round(noncritical_pass_rate, 4),
        passed=(
            not errors
            and not critical_failed
            and total > 0
            and noncritical_pass_rate >= PASS_RATE_FLOOR
        ),
        prompt_chars=prompt_chars,
        tool_calls=tool_calls,
        background_calls=background_calls,
        errors=errors,
    )


async def _run_one(
    spec: RunSpec, env: dict, budget: CallBudget, sem: asyncio.Semaphore
) -> RunResult:
    async with sem:
        data_dir = Path(tempfile.mkdtemp(prefix=f"mb-eval-{spec.scenario.name}-"))
        try:
            client = OpenAICompatClient(
                env["MB_EVAL_BASE_URL"],
                env["MB_EVAL_MODEL"],
                env["MB_EVAL_API_KEY"],
                budget=budget,
            )
            run = await run_scenario(
                spec.scenario,
                client,
                data_dir,
                model=env["MB_EVAL_MODEL"],
            )
            return RunResult(spec=spec, run=run)
        except EvalError as exc:
            return RunResult(spec=spec, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - 脚本自身的问题
            return RunResult(
                spec=spec,
                error=f"评测脚本内部错误：{type(exc).__name__}: {exc}",
            )
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


def _error_summary(message: str) -> dict:
    return {
        "passed": False,
        "critical_failed": [],
        "pass_rate": 0.0,
        "noncritical_pass_rate": 0.0,
        "checks": {},
        "metrics": {
            "prompt_chars_per_round": [],
            "tool_calls_per_reply": [],
            "background_calls": 0,
            "api_calls": 0,
        },
        "errors": [message],
    }


def _redact_value(value):
    """在序列化之前递归抹掉字符串里的密钥。

    先 json.dumps 再替换会漏掉被 JSON 转义过的密钥（引号、反斜杠、
    控制字符），所以必须在对象层面先处理。
    """
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def _print(summary: dict) -> None:
    """唯一的输出口：序列化前统一抹掉密钥，再写标准输出。"""
    sys.stdout.write(
        json.dumps(_redact_value(summary), ensure_ascii=False) + "\n"
    )
    sys.stdout.flush()


async def _main_async(args) -> int:
    env = load_env_file(Path(args.env_file).expanduser())
    scenarios = all_scenarios()
    if args.only:
        wanted = set(args.only)
        scenarios = [item for item in scenarios if item.name in wanted]
        missing = wanted - {item.name for item in scenarios}
        if missing:
            raise ConfigError("找不到场景：" + "、".join(sorted(missing)))
    if not scenarios:
        raise ConfigError("没有可运行的场景")

    specs = [
        RunSpec(scenario=scenario, index=index)
        for scenario in scenarios
        for index in range(max(1, args.runs))
    ]
    budget = CallBudget()
    sem = asyncio.Semaphore(max(1, args.concurrency))
    started = time.monotonic()
    with patched_time():
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *[_run_one(spec, env, budget, sem) for spec in specs]
                ),
                timeout=EVAL_DEADLINE_SECONDS,
            )
        except asyncio.TimeoutError:
            return _finish_timeout()

    agg = aggregate(scenarios, max(1, args.runs), results)
    if budget.exceeded and not any("接口调用超过" in item for item in agg.errors):
        # 插件自己的宽泛 except 可能吞掉调用异常，这里以共享预算的标志为准，
        # 确保超限一定按脚本问题退出 2。
        agg.errors.append(f"接口调用超过 {budget.limit} 次，已中止本次评测")
        agg.passed = False
    summary = {
        "passed": agg.passed,
        "critical_failed": agg.critical_failed,
        "pass_rate": agg.pass_rate,
        "noncritical_pass_rate": agg.noncritical_pass_rate,
        "checks": agg.checks,
        "metrics": {
            "prompt_chars_per_round": agg.prompt_chars,
            "tool_calls_per_reply": agg.tool_calls,
            "background_calls": agg.background_calls,
            "api_calls": budget.count,
            "duration_seconds": round(time.monotonic() - started, 1),
        },
    }
    if agg.errors:
        summary["errors"] = agg.errors
    _print(summary)
    if agg.errors:
        return 2
    return 0 if agg.passed else 1


def _finish_timeout() -> int:
    summary = _error_summary(f"整次评测超过 {EVAL_DEADLINE_SECONDS} 秒，已中止")
    _print(summary)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="鲸群记忆评测（PLAN 3.8.9）")
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help="评测环境文件路径，默认 ~/.config/memory-pod/eval.env",
    )
    parser.add_argument(
        "--runs", type=int, default=DEFAULT_RUNS, help="每个场景运行次数，默认 3"
    )
    parser.add_argument(
        "--json", action="store_true", help="只输出 JSON 汇总（默认就是）"
    )
    parser.add_argument(
        "--only", action="append", default=[], help="只跑指定场景，可重复"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="并发运行数，默认 6",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except ConfigError as exc:
        _print(_error_summary(str(exc)))
        return 2
    except ModelError as exc:
        _print(_error_summary(str(exc)))
        return 2
    except EvalError as exc:
        _print(_error_summary(str(exc)))
        return 2
    except KeyboardInterrupt:
        _print(_error_summary("评测被中断"))
        return 2
    except Exception as exc:  # noqa: BLE001 - 脚本自身的问题
        _print(_error_summary(f"评测脚本内部错误：{type(exc).__name__}: {exc}"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
