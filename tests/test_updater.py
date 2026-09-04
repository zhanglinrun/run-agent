from pathlib import Path
from subprocess import CompletedProcess

from run_agent_coding.updater import detect_install_method, update_run_agent


def _success(command: tuple[str, ...], **kwargs: object) -> CompletedProcess[str]:
    assert kwargs == {"capture_output": True, "text": True, "check": False}
    return CompletedProcess(command, 0, stdout="upgraded", stderr="")


def test_detect_install_method_supports_pipx_and_pip(tmp_path: Path) -> None:
    assert detect_install_method(tmp_path) is None
    assert detect_install_method(tmp_path, installer="pip") == "pip"
    assert detect_install_method(tmp_path, installer="pipx") == "pipx"

    (tmp_path / "pipx_metadata.json").touch()
    assert detect_install_method(tmp_path, installer="pip") == "pipx"


def test_update_run_agent_uses_pipx_for_pipx_owned_environment(tmp_path: Path) -> None:
    (tmp_path / "pipx_metadata.json").touch()

    result = update_run_agent(
        runner=_success,
        environment_prefix=tmp_path,
        inspect_distribution=False,
    )

    assert result.command == ("pipx", "upgrade", "run-agent-harness")
    assert result.stdout == "upgraded"


def test_update_run_agent_uses_current_environment_pip_for_pip_install(tmp_path: Path) -> None:
    result = update_run_agent(
        runner=_success,
        python_executable="/env/bin/python",
        environment_prefix=tmp_path,
        installer="pip",
        inspect_distribution=False,
    )

    assert result.command == (
        "/env/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "run-agent-harness",
    )


def test_update_run_agent_does_not_fall_back_when_owner_update_fails(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **kwargs: object) -> CompletedProcess[str]:
        del kwargs
        calls.append(command)
        return CompletedProcess(command, 2, stdout="", stderr="pip failed")

    result = update_run_agent(
        runner=runner,
        python_executable="/env/bin/python",
        environment_prefix=tmp_path,
        installer="pip",
        inspect_distribution=False,
    )

    command = ("/env/bin/python", "-m", "pip", "install", "--upgrade", "run-agent-harness")
    assert result.succeeded is False
    assert result.failures == (f"{' '.join(command)}: pip failed",)
    assert calls == [command]


def test_update_run_agent_reports_process_start_failure(tmp_path: Path) -> None:
    def runner(*args: object, **kwargs: object) -> CompletedProcess[str]:
        del args, kwargs
        raise OSError("executable not found")

    result = update_run_agent(
        runner=runner,
        environment_prefix=tmp_path,
        installer="pipx",
        inspect_distribution=False,
    )

    assert result.succeeded is False
    assert result.failures == ("pipx upgrade run-agent-harness: executable not found",)


def test_update_run_agent_refuses_direct_url_install(tmp_path: Path) -> None:
    result = update_run_agent(
        runner=_success,
        environment_prefix=tmp_path,
        direct_url="file:///checkout/run-agent",
        installer="pip",
        inspect_distribution=False,
    )

    assert result.succeeded is False
    assert "original source: file:///checkout/run-agent" in result.failures[0]


def test_update_run_agent_refuses_conda_or_pixi_environment(tmp_path: Path) -> None:
    (tmp_path / "conda-meta").mkdir()

    result = update_run_agent(
        runner=_success,
        environment_prefix=tmp_path,
        inspect_distribution=False,
    )

    assert result.succeeded is False
    assert "Conda/Pixi-managed" in result.failures[0]


def test_update_run_agent_refuses_unknown_installer(tmp_path: Path) -> None:
    result = update_run_agent(
        runner=_success,
        environment_prefix=tmp_path,
        installer="custom-manager",
        inspect_distribution=False,
    )

    assert result.succeeded is False
    assert "Package metadata reports: custom-manager" in result.failures[0]
