"""End-to-end tests that drive main() the way an operator does.

The unit tests call collectors directly, which let real defects survive: an
audit showed the username leaking through MANIFEST.json while every unit test
stayed green. These tests plant one distinct canary per documented asset, run
the real CLI entry point against a fake home, and search the ENTIRE bundle -
file contents, file names, and MANIFEST.json - for every canary.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import export_claude_harness as ex

OPERATOR = "quillfeather"

CANARIES = {
    "claude_md": "CANARYclaudeMd001",
    "claude_local": "CANARYclaudeLocal02",
    "settings_env": "CANARYsettingsEnv03",
    "settings_note": "prod db password is CANARYnote04x",
    "mcp_header": "CANARYmcpHeader05",
    "mcp_argv": "CANARYmcpArgv06",
    "skill": "CANARYskill07",
    "command": "CANARYcommand08",
    "agent": "CANARYagent09",
    "memory": "CANARYmemory10",
    "yaml_block": "CANARYyamlBlock11",
    "prompt": "CANARYprompt12",
    "private_key": "CANARYprivKeyBody13",
    "azure": "CANARYazureAcct14",
}


def _build_home(root):
    home = Path(root) / OPERATOR
    inst = home / ".claude"
    for sub in ("projects/p", "skills/s", "commands", "agents", "memory"):
        (inst / sub).mkdir(parents=True, exist_ok=True)

    (inst / "CLAUDE.md").write_text(
        "# Notes\n"
        "api_key: %(claude_md)s\n"
        "deploy_key: -----BEGIN OPENSSH PRIVATE KEY-----\n"
        "%(private_key)sAAAAAAAAAAAAAAAA\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
        "AccountKey=%(azure)s;EndpointSuffix=core.windows.net\n"
        "Home is /Users/%(op)s/work\n"
        % dict(CANARIES, op=OPERATOR))
    (inst / "CLAUDE.local.md").write_text("token: %(claude_local)s\n" % CANARIES)
    (inst / "settings.json").write_text(json.dumps({
        "model": "opus",
        "env": {"MY_API_KEY": CANARIES["settings_env"]},
        "note": CANARIES["settings_note"],
    }))
    (inst / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"acme": {
            "command": "uvx",
            "args": ["acme-mcp", "--api-key", CANARIES["mcp_argv"]],
            "headers": {"X-Acme-Auth": CANARIES["mcp_header"]},
        }}
    }))
    (inst / "skills" / "s" / "SKILL.md").write_text(
        "password: %(skill)s\n" % CANARIES)
    (inst / "skills" / "s" / "c.yaml").write_text(
        "password: |\n  %(yaml_block)s\n" % CANARIES)
    (inst / "commands" / "go.md").write_text("secret: %(command)s\n" % CANARIES)
    (inst / "agents" / "rev.md").write_text("api_key=%(agent)s\n" % CANARIES)
    (inst / "memory" / "MEMORY.md").write_text("token=%(memory)s\n" % CANARIES)

    proj = inst / "projects" / ("-Users-%s-src-Acme-Private" % OPERATOR)
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "0123.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "message": {
            "content": "my prompt with password: %(prompt)s" % CANARIES}}),
        json.dumps({"type": "user", "isMeta": True,
                    "message": {"content": "INJECTED payload"}}),
        "42",
    ]))
    return home, inst


def _run(home, argv):
    buf = io.StringIO()
    with mock.patch.object(ex.Path, "home", staticmethod(lambda: home)), \
            mock.patch.dict(os.environ, {"HOME": str(home)}), \
            redirect_stdout(buf):
        rc = ex.main(argv)
    return rc, buf.getvalue()


def _bundle_blob(out_dir):
    bundles = sorted(Path(out_dir).glob("claude_harness_export_*"))
    bundle = bundles[-1]
    blob = "".join(p.read_text() for p in sorted(bundle.rglob("*")) if p.is_file())
    # File and directory NAMES are part of what leaves the environment.
    blob += "\n".join(str(p.relative_to(bundle))
                      for p in sorted(bundle.rglob("*")))
    return bundle, blob


class CliDefaultMode(unittest.TestCase):
    def test_no_canary_survives_a_default_export(self):
        with tempfile.TemporaryDirectory() as d:
            home, _inst = _build_home(d)
            out = Path(d) / "out"
            rc, _stdout = _run(home, ["--output", str(out)])
            self.assertEqual(rc, 0)
            _bundle, blob = _bundle_blob(out)
            survivors = [k for k, v in CANARIES.items() if v in blob]
            self.assertEqual(survivors, [], "canaries survived: %s" % survivors)

    def test_operator_name_never_reaches_the_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            home, _inst = _build_home(d)
            out = Path(d) / "out"
            rc, _stdout = _run(home, ["--output", str(out)])
            self.assertEqual(rc, 0)
            _bundle, blob = _bundle_blob(out)
            self.assertNotIn(OPERATOR, blob)
            self.assertNotIn("Acme-Private", blob)

    def test_manifest_carries_no_filesystem_name_for_installations(self):
        with tempfile.TemporaryDirectory() as d:
            home, _inst = _build_home(d)
            out = Path(d) / "out"
            self.assertEqual(_run(home, ["--output", str(out)])[0], 0)
            bundle, _blob = _bundle_blob(out)
            manifest = json.loads((bundle / "MANIFEST.json").read_text())
            entries = manifest["installations"]
            self.assertTrue(entries)
            for e in entries:
                self.assertNotIn(OPERATOR, json.dumps(e))
                self.assertIn("location", e)

    def test_injected_events_and_bad_lines_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            home, _inst = _build_home(d)
            out = Path(d) / "out"
            self.assertEqual(_run(home, ["--output", str(out)])[0], 0)
            bundle, _blob = _bundle_blob(out)
            manifest = json.loads((bundle / "MANIFEST.json").read_text())
            self.assertGreaterEqual(manifest["dropped"]["injected_user_events"], 1)
            self.assertTrue(any("not usable JSON" in w
                                for w in manifest["warnings"]))


class CliOtherModes(unittest.TestCase):
    def test_conversations_and_balanced_leak_no_canary_either(self):
        for argv_extra in (["--include", "config,prompts,conversations"],
                           ["--include", "config,prompts,conversations",
                            "--redact-level", "balanced"]):
            with tempfile.TemporaryDirectory() as d:
                home, _inst = _build_home(d)
                out = Path(d) / "out"
                rc, _stdout = _run(home, ["--output", str(out)] + argv_extra)
                self.assertEqual(rc, 0)
                _bundle, blob = _bundle_blob(out)
                survivors = [k for k, v in CANARIES.items() if v in blob]
                self.assertEqual(survivors, [],
                                 "%s leaked %s" % (argv_extra, survivors))

    def test_dry_run_writes_only_the_manifest_and_leaks_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            home, _inst = _build_home(d)
            out = Path(d) / "out"
            rc, _stdout = _run(home, ["--output", str(out), "--dry-run"])
            self.assertEqual(rc, 0)
            bundle, blob = _bundle_blob(out)
            written = [p.name for p in bundle.rglob("*") if p.is_file()]
            self.assertEqual(written, ["MANIFEST.json"])
            self.assertNotIn(OPERATOR, blob)
            survivors = [k for k, v in CANARIES.items() if v in blob]
            self.assertEqual(survivors, [])

    def test_manifest_describes_exactly_what_was_written(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            home, _inst = _build_home(d)
            out = Path(d) / "out"
            self.assertEqual(_run(home, ["--output", str(out)])[0], 0)
            bundle, _blob = _bundle_blob(out)
            manifest = json.loads((bundle / "MANIFEST.json").read_text())
            listed = {e["path"] for e in manifest["files"]}
            on_disk = {p.relative_to(bundle).as_posix()
                       for p in bundle.rglob("*") if p.is_file()} - {"MANIFEST.json"}
            self.assertEqual(listed, on_disk)
            for e in manifest["files"]:
                data = (bundle / e["path"]).read_bytes()
                self.assertEqual(e["bytes"], len(data))
                self.assertEqual(e["sha256"], hashlib.sha256(data).hexdigest())


class CliNameInFileName(unittest.TestCase):
    """File names are not redacted, so the warning is the only control."""

    def test_operator_named_file_is_warned_about(self):
        with tempfile.TemporaryDirectory() as d:
            home, inst = _build_home(d)
            (inst / "memory" / ("%s-notes.md" % OPERATOR)).write_text("personal")
            out = Path(d) / "out"
            self.assertEqual(_run(home, ["--output", str(out)])[0], 0)
            bundle, _blob = _bundle_blob(out)
            manifest = json.loads((bundle / "MANIFEST.json").read_text())
            self.assertTrue(
                any("operator's name" in w for w in manifest["warnings"]),
                manifest["warnings"])


if __name__ == "__main__":
    unittest.main()
