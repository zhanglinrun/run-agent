# Project trust and security

Run Agent resolves trust for the canonical destination cwd before reading ambient
project Markdown/JSON or importing project extensions. Protected inputs include
project skills, prompts, themes, system-prompt files, `AGENTS.md` context,
extension candidates, and reserved project settings.

Interactive users can save exact or displayed-parent decisions or choose a
run-only result. Headless `ask`/`never` decisions decline project inputs;
`--approve` and `--no-approve` are run-only overrides. Cancelling an interactive
trust decision exits startup or preserves the current session during reload and
replacement. Trust is committed only after the staged session is adopted.

## General boundary

Project trust is an input-loading guard, not a filesystem, process, shell,
network, tool, credential, provider, model, package-install, prompt-injection,
or exfiltration sandbox. Extensions execute arbitrary Python. Use an OS
sandbox, container, VM, remote environment, and restricted credentials/network
when isolation is required.

Ollama, local OpenAI-compatible servers, and gateways use the manual custom-provider
path. Review extension code before loading it and keep secrets out of project files
and diagnostics.
