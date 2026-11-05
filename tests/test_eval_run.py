"""评测入口：命令行、JSON 字段、退出码与通过标准（沿用 3.7.7）。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import scripts.eval.run_eval as run_eval
from scripts.eval.harness import CheckOutcome, ScenarioRun


def _scenario(name="demo", critical=True):
    return SimpleNamespace(
        name=name,
        checks=[
            SimpleNamespace(
                name="ok", critical=critical, category="memory"
            ),
            SimpleNamespace(
                name="other", critical=False, category="memory"
            ),
        ],
    )


def _run_result(scenario, *, critical_ok=True, other_ok=True, error=""):
    spec = run_eval.RunSpec(scenario=scenario, index=0)
    if error:
        return run_eval.RunResult(spec=spec, error=error)
    run = ScenarioRun(
        scenario=scenario.name,
        outcomes=[
            CheckOutcome(
                name=f"{scenario.name}.ok",
                critical=True,
                category="memory",
                ok=critical_ok,
                detail="" if critical_ok else "关键项失败",
            ),
            CheckOutcome(
                name=f"{scenario.name}.other",
                critical=False,
                category="memory",
                ok=other_ok,
                detail="" if other_ok else "普通项失败",
            ),
        ],
        prompt_chars=[10],
        tool_calls_per_reply=[1],
        background_calls=0,
    )
    return run_eval.RunResult(spec=spec, run=run)


def test_aggregate_passes_only_when_critical_and_noncritical_meet_floor():
    scenario = _scenario()
    passed = run_eval.aggregate(
        [scenario], 1, [_run_result(scenario)]
    )
    assert passed.passed is True
    assert passed.critical_failed == []
    assert passed.pass_rate == 1.0

    critical = run_eval.aggregate(
        [scenario], 1, [_run_result(scenario, critical_ok=False)]
    )
    assert critical.passed is False
    assert critical.critical_failed == ["demo.ok"]


def test_aggregate_requires_ninety_percent_of_noncritical_checks():
    scenarios = [
        SimpleNamespace(
            name=f"s{index}",
            checks=[
                SimpleNamespace(
                    name="item", critical=False, category="memory"
                )
            ],
        )
        for index in range(10)
    ]
    results = []
    for index, scenario in enumerate(scenarios):
        spec = run_eval.RunSpec(scenario=scenario, index=0)
        results.append(
            run_eval.RunResult(
                spec=spec,
                run=ScenarioRun(
                    scenario=scenario.name,
                    outcomes=[
                        CheckOutcome(
                            name=scenario.name + ".item",
                            critical=False,
                            category="memory",
                            ok=index != 0,
                            detail="" if index else "失败",
                        )
                    ],
                    prompt_chars=[],
                    tool_calls_per_reply=[],
                    background_calls=0,
                ),
            )
        )
    agg = run_eval.aggregate(scenarios, 1, results)
    assert agg.noncritical_pass_rate == 0.9
    assert agg.passed is True


def test_aggregate_error_never_passes():
    scenario = _scenario()
    agg = run_eval.aggregate(
        [scenario], 1, [_run_result(scenario, error="接口错误")]
    )
    assert agg.passed is False
    assert agg.errors


def test_missing_env_file_exits_2_with_json(capsys):
    code = run_eval.main(["--env-file", "/nonexistent/mb-eval.env"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["passed"] is False
    assert payload["errors"]


def test_unknown_scenario_exits_2(tmp_path, capsys):
    env_file = tmp_path / "eval.env"
    env_file.write_text(
        "MB_EVAL_BASE_URL=http://127.0.0.1:9/v1\n"
        "MB_EVAL_MODEL=test-model\n"
        "MB_EVAL_API_KEY=sk-test-not-real\n",
        "utf-8",
    )
    code = run_eval.main(
        ["--env-file", str(env_file), "--only", "does-not-exist"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "找不到场景" in payload["errors"][0]


def test_cli_json_fields_and_exit_codes(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / "eval.env"
    env_file.write_text(
        "MB_EVAL_BASE_URL=http://127.0.0.1:9/v1\n"
        "MB_EVAL_MODEL=test-model\n"
        "MB_EVAL_API_KEY=sk-test-not-real\n",
        "utf-8",
    )
    monkeypatch.setattr(run_eval, "OpenAICompatClient", lambda *a, **k: object())

    async def fake_run(scenario, client, data_dir, *, model):
        return ScenarioRun(
            scenario=scenario.name,
            outcomes=[
                CheckOutcome(
                    name=f"{scenario.name}.{check.name}",
                    critical=check.critical,
                    category=check.category,
                    ok=True,
                    detail="",
                )
                for check in scenario.checks
            ],
            prompt_chars=[10],
            tool_calls_per_reply=[1],
            background_calls=0,
        )

    monkeypatch.setattr(run_eval, "run_scenario", fake_run)
    scenario_name = run_eval.all_scenarios()[0].name
    code = run_eval.main(
        [
            "--env-file",
            str(env_file),
            "--runs",
            "1",
            "--concurrency",
            "1",
            "--only",
            scenario_name,
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert code == 0
    assert set(payload) >= {
        "passed",
        "critical_failed",
        "pass_rate",
        "noncritical_pass_rate",
        "checks",
        "metrics",
    }
    assert set(payload["metrics"]) >= {
        "prompt_chars_per_round",
        "tool_calls_per_reply",
        "background_calls",
        "api_calls",
        "duration_seconds",
    }
    entry = next(iter(payload["checks"].values()))
    assert set(entry) == {
        "critical",
        "category",
        "runs",
        "passes",
        "passed",
        "failure_sample",
    }
    assert "sk-test-not-real" not in output

    async def failing_run(scenario, client, data_dir, *, model):
        return ScenarioRun(
            scenario=scenario.name,
            outcomes=[
                CheckOutcome(
                    name=f"{scenario.name}.{check.name}",
                    critical=check.critical,
                    category=check.category,
                    ok=not check.critical,
                    detail="" if not check.critical else "关键项失败",
                )
                for check in scenario.checks
            ],
            prompt_chars=[10],
            tool_calls_per_reply=[1],
            background_calls=0,
        )

    monkeypatch.setattr(run_eval, "run_scenario", failing_run)
    code = run_eval.main(
        [
            "--env-file",
            str(env_file),
            "--runs",
            "1",
            "--concurrency",
            "1",
            "--only",
            scenario_name,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["critical_failed"]

    async def broken_run(scenario, client, data_dir, *, model):
        raise run_eval.EvalError("脚本自己坏了")

    monkeypatch.setattr(run_eval, "run_scenario", broken_run)
    code = run_eval.main(
        [
            "--env-file",
            str(env_file),
            "--runs",
            "1",
            "--concurrency",
            "1",
            "--only",
            scenario_name,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["errors"]


def test_parser_flags():
    args = run_eval.build_parser().parse_args(
        [
            "--env-file",
            "/tmp/eval.env",
            "--runs",
            "2",
            "--json",
            "--only",
            "a",
            "--only",
            "b",
            "--concurrency",
            "3",
        ]
    )
    assert args.env_file == "/tmp/eval.env"
    assert args.runs == 2
    assert args.json is True
    assert args.only == ["a", "b"]
    assert args.concurrency == 3
