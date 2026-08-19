#!/usr/bin/env python3
"""
Shared per-session HF-traces style JSONL export for ALL extractors.

HF-traces stores each chat session as its OWN JSONL file, with ONE message
object per line (OpenAI-compatible chat schema):

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...",
     "tool_calls": [{"id": "...", "type": "function",
                     "function": {"name": "...", "arguments": "..."}}]}
    {"role": "tool", "name": "...", "tool_call_id": "...", "content": "..."}

Files are written to ``extracted_data/<harness>/sessions/<session_id>.jsonl``,
with a companion ``<session_id>.meta.json`` holding the conversation-level
fields (system prompt, model, title, events, usage, cwd, ...) that do not
belong on any single message line.

Every extractor funnels its conversations through :func:`write_session_files`
so the on-disk layout is identical across harnesses. Per-message metadata that
the source format carries (timestamp, model, reasoning/thoughts, ids, status)
is preserved as extra keys on each line — nothing is discarded. On top of the
preserved source fields, a few derived annotations are *added*: assistant
lines with empty content but real reasoning get ``reasoning_only: true`` (so
consumers can tell an intentional thought / tool-call turn from an extraction
gap), and tool calls / results carry any per-call extra keys the source had.
"""

import json
import re
from pathlib import Path


# --------------------------------------------------------------------------
# Message normalization
# --------------------------------------------------------------------------

def _content_str(content):
    """Coerce a message content (str, list of parts, or structured) to a string."""
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                # Claude/OpenAI style parts: {type: text, text: ...} / output_text
                if part.get('type') in ('text', 'output_text', 'input_text'):
                    texts.append(_content_str(part.get('text')))
                elif 'text' in part:
                    texts.append(_content_str(part.get('text')))
                else:
                    # Structured result parts (e.g. cline tool results like
                    # {query, result, error, success}): prefer the output
                    # field, else join the string values so the result text
                    # is never silently dropped.
                    picked = None
                    for key in ('result', 'output', 'content', 'error'):  # 'text' handled above
                        val = part.get(key)
                        if val is not None and str(val).strip():
                            picked = _content_str(val)
                            break
                    if picked is None:
                        # Skip meta keys so 'type'/'success' don't leak in
                        # (bool is an int subclass, so check by key name).
                        vals = [str(v) for k, v in part.items()
                                if k not in ('type', 'success')
                                and isinstance(v, (str, int, float))
                                and str(v).strip()]
                        picked = '\n'.join(vals)
                    if picked:
                        texts.append(picked)
        return '\n'.join(t for t in texts if t)
    if isinstance(content, dict):
        try:
            return json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(content)
    return str(content)


def _arguments_str(arguments):
    """Coerce tool-call arguments to a JSON string."""
    if arguments is None:
        return ''
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, (dict, list)):
        try:
            return json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(arguments)
    return str(arguments)


_HANDLED_TOOL_CALL_KEYS = frozenset({
    'id', 'type', 'function', 'name', 'arguments', 'input', 'args',
    'call_id', 'callID',
})


def _first_non_none(*values):
    """First value that is not None (preserves falsy-but-real values like {})."""
    for v in values:
        if v is not None:
            return v
    return None


def _tool_call_entry(tc):
    """Normalize one tool-call dict (any extractor shape) to HF-traces shape.

    Accepts:
      {id, type: "function", function: {name, arguments}}   (canonical)
      {id, function: {name, arguments}}                     (continue toolCalls)
      {name, input, call_id/id}                             (opencode/codex)
      {name, args, call_id/id}                              (gemini/qwen)
      {type: "tool_use", name, input, id}                   (claude tool_uses)
      {name, arguments, call_id/id}

    Argument selection uses ``_first_non_none`` rather than ``or`` so an
    explicit empty dict ``{}`` (a real "called with no args") is preserved
    as ``'{}'`` instead of being conflated with "args unknown" ('').

    Note on precedence: within a source entry, the FIRST non-None source wins
    (e.g. ``function.arguments`` beats a top-level ``input``), even when it is
    an empty string — for the canonical OpenAI shape the function-level
    arguments are authoritative. No current extractor emits both shapes.

    Extra source fields (e.g. freebuff's ``has_result`` interruption flag)
    are preserved on the output so no per-call metadata is dropped.
    """
    if not isinstance(tc, dict):
        return tc
    # OpenAI / HF-traces canonical (with or without explicit type)
    if isinstance(tc.get('function'), dict):
        entry = {
            'id': _first_non_none(tc.get('id'), tc.get('call_id'),
                                   tc.get('callID')),
            'type': 'function',
            'function': {
                'name': (tc['function'].get('name') or tc.get('name')),
                'arguments': _arguments_str(
                    _first_non_none(tc['function'].get('arguments'),
                                    tc.get('input'), tc.get('args'))),
            },
        }
    else:
        name = tc.get('name')
        arguments = _first_non_none(tc.get('arguments'), tc.get('input'),
                                    tc.get('args'))
        call_id = _first_non_none(tc.get('id'), tc.get('call_id'),
                                  tc.get('callID'))
        entry = {
            'id': call_id,
            'type': 'function',
            'function': {'name': name, 'arguments': _arguments_str(arguments)},
        }
    for k, v in tc.items():
        if k not in _HANDLED_TOOL_CALL_KEYS and k not in entry:
            entry[k] = v
    return entry


