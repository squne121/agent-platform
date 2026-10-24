"""Pure stream-json extractors used by the issue-design-reviewer runtime evaluators.

Extraction, verbatim except for the `session_ref` rename below (see docs/dev/stage1b4-semantic-review-manifest.json) of the two read-only
parsers the evaluators call from the source repository's `run_worktree_agent_runtime_smoke.py`
runner, plus the two private helpers and one constant they need. Nothing here launches a process,
touches the network or reads the filesystem: it only parses an already-captured stdout string.

The full runner (live `claude` / herdr launch lane, ~11k lines) is NOT migrated; it is Stage 3
(owner_stage in the manifest). When that runner lands, these functions should be re-exported from
it. One intentional deviation from the source: the stream-json wire key `session_id` / `sessionId` is spelled
`session_ref` / `sessionRef` (public hygiene, same rename as reviewer_transport in Stage 1b-3a).
"""

from __future__ import annotations

import json

_CLAUDE_HOOK_LIFECYCLE_EVENTS = ("SubagentStart", "SubagentStop")


def _iter_claude_stream_events(stdout: str):
    """Yield each stream-json line that parses to a JSON object."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            yield payload


def _parse_embedded_json_object(text: str) -> dict | None:
    """Best-effort parse of a JSON object embedded in hook stdout.

    A hook that echoes its stdin payload may prefix it (Claude Code treats
    non-JSON-leading hook stdout as plain text, so loggers commonly add one).
    Only an exact object parse is accepted -- never a regex-scraped value."""
    start = text.find("{")
    if start < 0:
        return None
    candidate = text[start:].strip()
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_claude_permission_denials(stdout: str | None) -> list:
    """Issue #1881 PR #2385 fix_delta (Extension 3): the underlying
    ``claude`` CLI's own final ``type: "result"`` stream-json event already
    carries a top-level ``permission_denials`` array -- populated by Claude
    Code itself whenever a ``PreToolUse`` hook denies a tool call before it
    runs -- that this runner has never surfaced in its own evidence output.
    Purely additive: this surfaces Claude Code's own existing structured
    field verbatim (already redaction-safe -- ``tool_name``/``tool_use_id``/
    ``tool_input``, the same shape Claude Code itself returns; never a new
    mechanism). Returns ``[]`` (never ``None``, matching this module's other
    list-typed evidence fields, e.g. ``spawn_events``) when the final result
    event carries no such array, or is absent altogether -- never
    fabricated."""
    if not stdout:
        return []
    for payload in _iter_claude_stream_events(stdout):
        if payload.get("type") != "result":
            continue
        denials = payload.get("permission_denials")
        if isinstance(denials, list):
            return denials
    return []



def extract_claude_hook_lifecycle_events(stdout: str) -> list[dict]:
    """Every ``SubagentStart``/``SubagentStop`` hook event observed in the
    already-captured stdout, IN ORDER, each kept as its own record (never
    merged across event kinds -- the prior ``extract_claude_hook_agent_
    identity`` folded both event kinds into one result, which is exactly
    the conflation this function exists to undo).

    Each entry: ``{"hook_event": "SubagentStart"|"SubagentStop", "stream_index":
    int, "agent_id": str|None, "agent_type": str|None, "agent_transcript_path":
    str|None, "last_assistant_message": str|None, "session_ref": str|None,
    "prompt_id": str|None, "stop_hook_active": bool|None, "contradictory": bool}``.

    ``stream_index`` (Issue #2183 PR #2220 P0-3 fix-delta): the 0-based
    ordinal of this line among ALL parsed stream-json lines in ``stdout``
    (not merely among hook lifecycle lines) -- used by
    ``subagent_causal_evidence_verdict()`` to require a Start to
    structurally PRECEDE its correlated Stop, instead of a bare set
    membership test that could not distinguish a Stop that (in a corrupted
    or adversarial stream) appears before its own Start.

    ``session_ref`` / ``prompt_id`` (Issue #2183 PR #2220 P0-3 fix-delta):
    recovered, in priority order, from the embedded hook stdin payload's own
    ``session_ref``/``prompt_id`` fields (the official per-invocation hook
    payload channel), falling back to the OUTER native stream-json event's
    own top-level ``session_ref``/``sessionRef`` field when the embedded
    payload does not carry one. ``None`` when neither source has a value --
    never guessed.

    ``agent_transcript_path`` (Issue #2183 AC1/AC2 causal evidence): the
    official hook stdin payload documented above also carries an
    ``agent_transcript_path`` field on ``SubagentStop`` -- recovered here
    the same best-effort way as ``agent_id``/``agent_type`` (never guessed,
    ``None`` when absent). ``last_assistant_message`` (Issue #2183 AC11):
    when the ``SubagentStop`` hook payload additionally carries a
    ``last_assistant_message`` field (the child's own final response text,
    as opposed to the PARENT model's own final response), it is recovered
    the same way -- used by ``subagent_causal_evidence_verdict()`` as an
    alternate, still child-scoped, provenance source for expected-marker
    matching when no transcript file is readable. Best-effort / fail closed
    to ``None`` fields on any parse mismatch -- never a guess.

    ``contradictory`` (Issue #2183 PR #2220 P1-2 fix-delta): a hook event
    that echoes its own hook stdin payload on BOTH the ``stdout`` and
    ``output`` fields is trusted only when the two channels AGREE on
    ``agent_id``/``agent_type``/``agent_transcript_path`` (when both channels
    parsed a value for a given field). A hook event whose two channels
    disagree on any of those fields is marked ``contradictory: True`` and
    every derived field on the entry (``agent_id``, ``agent_type``,
    ``agent_transcript_path``, ``last_assistant_message``, ``session_ref``,
    ``prompt_id``, ``stop_hook_active``) is left ``None`` -- an event this
    harness cannot honestly interpret must never silently prefer one channel
    over the other and must never participate in correlation."""
    events: list[dict] = []
    for stream_index, payload in enumerate(_iter_claude_stream_events(stdout)):
        if payload.get("type") != "system":
            continue
        hook_event = payload.get("hook_event")
        if hook_event not in _CLAUDE_HOOK_LIFECYCLE_EVENTS:
            continue
        entry: dict = {
            "hook_event": hook_event,
            "stream_index": stream_index,
            "agent_id": None,
            "agent_type": None,
            "agent_transcript_path": None,
            "last_assistant_message": None,
            "session_ref": None,
            "prompt_id": None,
            "stop_hook_active": None,
            "contradictory": False,
        }
        outer_session_ref = payload.get("session_ref")
        if not isinstance(outer_session_ref, str) or not outer_session_ref:
            outer_session_ref = payload.get("sessionRef")

        hook_name_agent_type: str | None = None
        hook_name = payload.get("hook_name")
        if isinstance(hook_name, str) and hook_name.startswith(f"{hook_event}:"):
            suffix = hook_name.split(":", 1)[1].strip()
            if suffix:
                hook_name_agent_type = suffix

        channel_parsed: dict[str, dict] = {}
        for key in ("stdout", "output"):
            raw_text = payload.get(key)
            if not isinstance(raw_text, str) or not raw_text.strip():
                continue
            parsed = _parse_embedded_json_object(raw_text)
            if parsed is not None:
                channel_parsed[key] = parsed

        if "stdout" in channel_parsed and "output" in channel_parsed:
            channel_a, channel_b = channel_parsed["stdout"], channel_parsed["output"]
            for field in ("agent_id", "agent_type", "agent_transcript_path"):
                value_a, value_b = channel_a.get(field), channel_b.get(field)
                if (
                    isinstance(value_a, str)
                    and value_a
                    and isinstance(value_b, str)
                    and value_b
                    and value_a != value_b
                ):
                    entry["contradictory"] = True
                    break

        if not entry["contradictory"]:
            for parsed in channel_parsed.values():
                agent_id = parsed.get("agent_id")
                if isinstance(agent_id, str) and agent_id and entry["agent_id"] is None:
                    entry["agent_id"] = agent_id
                agent_type = parsed.get("agent_type")
                if isinstance(agent_type, str) and agent_type and entry["agent_type"] is None:
                    entry["agent_type"] = agent_type
                agent_transcript_path = parsed.get("agent_transcript_path")
                if (
                    isinstance(agent_transcript_path, str)
                    and agent_transcript_path
                    and entry["agent_transcript_path"] is None
                ):
                    entry["agent_transcript_path"] = agent_transcript_path
                last_assistant_message = parsed.get("last_assistant_message")
                if (
                    isinstance(last_assistant_message, str)
                    and last_assistant_message
                    and entry["last_assistant_message"] is None
                ):
                    entry["last_assistant_message"] = last_assistant_message
                embedded_session_ref = parsed.get("session_ref")
                if (
                    isinstance(embedded_session_ref, str)
                    and embedded_session_ref
                    and entry["session_ref"] is None
                ):
                    entry["session_ref"] = embedded_session_ref
                prompt_id = parsed.get("prompt_id")
                if isinstance(prompt_id, str) and prompt_id and entry["prompt_id"] is None:
                    entry["prompt_id"] = prompt_id
                stop_hook_active = parsed.get("stop_hook_active")
                if isinstance(stop_hook_active, bool) and entry["stop_hook_active"] is None:
                    entry["stop_hook_active"] = stop_hook_active
            if entry["agent_type"] is None and hook_name_agent_type is not None:
                entry["agent_type"] = hook_name_agent_type
            if entry["session_ref"] is None and isinstance(outer_session_ref, str) and outer_session_ref:
                entry["session_ref"] = outer_session_ref

        events.append(entry)
    return events

