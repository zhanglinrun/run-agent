from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents.execution import (
    ExecRequest,
    ExecResult,
    SandboxSpec,
    prepare_workspace_for_container,
    scrub_workspace_credentials,
)
from agents.execution import docker_backend as docker_module
from tests.fakes import FakeSandboxBackend
from agents.verification import VerificationOrchestrator
from agents.verification.discovery import VerificationCommand
from agents.verification.orchestrator import VerificationOrchestrator as VerificationOrchestratorImpl


@pytest.mark.asyncio
async def test_fake_sandbox_routes_verification_and_closes(tmp_path: Path) -> None:
    backend = FakeSandboxBackend(
        results=[ExecResult(("python", "-m", "pytest"), 0, "ok", "", False, 1.0)],
        patch="diff --git a/a.py b/a.py\n",
    )
    session = await backend.start(SandboxSpec(workspace=tmp_path))
    report = await VerificationOrchestrator(
        workspace_root=tmp_path,
        commands=[VerificationCommand("pytest", ("python", "-m", "pytest"), 5)],
        sandbox=session,
    ).verify([])
    assert report.passed
    assert session.requests[0].argv == ("python", "-m", "pytest")
    assert await session.export_patch()
    await session.close()
    assert session.closed


@pytest.mark.asyncio
async def test_verification_maps_host_paths_to_official_testbed(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_value.py"
    target.parent.mkdir()
    target.write_text("def test_value(): pass\n", encoding="utf-8")
    backend = FakeSandboxBackend(
        results=[ExecResult(("python",), 0, "ok", "", False, 1.0)],
    )
    session = await backend.start(SandboxSpec(workspace=tmp_path, container_workspace="/testbed"))
    report = await VerificationOrchestrator(
        workspace_root=tmp_path,
        commands=[VerificationCommand("pytest", ("python", "-m", "pytest", str(target)), 5)],
        sandbox=session,
    ).verify([target])

    assert report.passed
    assert session.requests[0].cwd == "/testbed"
    assert session.requests[0].argv[-1] == "/testbed/tests/test_value.py"


@pytest.mark.asyncio
async def test_docker_start_builds_hardened_argument_array(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], *, timeout: float = 30.0):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "container-123\n", "")

    monkeypatch.setattr(docker_module, "_run_docker", fake_run)
    backend = docker_module.DockerSandboxBackend(docker_binary="docker")
    session = await backend.start(
        SandboxSpec(workspace=tmp_path, run_id="r1", case_id="case-1", network="none", memory_mb=512, cpus=1.5, pids_limit=32)
    )
    command = calls[0]
    assert command[:4] == ["docker", "run", "--init", "--detach"]
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert "--cap-drop" in command and command[command.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in command and "no-new-privileges:true" in command
    assert "--memory" in command and "512m" in command
    assert "--cpus" in command and "1.5" in command
    assert "--pids-limit" in command and "32" in command
    assert "OPENAI_API_KEY" not in command
    await session.close()
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_docker_exec_timeout_is_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(argv: list[str], *, timeout: float = 30.0):
        if "exec" in argv:
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(docker_module, "_run_docker", fake_run)
    session = docker_module.DockerSandboxSession(SandboxSpec(workspace=tmp_path), "container-123")
    result = await session.exec(ExecRequest(("pytest",), timeout_seconds=1))
    assert result.timed_out
    assert result.exit_code is None
    assert not session._closed
    await session.close()
    assert session.timed_out
    assert session.closed
    assert session.lifecycle_snapshot()["timed_out"] is True
    assert session.lifecycle_snapshot()["closed"] is True


@pytest.mark.asyncio
async def test_docker_close_reports_failed_container_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(argv: list[str], *, timeout: float = 30.0):
        if "rm" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "permission denied")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(docker_module, "_run_docker", fake_run)
    session = docker_module.DockerSandboxSession(SandboxSpec(workspace=tmp_path), "container-123")
    with pytest.raises(Exception, match="cleanup failed"):
        await session.close()
    assert not session._closed


@pytest.mark.asyncio
async def test_docker_exec_drops_secret_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], *, timeout: float = 30.0):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(docker_module, "_run_docker", fake_run)
    session = docker_module.DockerSandboxSession(SandboxSpec(workspace=tmp_path), "container-123")
    await session.exec(ExecRequest(("python", "-V"), env={"SAFE_FLAG": "1", "OPENAI_API_KEY": "secret"}))
    command = calls[0]
    assert "SAFE_FLAG=1" in command
    assert all("OPENAI_API_KEY" not in value and "secret" not in value for value in command)


@pytest.mark.asyncio
async def test_patch_export_uses_large_repo_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    timeouts: list[float] = []

    def fake_run(argv: list[str], *, timeout: float = 30.0):
        timeouts.append(timeout)
        output = "diff --git a/value.py b/value.py\n" if "diff" in argv else ""
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(docker_module, "_run_docker", fake_run)
    session = docker_module.DockerSandboxSession(
        SandboxSpec(workspace=tmp_path, timeout_seconds=1000, patch_timeout_seconds=321),
        "container-123",
    )

    patch = await session.export_patch()

    assert patch.startswith("diff --git")
    assert timeouts == [321, 321]


def test_prepare_workspace_for_container_only_changes_disposable_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "nested"
    source.mkdir()
    file_path = source / "value.py"
    file_path.write_text("VALUE = 1\n", encoding="utf-8")

    prepared = prepare_workspace_for_container(workspace)

    assert prepared == workspace.resolve()
    assert prepared.stat().st_mode & 0o777 == 0o777
    assert source.stat().st_mode & 0o777 == 0o777
    assert file_path.stat().st_mode & 0o777 == 0o666


def test_prepare_workspace_for_container_rejects_root_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = tmp_path / "workspace-link"
    try:
        link.symlink_to(workspace, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this host")
    with pytest.raises(ValueError, match="must not be a symlink"):
        prepare_workspace_for_container(link)


def test_scrub_workspace_credentials_preserves_original_copy_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    (source / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (source / ".env.example").write_text("API_KEY=placeholder\n", encoding="utf-8")
    (source / ".ssh").mkdir()
    (source / ".ssh" / "id_rsa").write_text("private", encoding="utf-8")
    import shutil
    shutil.copytree(source, workspace)

    removed = scrub_workspace_credentials(workspace)

    assert ".env" in removed
    assert ".ssh" in removed
    assert not (workspace / ".env").exists()
    assert not (workspace / ".ssh").exists()
    assert (workspace / ".env.example").exists()
    assert (source / ".env").exists()
    assert (source / ".ssh" / "id_rsa").exists()


def test_patch_apply_check_preserves_lf_patch_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, shell=False)
    target = tmp_path / "value.py"
    target.write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, shell=False)
    subprocess.run(
        ["git", "-c", "user.name=Run Agent Test", "-c", "user.email=test@local", "commit", "-qm", "base"],
        cwd=tmp_path, check=True, shell=False,
    )
    target.write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
    patch = subprocess.run(
        ["git", "diff", "HEAD", "--binary", "--no-ext-diff", "--no-color"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", shell=False, check=True,
    ).stdout

    report = VerificationOrchestratorImpl(workspace_root=tmp_path, require_patch=True)._patch_apply_step(patch)

    assert report is not None
    assert report.passed, report.stderr
