# Docker Sandbox

Coding/SWE-bench runs use a temporary workspace and can start one short-lived
container per task. `DockerExecutionEnvironment`, model-visible Shell (when active),
`VerificationOrchestrator`, and patch export share the same `SandboxSession`; the
user's original checkout is not mounted by the benchmark adapters.

The default container configuration uses no network, a read-only root filesystem,
writable `/workspace` and `/tmp`, a non-root uid, dropped Linux capabilities,
`no-new-privileges`, and CPU/memory/PID limits. Docker commands are argument arrays
with `shell=False`. Cleanup is owned by the execution extension and Harness
`session_shutdown`/finalization path.

Source evidence:

- `agents/execution/docker_backend.py`: container creation, limits, mounts, and stop.
- `agents/execution/docker.py`: execution-environment adapter and lifecycle snapshot.
- `agents/extensions/workspace.py::setup_execution`: sole default execution factory.
- `agents/harness/harness.py`: execution creation and unconditional final close.
- `agents/extensions/quality.py`: verification receives the same sandbox session.

Build the image:

```powershell
docker build -f Dockerfile.sandbox -t run-agent-python-sandbox:latest .
```

Coding benchmarks default to Docker:

```powershell
run-agent-coding-benchmark evals/coding/smoke/tasks.jsonl `
  --sandbox docker --max-cost 0.50
```

Local mode is an execution compatibility path, not a filesystem sandbox. The default
extension profile therefore removes model-visible `run_shell` when the backend is
local. Explicit host trust is required to expose it:

```powershell
run-agent-coding-benchmark evals/coding/smoke/tasks.jsonl `
  --sandbox local --allow-host-shell --max-cost 0.50
```

Reviewer and Verifier children never receive Shell. Plan Mode removes Shell from the
parent active-tool set before children compute their inherited ceiling.

When the Docker daemon is unavailable, the Harness records an
`infrastructure_failure`; it is not counted as solved. `--adapter-only` can exercise
benchmark adapter setup without a model, but the current `0.4` architecture rewrite
did not rerun Docker lifecycle, tests, builds, or adapter smoke commands. Do not cite
historical runtime results as validation of this revision until those commands are
executed again.
