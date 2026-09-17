"""The value types a review decision is made from, and the prompts a review runs on.

The policy, the evidence a completion carries, the decision and the foreground gate are
data; the trigger and coordinator in ``review`` are behaviour, and the bounded tool loop
a review runs lives in ``review_agent``.

A review is a forked agent in hermes-agent's design: after a finished turn it replays
the conversation, gets one of three review instructions as its user message (memory,
skills, or both, depending on which nudge fired) and calls the memory and skill tools
directly, so the next session starts already corrected. The instructions below are
hermes' ``_MEMORY_REVIEW_PROMPT``, ``_SKILL_REVIEW_PROMPT`` and
``_COMBINED_REVIEW_PROMPT`` with only the tool and command names adjusted to ours.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from run_agent_coding.thinking import ThinkingLevel

from .worker import ReviewBudget

# A review must never be triggered by another review, by the evaluating path, or by the
# naming path: those are auxiliary work, not a user turn worth learning from.
AUXILIARY_ORIGINS = frozenset({"review", "evaluation", "naming"})
REVIEW_REQUEST_PREFIX = "review-request:"
REVIEW_TASK_PREFIX = "review-task:"
REVIEW_CONSUMED_PREFIX = "review-consumed:"

# Phrases that mark a user turn as a correction of how the agent worked. hermes reviews
# on its nudge cadence only; treating a correction (or a failed run) as a review signal
# is this extension's addition, switched off with ``EXPERIENCE_REVIEW_ON_SIGNALS=false``.
CORRECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(stop|don't|do not|never|quit)\s+(doing|using|adding|explaining|formatting|writing)\b",
        r"\b(too|way too)\s+(verbose|long|wordy|chatty)\b",
        r"\bjust\s+(give|show|tell)\s+me\b",
        r"\b(remember|note)\s+(this|that)\b",
        r"\b(that's|that is|this is)\s+(wrong|incorrect|not what)\b",
        r"\bi\s+(said|asked|told you)\b",
        r"\byou\s+(always|keep|again)\b",
        r"不要(再|总是|每次)?(这样|那样|解释|加|用|写)",
        r"(太|过于)(啰嗦|长|复杂|多)",
        r"(记住|记一下|以后)(这个|这点|都要|不要)",
        r"(不对|错了|不是这个意思|我说的是)",
        r"(直接|只要)(给我|告诉我|说)",
    )
)

# The harness half of the review's system prompt: what the fork is, how it calls tools
# through a single completion per step, and what it may not do. The task half (which
# store to review) is one of the three instructions below, sent as the user message the
# way hermes sends its review prompt to the forked agent.
REVIEW_SYSTEM_PROMPT = (
    "You are the background self-improvement review of an AI coding agent. A turn just "
    "finished; the conversation is replayed for you below, followed by the review "
    "instruction. You work in a bounded tool loop: each of your replies is either "
    "exactly one tool call or your final answer.\n\n"
    "Use the native memory and skill_manage tools provided with the request. Their "
    "results are returned by the review runtime on the next model interaction. "
    "Do not claim a write succeeded before its tool result confirms it.\n\n"
    "For providers without native tool calling, the runtime also accepts ONLY a JSON "
    "object and nothing else:\n"
    '{"tool": "memory", "args": {...}}  or  {"tool": "skill_manage", "args": {...}}\n'
    "One call per reply. The tool result arrives in the next message. When you are done, "
    "or there is nothing worth saving, reply with a short plain-text summary instead of a "
    "tool call ('Nothing to save.' when nothing was written).\n\n"
    "memory args: target ('user' = who the user is, USER.md; 'memory' = your notes about "
    "this project, MEMORY.md), action ('add' with content; 'replace' with old_text and "
    "new_content; 'remove' with old_text), or operations (a list of those, applied "
    "all-or-nothing against the final character budget; preferred for several changes). "
    "old_text is a short unique substring of the entry to change.\n"
    "skill_manage args: action ('list', 'view', 'create', 'edit', 'patch', 'write_file', "
    "'remove_file', 'delete'), name (lowercase slug), scope ('project' or 'user'), "
    "description (one sentence, at most 60 characters, for create), body (full SKILL.md "
    "body for create/edit), file_path (defaults to SKILL.md; support files under "
    "references/, templates/, scripts/ or assets/), old_text and new_text (patch), "
    "content (write_file), absorbed_into (the umbrella that absorbed a skill you delete; "
    "the delete is an archive and needs it). 'view' a skill before you patch it: a patch "
    "to content you have not loaded in this review is refused.\n\n"
    "You can only call memory and skill management tools. Other tools will be denied at "
    "runtime — do not attempt them."
)

MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)

SKILL_REVIEW_PROMPT = (
    "Review the conversation above and update the skill library. Be "
    "ACTIVE — most sessions produce at least one skill update, even if "
    "small. A pass that does nothing is a missed learning opportunity, "
    "not a neutral outcome.\n\n"
    "Target shape of the library: CLASS-LEVEL skills, each with a rich "
    "SKILL.md and a `references/` directory for session-specific detail. "
    "Not a long flat list of narrow one-session-one-skill entries. This "
    "shapes HOW you update, not WHETHER you update.\n\n"
    "Signals to look for (any one of these warrants action):\n"
    "  • User corrected your style, tone, format, legibility, or "
    "verbosity. Frustration signals like 'stop doing X', 'this is too "
    "verbose', 'don't format like this', 'why are you explaining', "
    "'just give me the answer', 'you always do Y and I hate it', or an "
    "explicit 'remember this' are FIRST-CLASS skill signals, not just "
    "memory signals. Update the relevant skill(s) to embed the "
    "preference so the next session starts already knowing.\n"
    "  • User corrected your workflow, approach, or sequence of steps. "
    "Encode the correction as a pitfall or explicit step in the skill "
    "that governs that class of task.\n"
    "  • Non-trivial technique, fix, workaround, debugging path, or "
    "tool-usage pattern emerged that a future session would benefit "
    "from. Capture it.\n"
    "  • A skill that got loaded or consulted this session turned out "
    "to be wrong, missing a step, or outdated. Patch it NOW.\n\n"
    "Preference order — prefer the earliest action that fits, but do "
    "pick one when a signal above fired:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Look back through the "
    "conversation for skills the user loaded via /skill:name or you "
    "read via skill_manage view. If any of them covers the territory of the "
    "new learning, PATCH that one first. It is the skill that was in "
    "play, so it's the right one to extend — but only if it is "
    "curator-managed. Pinned and user-owned skills are "
    "off-limits to you no matter how relevant (see Protected skills "
    "below); for those, fall through to the next option.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (via skill_manage list + skill_manage view). "
    "If no loaded skill fits but an existing class-level skill does, "
    "patch it. Add a subsection, a pitfall, or broaden a trigger.\n"
    "  3. ADD A SUPPORT FILE under an existing umbrella. Skills can be "
    "packaged with three kinds of support files — use the right "
    "directory per kind:\n"
    "     • `references/<topic>.md` — session-specific detail (error "
    "transcripts, reproduction recipes, provider quirks) AND "
    "condensed knowledge banks: quoted research, API docs, external "
    "authoritative excerpts, or domain notes you found while working "
    "on the problem. Write it concise and for the value of the task, "
    "not as a full mirror of upstream docs.\n"
    "     • `templates/<name>.<ext>` — starter files meant to be "
    "copied and modified (boilerplate configs, scaffolding, a "
    "known-good example the agent can `reproduce with modifications`).\n"
    "     • `scripts/<name>.<ext>` — statically re-runnable actions "
    "the skill can invoke directly (verification scripts, fixture "
    "generators, deterministic probes, anything the agent should run "
    "rather than hand-type each time).\n"
    "     Add support files via skill_manage action=write_file with "
    "file_path starting 'references/', 'templates/', or 'scripts/'. "
    "The umbrella's SKILL.md should gain a one-line pointer to any "
    "new support file so future agents know it exists.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA SKILL when no existing "
    "skill covers the class. The name MUST be at the class level. "
    "The name MUST NOT be a specific PR number, error string, feature "
    "codename, library-alone name, or 'fix-X / debug-Y / audit-Z-today' "
    "session artifact. If the proposed name only makes sense for "
    "today's task, it's wrong — fall back to (1), (2), or (3).\n\n"
    "User-preference embedding (important): when the user expressed a "
    "style/format/workflow preference, the update belongs in the "
    "SKILL.md body, not just in memory. Memory captures 'who the user "
    "is and what the current situation and state of your operations "
    "are'; skills capture 'how to do this class of task for this "
    "user'. When they complain about how you handled a task, the "
    "skill that governs that task needs to carry the lesson.\n\n"
    "If you notice two existing skills that overlap, note it in your "
    "reply — the background curator handles consolidation at scale.\n\n"
    "Protected skills (DO NOT edit these):\n"
    "  • PINNED skills (marked via '/curator pin'). You are an "
    "autonomous no-user-present actor, so pin blocks your writes too — "
    "content updates included. Only the user, in a foreground session, "
    "can change a pinned skill.\n"
    "  • USER-OWNED skills — anything not curator-managed. A skill the "
    "user hand-wrote, installed by URL, or asked a foreground agent to "
    "create is theirs, not yours; your writes to it WILL be refused. "
    "This includes skills that were loaded or consulted this session: "
    "being in play does not make one yours to edit. Mark these as "
    "'[user-owned, do not patch]' in your reasoning and do not call patch on them. "
    "If such a skill is wrong or outdated, say so in your reply and recommend "
    "'/curator adopt <name>' — do not try to patch it.\n"
    "If the only skills that need updating are protected, say\n"
    "'Nothing to save.' and stop.\n\n"
    "Do NOT capture (these become persistent self-imposed constraints "
    "that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken', 'cannot use Y from execute_code'). These "
    "harden into refusals the agent cites against itself for months "
    "after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' or 'analyze this PR' is not a class of work that warrants "
    "a skill.\n\n"
    "  • Unresolved failures: if the session ended WITHOUT actually "
    "finding a working method — you tried several things, none worked, "
    "and told the user to check manually — do NOT write those attempts "
    "up as a 'reliable workflow' or 'recommended approach'. That presents "
    "an untested sequence of failures as validated guidance a future "
    "session will trust and repeat. Either say 'Nothing to save', or, "
    "only if you are independently confident of a real working alternative "
    "(not something you are merely guessing might work), capture ONLY that "
    "alternative — never the dead ends, and never dressed up as best practice.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint.\n\n"
    "'Nothing to save.' is a real option but should NOT be the "
    "default. If the session ran smoothly with no corrections and "
    "produced no new technique, just say 'Nothing to save.' and stop. "
    "Otherwise, act."
)

COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and update two things:\n\n"
    "**Memory**: who the user is. Did the user reveal persona, "
    "desires, preferences, personal details, or expectations about "
    "how you should behave? Save facts about the user and durable "
    "preferences with the memory tool.\n\n"
    "**Skills**: how to do this class of task. Be ACTIVE — most "
    "sessions produce at least one skill update. A pass that does "
    "nothing is a missed learning opportunity, not a neutral outcome.\n\n"
    "Target shape of the skill library: CLASS-LEVEL skills with a rich "
    "SKILL.md and a `references/` directory for session-specific detail. "
    "Not a long flat list of narrow one-session-one-skill entries.\n\n"
    "Signals that warrant a skill update (any one is enough):\n"
    "  • User corrected your style, tone, format, legibility, "
    "verbosity, or approach. Frustration is a FIRST-CLASS skill "
    "signal, not just a memory signal. 'stop doing X', 'don't format "
    "like this', 'I hate when you Y' — embed the lesson in the skill "
    "that governs that task so the next session starts fixed.\n"
    "  • Non-trivial technique, fix, workaround, or debugging path "
    "emerged.\n"
    "  • A skill that was loaded or consulted turned out wrong, "
    "missing, or outdated — patch it now.\n\n"
    "Preference order for skills — pick the earliest that fits:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Check what skills were "
    "loaded via /skill:name or skill_manage view in the conversation. If one "
    "of them covers the learning, PATCH it first. It was in play; "
    "it's the right place — provided it is curator-managed. Protected "
    "and user-owned skills are off-limits however relevant; fall "
    "through when one of those is the best fit.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (skill_manage list + skill_manage view to "
    "find the right one). Patch it.\n"
    "  3. ADD A SUPPORT FILE under an existing umbrella via "
    "skill_manage action=write_file. Three kinds: "
    "`references/<topic>.md` for session-specific detail OR condensed "
    "knowledge banks (quoted research, API docs excerpts, domain "
    "notes) written concise and task-focused; `templates/<name>.<ext>` "
    "for starter files meant to be copied and modified; "
    "`scripts/<name>.<ext>` for statically re-runnable actions "
    "(verification, fixture generators, probes). Add a one-line "
    "pointer in SKILL.md so future agents find them.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA when nothing exists. "
    "Name at the class level — NOT a PR number, error string, "
    "codename, library-alone name, or 'fix-X / debug-Y' session "
    "artifact. If the name only fits today's task, fall back to (1), "
    "(2), or (3).\n\n"
    "User-preference embedding: when the user complains about how "
    "you handled a task, update the skill that governs that task — "
    "memory alone isn't enough. Memory says 'who the user is and "
    "what the current situation and state of your operations are'; "
    "skills say 'how to do this class of task for this user'. Both "
    "should carry user-preference lessons when relevant.\n\n"
    "If you notice overlapping existing skills, mention it — the "
    "background curator handles consolidation.\n\n"
    "Protected skills (DO NOT edit these):\n"
    "  • PINNED skills (marked via '/curator pin'). Pin blocks "
    "autonomous writes entirely — content updates included — because no "
    "user is present to consent. Only a foreground session can change one.\n"
    "  • USER-OWNED skills — anything not curator-managed (hand-written, "
    "URL-installed, or created by a foreground agent at the user's "
    "request). Your writes to these WILL be refused, including to skills "
    "loaded or consulted this session. If one is wrong, say so in your "
    "reply and recommend '/curator adopt <name>' instead.\n"
    "If the only skills that need updating are protected, say\n"
    "'Nothing to save.' and stop.\n\n"
    "Do NOT capture as skills (these become persistent self-imposed "
    "constraints that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken', 'cannot use Y from execute_code'). These "
    "harden into refusals the agent cites against itself for months "
    "after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' or 'analyze this PR' is not a class of work that warrants "
    "a skill.\n\n"
    "  • Unresolved failures: if the session ended WITHOUT actually "
    "finding a working method — you tried several things, none worked, "
    "and told the user to check manually — do NOT write those attempts "
    "up as a 'reliable workflow' or 'recommended approach'. That presents "
    "an untested sequence of failures as validated guidance a future "
    "session will trust and repeat. Either say 'Nothing to save', or, "
    "only if you are independently confident of a real working alternative "
    "(not something you are merely guessing might work), capture ONLY that "
    "alternative — never the dead ends, and never dressed up as best practice.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint.\n\n"
    "Act on whichever of the two dimensions has real signal. If "
    "genuinely nothing stands out on either, say 'Nothing to save.' "
    "and stop — but don't reach for that conclusion as a default."
)

# hermes appends this to every review prompt so the fork does not reach for tools it
# would be denied.
REVIEW_TOOL_NOTE = (
    "You can only call memory and skill management tools. Other tools will be denied "
    "at runtime — do not attempt them. Protected skills are marked "
    "'[user-owned, do not patch]' and must not be modified. Use the skill view tool before "
    "proposing a patch so the target body is loaded in this review pass."
)

REVIEW_DENIED_TOOL = (
    "Background review denied non-whitelisted tool: {tool_name}. "
    "Only memory/skill tools are allowed."
)


def choose_review_prompt(*, review_memory: bool, review_skills: bool, focus: str = "") -> str:
    """Pick the instruction for the nudges that fired, with the user's focus appended.

    Both nudges pick the combined instruction, only the memory nudge the memory one,
    anything else (a skill nudge, a manual review, a correction) the skill one, exactly
    as hermes' ``spawn_background_review_thread`` chooses.
    """
    if review_memory and review_skills:
        prompt = COMBINED_REVIEW_PROMPT
    elif review_memory:
        prompt = MEMORY_REVIEW_PROMPT
    else:
        prompt = SKILL_REVIEW_PROMPT
    if review_memory:
        prompt = (
            "Compare the conversation with the current saved assets below. Review each "
            "asset independently: durable user preferences belong in USER.md "
            "(memory target=user); stable project facts belong in MEMORY.md "
            "(memory target=memory); a reusable successful procedure belongs in a Skill. "
            "An assistant acknowledgement is not evidence that a fact was saved. "
            "When the user explicitly asks to remember a durable preference or project "
            "fact that is absent, save it with the appropriate memory call. Skip duplicates, "
            "temporary facts and unsupported claims. A decision not to edit a Skill must "
            "not discard an independent memory update. Before concluding 'Nothing to save.', "
            "check all applicable stores against their actual saved content.\n\n"
            f"{prompt}"
        )
    focus = (focus or "").strip()
    if focus:
        prompt = (
            f"{prompt}\n\n"
            "The user explicitly requested this review with the following "
            f"focus — prioritize it over the general instructions above:\n{focus}"
        )
    return f"{prompt}\n\n{REVIEW_TOOL_NOTE}"


def looks_like_correction(text: str) -> bool:
    """Whether one user message reads as a correction of how the agent worked."""
    return any(pattern.search(text) for pattern in CORRECTION_PATTERNS)


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    """Startup configuration; versioned so a policy change may re-review a run."""

    policy_version: str = "1"
    cooldown_seconds: float = 900.0
    min_assistant_turns: int = 2
    # Every this many runs a review is admitted regardless, the fallback cadence for a
    # host that reports no nudge counters; the coordinator normally feeds hermes' two
    # nudge flags instead. 0 switches the cadence off.
    review_every_turns: int = 10
    # A failed run or a user correction admits a review on its own (this extension's
    # addition to hermes' cadence-only trigger; off by default).
    review_on_signals: bool = False
    # A completion event arrives while its own session is still unwinding, so a review
    # submitted there always finds the foreground busy. Waiting a bounded time is what
    # turns that race into a short delay, and the request's own retry covers the rest, so
    # this stays short enough not to hold a task slot while a busy session works.
    foreground_wait_seconds: float = 2.0
    foreground_poll_seconds: float = 0.05
    # A session that never goes idle would otherwise accumulate one abandoned task row
    # per completed run. Reviewing is best effort, so a request is retried a bounded
    # number of times and then left alone.
    max_submissions: int = 3
    # The review fork's ceilings: hermes' 16-iteration loop and 600k aggregate input
    # tokens (0 = uncapped), and how long a live turn waits for a cancelled review.
    max_iterations: int = 16
    max_input_tokens: int = 600_000
    thinking_level: ThinkingLevel = "off"
    max_output_tokens: int = 1600
    cancel_timeout_seconds: float = 2.0
    # Only the tail of the replayed conversation that fits the budget is shown.
    evidence_chars: int = 200_000

    @property
    def budget(self) -> ReviewBudget:
        """The bounded task declaration stored with the host job."""
        return self.execution_budget

    @property
    def execution_budget(self) -> ReviewBudget:
        """The configured budget enforced by the review loop itself."""
        return ReviewBudget(
            max_model_requests=self.max_iterations,
            max_input_tokens=self.max_input_tokens,
            max_output_tokens=16_000,
        )


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """The evidence a durable completion carries into the trigger."""

    source_run_id: str
    session_id: str
    status: str
    assistant_turns: int
    corrections: int
    failures: int
    origin_kind: str = "user"
    runs_since_review: int = 0
    # hermes' two nudges, computed by the host's counters for this run.
    memory_due: bool = False
    skills_due: bool = False


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """Whether to review, why not when not, and the idempotency key."""

    admitted: bool
    reason: str
    key: str


@dataclass(frozen=True, slots=True)
class ForegroundGate:
    """Whether a foreground run is in flight, so a review can yield to it.

    The gate belongs at the point where a review would start, not where one is queued: a
    completion event arrives while its own session is still running, so checking the
    foreground while queueing would defer every review forever.
    """

    busy: Callable[[], bool]

    def deferral(self) -> str | None:
        """The reason to hold the review back, or ``None`` to proceed."""
        return "foreground busy" if self.busy() else None

    async def wait_idle(self, *, timeout: float, interval: float) -> str | None:
        """Yield to the foreground for a bounded time; the reason left, or ``None``."""
        reason = self.deferral()
        if reason is None or timeout <= 0:
            return reason
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            reason = self.deferral()
            if reason is None:
                return None
        return reason


def summarize_edits(applied: list[str], refused: list[str], *, verbose: bool = True) -> str:
    """Render a compact review result for command/status callers.

    The current review protocol writes through the memory and skill tools; this helper
    only formats the tool loop's action receipts and does not recreate the removed
    edit-list protocol.
    """
    memory_count = sum(item.startswith("user/") or item.startswith("memory/") for item in applied)
    skill_count = sum(item.startswith("skill ") for item in applied)
    refused_count = len(refused)
    parts = [f"review: memory updated ({memory_count})", f"skills updated ({skill_count})"]
    if refused_count:
        parts.append(f"{refused_count} refused")
    if verbose and applied:
        parts.append("; ".join(applied))
    return ", ".join(parts)


__all__ = [
    "AUXILIARY_ORIGINS",
    "COMBINED_REVIEW_PROMPT",
    "CORRECTION_PATTERNS",
    "MEMORY_REVIEW_PROMPT",
    "REVIEW_CONSUMED_PREFIX",
    "REVIEW_DENIED_TOOL",
    "REVIEW_REQUEST_PREFIX",
    "REVIEW_SYSTEM_PROMPT",
    "REVIEW_TASK_PREFIX",
    "REVIEW_TOOL_NOTE",
    "SKILL_REVIEW_PROMPT",
    "ForegroundGate",
    "ReviewDecision",
    "ReviewPolicy",
    "ReviewRequest",
    "choose_review_prompt",
    "looks_like_correction",
]
