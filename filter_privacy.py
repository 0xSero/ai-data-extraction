#!/usr/bin/env python3
"""Redact private data from extracted JSONL corpora with OpenAI Privacy Filter."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


MODEL_ID = "openai/privacy-filter"


@dataclass
class FilterStats:
    records: int = 0
    strings: int = 0
    changed_strings: int = 0


def redact_value(
    value: Any,
    redact: Callable[[str], str],
    stats: FilterStats,
) -> Any:
    """Recursively redact every string value while preserving JSON structure."""
    if isinstance(value, str):
        stats.strings += 1
        if not value:
            return value
        redacted = redact(value)
        if redacted != value:
            stats.changed_strings += 1
        return redacted
    if isinstance(value, list):
        return [redact_value(item, redact, stats) for item in value]
    if isinstance(value, dict):
        return {
            key: redact_value(item, redact, stats)
            for key, item in value.items()
        }
    return value


def filter_jsonl(
    input_path: Path,
    output_path: Path,
    redact: Callable[[str], str],
    *,
    overwrite: bool = False,
) -> FilterStats:
    """Filter one JSONL file into a separate output file."""
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Refusing to overwrite {output_path}; pass --overwrite to replace it"
        )
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Input and output paths must be different")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    stats = FilterStats()

    try:
        with input_path.open("r", encoding="utf-8") as source, temporary_path.open(
            "w", encoding="utf-8"
        ) as destination:
            for line_number, raw_line in enumerate(source, start=1):
                if not raw_line.strip():
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON in {input_path} at line {line_number}: {exc.msg}"
                    ) from exc

                filtered = redact_value(record, redact, stats)
                destination.write(json.dumps(filtered, ensure_ascii=False) + "\n")
                stats.records += 1

        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise

    return stats


def discover_inputs(paths: Iterable[Path]) -> list[tuple[Path, Path]]:
    """Return input files paired with their relative output paths."""
    discovered: list[tuple[Path, Path]] = []
    seen_outputs: dict[Path, Path] = {}

    for path in paths:
        if path.is_file():
            candidates = [(path, Path(path.name))]
        elif path.is_dir():
            candidates = [
                (candidate, candidate.relative_to(path))
                for candidate in sorted(path.rglob("*.jsonl"))
                if candidate.is_file()
            ]
        else:
            raise FileNotFoundError(f"Input path does not exist: {path}")

        for input_path, relative_output in candidates:
            previous = seen_outputs.get(relative_output)
            if previous is not None and previous.resolve() != input_path.resolve():
                raise ValueError(
                    "Two inputs map to the same output path "
                    f"{relative_output}: {previous} and {input_path}"
                )
            seen_outputs[relative_output] = input_path
            discovered.append((input_path, relative_output))

    if not discovered:
        raise ValueError("No JSONL inputs found")
    return discovered


def load_redactor(*, checkpoint: str | None, device: str) -> Callable[[str], str]:
    """Load the official OPF runtime and return its text redaction method."""
    try:
        from opf import OPF
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI Privacy Filter is not installed. Run "
            "`python3 -m pip install -r requirements-privacy-filter.txt`."
        ) from exc

    redactor = OPF(
        model=checkpoint,
        device=device,
        output_mode="typed",
        output_text_only=True,
    )
    return redactor.redact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Redact all string values in extracted JSONL with the official "
            f"{MODEL_ID} model."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="JSONL files or directories containing JSONL files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("filtered_data"),
        help="Destination directory (default: filtered_data)",
    )
    parser.add_argument(
        "--device",
        choices=("cpu", "cuda"),
        default="cpu",
        help="Inference device (default: cpu)",
    )
    parser.add_argument(
        "--checkpoint",
        help=(
            "Optional local OPF checkpoint directory. Without this option, the "
            f"runtime downloads {MODEL_ID} from Hugging Face."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing output files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        inputs = discover_inputs(args.inputs)
        redact = load_redactor(checkpoint=args.checkpoint, device=args.device)

        totals = FilterStats()
        for input_path, relative_output in inputs:
            output_path = args.output_dir / relative_output
            stats = filter_jsonl(
                input_path,
                output_path,
                redact,
                overwrite=args.overwrite,
            )
            totals.records += stats.records
            totals.strings += stats.strings
            totals.changed_strings += stats.changed_strings
            print(
                f"Filtered {stats.records:,} records from {input_path} -> {output_path} "
                f"({stats.changed_strings:,}/{stats.strings:,} strings changed)"
            )

        print(
            f"Complete: {totals.records:,} records; "
            f"{totals.changed_strings:,}/{totals.strings:,} strings changed"
        )
        return 0
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
