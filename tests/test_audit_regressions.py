"""Regression tests for the defects found in the independent security audit.

One test per finding, named for its id. Each reproduces the original defect, so
a regression fails here rather than in a bundle someone already shared.
"""

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import export_claude_harness as ex
from sanitize import Sanitizer, find_suspicious_tokens, is_secret_key


def s():
    return Sanitizer(use_detect_secrets=False)


def _install(root, name=".claude"):
    inst = Path(root) / name
    (inst / "projects" / "p").mkdir(parents=True)
    return inst


def _session(inst, lines, name="s.jsonl"):
    proj = inst / "projects" / "p"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / name).write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines))


class C1AuthorizationHeader(unittest.TestCase):
    """Only the scheme word was redacted; the credential shipped behind a marker."""

    def test_bearer_credential_is_redacted_not_just_the_scheme(self):
        out, counts = s().scrub_text("Authorization: Bearer b7f3CANARYtok9921")
        self.assertNotIn("b7f3CANARYtok9921", out)
        self.assertIn("Bearer", out)
        self.assertEqual(counts["secrets"], 1)

    def test_five_char_schemes_do_not_escape_the_length_floor(self):
        for line in ("Authorization: Token opsCANARY4Wq8Zx1Ly",
                     "Proxy-Authorization: Basic YWRtaW46c3VwZXJzZWNyZXQ="):
            out, counts = s().scrub_text(line)
            self.assertNotIn("CANARY", out)
            self.assertNotIn("YWRtaW46", out)
            self.assertEqual(counts["secrets"], 1, line)

    def test_header_inside_quotes_keeps_the_line_balanced(self):
        out, _ = s().scrub_text(
            'curl -H "Authorization: Bearer sk_live_CANARY_9921" https://x')
        self.assertNotIn("CANARY", out)
        self.assertEqual(out.count('"'), 2)
        self.assertIn("https://x", out)


