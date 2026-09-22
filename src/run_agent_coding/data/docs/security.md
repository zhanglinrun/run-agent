# Project trust and security

Run Agent resolves trust for the canonical destination cwd before reading ambient
project Markdown/JSON or importing project extensions. Protected inputs include
project skills, prompts, system-prompt files, `AGENTS.md` context, project
extensions, memory and project settings.

Interactive users can save exact or displayed-parent decisions or choose a
run-only result. Without a saved decision or explicit override, headless
`ask`/`never` defaults decline project inputs. `--trust-project` approves project
inputs for this invocation. Set `defaultProjectTrust` (`ask`, `always`, or `never`)
in the user settings file; project settings cannot override it. Cancelling an interactive
trust decision exits startup or preserves the current session during reload and
replacement. Trust is committed only after the staged session is adopted.

Settings live in `~/.run/settings.json` and trusted `<cwd>/.run/settings.json`;
saved trust decisions live in `~/.run/trust.json`. `--state-dir` relocates the
user state directory.

## General boundary

Project trust is an input-loading guard, not a filesystem, process, shell,
network, tool, credential, provider, model, package-install, prompt-injection,
or exfiltration sandbox. Extensions execute arbitrary Python. Use an OS
sandbox, container, VM, remote environment, and restricted credentials/network
when isolation is required.

Staged extension adoption protects the live registration set, but setup imports and trusted
extension code can still perform direct file or network effects before publication. Only
source-owned registrations, managed tasks and callbacks explicitly registered with
`register_disposer()` participate in retirement. A failed publication preserves the old
runtime and cleans the staged runtime; it cannot undo arbitrary Python effects.

Experience project probes are read-only and require trusted project inputs. Probe paths must
be relative UTF-8 files inside the canonical cwd; absolute paths, `..`, symlinks/junctions,
oversized files, shell and network access are refused. A matching SHA-256 proves only that
the probed bytes did not drift; it does not prove a natural-language claim is semantically true.

Review extension code before loading it and keep secrets out of project files
and diagnostics.

The default `permission_policy` extension is my-pi-agent's `PermissionGate` on
`tool_call`: `review` (default) auto-allows `read`/`grep`/`find` and safe bash
prefixes, then prompts when a UI is attached; `yolo`/`autonomous` allows all;
`strict` prompts every tool including reads. Missing confirm callback allows the
call. This is not an OS sandbox. The `bash` tool has no default timeout: callers
must supply one when a command needs a time limit.