def normalize_message(msg):
    """Normalize one extractor message dict into an HF-traces line dict.

    Unknown fields are preserved so no data is lost.
    """
    if not isinstance(msg, dict):
        return msg

    role = msg.get('role')
    if role == 'tool_result':
        role = 'tool'
    # Only default an empty role to 'user'; never relabel a real role string
    # (e.g. 'function', 'thinking') as 'user'.
    if not role:
        role = 'user'

    line = {'role': role, 'content': _content_str(msg.get('content', ''))}

    # Preserve the ORIGINAL content object when it was structured (list/dict),
    # so the traces flattening to a string never loses the source shape. If the
    # traces format is later retired, this keeps the extraction 100% lossless.
    raw_content = msg.get('content', '')
    if not isinstance(raw_content, str):
        line['raw_content'] = raw_content

    # Tool calls on assistant messages (incl. Claude-style tool_uses)
    if role == 'assistant':
        tcs = msg.get('tool_calls') or msg.get('tool_uses')
        if isinstance(tcs, list) and tcs:
            line['tool_calls'] = [_tool_call_entry(tc) for tc in tcs]

    # Reasoning / thoughts -> 'reasoning' (HF-traces tolerant of extra keys).
    # The original structured thoughts are also kept verbatim in 'raw_thoughts'
    # for lossless source fidelity.
    thoughts = msg.get('thoughts') or msg.get('reasoning')
    if thoughts and isinstance(thoughts, (list, dict)):
        line['raw_thoughts'] = thoughts
    if thoughts:
        if isinstance(thoughts, list):
            parts = []
            for t in thoughts:
                if isinstance(t, dict):
                    # Gemini-style thought: {subject, description, ...}
                    desc = t.get('description') or t.get('text') or t.get('subject')
                    if desc:
                        parts.append(_content_str(desc))
                    else:
                        parts.append(json.dumps(t, ensure_ascii=False))
                elif t:
                    parts.append(_content_str(t))
            line['reasoning'] = '\n'.join(parts)
        else:
            line['reasoning'] = _content_str(thoughts)

    # Annotate reasoning-only assistant turns (Bug 6): empty content + reasoning
    # is intentional, not an extraction gap — either a thought-only turn or a
    # valid tool-call turn (reasoning + tool_calls, no text). Consumers can use
    # this flag together with the presence of tool_calls to classify the line.
    if (role == 'assistant'
            and str(line.get('reasoning') or '').strip()
            and not str(line.get('content') or '').strip()):
        line['reasoning_only'] = True

    # Tool-role messages: ensure name + tool_call_id are populated
    if role == 'tool':
        line.setdefault('name', msg.get('tool_name') or msg.get('name'))
        line.setdefault('tool_call_id',
                        msg.get('tool_call_id') or msg.get('call_id')
                        or msg.get('callID') or msg.get('tool_use_id'))
        line.setdefault('status', msg.get('status')
                        or ('error' if msg.get('is_error') else 'success'))

    # Preserve ALL remaining source fields (tokens, cost, parts, subagents,
    # attachments, diffs, ...) — HF-traces tolerates extra keys, and this
    # guarantees the export never drops data. Keys handled above are excluded.
    handled = {'role', 'content', 'thoughts', 'reasoning', 'tool_calls',
               'tool_uses', 'tool_results', 'tool_name', 'call_id', 'callID',
               'tool_use_id'}
    for key, value in msg.items():
        if key in handled or key in line:
            continue
        line[key] = value

    return line


