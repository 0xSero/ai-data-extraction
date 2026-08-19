import json
import tempfile
import unittest
from pathlib import Path

from filter_privacy import FilterStats, discover_inputs, filter_jsonl, redact_value


def fake_redact(text):
    return text.replace("alice@example.com", "<PRIVATE_EMAIL>").replace(
        "sk-secret", "<SECRET>"
    )


class RedactValueTests(unittest.TestCase):
    def test_redacts_nested_string_values_without_changing_keys(self):
        stats = FilterStats()
        value = {
            "messages": [
                {"role": "user", "content": "Email alice@example.com"},
                {"role": "assistant", "tool": {"output": "sk-secret"}},
            ],
            "count": 2,
        }

        filtered = redact_value(value, fake_redact, stats)

        self.assertEqual(
            filtered["messages"][0]["content"], "Email <PRIVATE_EMAIL>"
        )
        self.assertEqual(filtered["messages"][1]["tool"]["output"], "<SECRET>")
        self.assertEqual(filtered["messages"][0]["role"], "user")
        self.assertEqual(filtered["count"], 2)
        self.assertEqual(stats.strings, 4)
        self.assertEqual(stats.changed_strings, 2)


class FilterJsonlTests(unittest.TestCase):
    def test_writes_valid_jsonl_and_preserves_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            output = root / "filtered" / "source.jsonl"
            original = {"messages": [{"content": "alice@example.com"}]}
            source.write_text(json.dumps(original) + "\n", encoding="utf-8")

            stats = filter_jsonl(source, output, fake_redact)

            self.assertEqual(json.loads(source.read_text()), original)
            self.assertEqual(
                json.loads(output.read_text())["messages"][0]["content"],
                "<PRIVATE_EMAIL>",
            )
            self.assertEqual(stats.records, 1)

    def test_refuses_to_overwrite_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            output = root / "output.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            output.write_text("existing\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                filter_jsonl(source, output, fake_redact)

    def test_reports_invalid_json_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            output = root / "output.jsonl"
            source.write_text("{}\nnot-json\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "line 2"):
                filter_jsonl(source, output, fake_redact)
            self.assertFalse(output.exists())


class DiscoverInputsTests(unittest.TestCase):
    def test_recurses_through_input_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            input_path = nested / "corpus.jsonl"
            input_path.write_text("{}\n", encoding="utf-8")

            self.assertEqual(
                discover_inputs([root]), [(input_path, Path("nested/corpus.jsonl"))]
            )


if __name__ == "__main__":
    unittest.main()
