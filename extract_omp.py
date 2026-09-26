#!/usr/bin/env python3
"""
Extract ALL omp (oh-my-pi) coding-agent session data
Includes: messages, thinking/reasoning, tool calls + results, model/provider per turn,
token usage, timestamps, system prompt, advisor and subagent sessions
Auto-discovers omp installations on the device

omp is a fork of pi and uses the same session JSONL format, stored under
~/.omp/agent/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl, with subagent and
advisor sessions in a sibling directory named after the parent session.
The parsing lives in extract_pi.py.
"""

from extract_pi import run


def main():
    run('omp', 'omp', ['.omp'], 'OMP_CODING_AGENT_DIR', 'omp')


if __name__ == '__main__':
    main()
