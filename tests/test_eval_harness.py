"""评测脚本自身的测试：确定性假模型、聚合规则与 CLI 输出。"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

from scripts.eval import harness
from scripts.eval.scenarios import all_scenarios
from tests.fake_eval_models import FakeEvalModel


def _results(model_mode: str, tmp_path: Path):
    runs = []
    for scenario in all_scenarios():
        data_dir = tmp_path / scenario.name
        runs.append(
            (
                scenario,
                asyncio.run(
                    harness.run_scenario(
                        scenario, FakeEvalModel(model_mode), data_dir
                    )
                ),
            )
        )
    return runs


def test_good_fake_model_passes_every_check(tmp_path):
    """好模型让全部 25 个场景的所有检查通过。"""
    runs = _results("good", tmp_path)
    failed = [
        (scenario.name, outcome.name, outcome.detail)
        for scenario, run in runs
        for outcome in run.outcomes
        if not outcome.ok
    ]
    assert failed == []
    checks = [outcome for _, run in runs for outcome in run.outcomes]
    assert len(checks) == 25
    assert sum(1 for item in checks if item.critical) == 12


def test_bad_fake_model_fails_all_critical_checks(tmp_path):
    """坏模型让全部关键检查项失败，普通项也不会被整体误判为通过。"""
    runs = _results("bad", tmp_path)
    critical = {
        f"{scenario.name}.{check.name}"
        for scenario in all_scenarios()
        for check in scenario.checks
        if check.critical
    }
    failed_critical = {
        outcome.name
        for _, run in runs
        for outcome in run.outcomes
        if not outcome.ok and outcome.critical
    }
    assert failed_critical == critical
    non_critical_passed = [
        outcome.name
        for _, run in runs
        for outcome in run.outcomes
        if outcome.ok and not outcome.critical
    ]
    assert non_critical_passed  # 不是所有检查都恒失败


def test_aggregate_requires_two_of_three():
    """一个检查项 3 次里至少 2 次通过才算通过。"""
    from scripts.eval.run_eval import RunResult, RunSpec, aggregate
    from scripts.eval.harness import CheckOutcome, ScenarioRun

    scenario = types.SimpleNamespace(
        name="demo",
        checks=[types.SimpleNamespace(name="c", critical=True, category="x")],
    )
    spec = RunSpec(scenario=scenario, index=0)

    def outcome(ok: bool):
        return CheckOutcome(
            name="demo.c", critical=True, category="x", ok=ok, detail="" if ok else "失败"
        )

    def run(oks):
        return RunResult(
            spec=spec,
            run=ScenarioRun(
                scenario="demo",
                outcomes=[outcome(ok) for ok in oks],
                prompt_chars=[1],
                tool_calls_per_reply=[0],
                background_calls=1,
            ),
        )

    passed = aggregate([scenario], 3, [run([True]), run([True]), run([False])])
    assert passed.checks["demo.c"]["passes"] == 2
    assert passed.checks["demo.c"]["passed"] is True
    assert passed.passed is True

    failed = aggregate([scenario], 3, [run([True]), run([False]), run([False])])
    assert failed.checks["demo.c"]["passed"] is False
    assert failed.critical_failed == ["demo.c"]
    assert failed.passed is False


def _write_env(tmp_path: Path) -> Path:
    path = tmp_path / "eval.env"
    path.write_text(
        "MB_EVAL_BASE_URL=http://127.0.0.1:1\n"
        "MB_EVAL_MODEL=fake-model\n"
        "MB_EVAL_API_KEY=fake-key\n",
        "utf-8",
    )
    return path


def test_cli_only_good_model_outputs_json(tmp_path, monkeypatch, capsys):
    from scripts.eval import run_eval

    class FakeClient(FakeEvalModel):
        def __init__(self, *args, budget=None, **kwargs):
            super().__init__("good")

    monkeypatch.setattr(run_eval, "OpenAICompatClient", FakeClient)
    env_file = _write_env(tmp_path)
    code = run_eval.main(
        [
            "--env-file",
            str(env_file),
            "--only",
            "private_profile_intro",
            "--runs",
            "1",
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["passed"] is True
    assert output["critical_failed"] == []
    assert output["pass_rate"] == 1.0
    assert list(output["checks"]) == ["private_profile_intro.profile_in_people"]
    assert len(output["metrics"]["prompt_chars_per_round"]) == 1
    assert len(output["metrics"]["tool_calls_per_reply"]) == 1


def test_cli_bad_model_exits_one(tmp_path, monkeypatch, capsys):
    from scripts.eval import run_eval

    class FakeClient(FakeEvalModel):
        def __init__(self, *args, budget=None, **kwargs):
            super().__init__("bad")

    monkeypatch.setattr(run_eval, "OpenAICompatClient", FakeClient)
    env_file = _write_env(tmp_path)
    code = run_eval.main(
        [
            "--env-file",
            str(env_file),
            "--only",
            "private_health_only_session",
            "--runs",
            "2",
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    assert output["passed"] is False
    assert output["critical_failed"] == [
        "private_health_only_session.health_not_in_people"
    ]
    item = output["checks"]["private_health_only_session.health_not_in_people"]
    assert item["passes"] == 0
    assert item["runs"] == 2
    assert item["failure_sample"]


def test_cli_missing_env_file_exits_two(tmp_path, capsys):
    from scripts.eval import run_eval

    code = run_eval.main(
        ["--env-file", str(tmp_path / "missing.env"), "--json"]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 2
    assert output["passed"] is False
    assert output["errors"]


def test_cli_unknown_scenario_exits_two(tmp_path, capsys):
    from scripts.eval import run_eval

    code = run_eval.main(
        ["--env-file", str(_write_env(tmp_path)), "--only", "no-such", "--json"]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "找不到场景" in output["errors"][0]
