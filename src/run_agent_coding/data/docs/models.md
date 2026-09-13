# Run Agent providers and models

A provider is one of two wire protocols; a model is the exact ID the endpoint
accepts. Everything is configured from the environment (a project `.env` is
loaded at startup without overriding real process variables), the way Pi resolves
ambient API keys.

There is no provider catalog file or OAuth/login flow. Use the endpoint's exact
model ID and keep API keys in the environment or a local, uncommitted `.env`.

## Providers

| Provider | Protocol | Variables |
| --- | --- | --- |
| `openai` | OpenAI-compatible chat completions or responses | `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_API` |
| `anthropic` | Anthropic Messages | `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_THINKING_MODE` |

`OPENAI_API` is `openai-completions` (default) or `openai-responses`. Any endpoint
that speaks one of these protocols works: point `OPENAI_BASE_URL` at a gateway,
a local server or a vendor host. The reasoning dialect for `deepseek.com`,
`api.z.ai`, `api.together.ai` and `openrouter.ai` is detected from the URL and
can be forced with `OPENAI_THINKING_FORMAT`.

`ANTHROPIC_BASE_URL` gets `/v1` appended when it is missing.
`ANTHROPIC_THINKING_MODE` is `budget` (default, extended thinking with a token
budget per level) or `adaptive` (an `effort` value, for models that expect one).

## Selection

- `PROVIDER` chooses the startup provider. When unset, `anthropic` is used if only
  `ANTHROPIC_API_KEY` is set, otherwise `openai`.
- `MODEL` sets the default model; `--model` overrides it for one run.
- `--provider` overrides `PROVIDER` for one run.
- `/model <id>` switches the model in a session; `/model` lists the choices.
- `run --providers` shows both providers and whether their key is set.

Model IDs are not validated against a list: the endpoint decides. A wrong ID
fails on the first request with the provider's own error.

## Thinking

Run Agent thinking levels match Pi: `off`, `minimal`, `low`, `medium`, `high`,
`xhigh` and `max`. `REASONING_EFFORT` sets the default, `--thinking` overrides it
for one run and `/thinking <level>` changes it in a session. The level is sent as
`reasoning_effort` (or the vendor dialect) on OpenAI-compatible endpoints and as
an extended-thinking budget or `effort` on Anthropic. Whether a given model
honours it is up to the endpoint.

## Transport and metadata

Per provider prefix (`OPENAI_` or `ANTHROPIC_`): `_TIMEOUT_SECONDS` (60),
`_MAX_RETRIES` (2), `_MAX_RETRY_DELAY_SECONDS` (1.0).

Endpoints that do not report limits can be described with `MODEL_CONTEXT_WINDOW`,
`MODEL_MAX_TOKENS` and `MODEL_SUPPORTS_IMAGES`. When the endpoint reports a
context window at runtime, that value wins.

## Dynamic providers

Extensions may register process-local OpenAI-compatible providers through the
extension API. Their definitions and refresh snapshots belong to the active
extension generation and are never persisted; secrets resolve immediately before
runtime creation and are excluded from representations, diagnostics, snapshots,
sessions and exports.
