from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from run_agent_evals.task_spec import admit_task, load_task_spec, materialize_environment

_MANIFESTS = (Path("evals/evolution/config.toml"), Path("evals/evolution/normalization.toml"))


def _task_directories() -> list[Path]:
    roots: list[Path] = []
    for manifest_path in _MANIFESTS:
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


def test_evolution_suites_freeze_the_budget_and_bump_the_version() -> None:
    """All 18 frozen tasks share one 300 s budget under suite version 2."""
    versions: dict[str, str] = {}
    for manifest_path in _MANIFESTS:
        document = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        versions[str(document["family"])] = str(document["version"])
    assert versions == {"config": "2", "normalization": "2"}

    roots = _task_directories()
    assert len(roots) == 18
    specs = [load_task_spec(root) for root in roots]
    assert {spec.budget_seconds for spec in specs} == {300}
    assert all(spec.budget_seconds == 300 for spec in specs)


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