_HANDLED_TOOL_RESULT_KEYS = frozenset({
    'role', 'content', 'output', 'name', 'tool', 'tool_call_id', 'call_id',
    'callID', 'toolUseId', 'tool_use_id', 'isError', 'is_error',
})


def _tool_result_status(tr, content):
    """Map a tool-result entry to the HF-traces 'success'/'error' status."""
    if tr.get('isError') or tr.get('is_error'):
        return 'error'
    # cline-style results: content is a list of {success: bool} items
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and (part.get('success') is False
                                           or part.get('is_error')):
                return 'error'
    return 'success'


def conversation_to_lines(conv):
    """Convert one conversation dict into a list of HF-traces message lines.

    Handles extractors that attach ``tool_results`` / ``tool_uses`` to the
    assistant message by emitting them as interleaved ``tool``-role lines.
    Conversations without a ``messages`` list (e.g. antigravity implicit /
    encrypted metadata records) are emitted as a single metadata line.
    """
    messages = conv.get('messages')
    if not isinstance(messages, list):
        return [dict(conv)]  # metadata-only record

    lines = []
    for msg in messages:
        lines.append(normalize_message(msg))

        # Claude/Continue style: assistant message carries its tool results
        for tr in (msg.get('tool_results') or []):
            if not isinstance(tr, dict):
                continue
            tr_content = tr.get('output', tr.get('content', ''))
            tool_line = {
                'role': 'tool',
                'name': tr.get('tool') or tr.get('name'),
                'tool_call_id': (tr.get('tool_call_id') or tr.get('call_id')
                                 or tr.get('callID') or tr.get('toolUseId')
                                 or tr.get('tool_use_id')),
                'content': _content_str(tr_content),
                'status': _tool_result_status(tr, tr_content),
            }
            # Preserve any extra source fields (e.g. claude toolUseResult)
            for k, v in tr.items():
                if k not in _HANDLED_TOOL_RESULT_KEYS:
                    tool_line[k] = v
            lines.append(tool_line)
    return lines


# --------------------------------------------------------------------------
# File writing
# --------------------------------------------------------------------------

def _safe_filename(name, fallback_idx):
    """Filesystem-safe session id; falls back to an index-based name."""
    if not name:
        return f'session_{fallback_idx:05d}'
    s = re.sub(r'[^A-Za-z0-9._-]+', '_', str(name)).strip('._')
    if not s:
        return f'session_{fallback_idx:05d}'
    return s[:180]  # keep filenames reasonable


def write_session_files(conversations, harness, base_dir='extracted_data',
                        sessions_dir_name='sessions'):
    """Write one HF-traces JSONL file per conversation.

    Returns ``(files_written, lines_written)``.
    """
    out_dir = Path(base_dir) / harness / sessions_dir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    files_written = 0
    lines_written = 0
    used_names = set()

    for idx, conv in enumerate(conversations):
        lines = conversation_to_lines(conv)
        if not lines:
            continue

        sid = conv.get('session_id') or conv.get('id') or conv.get('store_key')
        fname = _safe_filename(sid, idx)
        # Avoid clobbering duplicate session ids
        n = 2
        while fname in used_names:
            fname = f'{_safe_filename(sid, idx)}_{n}'
            n += 1
        used_names.add(fname)

        out_file = out_dir / f'{fname}.jsonl'
        with open(out_file, 'w', encoding='utf-8') as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False) + '\n')
                lines_written += 1

        # Companion meta file: conversation-level fields that do not belong on
        # any single message line (system_prompt, model, events, title, usage,
        # cwd, ...). Without this, conv-level metadata extracted by the
        # harness extractors (e.g. cline's system_prompt/model, opencode's
        # --events log, freebuff's --logs log) was silently dropped.
        # Metadata-only conversations (no ``messages`` list) are skipped here:
        # their full dict is already emitted as the single line, so writing
        # an identical meta file would just duplicate it.
        if isinstance(conv.get('messages'), list):
            meta = {k: v for k, v in conv.items() if k not in ('messages',)}
            if meta:
                meta_file = out_dir / f'{fname}.meta.json'
                with open(meta_file, 'w', encoding='utf-8') as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)

        files_written += 1

    return files_written, lines_written
