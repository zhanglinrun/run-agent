# Run Agent architecture

Run Agent is a local-first Python agent harness. Its packages follow one-way
dependency boundaries so the provider-neutral loop stays reusable:

```text
run_agent_ai       run_agent_core
       \             /
        run_agent_coding
          /        \
run_agent_gateway  run_agent_observability
          \        /
          run_agent_evals
```

## Runtime layers

- `run_agent_ai` implements provider transports, retry behavior, prompt-cache
  handling, and provider-neutral streaming events.
- `run_agent_core` owns messages, tools, the Agent loop, the reusable harness,
  and JSONL session primitives. It has no CLI, TUI, or provider implementation.
- `run_agent_coding` composes the core into a `CodingSession`, adds context
  budgeting, compaction, Skills, persistence, CLI/TUI frontends, and the Python
  extension host.
- `run_agent_gateway` owns multi-session ingress, per-session FIFO scheduling,
  foreground/background concurrency limits, and channel adapters.
- `run_agent_observability` provides the physical-call ledger and generic span
  recorder. Session trace registration is an optional filesystem extension.
- `run_agent_evals` runs isolated tasks through the real `CodingSession`, invokes
  deterministic verifiers, freezes evidence, and supports offline reconstruction.

`run_agent_core` must remain independent of Typer, Rich, Textual, filesystem
resource locations, and provider-specific request formats.

## Agent request path

```text
CLI / TUI / Gateway / Evaluation
  -> CodingSession
  -> AgentHarness
  -> provider stream
  -> tool batch
  -> JSONL session tree
```

The core loop only understands messages, provider events, tool definitions, and
cancellation. It does not branch on Mem0, MCP, planning, permissions, verification,
or tracing.

The default CodingSession exposes `read`, `write`, `edit`, and `bash`. Read is
parallel-capable; write, edit, and bash are sequential. A tool batch containing
any sequential tool executes in declaration order. Pure-read batches use bounded
concurrency, converge cancelled tasks, and publish results in declaration order.

## Extension ownership

Providers are supplied through durable configuration or explicit filesystem
extensions. Other optional product capabilities are explicit filesystem or Gateway
extensions.

Mem0, MCP, Plan, Permission, Verification, and the session Trace Recorder live in
the repository's top-level `extensions/` directory. They are ordinary trusted
Python extensions loaded from an explicit path, the user extension directory, or
an approved project extension directory. When unloaded, they add no tools, hooks,
commands, prompt content, or background resources.

Each staged extension runtime has a fresh generation. Failed setup rolls back the
source's registrations. Reload, resume, and session replacement invalidate old
API/context/UI handles and drain or contain owned asynchronous work.

## Gateway ownership

Gateway is a host, not a per-session Agent extension. It owns multiple durable
sessions and global scheduler capacity. A channel module exports
`setup_gateway(api)` and registers an adapter that implements `messages`, `send`,
and `close`.

The scheduler chains requests by session before acquiring a lane semaphore. This
keeps one conversation ordered while allowing unrelated conversations to run in
parallel. Adapter setup is version checked and atomically rolled back on failure.

## Evidence ownership

An evaluation campaign freezes repository state, fixture digests, prompt hashes,
verifier commands, seeds, candidate identity, and concurrency. Trial artifacts
contain before/after workspace digests, verifier output, model-call metadata, and
optional trace paths. Inventory receipts protect artifact byte lengths and SHA-256
digests; offline rebuild rejects missing, unexpected, or modified evidence.

The runtime benchmark exercises the production scheduler, Agent loop, and trace
recorder. Its fixed-delay tool is synthetic and must not be presented as real
filesystem or network latency.

## Security boundary

Project trust controls ambient project-input loading. It is not a filesystem,
process, network, tool, or prompt-injection sandbox. Python extensions and Gateway
adapters execute with the current OS user's permissions. Use an OS sandbox,
container, VM, restricted credentials, and network policy when isolation is needed.

For changes, inspect the relevant implementation under `run_agent_core`,
`run_agent_coding/extensions`, `run_agent_gateway`, or `run_agent_evals`; update
the corresponding installed document and cover lifecycle behavior with deterministic
tests.
