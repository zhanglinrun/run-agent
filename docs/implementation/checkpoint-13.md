# Checkpoint 13: extension context resource snapshots

Date: 2026-09-10. The full redesign remains active.

## Implemented

- Session extensions register `api.register_resource_provider(name, selector, version=...)`.
  A synchronous selector receives a detached `ResourceView`, returning `ResourceSelection`
  values with scope, key, immutable version, title, kind and estimated token budget.
- The Coding host reads all registered sources and session/project/user scopes in one
  SQLite read transaction. No writes, task submission or live reads are available to the
  staged selector. Source identities and scopes come from the host, not model arguments.
- The host resolves selected content from that captured view, verifies its content hash,
  limits total capture size and context contribution size, and persists the complete
  resource snapshot with the activation marker. Binding replacement and the marker still
  share the existing commit transaction. Selector or publication failure leaves the live
  runtime and prompt unchanged. Failed setup removes its resource registrations.
- Published resource heads do not silently replace active prompt content. `/reload`
  captures new heads; resume, history branch and Gateway background sessions restore
  the saved contributions. Model request evidence includes the actual resource input.
- Snapshots identify extension Python source packages/manifests and tool definitions,
  Python execution/preparation entry points. Resume and branching reject changed versions.
  Extension source changes during a live session are checked before model and tool calls.
- Extension entry points and relative Python imports load current source directly, avoiding
  timestamp-valid stale pyc files. Package hashes include Python sources and TOML manifests.
- `run --session ID --refresh-resources` explicitly opts into current resources and records
  a `refresh` activation. It provides a path forward after extension upgrades. It requires
  a session argument and is rejected for pinned background resources. This is not an old
  schema migration or compatibility adapter.

## Extension Example

```python
from run_agent_coding.extensions import ResourceSelection


def setup(api):
    def notes(view):
        return [
            ResourceSelection("project", key, version, key, max_tokens=2048)
            for key, version in view.heads("project").items()
            if key == "MEMORY.md"
        ]

    api.register_resource_provider("notes", notes, version="1")
```

Resource creation and head publication use the existing scoped ResourceService after
activation. Reading Markdown, proposing candidates and deciding whether to publish remain
extension responsibilities. No USER/MEMORY business policy was added to Core or Gateway.

## Evidence

- `resource-provider-tests.xml`: Windows full suite, 187 passed and 2 platform skips.
- `resource-provider-linux-tests.xml`: WSL Ubuntu resource/Skill/context suite, 24 passed.
- `test_extension_resources.py`: 10 actual integration tests cover consistent capture,
  source/principal/project/session isolation, explicit refresh, active prompt immutability,
  resume, branching, Gateway background input, publication rollback, context budget failure,
  failed setup, stale API rejection, changed implementations and stale package bytecode.
- Mypy passes 168 source files; Ruff passes. Wheel/sdist in `.run/redesign/dist-13` and a
  clean install in `.run/redesign/install-env-13` passed `validate_distribution.py`.
  `distribution-check-13.json` includes installed extension resource capture/reload/resume,
  launcher argument checks and existing terminal/Gateway/recovery smoke scenarios.
- No real model or real channel credentials were used. These results validate contracts,
  not coding success rate or improvement from learned experience.

## Limits and Remaining Work

Shared database schema remains 8; Gateway schema remains 5. Resource marker builder is
`coding-resources-v2`, with no v1 decoder or migration. Fresh initialization is supported.

Selectors must be short synchronous transformations; arbitrary Python extensions are
trusted and are not sandboxed. Token budgets use the existing approximate counter, plus
explicit byte limits; they are not provider-tokenizer guarantees. Captured heads may be
superseded concurrently; their immutable versions and complete content remain fixed.

Code identity is not a complete execution environment image. Third-party dependencies,
closure state, external helper behavior and remote MCP server versions are not all pinned.
Source path identities remain local to the installation. Native callable tool entries
without a Python code identity are rejected. Backup relocation and dynamic MCP capability
versioning need further design and verification.

Resource manifests describe captured content and source versions; they do not yet expose
the planned experience policies, candidate review/promotion, EvaluationService, durable
Gateway extension handlers or full idle-extension-turn integration. Those remain open.
