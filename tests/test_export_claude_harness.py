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

    def test_excludes_tool_result_block_from_user_content(self):
        with tempfile.TemporaryDirectory() as d:
            inst = Path(d) / ".claude"
            proj = inst / "projects" / "myproj"
            proj.mkdir(parents=True)
            lines = [
                {"type": "user", "message": {"content": [
                    {"type": "text", "text": "please look at this"},
                    {"type": "tool_result", "content": "SECRET TOOL OUTPUT"},
                ]}, "timestamp": "t1"},
            ]
            (proj / "sess1.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
            recs, counts = ex.collect_prompts(inst, Sanitizer(use_detect_secrets=False))
            self.assertEqual(len(recs), 1)
            self.assertIn("please look at this", recs[0]["text"])
            self.assertNotIn("SECRET TOOL OUTPUT", recs[0]["text"])


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

    def test_strict_drops_toolresult_block_in_user_content(self):
        with tempfile.TemporaryDirectory() as d:
            inst = Path(d) / ".claude"
            proj = inst / "projects" / "p"
            proj.mkdir(parents=True)
            lines = [
                {"type": "user", "message": {"content": [
                    {"type": "text", "text": "see this"},
                    {"type": "tool_result", "content": "FILE BODY /Users/joe/secret.py ghp_" + "a" * 36}
                ]}, "timestamp": "t1"},
            ]
            (proj / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
            convs, counts, dropped = ex.collect_conversations(
                inst, Sanitizer(use_detect_secrets=False), "strict")
            blob = json.dumps(convs)
            self.assertNotIn("FILE BODY", blob)      # tool output dropped
            self.assertNotIn("ghp_", blob)           # secret redacted
            self.assertIn("see this", blob)          # text kept
            self.assertGreaterEqual(dropped.get("content_blocks", 0), 1)

    def test_balanced_keeps_but_sanitizes_user_content_block(self):
        with tempfile.TemporaryDirectory() as d:
            inst = Path(d) / ".claude"
            proj = inst / "projects" / "p"
            proj.mkdir(parents=True)
            lines = [
                {"type": "user", "message": {"content": [
                    {"type": "text", "text": "see this"},
                    {"type": "tool_result", "content": "FILE BODY /Users/joe/secret.py ghp_" + "a" * 36}
                ]}, "timestamp": "t1"},
            ]
            (proj / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
            convs, counts, dropped = ex.collect_conversations(
                inst, Sanitizer(use_detect_secrets=False), "balanced")
            blob = json.dumps(convs)
            self.assertIn("FILE BODY", blob)         # tool output kept
            self.assertNotIn("/Users/joe", blob)      # path sanitized
            self.assertNotIn("ghp_", blob)            # secret redacted
            self.assertIn("see this", blob)           # text kept


class BuildAndWrite(unittest.TestCase):
    def _full_install(self, root):
        inst = Path(root) / ".claude"
        proj = inst / "projects" / "p"
        proj.mkdir(parents=True)
        (inst / "CLAUDE.md").write_text("hi /Users/fabian/x")
        (inst / "settings.json").write_text(json.dumps({"token": "ghp_" + "a" * 36}))
        lines = [{"type": "user", "message": {"content": "leak sk-ant-" + "z" * 40}}]
        (proj / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
        return inst

    def test_default_excludes_conversations(self):
        with tempfile.TemporaryDirectory() as d:
            inst = self._full_install(d)
            export = ex.build_export([inst], {"config", "prompts"}, "strict", False, "NOW")
            self.assertNotIn(
                "conversations/conversations.sanitized.jsonl", export["files"])
            self.assertIn("config/CLAUDE.md", export["files"])
            self.assertIn("prompts/prompt_patterns.jsonl", export["files"])

    def test_no_planted_secret_survives(self):
        with tempfile.TemporaryDirectory() as d:
            inst = self._full_install(d)
            export = ex.build_export(
                [inst], {"config", "prompts", "conversations"}, "strict", False, "NOW")
            blob = "\n".join(export["files"].values())
            self.assertNotIn("ghp_", blob)
            self.assertNotIn("sk-ant-", blob)
            self.assertNotIn("fabian", blob)

    def test_dry_run_writes_only_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            inst = self._full_install(d)
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            out = Path(d) / "out"
            bundle = ex.write_bundle(export, out, dry_run=True)
            written = [p.name for p in bundle.rglob("*") if p.is_file()]
            self.assertEqual(written, ["MANIFEST.json"])

    def test_manifest_hashes_match_written_files(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            inst = self._full_install(d)
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            out = Path(d) / "out"
            bundle = ex.write_bundle(export, out, dry_run=False)
            manifest = json.loads((bundle / "MANIFEST.json").read_text())
            for entry in manifest["files"]:
                data = (bundle / entry["path"]).read_bytes()
                self.assertEqual(entry["bytes"], len(data))
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())

    def test_malformed_config_does_not_crash_and_warns(self):
        with tempfile.TemporaryDirectory() as d:
            inst = Path(d) / ".claude"
            inst.mkdir(parents=True)
            (inst / "settings.json").write_text("{ not valid json ")
            (inst / "CLAUDE.md").write_text("hello world")

            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")

            self.assertIn("config/CLAUDE.md", export["files"])
            self.assertNotIn("config/", export["files"])
            self.assertTrue(
                all(entry["path"] != "config/" for entry in export["manifest"]["files"]))

            out = Path(d) / "out"
            bundle = ex.write_bundle(export, out, dry_run=False)  # must not raise

            self.assertTrue(
                any("settings.json" in w for w in export["manifest"]["warnings"]))

    def test_multiple_installations_no_collision(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            root_a = Path(d) / "a"
            root_b = Path(d) / "b"
            inst_a = root_a / ".claude"
            inst_b = root_b / ".claude"
            proj_a = inst_a / "projects" / "p"
            proj_b = inst_b / "projects" / "p"
            proj_a.mkdir(parents=True)
            proj_b.mkdir(parents=True)
            (inst_a / "CLAUDE.md").write_text("install A notes")
            (inst_b / "CLAUDE.md").write_text("install B notes")
            (proj_a / "s.jsonl").write_text(json.dumps(
                {"type": "user", "message": {"content": "prompt from A"}}))
            (proj_b / "s.jsonl").write_text(json.dumps(
                {"type": "user", "message": {"content": "prompt from B"}}))

            export = ex.build_export(
                [inst_a, inst_b], {"config", "prompts"}, "strict", False, "NOW")

            claude_md_keys = [k for k in export["files"] if k.endswith("/CLAUDE.md")]
            self.assertEqual(len(claude_md_keys), 2)
            self.assertEqual(len(set(claude_md_keys)), 2)

            paths = [entry["path"] for entry in export["manifest"]["files"]]
            self.assertEqual(len(paths), len(set(paths)))

            out = Path(d) / "out"
            bundle = ex.write_bundle(export, out, dry_run=False)
            manifest = json.loads((bundle / "MANIFEST.json").read_text())
            for entry in manifest["files"]:
                data = (bundle / entry["path"]).read_bytes()
                self.assertEqual(entry["bytes"], len(data))
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
