"""Pinned dataset loading and clean repository checkout."""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
from typing import Any, Iterable
from urllib.request import Request, urlopen

from ...runtime.contracts import utc_now
from .models import SWEBenchInstance


DATASET_ID = "SWE-bench/SWE-bench_Verified"
DATASET_SPLIT = "test"
DATASET_URL = (
    "https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified/"
    "resolve/main/data/test-00000-of-00001.parquet?download=true"
)
EXPECTED_ROWS = 500
EXPECTED_SHA256 = "030cfd7f2a704c4c0226e7f104c725a3b41230b1d3517f9c915ad7ea5be3fa25"
DEFAULT_DATASET_PATH = Path("data/SWE-bench_Verified/test-00000-of-00001.parquet")


def _as_string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    text = str(value).strip()
    if not text:
        return ()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return (text,)
    return _as_string_list(parsed)


def _require_pyarrow():
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:  # pragma: no cover - minimal installs only
        raise RuntimeError("Reading SWE-bench parquet requires pyarrow; install the swebench extra") from exc
    return parquet


def load_swebench_verified(path: str | Path = DEFAULT_DATASET_PATH) -> list[SWEBenchInstance]:
    dataset_path = Path(path)
    if not dataset_path.is_file():
        raise FileNotFoundError(f"SWE-bench Verified dataset not found: {dataset_path}")
    rows = _require_pyarrow().read_table(dataset_path).to_pylist()
    instances: list[SWEBenchInstance] = []
    seen_instance_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"invalid SWE-bench row {index}: expected object")
        instance_id = str(row.get("instance_id") or "").strip()
        repo = str(row.get("repo") or "").strip()
        base_commit = str(row.get("base_commit") or "").strip()
        problem_statement = str(row.get("problem_statement") or "").strip()
        if not instance_id or "/" not in repo or not base_commit or not problem_statement:
            raise ValueError(
                f"invalid SWE-bench row {index}: instance_id, repo, base_commit and problem_statement are required"
            )
        if instance_id in seen_instance_ids:
            raise ValueError(f"duplicate SWE-bench instance_id {instance_id!r} at row {index}")
        seen_instance_ids.add(instance_id)
        instances.append(SWEBenchInstance(
            instance_id=instance_id,
            repo=repo,
            base_commit=base_commit,
            problem_statement=problem_statement,
            test_patch=str(row.get("test_patch") or ""),
            eval_script=str(row.get("eval_script") or ""),
            fail_to_pass=_as_string_list(row.get("FAIL_TO_PASS")),
            pass_to_pass=_as_string_list(row.get("PASS_TO_PASS")),
            image=str(row.get("image") or ""),
            version=str(row.get("version") or ""),
            environment_setup_commit=str(row.get("environment_setup_commit") or ""),
            difficulty=str(row.get("difficulty") or ""),
            eval_type=str(row.get("eval_type") or ""),
            hints_text=str(row.get("hints_text") or ""),
            log_parser=str(row.get("log_parser") or ""),
            gold_patch=str(row.get("patch") or ""),
        ))
    return instances


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_swebench_verified(
    output: str | Path = DEFAULT_DATASET_PATH,
    *,
    overwrite: bool = False,
    timeout: int = 120,
) -> Path:
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        actual = sha256_file(destination)
        if actual != EXPECTED_SHA256:
            raise RuntimeError(f"existing dataset hash mismatch: {actual}; pass --overwrite to replace it")
        _write_dataset_manifest(destination)
        return destination
    request = Request(DATASET_URL, headers={"User-Agent": "run-agent-harness/0.3"})
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with urlopen(request, timeout=timeout) as response, temporary.open("wb") as file:
            shutil.copyfileobj(response, file)
        actual = sha256_file(temporary)
        if actual != EXPECTED_SHA256:
            raise RuntimeError(f"downloaded dataset hash mismatch: {actual}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    _write_dataset_manifest(destination)
    return destination


def _write_dataset_manifest(destination: Path) -> None:
    rows = load_swebench_verified(destination)
    if len(rows) != EXPECTED_ROWS:
        raise RuntimeError(f"expected {EXPECTED_ROWS} rows, found {len(rows)}")
    destination.with_name("dataset-manifest.json").write_text(json.dumps({
        "dataset": DATASET_ID,
        "split": DATASET_SPLIT,
        "source_url": DATASET_URL,
        "sha256": EXPECTED_SHA256,
        "bytes": destination.stat().st_size,
        "rows": len(rows),
        "downloaded_at": utc_now(),
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_git(workspace: Path, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(workspace), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, shell=False, check=False,
    )


def checkout_instance(instance: SWEBenchInstance, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=False)
    initialize = _run_git(workspace, "init", "-q")
    if initialize.returncode != 0:
        raise RuntimeError(f"git init failed for {instance.instance_id}: {initialize.stderr[-2000:]}")
    config = _run_git(workspace, "config", "core.autocrlf", "false")
    if config.returncode != 0:
        raise RuntimeError(f"git config failed for {instance.instance_id}: {config.stderr[-2000:]}")
    remote = _run_git(workspace, "remote", "add", "origin", f"https://github.com/{instance.repo}.git")
    if remote.returncode != 0:
        raise RuntimeError(f"git remote add failed for {instance.instance_id}: {remote.stderr[-2000:]}")
    # Fetch one complete commit without a partial-clone filter.  The sandbox
    # runs with network=none, so git diff must never need lazy blob hydration.
    fetch = _run_git(workspace, "fetch", "--depth=1", "origin", instance.base_commit)
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch failed for {instance.instance_id}: {fetch.stderr[-2000:]}")
    checkout = _run_git(workspace, "checkout", "--detach", instance.base_commit)
    if checkout.returncode != 0:
        raise RuntimeError(f"git checkout failed for {instance.instance_id}: {checkout.stderr[-2000:]}")
    reset = _run_git(workspace, "reset", "--hard", instance.base_commit)
    if reset.returncode != 0:
        raise RuntimeError(f"git reset failed for {instance.instance_id}: {reset.stderr[-2000:]}")


def export_host_patch(workspace: Path) -> str:
    untracked = _run_git(workspace, "ls-files", "--others", "--exclude-standard", timeout=60)
    ignored_parts = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox"}
    paths = [
        item for item in untracked.stdout.splitlines()
        if item.strip()
        and not ignored_parts.intersection(Path(item).parts)
        and Path(item).suffix not in {".pyc", ".pyo"}
        and Path(item).name != ".coverage"
    ] if untracked.returncode == 0 else []
    result = _run_git(workspace, "diff", "HEAD", "--binary", "--no-ext-diff", "--no-color", timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr[-2000:]}")
    additions: list[str] = []
    for path in paths:
        added = _run_git(workspace, "diff", "--no-index", "--binary", "--no-ext-diff", "--", os.devnull, path, timeout=60)
        if added.returncode not in {0, 1}:
            raise RuntimeError(f"untracked patch export failed for {path}: {added.stderr[-2000:]}")
        additions.append(added.stdout)
    return result.stdout + "".join(additions)


def select_instances(
    instances: Iterable[SWEBenchInstance],
    *,
    limit: int | None,
    seed: int,
    instance_ids: list[str] | None,
) -> list[SWEBenchInstance]:
    selected = list(instances)
    if instance_ids:
        by_id = {item.instance_id: item for item in selected}
        missing = set(instance_ids) - set(by_id)
        if missing:
            raise ValueError(f"unknown SWE-bench instance ids: {sorted(missing)}")
        selected = [by_id[instance_id] for instance_id in instance_ids]
    else:
        random.Random(seed).shuffle(selected)
    return selected[:limit] if limit else selected


__all__ = [
    "DATASET_ID", "DATASET_SPLIT", "DATASET_URL", "EXPECTED_ROWS", "EXPECTED_SHA256",
    "DEFAULT_DATASET_PATH", "load_swebench_verified", "sha256_file", "download_swebench_verified",
    "checkout_instance", "export_host_patch", "select_instances",
]
