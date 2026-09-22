from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from run_agent_evals.task_spec import admit_task, load_task_spec, materialize_environment


def _task_directories() -> list[Path]:
    roots: list[Path] = []
    for manifest_path in (
        Path("evals/evolution/config.toml"),
        Path("evals/evolution/normalization.toml"),
    ):
        document = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        assert document["schema"] == "run-agent.evolution-suite"
        splits = [item["split"] for item in document["tasks"]]
        assert {split: splits.count(split) for split in set(splits)} == {
            "train": 3,
            "selection": 3,
            "test": 3,
        }
        roots.extend((manifest_path.parent / item["path"]).resolve() for item in document["tasks"])
    return roots


@pytest.mark.parametrize("task_root", _task_directories(), ids=lambda path: path.name)
def test_evolution_task_rejects_noop_and_accepts_reference(task_root: Path, tmp_path: Path) -> None:
    spec = load_task_spec(task_root)
    assert spec.artifacts
    assert all("test" not in Path(artifact).parts for artifact in spec.artifacts)

    noop = tmp_path / "noop"
    materialize_environment(spec, noop)
    assert admit_task(spec, noop).admitted is False

    solved = tmp_path / "solved"
    materialize_environment(spec, solved)
    for source in spec.reference.iterdir():
        target = solved / source.name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    outcome = admit_task(spec, solved)
    assert outcome.admitted is True, outcome.output
