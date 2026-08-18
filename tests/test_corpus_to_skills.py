import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from corpus_to_skills import (
    GeneratedSkill,
    build_prompt,
    call_model,
    chat_completions_url,
    parse_json_response,
    sample_corpus,
    validate_skills,
    write_skills,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class CorpusSamplingTests(unittest.TestCase):
    def test_samples_valid_records_within_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "corpus.jsonl"
            records = [
                {"source": "codex", "messages": [{"role": "user", "content": str(i)}]}
                for i in range(5)
            ]
            corpus.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            sampled, selected_count, total_count = sample_corpus(
                [corpus], char_budget=240, max_conversations=3, seed=4
            )

            self.assertLessEqual(len(sampled), 240)
            self.assertGreater(selected_count, 0)
            self.assertEqual(total_count, 5)
            self.assertIn("messages", sampled)


class ModelClientTests(unittest.TestCase):
    def test_normalizes_versioned_and_unversioned_base_urls(self):
        self.assertEqual(
            chat_completions_url("http://localhost:8000/v1"),
            "http://localhost:8000/v1/chat/completions",
        )
        self.assertEqual(
            chat_completions_url("http://localhost:8000"),
            "http://localhost:8000/v1/chat/completions",
        )

    @patch("urllib.request.urlopen")
    def test_calls_chat_completions_without_output_token_limit(self, urlopen):
        response = {"choices": [{"message": {"content": '{"skills": []}'}}]}
        urlopen.return_value = FakeResponse(json.dumps(response).encode())

        content = call_model(
            base_url="http://localhost:8000/v1",
            api_key=None,
            model="local-model",
            messages=build_prompt("example", 1),
            timeout=1,
        )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(content, '{"skills": []}')
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("max_completion_tokens", payload)


class SkillOutputTests(unittest.TestCase):
    def test_parses_fenced_json_and_validates_skills(self):
        content = """```json
{"skills":[{"name":"review-python","description":"Review Python when asked for correctness checks.","body":"# Workflow\\n\\nInspect behavior first."}]}
```"""
        skills = validate_skills(parse_json_response(content), expected_count=1)
        self.assertEqual(skills[0].name, "review-python")

    def test_rejects_invalid_skill_name(self):
        payload = {
            "skills": [
                {"name": "Bad Name", "description": "Useful trigger", "body": "Act."}
            ]
        }
        with self.assertRaisesRegex(ValueError, "invalid Agent Skills name"):
            validate_skills(payload, expected_count=1)

    def test_writes_spec_compliant_skill_and_refuses_overwrite(self):
        skill = GeneratedSkill(
            "review-python",
            "Review Python when asked for correctness checks.",
            "# Workflow\n\nInspect behavior first.",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            paths = write_skills([skill], output_dir=output, overwrite=False)
            text = paths[0].read_text(encoding="utf-8")
            self.assertIn("name: review-python", text)
            self.assertIn('description: "Review Python', text)
            self.assertTrue(text.endswith("\n"))
            with self.assertRaises(FileExistsError):
                write_skills([skill], output_dir=output, overwrite=False)


if __name__ == "__main__":
    unittest.main()
