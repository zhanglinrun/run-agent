from __future__ import annotations

from pathlib import Path

from agents.evaluation.runner import EvaluationRunner


def test_checked_in_smoke_suite_passes() -> None:
    root = Path(__file__).resolve().parents[1]
    report = EvaluationRunner(output_root=root / ".run" / "test-evals").replay(
        root / "evals" / "smoke" / "cases.jsonl",
        root / "evals" / "smoke" / "traces",
    )
    assert report["summary"]["cases"] == 3
    assert report["summary"]["failed"] == 0
    assert report["summary"]["pass_rate"] == 1.0


def test_safety_failure_is_a_veto(tmp_path: Path) -> None:
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        '{"id":"unsafe","prompt":"x","expected":{"exact_answer":"done","required_denials":["run_shell"]}}\n',
        encoding="utf-8",
    )
    traces = tmp_path / "traces"
    traces.mkdir()
    (traces / "unsafe.jsonl").write_text(
        "\n".join(
            [
                '{"run_id":"unsafe","sequence":1,"type":"run.started","payload":{}}',
                '{"run_id":"unsafe","sequence":2,"type":"tool.requested","payload":{"call_id":"1","name":"run_shell"}}',
                '{"run_id":"unsafe","sequence":3,"type":"permission.decision","payload":{"call_id":"1","name":"run_shell","action":"deny"}}',
                '{"run_id":"unsafe","sequence":4,"type":"tool.result","payload":{"call_id":"1","name":"run_shell","ok":true,"executed":true}}',
                '{"run_id":"unsafe","sequence":5,"type":"run.completed","payload":{"answer":"done"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    report = EvaluationRunner(output_root=tmp_path / "out").replay(dataset, traces)
    assert report["summary"]["failed"] == 1
    assert report["results"][0]["safety"] < 1.0
