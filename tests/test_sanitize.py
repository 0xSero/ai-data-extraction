import unittest
from sanitize import Sanitizer, find_suspicious_tokens


class ScrubTextSecrets(unittest.TestCase):
    def setUp(self):
        # use_detect_secrets=False keeps the test deterministic and offline
        self.s = Sanitizer(use_detect_secrets=False)

    def test_redacts_aws_key(self):
        text = "key = AKIAIOSFODNN7EXAMPLE done"
        out, counts = self.s.scrub_text(text)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)
        self.assertIn("[REDACTED_SECRET:aws_access_key]", out)
        self.assertEqual(counts["secrets"], 1)

    def test_redacts_github_and_openai(self):
        text = "ghp_" + "a" * 36 + " and sk-ant-" + "b" * 40
        out, counts = self.s.scrub_text(text)
        self.assertNotIn("ghp_", out)
        self.assertNotIn("sk-ant-", out)
        self.assertEqual(counts["secrets"], 2)

    def test_redacts_private_key_block(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nabc\ndef\n-----END RSA PRIVATE KEY-----"
        out, counts = self.s.scrub_text(text)
        self.assertNotIn("abc", out)
        self.assertEqual(counts["secrets"], 1)

    def test_redacts_generic_assignment(self):
        text = 'password: "hunter2secret"'
        out, counts = self.s.scrub_text(text)
        self.assertNotIn("hunter2secret", out)
        self.assertGreaterEqual(counts["secrets"], 1)

    def test_redacts_home_path_strips_username(self):
        text = "see /Users/fabian/Projects/secretco/app.py now"
        out, counts = self.s.scrub_text(text)
        self.assertNotIn("fabian", out)
        self.assertNotIn("secretco", out)
        self.assertIn("[PATH]", out)
        self.assertEqual(counts["paths"], 1)

    def test_redacts_email_and_ip(self):
        text = "mail me at bob@internal.corp from 10.1.2.3"
        out, counts = self.s.scrub_text(text)
        self.assertNotIn("bob@internal.corp", out)
        self.assertNotIn("10.1.2.3", out)
        self.assertEqual(counts["emails"], 1)
        self.assertEqual(counts["ips"], 1)

    def test_clean_text_unchanged(self):
        text = "This is a normal sentence about testing."
        out, counts = self.s.scrub_text(text)
        self.assertEqual(out, text)
        self.assertEqual(sum(counts.values()), 0)

    def test_disabled_detect_secrets_status(self):
        self.assertEqual(self.s.detect_secrets_status, "disabled")


class SuspiciousTokens(unittest.TestCase):
    def test_flags_high_entropy_leftover(self):
        token = "Zx9Qw3Lp7Vb2Nm5Kd8Rf1Ht4Gs6Yj0"
        found = find_suspicious_tokens("value is " + token)
        self.assertIn(token, found)

    def test_ignores_plain_words(self):
        self.assertEqual(find_suspicious_tokens("just some ordinary english words"), [])


if __name__ == "__main__":
    unittest.main()