class C2InjectedPrompts(unittest.TestCase):
    """Machine-injected tool output was exported as personal prompt patterns."""

    def test_flagged_and_tagged_events_are_not_prompt_patterns(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            _session(inst, [
                {"type": "user", "message": {"content": "my real prompt"}},
                {"type": "user", "isMeta": True,
                 "message": {"content": "INJECTED meta payload"}},
                {"type": "user", "sourceToolUseID": "abc",
                 "message": {"content": "INJECTED tool payload"}},
                {"type": "user", "message": {
                    "content": "<task-notification>INJECTED notification</task-notification>"}},
                {"type": "user", "message": {
                    "content": "<command-name>/foo</command-name>"}},
                {"type": "user", "message": {
                    "content": "Base directory for this skill: /x\nINJECTED skill body"}},
                {"type": "user", "message": {
                    "content": "<local-command-stdout>INJECTED stdout</local-command-stdout>"}},
            ])
            recs, _counts, dropped, _w = ex.collect_prompts(inst, s())

            self.assertEqual([r["text"] for r in recs], ["my real prompt"])
            self.assertNotIn("INJECTED", json.dumps(recs))
            self.assertEqual(dropped["injected_user_events"], 6)

    def test_strict_conversations_drop_injected_events_too(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            _session(inst, [
                {"type": "user", "message": {"content": "my real prompt"}},
                {"type": "user", "isMeta": True,
                 "message": {"content": "INJECTED payload"}},
                {"type": "user", "message": {
                    "content": "<task-notification>INJECTED notice</task-notification>"}},
            ])
            convs, _c, dropped, _w = ex.collect_conversations(inst, s(), "strict")
            self.assertNotIn("INJECTED", json.dumps(convs))
            self.assertEqual(dropped["injected_user_events"], 2)

    def test_balanced_keeps_injected_events_but_sanitizes_them(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            _session(inst, [{"type": "user", "isMeta": True, "message": {
                "content": "INJECTED /Users/carol/x"}}])
            convs, _c, _d, _w = ex.collect_conversations(inst, s(), "balanced")
            blob = json.dumps(convs)
            self.assertIn("INJECTED", blob)
            self.assertNotIn("carol", blob)
            self.assertNotIn("injected", blob)  # the internal flag is not exported


class C3JsonKeyNames(unittest.TestCase):
    """scrub_json rebuilt dicts as {k: walk(v)}, so key names were never scrubbed."""

    def test_home_paths_and_emails_used_as_keys_are_redacted(self):
        obj = {"projects": {"/Users/carol/work/acme": {"lastUsed": 1},
                            "carol@corp.example.com": {"role": "owner"}}}
        out, counts = s().scrub_json(obj)
        blob = json.dumps(out)
        self.assertNotIn("carol", blob)
        self.assertEqual(counts["paths"], 1)
        self.assertEqual(counts["emails"], 1)

    def test_keys_colliding_after_redaction_do_not_overwrite_each_other(self):
        obj = {"/Users/a/x": 1, "/Users/b/y": 2}
        out, _ = s().scrub_json(obj)
        self.assertEqual(len(out), 2)


class I1SameSecondBundle(unittest.TestCase):
    """Two runs in the same second shared a directory; the manifest under-described it."""

    def test_second_run_gets_its_own_directory(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            (inst / "CLAUDE.md").write_text("notes")
            out = Path(d) / "out"
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            with mock.patch.object(ex, "_timestamp", return_value="20260815_090000"):
                first = ex.write_bundle(export, out, dry_run=False)
                second = ex.write_bundle(export, out, dry_run=True)
            self.assertNotEqual(first, second)
            self.assertEqual([p.name for p in second.rglob("*") if p.is_file()],
                             ["MANIFEST.json"])

    def test_manifest_describes_every_file_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            (inst / "CLAUDE.md").write_text("notes")
            _session(inst, [{"type": "user", "message": {"content": "a prompt"}}])
            export = ex.build_export([inst], {"config", "prompts"}, "strict", False, "NOW")
            bundle = ex.write_bundle(export, Path(d) / "out", dry_run=False)
            on_disk = {p.relative_to(bundle).as_posix()
                       for p in bundle.rglob("*") if p.is_file()} - {"MANIFEST.json"}
            manifest = json.loads((bundle / "MANIFEST.json").read_text())
            listed = {e["path"] for e in manifest["files"]}
            self.assertEqual(on_disk, listed)


class I2MalformedSessionLine(unittest.TestCase):
    """A bare scalar line or a null message aborted the entire export."""

    def test_bad_lines_degrade_to_a_warning_and_the_bundle_survives(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            (inst / "CLAUDE.md").write_text("config still matters")
            _session(inst, [
                json.dumps({"type": "user", "message": {"content": "good prompt"}}),
                "42",
                json.dumps({"type": "user", "message": None}),
                json.dumps({"type": "user", "message": "not a dict"}),
                "{ truncated",
            ])
            export = ex.build_export([inst], {"config", "prompts"}, "strict", False, "NOW")
            self.assertIn("config/CLAUDE.md", export["files"])
            self.assertIn("good prompt", export["files"]["prompts/prompt_patterns.jsonl"])
            self.assertTrue(any("not usable JSON" in w
                                for w in export["manifest"]["warnings"]))

    def test_conversations_survive_the_same_input(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            _session(inst, [json.dumps({"type": "user", "message": {"content": "hi"}}), "42"])
            convs, _c, _d, warns = ex.collect_conversations(inst, s(), "strict")
            self.assertEqual(len(convs), 1)
            self.assertTrue(warns)


class I3ManifestWarningPaths(unittest.TestCase):
    """Warnings interpolated the absolute source path into the unsanitized manifest."""

    def test_unreadable_file_warning_carries_no_home_path(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d, ".claude")
            (inst / "memory").mkdir()
            secret = inst / "memory" / "private.md"
            secret.write_text("x")
            os.chmod(secret, 0o000)
            try:
                export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
                blob = json.dumps(export["manifest"])
                self.assertNotIn(str(inst), blob)
                self.assertNotIn(str(Path(d)), blob)
                self.assertTrue(any("memory/private.md" in w
                                    for w in export["manifest"]["warnings"]))
            finally:
                os.chmod(secret, 0o600)

    def test_invalid_json_warning_is_installation_relative(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            (inst / "settings.json").write_text("{ nope ")
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            self.assertNotIn(str(inst), json.dumps(export["manifest"]))


class I4BalancedKeyAwareness(unittest.TestCase):
    """Conversations used a string-only walker, leaking what config redacted by key."""

    def test_secret_key_inside_a_tool_use_is_redacted_like_settings(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            (inst / "settings.json").write_text(
                json.dumps({"env": {"DATABASE_PASSWORD": "corpProdPw2026"}}))
            _session(inst, [{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"env": {"DATABASE_PASSWORD": "corpProdPw2026"}}}]}}])
            export = ex.build_export(
                [inst], {"config", "conversations"}, "balanced", False, "NOW")
            blob = "\n".join(export["files"].values())
            self.assertIn("tool_uses", blob)
            self.assertNotIn("corpProdPw2026", blob)


class I5KeyOverMatching(unittest.TestCase):
    """Substring key matching blanked ordinary identifiers and prose."""

    def test_non_secret_identifiers_survive(self):
        for line in ('author = "Fabian Hug"',
                     "authorId: 12345678",
                     "oauth_provider: github.com/login",
                     "MAX_THINKING_TOKENS=31999",
                     "CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192"):
            out, counts = s().scrub_text(line)
            self.assertEqual(out, line, line)
            self.assertEqual(counts["secrets"], 0, line)

    def test_real_secret_keys_still_match(self):
        for name in ("password", "DATABASE_PASSWORD", "api_key", "x-api-key",
                     "apiKey", "accessToken", "AWS_SECRET_ACCESS_KEY",
                     "Proxy-Authorization", "client_secret"):
            self.assertTrue(is_secret_key(name), name)
        for name in ("author", "authorId", "oauth_provider", "MAX_THINKING_TOKENS",
                     "model", "DATABASE_URL"):
            self.assertFalse(is_secret_key(name), name)

    def test_separator_does_not_span_a_newline(self):
        text = "## Authentication:\nSee the docs for details."
        self.assertEqual(s().scrub_text(text)[0], text)


class I6Ipv6Slices(unittest.TestCase):
    """A bare :: matched, rewriting Python slices into invalid syntax."""

    def test_slices_and_scope_resolution_survive(self):
        for line in ("arr[::-1]", "arr[::2]", "matrix[::2, ::3]", "a[1::2]",
                     "arr[start::step]", "std::vector<int> Foo::Bar()",
                     'd["k"][::2]', "meeting at 12:34:56"):
            out, counts = s().scrub_text(line)
            self.assertEqual(out, line, line)
            self.assertEqual(counts["ips"], 0, line)

    def test_real_addresses_are_still_redacted(self):
        for line in ("host ::1 up", "addr 2001:db8::1", "[fe80::1]:443",
                     "http://[::1]:8080/"):
            out, counts = s().scrub_text(line)
            self.assertIn("[IP]", out, line)
            self.assertGreaterEqual(counts["ips"], 1, line)


class I7EntropyBackstop(unittest.TestCase):
    """The backstop flagged the exporter's own output and named no file."""

    def test_own_placeholders_and_uuids_are_not_flagged(self):
        self.assertEqual(find_suspicious_tokens(
            '{"source_session": "0e9c1a55-7b2e-4f1a-9c33-1d2b3c4d5e6f"}'), [])
        self.assertEqual(find_suspicious_tokens(
            "a [REDACTED_SECRET:openai_anthropic_key] b [PATH] c [EMAIL] d [IP]"), [])

    def test_manifest_attributes_leftovers_to_a_named_file(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            (inst / "CLAUDE.md").write_text("blob T0kX9vQzL4mNpR7wYc2BdF5gHj8K")
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            entry = next(e for e in export["manifest"]["files"]
                         if e["path"] == "config/CLAUDE.md")
            self.assertGreaterEqual(entry["suspicious_tokens"], 1)
            self.assertTrue(any("config/CLAUDE.md" in w
                                for w in export["manifest"]["warnings"]))


class I8SkippedAssets(unittest.TestCase):
    """The agents/ tree was missing and non-text assets vanished with no warning."""

    def test_agents_tree_is_exported(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            (inst / "agents").mkdir()
            (inst / "agents" / "code-reviewer.md").write_text("review carefully")
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            self.assertIn("config/agents/code-reviewer.md", export["files"])

    def test_non_text_assets_are_reported_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            skill = inst / "skills" / "s"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("Use scripts/convert.js")
            (skill / "convert.js").write_text("console.log(1)")
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            self.assertNotIn("config/skills/s/convert.js", export["files"])
            self.assertTrue(any("convert.js" in w
                                for w in export["manifest"]["warnings"]))


class I9AwsSecretKey(unittest.TestCase):
    """The 40-char AWS secret access key had no shape pattern."""

    KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

    def test_redacted_when_aws_context_is_present(self):
        for line in ("The AWS secret access key is " + self.KEY,
                     "aws_secret_access_key is " + self.KEY,
                     "AKIAIOSFODNN7EXAMPLE / " + self.KEY):
            out, _ = s().scrub_text(line)
            self.assertNotIn(self.KEY, out, line)

    def test_unrelated_40_char_blobs_are_not_eaten(self):
        line = "sha256 digest " + self.KEY + " here"
        self.assertEqual(s().scrub_text(line)[0], line)


class I10PartialValueRedaction(unittest.TestCase):
    """Values were cut at the first space or comma, shipping the rest."""

    def test_passphrase_is_fully_redacted(self):
        out, counts = s().scrub_text('prod password = "correct horse battery staple"')
        self.assertNotIn("horse", out)
        self.assertEqual(counts["secrets"], 1)

    def test_short_and_comma_bearing_values_are_redacted(self):
        for line in ('password="hunt2"', "legacy password=pass,word,123"):
            out, counts = s().scrub_text(line)
            self.assertNotIn("hunt2", out)
            self.assertNotIn("pass,word", out)
            self.assertEqual(counts["secrets"], 1, line)


class I11SpaceSeparatedCredentials(unittest.TestCase):
    """.netrc and curl -u forms have no [:=] adjacency and were invisible."""

    def test_netrc_and_curl_user_forms(self):
        out, _ = s().scrub_text(
            "machine api.acme login bob password Hunter2Prod")
        self.assertNotIn("Hunter2Prod", out)
        out, _ = s().scrub_text("curl -u deploybot:S3cretPassw0rd https://api.internal")
        self.assertNotIn("S3cretPassw0rd", out)
        self.assertIn("deploybot", out)

    def test_ambiguous_flags_are_left_alone(self):
        line = "sort -u results.txt && mkdir -p /tmp/x && docker run -u 1000 img"
        self.assertEqual(s().scrub_text(line)[0], line)


class I12ProseAndUrlSecrets(unittest.TestCase):
    """Secrets in prose, under innocuous keys, or in URL userinfo slipped through."""

    def test_prose_secret_is_redacted_but_english_survives(self):
        out, _ = s().scrub_text("prod db password is Xk9mQ2vL, rotate quarterly")
        self.assertNotIn("Xk9mQ2vL", out)
        self.assertIn("rotate quarterly", out)
        line = "the secret is important to us"
        self.assertEqual(s().scrub_text(line)[0], line)

    def test_url_credentials_including_dotless_hosts(self):
        for line, secret in (
                ("postgres://svc:Sup3rS3cretDbPw@db:5432/prod", "Sup3rS3cretDbPw"),
                ("redis://:hunter2pass@localhost:6379/0", "hunter2pass"),
                ("https://alice:Tr0ub4dor3xyzQ@example.com/x", "Tr0ub4dor3xyzQ")):
            out, counts = s().scrub_text(line)
            self.assertNotIn(secret, out, line)
            self.assertGreaterEqual(counts["secrets"], 1, line)

    def test_secret_under_an_innocuous_json_key(self):
        out, _ = s().scrub_json(
            {"note": "prod db password is Xk9mQ2vL", "comment": "ok"})
        self.assertNotIn("Xk9mQ2vL", json.dumps(out))


class M1SilentDrops(unittest.TestCase):
    """Corrupt lines, unreadable sessions and symlinked dirs vanished silently."""

    def test_unreadable_session_is_warned_about(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            _session(inst, [{"type": "user", "message": {"content": "a"}}], "s1.jsonl")
            bad = inst / "projects" / "p" / "s2.jsonl"
            bad.write_text("{}")
            os.chmod(bad, 0o000)
            try:
                _recs, _c, _d, warns = ex.collect_prompts(inst, s())
                self.assertTrue(any("s2.jsonl" in w for w in warns))
            finally:
                os.chmod(bad, 0o600)

    def test_symlinked_skill_directory_is_traversed(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            outside = Path(d) / "outside"
            outside.mkdir()
            (outside / "SKILL.md").write_text("linked skill body")
            (inst / "skills").mkdir()
            os.symlink(outside, inst / "skills" / "linked")
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            self.assertIn("config/skills/linked/SKILL.md", export["files"])

    def test_symlink_loop_terminates(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            skills = inst / "skills"
            skills.mkdir()
            (skills / "a").mkdir()
            os.symlink(skills, skills / "a" / "loop")
            ex.build_export([inst], {"config"}, "strict", False, "NOW")


class M2UnrecognisedContentShape(unittest.TestCase):
    """Strict passed a dict-shaped message.content through verbatim."""

    def test_dict_content_is_dropped_and_counted(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            _session(inst, [{"type": "user", "message": {"content": {
                "type": "tool_result", "text": "PROPRIETARY_SOURCE_CODE"}}}])
            convs, _c, dropped, _w = ex.collect_conversations(inst, s(), "strict")
            self.assertNotIn("PROPRIETARY_SOURCE_CODE", json.dumps(convs))
            self.assertGreaterEqual(dropped.get("content_blocks", 0), 1)


class M3SymlinkProvenance(unittest.TestCase):
    """A leaf symlink pulled content in from outside with no note in the manifest."""

    def test_outside_target_is_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            outside = Path(d) / "outside_creds.md"
            outside.write_text("# notes from elsewhere")
            (inst / "skills").mkdir()
            os.symlink(outside, inst / "skills" / "notes.md")
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            entry = next(e for e in export["manifest"]["files"]
                         if e["path"] == "config/skills/notes.md")
            self.assertIn("source", entry)


class M4BundlePathNames(unittest.TestCase):
    """File names were never sanitized or entropy-scanned, on disk or in the manifest."""

    def test_sensitive_file_name_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            (inst / "skills").mkdir()
            (inst / "skills" / "creds-for-bob@example-corp.com.md").write_text("x")
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            self.assertTrue(any("bundle path may itself carry" in w
                                for w in export["manifest"]["warnings"]))


class M5InstallationDiscovery(unittest.TestCase):
    """~/.claude was undiscoverable when no platform base directory existed."""

    def test_home_install_found_without_any_base_dir(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / ".claude").mkdir()
            (home / ".claude" / "CLAUDE.md").write_text("notes")
            with mock.patch.object(ex.Path, "home", staticmethod(lambda: home)):
                found = ex.find_claude_installations()
            self.assertEqual(found, [home / ".claude"])

    def test_discovery_is_deterministic_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / ".claude").mkdir()
            (home / ".config").mkdir()
            with mock.patch.object(ex.Path, "home", staticmethod(lambda: home)):
                a = ex.find_claude_installations()
                b = ex.find_claude_installations()
            self.assertEqual(a, b)
            self.assertEqual(len(a), len(set(a)))


class M6M7Namespaces(unittest.TestCase):
    """Suffixes collided with real basenames, and labels had no provenance."""

    def _inst(self, root, parent, name):
        p = Path(root) / parent / name
        p.mkdir(parents=True)
        (p / "CLAUDE.md").write_text("notes from %s/%s" % (parent, name))
        return p

    def test_generated_suffix_never_collides_with_a_real_basename(self):
        with tempfile.TemporaryDirectory() as d:
            insts = [self._inst(d, "p1", "claude"),
                     self._inst(d, "p2", "claude"),
                     self._inst(d, "p3", "claude-2")]
            export = ex.build_export(insts, {"config"}, "strict", False, "NOW")
            paths = [e["path"] for e in export["manifest"]["files"]]
            self.assertEqual(len(paths), len(set(paths)))
            self.assertEqual(len(paths), 3)

    def test_manifest_maps_each_namespace_to_a_stable_installation_id(self):
        with tempfile.TemporaryDirectory() as d:
            insts = [self._inst(d, "p1", "claude"), self._inst(d, "p2", "claude")]
            export = ex.build_export(insts, {"config"}, "strict", False, "NOW")
            entries = export["manifest"]["installations"]
            self.assertEqual(len(entries), 2)
            self.assertEqual(len({e["namespace"] for e in entries}), 2)
            for e in entries:
                self.assertIn("location", e)
                # A location class, never a filesystem name: the parent
                # directory of ~/.claude IS the operator's username.
                self.assertIn(e["location"],
                              ("home", "app-support", "xdg-config",
                               "local-share", "appdata-roaming",
                               "appdata-local", "other"))
            # Provenance must not carry the home path, and must not be a
            # brute-forceable digest of it either.
            self.assertNotIn(str(Path(d)), json.dumps(entries))
            self.assertNotIn("id", entries[0])


class M8DeepJson(unittest.TestCase):
    """Deeply nested valid JSON died with an uncaught RecursionError."""

    def test_deep_config_degrades_to_a_warning(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            (inst / "CLAUDE.md").write_text("keep me")
            (inst / "settings.json").write_text("[" * 2000 + "1" + "]" * 2000)
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            self.assertIn("config/CLAUDE.md", export["files"])
            self.assertNotIn("config/settings.sanitized.json", export["files"])
            self.assertTrue(any("settings.json" in w
                                for w in export["manifest"]["warnings"]))


class M9PrivateKeyRegex(unittest.TestCase):
    """An unbounded DOTALL body made unterminated BEGIN markers quadratic."""

    def test_many_unterminated_headers_stay_fast(self):
        text = ("-----BEGIN PRIVATE KEY-----\n" + "A" * 100 + "\n") * 6400
        start = time.time()
        s().scrub_text(text)
        # Measured at 20.6s before the fix, ~1.3s after. The bound is loose
        # enough for a slow machine and still fails a return to quadratic.
        self.assertLess(time.time() - start, 8.0)

    def test_complete_and_truncated_key_blocks_are_both_redacted(self):
        body = "\n".join(["MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ"] * 3)
        full = "-----BEGIN PRIVATE KEY-----\n%s\n-----END PRIVATE KEY-----" % body
        self.assertNotIn("MIIEvQ", s().scrub_text(full)[0])
        truncated = "-----BEGIN RSA PRIVATE KEY-----\n%s\n" % body
        self.assertNotIn("MIIEvQ", s().scrub_text(truncated)[0])


class M10DroppedUnits(unittest.TestCase):
    """dropped[field] counted messages while content_blocks counted blocks."""

    def test_every_dropped_key_counts_items(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            tool_uses = [{"type": "tool_use", "name": "Edit", "input": {"i": i}}
                         for i in range(10)]
            _session(inst, [{"type": "assistant", "message": {
                "content": [{"type": "text", "text": "ok"}] + tool_uses}}])
            _convs, _c, dropped, _w = ex.collect_conversations(inst, s(), "strict")
            self.assertEqual(dropped["tool_uses"], 10)


class NoRegressionInVerifiedBehaviour(unittest.TestCase):
    """Properties the audit confirmed were already correct."""

    def test_manifest_hashes_match_in_both_directions(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            (inst / "CLAUDE.md").write_text("hi /Users/fabian/x")
            _session(inst, [{"type": "user", "message": {"content": "a prompt"}}])
            export = ex.build_export([inst], {"config", "prompts"}, "strict", False, "NOW")
            bundle = ex.write_bundle(export, Path(d) / "out", dry_run=False)
            manifest = json.loads((bundle / "MANIFEST.json").read_text())
            for entry in manifest["files"]:
                data = (bundle / entry["path"]).read_bytes()
                self.assertEqual(entry["bytes"], len(data))
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())

    def test_known_secret_shapes_still_redacted(self):
        text = ("AKIAIOSFODNN7EXAMPLE ghp_" + "a" * 36 +
                " sk-ant-api03-" + "b" * 30 + " xoxb-123456789012-abcdef"
                " user@corp.example.com 10.0.0.5 /Users/fabian/x")
        out, counts = s().scrub_text(text)
        for leaked in ("AKIA", "ghp_", "sk-ant-", "xoxb-", "@corp.example.com",
                       "10.0.0.5", "fabian"):
            self.assertNotIn(leaked, out, leaked)
        self.assertGreaterEqual(counts["secrets"], 4)

    def test_strict_still_drops_tool_result_blocks_in_user_content(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            _session(inst, [{"type": "user", "message": {"content": [
                {"type": "text", "text": "see this"},
                {"type": "tool_result", "content": "FILE BODY ghp_" + "a" * 36}]}}])
            convs, _c, dropped, _w = ex.collect_conversations(inst, s(), "strict")
            blob = json.dumps(convs)
            self.assertNotIn("FILE BODY", blob)
            self.assertIn("see this", blob)
            self.assertGreaterEqual(dropped["content_blocks"], 1)


class V1KeyVocabulary(unittest.TestCase):
    """Round-2: whole-segment matching had narrowed too far to catch real keys."""

    def test_concatenated_and_bare_auth_keys_match(self):
        for name in ("PGPASSWORD", "AUTHTOKEN", "CLIENTSECRET", "SECRETKEY",
                     "mypassword", "api_keys", "access_keys", "private_keys",
                     "X-Acme-Auth", "Cookie", "Set-Cookie", "Authentication",
                     "ssh_key", "deploy_key", "master_key",
                     "Ocp-Apim-Subscription-Key"):
            self.assertTrue(is_secret_key(name), name)

    def test_bare_plurals_count_as_json_keys_but_not_in_free_text(self):
        # {"tokens": [...]} in settings.json is a credential store;
        # `tokens = tokenizer.encode(x)` in a code block is not.
        for name in ("secrets", "tokens", "passwords"):
            self.assertTrue(is_secret_key(name, json_key=True), name)
            self.assertFalse(is_secret_key(name), name)

    def test_duration_keys_are_not_credentials(self):
        for name in ("TOKEN_TTL_SECONDS", "TOKEN_TTL_UNUSED",
                     "session_timeout_ms", "token_expiry_days"):
            self.assertFalse(is_secret_key(name), name)

    def test_benign_identifiers_still_survive(self):
        for name in ("author", "authorId", "authors", "oauth_provider",
                     "MAX_THINKING_TOKENS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                     "output_tokens", "input_tokens", "token_count",
                     "total_tokens", "pwd", "PWD", "model", "DATABASE_URL",
                     "authorization_code_flow_docs", "monkey", "turkey"):
            self.assertFalse(is_secret_key(name), name)

    def test_pwd_is_not_a_secret_word(self):
        line = "docker run -v $PWD:/app image"
        self.assertEqual(s().scrub_text(line)[0], line)


class V2BlockValues(unittest.TestCase):
    """Round-2: a value on the line after its key was never redacted."""

    def test_indented_block_and_yaml_scalar(self):
        out, _ = s().scrub_text("password:\n    CANARYnextLine42\nother: keep")
        self.assertNotIn("CANARYnextLine42", out)
        self.assertIn("other: keep", out)

    def test_block_indicator_is_not_eaten(self):
        out, _ = s().scrub_text(
            "password: |\n  Hunter2Block\napi_key:\n  - Hunter2List\n")
        self.assertIn("password: |", out)
        self.assertNotIn("Hunter2Block", out)
        self.assertNotIn("Hunter2List", out)
        self.assertIn("- [REDACTED", out)

    def test_env_style_value_on_next_line(self):
        out, _ = s().scrub_text("DATABASE_PASSWORD=\nNextLineVal99\n")
        self.assertNotIn("NextLineVal99", out)

    def test_a_sibling_key_is_not_mistaken_for_the_value(self):
        for text in ("PASSWORD=\nusername: bob\n", "password:\nusername: bob\n"):
            self.assertEqual(s().scrub_text(text)[0], text)


class V3ProseIsNotDestroyed(unittest.TestCase):
    """Round-2: value-to-end-of-line and a loose heuristic ate documentation."""

    def test_documentation_survives(self):
        for line in ("## The auth token: how it works in our system",
                     "See the [secret handling](https://docs.example.com/s) page",
                     "# Token budget and context windows",
                     "The auth flow uses a secret handshake to negotiate",
                     "Our token budget is large and the secret sauce is caching",
                     "the token budget is roughly 200k for this model"):
            self.assertEqual(s().scrub_text(line)[0], line, line)

    def test_real_secrets_in_prose_still_go(self):
        for line, secret in (
                ("Set the token: abc123secret in the config", "abc123secret"),
                ("machine x login bob password Hunter2Prod", "Hunter2Prod")):
            self.assertNotIn(secret, s().scrub_text(line)[0], line)

    def test_plural_s_is_not_deleted_from_identifiers(self):
        out, _ = s().scrub_text("the secrets module is fine")
        self.assertIn("secrets", out)


class V4KeyNameNotRepublished(unittest.TestCase):
    """Round-2: the placeholder re-published the raw key it had just scrubbed."""

    def test_placeholder_carries_the_scrubbed_key(self):
        out, _ = s().scrub_json(
            {"projects": {"/Users/carol/repos/token-service": "abc123def456"}})
        blob = json.dumps(out)
        self.assertNotIn("carol", blob)
        self.assertNotIn("/Users/", blob)


class V5UrlAndIpEdges(unittest.TestCase):
    """Round-2: single-field userinfo, query-string tokens, version numbers."""

    def test_single_field_userinfo_and_query_tokens(self):
        for line, secret in (
                ("https://ghp_CANARYtoken1234567890abcdefghij@github.com/x", "ghp_CANARY"),
                ("https://api.x.com/v1?access_token=CANARYqueryTok99&page=2", "CANARYqueryTok99")):
            self.assertNotIn(secret, s().scrub_text(line)[0], line)

    def test_out_of_range_dotted_quads_are_not_addresses(self):
        # A dotted quad with an octet over 255 cannot be an address.
        for line in ("version 10.999.0.1", "build 2026.8.15.1"):
            self.assertEqual(s().scrub_text(line)[0], line, line)
        self.assertIn("[IP]", s().scrub_text("host 10.0.0.5")[0])
        # A valid dotted quad stays ambiguous with a four-part version string.
        # It is redacted: an address leaking matters more than a version
        # surviving. Documented in README under "What is redacted".
        self.assertIn("[IP]", s().scrub_text("upgraded to 1.2.3.4 today")[0])


class V6MoreOverMatching(unittest.TestCase):
    """Round-2: tilde, CLI flags and shape anchors."""

    def test_strikethrough_and_approximately_survive(self):
        for line in ("~~deprecated~~ use the new one", "takes ~about 5 minutes"):
            self.assertEqual(s().scrub_text(line)[0], line, line)
        self.assertNotIn("carol", s().scrub_text("~carol/.ssh/id_rsa")[0])

    def test_benign_cli_flags_survive(self):
        for line in ("docker run -u 1000:1000 img", "docker --secret my-resource img"):
            self.assertEqual(s().scrub_text(line)[0], line, line)

    def test_underscore_does_not_defeat_the_shape_anchor(self):
        out, _ = s().scrub_text("PREFIX_AKIAIOSFODNN7EXAMPLE")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)


class V7ExporterRoundTwo(unittest.TestCase):
    """Round-2 exporter defects."""

    def test_project_directory_names_do_not_leak_the_username(self):
        with tempfile.TemporaryDirectory() as d:
            inst = Path(d) / ".claude"
            proj = inst / "projects" / "-Users-carol-Projects-secret-client"
            proj.mkdir(parents=True)
            (proj / "s.jsonl").write_text('{"type":"user","message":{"content":"hi"}}\n42')
            export = ex.build_export([inst], {"config", "prompts"}, "strict", False, "NOW")
            blob = json.dumps(export["manifest"])
            self.assertNotIn("carol", blob)
            self.assertNotIn("secret-client", blob)
            self.assertTrue(any("not usable JSON" in w
                                for w in export["manifest"]["warnings"]))

    def test_non_string_text_field_does_not_abort_the_export(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            _session(inst, [
                {"type": "user", "message": {"content": [
                    {"type": "text", "text": {"nested": "dict"}}]}},
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": 12345}]}},
                {"type": "user", "message": {"content": "real prompt"}},
            ])
            export = ex.build_export([inst], {"config", "prompts", "conversations"},
                                     "strict", False, "NOW")
            self.assertIn("real prompt", export["files"]["prompts/prompt_patterns.jsonl"])

    def test_unreadable_project_directory_is_warned_about(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            proj = inst / "projects" / "locked"
            proj.mkdir()
            (proj / "s.jsonl").write_text("{}")
            os.chmod(proj, 0o000)
            try:
                _recs, _c, _dr, warns = ex.collect_prompts(inst, s())
                self.assertTrue(any("locked" in w for w in warns), warns)
            finally:
                os.chmod(proj, 0o700)

    def test_symlinked_alias_does_not_drop_the_real_directory(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            skills = inst / "skills"
            (skills / "real").mkdir(parents=True)
            (skills / "real" / "SKILL.md").write_text("real body")
            os.symlink(skills / "real", skills / "alias")
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            self.assertIn("config/skills/real/SKILL.md", export["files"])

    def test_symlink_source_is_warned_about_as_the_readme_promises(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            outside = Path(d) / "outside.md"
            outside.write_text("elsewhere")
            (inst / "skills").mkdir()
            os.symlink(outside, inst / "skills" / "notes.md")
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            self.assertTrue(any("symlink" in w
                                for w in export["manifest"]["warnings"]))

    def test_deeply_nested_session_line_does_not_kill_the_export(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            deep = json.dumps({"type": "user", "message": {"content": "hi"}})
            _session(inst, [deep, "[" * 5000 + "1" + "]" * 5000])
            export = ex.build_export([inst], {"prompts"}, "strict", False, "NOW")
            self.assertIn("prompts/prompt_patterns.jsonl", export["files"])


class V8AssignmentPerformance(unittest.TestCase):
    """Round-2: _ASSIGN_HEAD retried inside every long identifier run."""

    def test_long_single_line_identifier_blob_is_not_quadratic(self):
        text = "a" * 32768
        start = time.time()
        s().scrub_text(text)
        # 7.5s before the lookbehind was added.
        self.assertLess(time.time() - start, 2.0)


class W1SurrogateCrash(unittest.TestCase):
    """Round-3: a lone surrogate aborted the export and wrote nothing at all."""

    def test_lone_surrogate_does_not_abort_the_export(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            (inst / "CLAUDE.md").write_text("keep me")
            (inst / "settings.json").write_text('{"note": "x \\ud800 y"}')
            _session(inst, ['{"type":"user","message":{"content":"hi \\udccc there"}}'])
            export = ex.build_export([inst], {"config", "prompts"}, "strict", False, "NOW")
            bundle = ex.write_bundle(export, Path(d) / "out", dry_run=False)
            self.assertTrue((bundle / "MANIFEST.json").exists())
            self.assertIn("config/CLAUDE.md", export["files"])


class W2ManifestIdentity(unittest.TestCase):
    """Round-3: installations[].parent was literally the operator's username."""

    def test_provenance_is_a_location_class_not_a_directory_name(self):
        self.assertEqual(ex._install_provenance(Path("/nowhere/carol/.claude")),
                         "other")
        for p in ("/Users/carol/.claude", "/home/carol/.claude"):
            self.assertNotIn("carol", ex._install_provenance(Path(p)))

    def test_project_directory_name_is_hashed_in_warnings(self):
        with tempfile.TemporaryDirectory() as d:
            inst = Path(d) / ".claude"
            proj = inst / "projects" / "-Users-carol-src-Acme-Private"
            proj.mkdir(parents=True)
            (proj / "s.jsonl").write_text("42")
            export = ex.build_export([inst], {"prompts"}, "strict", False, "NOW")
            blob = json.dumps(export["manifest"])
            self.assertNotIn("carol", blob)
            self.assertNotIn("Acme-Private", blob)

    def test_shallow_windows_style_project_name_is_also_hashed(self):
        self.assertNotIn("work", ex._safe_component("D--work"))
        self.assertEqual(ex._safe_component("myproject"), "myproject")


class W3MoreCredentialShapes(unittest.TestCase):
    """Round-3: pass/pwd abbreviations, PGP blocks, vendor tokens, home paths."""

    def test_pass_and_pwd_abbreviations(self):
        for line, secret in (("export DB_PASS=RealCanaryPw99", "RealCanaryPw99"),
                             ("MYSQL_PWD=AnotherCanary11", "AnotherCanary11"),
                             ("Pwd=OdbcCanary22;Server=x", "OdbcCanary22")):
            self.assertNotIn(secret, s().scrub_text(line)[0], line)

    def test_shell_pwd_variable_survives(self):
        line = 'docker run -v "$PWD:/app" -w /app img'
        self.assertEqual(s().scrub_text(line)[0], line)

    def test_pgp_and_openssh_key_blocks(self):
        body = "\n".join(["mQINBGCanaryKeyMaterialAAAAAAAAAAAAAAAAAA"] * 3)
        for header in ("PGP PRIVATE KEY BLOCK", "OPENSSH PRIVATE KEY"):
            text = "-----BEGIN %s-----\n%s\n-----END %s-----" % (header, body, header)
            self.assertNotIn("CanaryKeyMaterial", s().scrub_text(text)[0], header)

    def test_vendor_token_shapes(self):
        for secret in ("sk_live_CanaryStripe123456",
                       "glpat-CanaryGitlabToken1234",
                       "npm_" + "C" * 36,
                       "SG.CanarySendgridAAAAAA.BBBBBBCanarySendgrid",
                       "https://hooks.slack.com/services/T00/B00/CanaryWebhook12"):
            self.assertNotIn("Canary", s().scrub_text("val " + secret)[0], secret)

    def test_home_path_case_and_unc_variants(self):
        for line in ("d:\\users\\carol\\notes.txt",
                     "D:/Users/carol/notes.txt",
                     "\\\\server\\carol\\notes.txt"):
            self.assertNotIn("carol", s().scrub_text(line)[0], line)

    def test_generalised_credential_flags(self):
        for line, secret in (
                ("cmd --auth-token AbcCanary1234 rest", "AbcCanary1234"),
                ("cmd --client-secret XyzCanary9876 rest", "XyzCanary9876")):
            self.assertNotIn(secret, s().scrub_text(line)[0], line)
        benign = "cmd --output-dir build --verbose --max-tokens 4096"
        self.assertEqual(s().scrub_text(benign)[0], benign)


class W4OverRedactionRoundThree(unittest.TestCase):
    """Round-3: 17 of 43 credential-free lines were being destroyed."""

    DOC = """# Notes
- The auth flow uses a shared secret: see the design doc.
> Blockquote about token budgets and password policy.
Use `--api-key` to pass credentials.
def connect(host, password=None, timeout=30):
docker run -e MODE=prod -e DEBUG=1 image
| token limit | 200000 |
export PATH=/usr/local/bin:$PATH
The well-documented-approach is preferred here.
"""

    def test_credential_free_document_is_untouched(self):
        out, counts = s().scrub_text(self.DOC)
        self.assertEqual(out, self.DOC)
        self.assertEqual(counts["secrets"], 0)

    def test_real_credentials_in_the_same_document_still_go(self):
        text = self.DOC + "password: hunter2Canary\nexport DB_PASS=RealCanary99\n"
        out, counts = s().scrub_text(text)
        self.assertNotIn("hunter2Canary", out)
        self.assertNotIn("RealCanary99", out)
        self.assertEqual(counts["secrets"], 2)

    def test_nested_mapping_under_a_secret_key_is_not_swallowed(self):
        text = "auth:\n  type: oauth\n  scopes: read\nother: keep\n"
        out, _ = s().scrub_text(text)
        self.assertIn("type:", out)
        self.assertIn("other: keep", out)

    def test_code_fence_is_not_eaten_by_an_empty_assignment(self):
        text = "```sh\nexport TOKEN=\n```\ntail\n"
        out, _ = s().scrub_text(text)
        self.assertIn("```\ntail", out)


class W5SessionParsingRoundThree(unittest.TestCase):
    """Round-3: line splitting and block accounting."""

    def test_unicode_line_separator_does_not_split_a_record(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            _session(inst, [json.dumps(
                {"type": "user", "message": {"content": "before\u2028after"}})])
            recs, _c, _dr, _w = ex.collect_prompts(inst, s())
            self.assertEqual(len(recs), 1)
            self.assertIn("after", recs[0]["text"])

    def test_a_mixed_turn_keeps_the_operators_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            _session(inst, [{"type": "user", "message": {"content": [
                {"type": "text", "text": "my real question"},
                {"type": "text", "text": "<system-reminder>INJECTED</system-reminder>"}]}}])
            recs, _c, _dr, _w = ex.collect_prompts(inst, s())
            self.assertEqual(len(recs), 1)
            self.assertIn("my real question", recs[0]["text"])
            self.assertNotIn("INJECTED", recs[0]["text"])

    def test_discarded_assistant_blocks_are_counted(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            _session(inst, [{"type": "assistant", "message": {"content": [
                {"type": "text", "text": "answer"},
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "redacted_thinking", "data": "xx"}]}}])
            convs, _c, dropped, _w = ex.collect_conversations(inst, s(), "strict")
            self.assertNotIn("private reasoning", json.dumps(convs))
            self.assertEqual(dropped["assistant_blocks"], 2)

    def test_nan_config_is_skipped_with_a_warning(self):
        with tempfile.TemporaryDirectory() as d:
            inst = _install(d)
            (inst / "CLAUDE.md").write_text("keep")
            (inst / "settings.json").write_text('{"x": NaN}')
            export = ex.build_export([inst], {"config"}, "strict", False, "NOW")
            self.assertNotIn("config/settings.sanitized.json", export["files"])
            self.assertTrue(any("settings.json" in w
                                for w in export["manifest"]["warnings"]))


class X1PassOrdering(unittest.TestCase):
    """Round-4: a credential key name destroyed the BEGIN marker the shape needed."""

    BLOCK = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
             "b3BlbnNzaENBTkFSWWtleWJvZHk\n"
             "-----END OPENSSH PRIVATE KEY-----\n")

    def test_naming_a_key_does_not_make_redaction_worse(self):
        bare = s().scrub_text(self.BLOCK)[0]
        named = s().scrub_text("deploy_key: " + self.BLOCK)[0]
        self.assertNotIn("CANARY", bare)
        self.assertNotIn("CANARY", named)
        self.assertIn("private_key", named)

    def test_shape_label_is_not_overwritten_by_the_assignment_pass(self):
        out, counts = s().scrub_text("api_key: sk-ant-api03-" + "z" * 30)
        self.assertIn("openai_anthropic_key", out)
        self.assertEqual(counts["secrets"], 1)


class X2ArgvCredentials(unittest.TestCase):
    """Round-4: the canonical MCP argv form put flag and value in separate strings."""

    def test_split_argv_credential_is_redacted(self):
        out, counts = s().scrub_json({"args": [
            "acme-mcp", "--api-key", "Jk8812xQa", "--password", "Passw0rd77aa"]})
        blob = json.dumps(out)
        self.assertNotIn("Jk8812xQa", blob)
        self.assertNotIn("Passw0rd77aa", blob)
        self.assertIn("acme-mcp", blob)
        self.assertEqual(counts["secrets"], 2)

    def test_a_non_credential_flag_keeps_its_value(self):
        out, _ = s().scrub_json({"args": ["--output-dir", "build", "--verbose"]})
        self.assertIn("build", json.dumps(out))


class X3MoreVocabulary(unittest.TestCase):
    """Round-4: Azure/GCP connection-string vocabulary was missing."""

    def test_azure_and_pat_keys(self):
        for name in ("AccountKey", "SharedAccessKey", "AZURE_DEVOPS_EXT_PAT",
                     "SharedAccessSignature", "connection_string"):
            self.assertTrue(is_secret_key(name), name)

    def test_descriptor_keys_keep_their_values(self):
        for name in ("auth_mode", "secret_name", "cookie_samesite",
                     "token_endpoint", "key_algorithm", "auth_provider"):
            self.assertFalse(is_secret_key(name), name)
        for name in ("api_key", "DATABASE_PASSWORD", "AccountKey"):
            self.assertTrue(is_secret_key(name), name)

    def test_all_caps_plural_env_var_is_a_credential(self):
        out, _ = s().scrub_text("CREDENTIALS=CanaryPluralValue1")
        self.assertNotIn("CanaryPluralValue1", out)
        code = "credentials = load_from_disk()"
        self.assertEqual(s().scrub_text(code)[0], code)


class X4SpacedHomePaths(unittest.TestCase):
    """Round-4: a home path containing a space was only half redacted."""

    def test_space_in_path_is_consumed_but_the_sentence_is_not(self):
        out, _ = s().scrub_text("see /Users/First Last/repos/x for detail")
        self.assertNotIn("Last", out)
        self.assertIn("for detail", out)

    def test_windows_and_unc_spaced_paths(self):
        for line in ("path C:\\Users\\First Last\\docs\\a.txt and more",
                     "unc \\\\srv\\First Last\\a.txt and more"):
            out, _ = s().scrub_text(line)
            self.assertNotIn("Last", out, line)
            self.assertIn("and more", out, line)


class X5ValueSpanEdges(unittest.TestCase):
    """Round-4: triple quotes, sibling assignments, and literal values."""

    def test_triple_quoted_value(self):
        out, _ = s().scrub_text('password = """multi CANARYtriple line"""')
        self.assertNotIn("CANARYtriple", out)

    def test_sibling_assignment_on_the_same_line_survives(self):
        out, _ = s().scrub_text("export TOKEN=CanaryTok123 PATH=/usr/bin")
        self.assertNotIn("CanaryTok123", out)
        self.assertIn("PATH=/usr/bin", out)

    def test_literal_values_keep_their_type(self):
        for line in ("auth_enabled = true", "secret = None", "token = null",
                     "api_key = false"):
            self.assertEqual(s().scrub_text(line)[0], line, line)

    def test_bullet_credential_goes_but_bullet_prose_stays(self):
        out, _ = s().scrub_text("- password: correct horse battery staple")
        self.assertNotIn("horse", out)
        for line in ("- The auth flow uses a shared secret: see the design doc.",
                     "### Token: what the field means",
                     "> Notes about token budgets and password policy."):
            self.assertEqual(s().scrub_text(line)[0], line, line)


class X6EntropyPrecision(unittest.TestCase):
    """Round-4: the backstop was ~100% false positives on a real installation."""

    def test_markdown_paths_are_not_flagged(self):
        self.assertEqual(find_suspicious_tokens(
            "see docs/reference/api-guide/configuration and src/lib/handlers"), [])

    def test_a_real_token_is_still_flagged(self):
        self.assertTrue(find_suspicious_tokens("tok Xk92mQpLz84vNbRt3wYcAB"))

    def test_ip_counter_only_counts_real_replacements(self):
        _out, counts = s().scrub_text("version 10.999.0.1 and 2026.8.15.1")
        self.assertEqual(counts["ips"], 0)


class X7Performance(unittest.TestCase):
    """Round-4: two quadratic hot spots in the default path."""

    def test_prose_rule_is_linear_on_a_whitespace_run(self):
        text = "password" + (" " * 100000) + "x"
        start = time.time()
        s().scrub_text(text)
        self.assertLess(time.time() - start, 3.0)

    def test_many_assignments_are_linear(self):
        text = "\n".join("password%d=Secret%dValue" % (i, i) for i in range(8000))
        start = time.time()
        s().scrub_text(text)
        self.assertLess(time.time() - start, 3.0)


if __name__ == "__main__":
    unittest.main()
