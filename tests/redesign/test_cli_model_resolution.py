"""The Coding CLI must resolve its model from ``MODEL``, like the other two hosts.

Found by running the shipped CLI against a real endpoint: ``.env`` carried
``MODEL=gpt-5.6-luna``, the run used ``gpt-5.4``, and the provider answered 503
``model_not_found``. The error names the model that was used and says nothing about the
one that was asked for, so the cause is invisible from the failure.

``run bench`` and ``run gateway`` already read the variable. The Coding host passed its
``--model`` default of ``None`` straight through to the provider config, which then fell
back to ``DEFAULT_MODEL``.

The probe drives the real Typer app - so the real option parsing and the real
``load_dotenv`` run - and stubs only ``_run``, which is the one piece that would otherwise
need a live model.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

MODEL_PROBE = """
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1])))
STATE = Path(sys.argv[2])
EXTRA = json.loads(sys.argv[3])

from run_agent_coding import cli

captured = {}


async def fake_run(options, prompt, *, print_mode, output):
    captured["model"] = options.model
    captured["prompt"] = prompt
    return True


cli._run = fake_run
try:
    cli.app(
        args=["--print", "hello", "--no-extensions", "--state-dir", str(STATE), *EXTRA],
        prog_name="run",
    )
except SystemExit:
    pass
print(json.dumps(captured))
"""


def run_probe(tmp_path: Path, *, extra=(), env=None) -> dict:
    """Drive the real CLI with ``_run`` stubbed, and return what it resolved."""
    probe = tmp_path / "model_probe.py"
    probe.write_text(MODEL_PROBE, encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(probe),
            str(REPO),
            str(tmp_path / "state"),
            json.dumps(list(extra)),
        ],
        input="",
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(REPO), **(env or {})},
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_the_coding_cli_honours_the_model_environment_variable(tmp_path):
    resolved = run_probe(tmp_path, env={"MODEL": "a-model-from-the-environment"})
    assert resolved["model"] == "a-model-from-the-environment"


def test_an_explicit_model_flag_beats_the_environment(tmp_path):
    resolved = run_probe(
        tmp_path,
        extra=["--model", "a-model-from-the-flag"],
        env={"MODEL": "a-model-from-the-environment"},
    )
    assert resolved["model"] == "a-model-from-the-flag"
