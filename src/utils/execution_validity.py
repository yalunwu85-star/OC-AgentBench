"""Read native runtime envelopes, never classify failures from agent/tool prose."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


def _events(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def codex_provider_error(log_path: Path) -> str | None:
    """Track unresolved CLI stream/turn errors, allowing subsequent recovery.

    With ``codex exec --json``, provider/stream failures are top-level error or
    turn.failed events. Tool failures and assistant text are nested in items.
    A subsequent model-generated item or completed turn establishes recovery;
    a mere new turn, runner timeout, or local tool output does not.
    """
    pending = None
    generated_items = {'agent_message', 'reasoning', 'command_execution',
                       'mcp_tool_call', 'web_search', 'file_change'}
    for event in _events(log_path):
        kind = event.get('type')
        if kind in ('error', 'turn.failed'):
            error = event.get('error')
            detail = error.get('message') if isinstance(error, dict) else None
            detail = detail or event.get('message') or 'provider stream/turn failed'
            pending = f'Codex runtime API/stream failure: {detail}'
        elif kind == 'turn.completed':
            pending = None
        elif kind in ('item.started', 'item.completed'):
            item = event.get('item')
            # started tool items represent a new model-generated tool request.
            # Completed tool output alone cannot prove a failed stream recovered.
            if isinstance(item, dict) and item.get('type') in generated_items:
                if kind == 'item.started' or item.get('type') in ('agent_message', 'reasoning'):
                    pending = None
    return pending


def openclaw_provider_error(transcript_path: Path) -> str | None:
    """Inspect assistant envelope stopReason; ignore task text and tool HTTP errors."""
    pending = None
    for event in _events(transcript_path):
        if event.get('type') != 'message':
            continue
        message = event.get('message')
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        reason = message.get('stopReason')
        if reason == 'error':
            pending = 'OpenClaw runtime API failure: ' + str(
                message.get('errorMessage') or 'assistant provider request failed')
        elif reason in ('stop', 'length', 'toolUse'):
            pending = None
        # Host cancellation can append an aborted event after a provider error.
        # It is not evidence that the failed request recovered.
    return pending


def claudecode_provider_error(output_dir: Path, *, timed_out: bool = False) -> str | None:
    """Read the legacy query_yield wrapper used by claudecode/transcript.py.

    Also accept explicit structured result/stream envelopes. Never inspect text
    content, recurse through tool payloads, or treat query_end alone as success.
    Unknown normal-exit formats fail closed; a host deadline remains scoreable
    unless a recognized unresolved provider error precedes it.
    """
    candidates = (output_dir / 'claude_code_log' / 'chat.json',
                  output_dir / 'claudecode_stdout.log', output_dir / 'agent.log')
    any_evidence = False
    for path in candidates:
        try:
            raw = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        try:
            parsed = json.loads(raw)
            rows = parsed if isinstance(parsed, list) else [parsed]
        except ValueError:
            rows = list(_events(path))
        pending = None
        recognized = False
        evidence = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            event = row
            if row.get('event') == 'query_yield':
                payload = row.get('payload')
                event = payload.get('message') if isinstance(payload, dict) else None
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'stream_event':
                event = event.get('event')
            if not isinstance(event, dict):
                continue
            kind = event.get('type')
            if kind == 'error' and isinstance(event.get('error'), dict):
                recognized = True
                pending = 'ClaudeCode runtime API/stream failure'
            elif kind == 'result' and isinstance(event.get('is_error'), bool):
                recognized = True
                if event['is_error']:
                    pending = 'ClaudeCode runtime reported an unsuccessful result'
                else:
                    pending = None
                    evidence = True
            elif kind == 'message_start':
                message = event.get('message')
                if isinstance(message, dict) and message.get('role') == 'assistant':
                    recognized = True
                    evidence = True
                    pending = None
            elif kind == 'assistant' and isinstance(event.get('message'), dict):
                message = event['message']
                if message.get('role') == 'assistant':
                    recognized = True
                    if event.get('error'):
                        pending = 'ClaudeCode runtime reported an assistant error'
                    else:
                        evidence = True
                        pending = None
        if recognized:
            if pending:
                return pending
            any_evidence = any_evidence or evidence
    if any_evidence or timed_out:
        return None
    return 'ClaudeCode runtime validity unverified: no supported native model/result evidence'
