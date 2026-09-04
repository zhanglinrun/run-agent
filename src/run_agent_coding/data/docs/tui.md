# Run Agent TUI

Run Agent's interactive interface uses Textual behind an adapter boundary. The
portable `run_agent_core` harness emits provider-neutral events; the TUI renders
them and owns interaction. Ctrl+P cycles forward through scoped models;
Shift+Ctrl+P cycles backward.

The sidebar usage section shows `avg TPS` and `avg TTFT` across timed session
history. Effective TPS uses the accumulated time Run Agent spends awaiting provider
events, including provider queueing, network waits, prefill, and TTFT; it
excludes Run Agent's rendering and persistence between stream pulls. TPS is
token-weighted. TTFT is the arithmetic mean of provider-wait time through Run Agent's
first text, thinking, or tool-call output event. Older assistant messages
without persisted timing still count toward token usage but not these metrics.

## `/model`

The picker renders cached/bundled choices immediately, then refreshes remote
catalogs in the background and updates the open list. Refreshes are throttled to
four hours and failures leave the existing list usable. Use
`run-agent update --models` for forced revalidation or `RUN_AGENT_OFFLINE=1` to disable
catalog network access.

Do not introduce Textual dependencies into `run_agent_core`. Keep reusable behavior
in the harness/session layers and UI behavior in this adapter. Use Textual pilot
tests and deterministic fake providers for interaction tests.
