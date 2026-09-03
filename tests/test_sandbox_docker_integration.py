from __future__ import annotations

import os
from pathlib import Path
import subprocess
import json

import pytest

from agents.execution import DockerSandboxBackend, ExecRequest, SandboxSpec


def _docker_ready(image: str) -> tuple[bool, str]:
    try:
        info = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=15, shell=False, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Docker CLI/daemon unavailable: {exc}"
    if info.returncode != 0:
        return False, f"Docker daemon unavailable: {(info.stderr or info.stdout).strip()[-400:]}"
    inspect = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True, timeout=15, shell=False, check=False
    )
    if inspect.returncode != 0:
        return False, f"Sandbox image is not built: {image}"
    return True, ""


@pytest.mark.asyncio
async def test_real_docker_sandbox_lifecycle(tmp_path: Path) -> None:
    image = os.environ.get("RUN_AGENT_SANDBOX_IMAGE", "run-agent-python-sandbox:latest")
    ready, reason = _docker_ready(image)
    if not ready:
        pytest.skip(reason)

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, shell=False)
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, shell=False)
    subprocess.run(
        ["git", "-c", "user.name=Run Agent Test", "-c", "user.email=test@local", "commit", "-qm", "base"],
        cwd=tmp_path, check=True, shell=False,
    )

    session = await DockerSandboxBackend().start(
        SandboxSpec(image=image, workspace=tmp_path, timeout_seconds=30, run_id="integration", case_id="lifecycle")
    )
    container_id = session.container_id
    try:
        inspected = subprocess.run(
            ["docker", "inspect", container_id], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, shell=False, check=True,
        )
        config = json.loads(inspected.stdout)[0]
        host_config = config["HostConfig"]
        assert config["Config"]["User"] == "1000:1000"
        assert host_config["NetworkMode"] == "none"
        assert host_config["ReadonlyRootfs"] is True
        assert "ALL" in host_config["CapDrop"]
        assert "no-new-privileges:true" in host_config["SecurityOpt"]
        assert host_config["Memory"] == 2048 * 1024 * 1024
        assert host_config["PidsLimit"] == 256
        assert "/tmp" in host_config["Tmpfs"]
        env = config["Config"]["Env"]
        assert "HOME=/tmp" in env
        assert not any("API_KEY=" in item or "TOKEN=" in item for item in env)

        write = await session.exec(
            ExecRequest(("python", "-c", "from pathlib import Path; Path('value.py').write_text('VALUE = 2\\n')"))
        )
        assert write.ok
        verify = await session.exec(ExecRequest(("python", "-c", "import value; assert value.VALUE == 2")))
        assert verify.ok
        assert "VALUE = 2" in await session.export_patch()
        timeout = await session.exec(ExecRequest(("python", "-c", "import time; time.sleep(2)"), timeout_seconds=0.1))
        assert timeout.timed_out
        after_timeout = await session.exec(ExecRequest(("python", "-c", "print('after-timeout')")))
        assert after_timeout.ok
        assert "VALUE = 2" in await session.export_patch()
    finally:
        await session.close()
    inspect = subprocess.run(
        ["docker", "inspect", container_id], capture_output=True, text=True, timeout=15, shell=False, check=False
    )
    assert inspect.returncode != 0
