import json
import unittest
import tempfile
from pathlib import Path

from sanitize import Sanitizer
import export_claude_harness as ex


def _make_install(root):
    inst = Path(root) / ".claude"
    inst.mkdir(parents=True)
    (inst / "CLAUDE.md").write_text("Prefer tabs. My path /Users/fabian/work/app.py\n")
    (inst / "settings.json").write_text(json.dumps({
        "model": "opus",
        "env": {"MY_API_KEY": "sk-ant-" + "q" * 40},
    }))
    (inst / ".mcp.json").write_text(json.dumps({
        "servers": {"x": {"token": "ghp_" + "a" * 36}}
    }))
    skills = inst / "skills"
    skills.mkdir()
    (skills / "review.md").write_text("Contact bob@corp.com to review.\n")
    mem = inst / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("Learned: deploy on fridays.\n")
    return inst


class CollectConfig(unittest.TestCase):
    def test_collects_and_sanitizes(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _make_install(d)
            recs = ex.collect_config(inst, Sanitizer(use_detect_secrets=False))
            by_path = {r["rel_path"]: r for r in recs}

            self.assertIn("CLAUDE.md", by_path)
            self.assertNotIn("fabian", by_path["CLAUDE.md"]["content"])

            self.assertIn("settings.sanitized.json", by_path)
            settings = json.loads(by_path["settings.sanitized.json"]["content"])
            self.assertNotIn("sk-ant-", json.dumps(settings))

            self.assertIn("mcp.sanitized.json", by_path)
            self.assertNotIn("ghp_", by_path["mcp.sanitized.json"]["content"])

            self.assertIn("skills/review.md", by_path)
            self.assertNotIn("bob@corp.com", by_path["skills/review.md"]["content"])

            self.assertIn("memory/MEMORY.md", by_path)

    def test_unparseable_json_is_skipped_with_warning(self):
        with tempfile.TemporaryDirectory() as d:
            inst = Path(d) / ".claude"
            inst.mkdir()
            (inst / "settings.json").write_text("{ this is not json ")
            recs = ex.collect_config(inst, Sanitizer(use_detect_secrets=False))
            paths = [r["rel_path"] for r in recs]
            self.assertNotIn("settings.sanitized.json", paths)
            all_warnings = [w for r in recs for w in r["warnings"]]
            self.assertTrue(any("settings.json" in w for w in all_warnings))


class CollectPrompts(unittest.TestCase):
    def _install_with_session(self, root):
        inst = Path(root) / ".claude"
        proj = inst / "projects" / "myproj"
        proj.mkdir(parents=True)
        lines = [
            {"type": "user", "message": {"content": "Fix the bug near /Users/fabian/x.py"},
             "timestamp": "t1"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "sure"}]},
             "timestamp": "t2"},
        ]
        (proj / "sess1.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
        return inst

    def test_keeps_only_sanitized_user_prompts(self):
        with tempfile.TemporaryDirectory() as d:
            inst = self._install_with_session(d)
            recs, counts = ex.collect_prompts(inst, Sanitizer(use_detect_secrets=False))
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["source_session"], "sess1")
            self.assertNotIn("fabian", recs[0]["text"])
            self.assertIn("[PATH]", recs[0]["text"])
            self.assertNotIn("sure", json.dumps(recs))
            self.assertEqual(counts["paths"], 1)


class CollectConversations(unittest.TestCase):
    def _install(self, root):
        inst = Path(root) / ".claude"
        proj = inst / "projects" / "p"
        proj.mkdir(parents=True)
        lines = [
            {"type": "user", "message": {"content": "hello token ghp_" + "a" * 36},
             "timestamp": "t1"},
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "answer"},
                                     {"type": "tool_use", "name": "Edit", "input": {"x": 1}}]},
             "timestamp": "t2"},
            {"type": "tool_result", "toolResult": {"stdout": "SECRET /Users/joe/a"}},
        ]
        (proj / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
        return inst

    def test_strict_drops_tools_and_sanitizes(self):
        with tempfile.TemporaryDirectory() as d:
            inst = self._install(d)
            convs, counts, dropped = ex.collect_conversations(
                inst, Sanitizer(use_detect_secrets=False), "strict")
            blob = json.dumps(convs)
            self.assertNotIn("ghp_", blob)          # user secret redacted
            self.assertNotIn("tool_use", blob)       # tool use dropped
            self.assertNotIn("SECRET /Users/joe", blob)  # tool result dropped
            self.assertGreaterEqual(dropped.get("tool_uses", 0), 1)

    def test_balanced_keeps_but_sanitizes_tools(self):
        with tempfile.TemporaryDirectory() as d:
            inst = self._install(d)
            convs, counts, dropped = ex.collect_conversations(
                inst, Sanitizer(use_detect_secrets=False), "balanced")
            blob = json.dumps(convs)
            self.assertIn("tool_uses", blob)         # kept
            self.assertNotIn("/Users/joe", blob)      # but path sanitized
