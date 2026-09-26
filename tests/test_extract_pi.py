import json
import tempfile
import unittest
from pathlib import Path

from extract_pi import active_branch, extract_session


def entry(type_, id_, parent, **kw):
    return {"type": type_, "id": id_, "parentId": parent, "timestamp": "2026-09-01T00:00:00.000Z", **kw}


SESSION = [
    {"type": "session", "version": 3, "id": "sess-1", "timestamp": "2026-09-01T00:00:00.000Z", "cwd": "/work"},
    entry("model_change", "a", None, provider="homelab", modelId="glm-5.2"),
    entry("session_init", "b", "a", systemPrompt="You are omp.", tools=["read", "bash"]),
    entry("message", "c", "b", message={"role": "user", "content": [{"type": "text", "text": "list files"}], "timestamp": 1788000000000}),
    entry("message", "d", "c", message={
        "role": "assistant", "model": "glm-5.2", "provider": "homelab", "api": "openai-completions",
        "usage": {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 15},
        "stopReason": "toolUse", "timestamp": 1788000001000,
        "content": [
            {"type": "thinking", "thinking": "Use ls."},
            {"type": "text", "text": "Listing."},
            {"type": "toolCall", "id": "call-1", "name": "bash", "arguments": {"command": "ls"}},
        ]}),
    entry("message", "e", "d", message={
        "role": "toolResult", "toolCallId": "call-1", "toolName": "bash", "isError": False,
        "content": [{"type": "text", "text": "a.py\nb.py"}], "timestamp": 1788000002000}),
    # abandoned branch off "d" followed by the active branch continuing from "e"
    entry("message", "x", "c", message={"role": "assistant", "model": "old", "provider": "p", "content": [{"type": "text", "text": "stale"}]}),
    entry("message", "f", "e", message={
        "role": "assistant", "model": "claude-opus-4-8", "provider": "anthropic",
        "content": [{"type": "text", "text": "Two files."}], "timestamp": 1788000003000}),
]


class ExtractPiTests(unittest.TestCase):
    def write(self, directory, name, rows):
        path = Path(directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path

    def test_active_branch_skips_abandoned_entries(self):
        ids = [e["id"] for e in active_branch([e for e in SESSION if e["type"] != "session"])]
        self.assertEqual(ids, ["a", "b", "c", "d", "e", "f"])

    def test_extracts_messages_reasoning_tools_and_models(self):
        with tempfile.TemporaryDirectory() as d:
            conv = extract_session(self.write(d, "s.jsonl", SESSION), "omp")
        roles = [m["role"] for m in conv["messages"]]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
        a = conv["messages"][1]
        self.assertEqual(a["reasoning"], "Use ls.")
        self.assertEqual(a["content"], "Listing.")
        self.assertEqual(a["tool_calls"][0]["function"], {"name": "bash", "arguments": '{"command": "ls"}'})
        self.assertEqual(a["model"], "glm-5.2")
        self.assertEqual(a["usage"]["totalTokens"], 15)
        self.assertEqual(conv["messages"][2]["tool_call_id"], "call-1")
        self.assertEqual(conv["messages"][2]["content"], "a.py\nb.py")
        self.assertEqual(conv["messages"][0]["timestamp"], "2026-08-29T10:40:00Z")
        self.assertEqual(conv["models"], ["claude-opus-4-8", "glm-5.2"])
        self.assertEqual(conv["model_changes"], ["homelab/glm-5.2"])
        self.assertEqual(conv["system_prompt"], "You are omp.")
        self.assertEqual(conv["tools"], ["read", "bash"])
        self.assertEqual(conv["session_id"], "sess-1")
        self.assertEqual(conv["project_path"], "/work")
        self.assertNotIn("subagent", conv)

    def test_marks_subagent_sessions_and_skips_sessions_without_assistant(self):
        with tempfile.TemporaryDirectory() as d:
            sub = extract_session(self.write(d, "2026-09-01T00-00-00-000Z_abc/__advisor.jsonl", SESSION), "omp")
            empty = extract_session(self.write(d, "e.jsonl", SESSION[:4]), "pi")
        self.assertTrue(sub["subagent"])
        self.assertEqual(sub["agent"], "__advisor")
        self.assertIsNone(empty)


if __name__ == "__main__":
    unittest.main()
