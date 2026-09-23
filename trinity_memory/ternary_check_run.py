"""`trinity-memory ternary-check`: run the conformance vectors against a decoder.

    trinity-memory ternary-check run --decoder CMD [--vectors FILE...] [--formats LIST]
                                     [--report PATH] [--summary PATH]
                                     [--fail-on mismatch|silent|never] [--timeout S] [--jobs N]
    trinity-memory ternary-check formats | decode ... | encode ...   (the reference decoder)

The runner is I/O glue: it turns every vector of conformance/formats_*.json
into one call of the CLI contract (ternary-check/CONTRACT.md), runs the
decoder in a fresh directory, and hands the outputs to the t27 comparison and
verdict functions (trinity_memory.ternary_contract). The JSON report
(trinity.ternary-check-run.v1) is deterministic except for its "run" block;
the Markdown summary renders the same numbers.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time

from . import ternary_contract as tc

SCHEMA = "trinity.ternary-check-run.v1"
CONTRACT = "trinity.ternary-check-cli.v1"
ROOT = Path(__file__).resolve().parent.parent
DECODE_KINDS = {"block": None, "i2s": "I2_S", "hf_packed": "HF_PACKED", "mlx": "MLX2", "onnx": "ONNX2"}
ENCODE_KINDS = {"block_encode": None, "i2s_encode": "I2_S", "hf_encode": "HF_PACKED", "mlx_encode": "MLX2",
                "onnx_encode": "ONNX2"}
CONTAINER_KINDS = ("gguf", "safetensors")
STDERR_LINES = 20


class RunError(Exception):
    """A usage or I/O problem of the run itself (exit status 2)."""


def default_vectors() -> list[Path]:
    return sorted((ROOT / "conformance").glob("formats_*.json"))


def _values(hex_text: str) -> bytes:
    return bytes.fromhex(hex_text)


def _pack(words, width: int) -> bytes:
    return struct.pack(f"<{len(words)}{'H' if width == 2 else 'I'}", *words)


def _vector_class(v) -> str:
    for key, label in (("error_class", "reject"), ("flag_class", "flag"), ("silent_class", "silent")):
        if key in v:
            return f"{label}:{v[key]}"
    return "positive"


def _geometry_options(v, fmt: str) -> list[str]:
    if fmt in ("HF_PACKED", "MLX2"):
        options = [f"rows={v['rows']}", f"cols={v['cols']}"]
        if fmt == "MLX2":
            options.append(f"group={v['group']}")
        return options
    if fmt == "ONNX2":
        return [f"n={v['n']}", f"k={v['k']}", f"block_size={v['block_size']}"]
    return []


def _count(v, fmt: str) -> int:
    if fmt in ("HF_PACKED", "MLX2"):
        return v["rows"] * v["cols"]
    if fmt == "ONNX2":
        return v["n"] * v["k"]
    return v["count"]


def _side_scales(v, fmt: str):
    """Scale words of a separate tensor and their width, or (None, 0)."""
    if fmt == "HF_PACKED":
        words = [v["scale_word"]]
    elif fmt in ("MLX2", "ONNX2"):
        words = v["scale_words"]
    else:
        return None, 0
    return words, 4 if v["scale_kind"] == "F32" else 2


def cases_of(document, file_name: str, errors: dict, flag_slots: dict):
    """(cases, not_run) of one formats_*.json document, in vector order."""
    cases, not_run = [], []
    for v in document["vectors"]:
        kind = v["reader"] if v["kind"] == "reject" else v["kind"]
        label = _vector_class(v)
        expected_status = errors[v["error_class"]] if "error_class" in v else 0
        base = {"file": file_name, "id": v["id"], "class": label}
        if kind in CONTAINER_KINDS:
            not_run.append({**base, "reader": kind, "reason": "container"})
            continue
        if kind in DECODE_KINDS:
            fmt = DECODE_KINDS[kind] or v["format"]
            files = {"input": _values(v["data_hex"])}
            options = _geometry_options(v, fmt)
            side, width = _side_scales(v, fmt)
            if side is not None:
                files["side-scales"] = _pack(side, width)
                options += [f"scale_kind={v['scale_kind']}", "scales={side-scales}"]
                if fmt == "MLX2":
                    files["side-biases"] = _pack(v["bias_words"], width)
                    options.append("biases={side-biases}")
            if fmt == "ONNX2" and v.get("zero_points_hex"):
                files["zero-points"] = _values(v["zero_points_hex"])
                options.append("zero_points={zero-points}")
            case = {**base, "op": "decode", "format": fmt, "count": _count(v, fmt), "expected": expected_status,
                    "files": files, "options": options}
            if expected_status == 0:
                e = v["expect"]
                if side is not None:
                    words, scale_width = side, width
                elif fmt == "I2_S":
                    words, scale_width = [e["scale_word"]], 4
                else:
                    words, scale_width = e["scale_words"], 2
                flags = [0] * tc.FLAG_COUNT
                for token, count in e["flags"].items():
                    flags[flag_slots[token]] = count
                case.update(values=_values(e["values_hex"]), scales=words, scale_width=scale_width, flags=flags)
            cases.append(case)
            if expected_status == 0 and v.get("encode"):
                cases.append(_encode_case(base, v, fmt, case["values"], case["scales"]))
            continue
        if kind in ENCODE_KINDS:
            fmt = ENCODE_KINDS[kind] or v["format"]
            words = v.get("scale_words", [v["scale_word"]] if "scale_word" in v else [])
            case = _encode_case(base, v, fmt, _values(v["values_hex"]), words)
            case["expected"] = expected_status
            cases.append(case)
            continue
        not_run.append({**base, "reader": kind, "reason": "unknown_reader"})
    return cases, not_run


def _encode_case(base, v, fmt, values: bytes, words):
    files = {"values": values}
    if fmt in tc.SIDE_SCALES:
        files["scales"] = b""
    else:
        files["scales"] = _pack(words, 4 if fmt == "I2_S" else 2)
    options = _geometry_options(v, fmt)
    if fmt == "ONNX2" and v.get("zero_points_hex"):
        files["zero-points"] = _values(v["zero_points_hex"])
        options.append("zero_points={zero-points}")
    case = {**base, "op": "encode", "format": fmt, "count": len(values), "expected": 0, "files": files,
            "options": options}
    if "data_hex" in v:
        case["output"] = _values(v["data_hex"])
    return case


# ---- running the decoder -----------------------------------------------------
def decoder_command(text: str) -> list[str]:
    """The decoder command; words naming existing relative paths become absolute."""
    words = shlex.split(text)
    if not words:
        raise RunError("--decoder is empty")
    out = []
    for word in words:
        if not word.startswith("-") and os.sep in word and not os.path.isabs(word) and os.path.exists(word):
            word = os.path.abspath(word)
        out.append(word)
    return out


def _run(command, directory: Path, timeout: float):
    """(ended, exit code, stderr text)."""
    try:
        process = subprocess.run(command, cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return True, 0, f"stopped after {timeout} s\n" + (error.stderr or b"").decode("utf-8", "replace")
    except OSError as error:
        raise RunError(f"cannot run the decoder {command[0]!r}: {error}") from error
    stderr = process.stderr.decode("utf-8", "replace")
    if process.returncode < 0:
        return True, process.returncode, stderr
    return False, process.returncode, stderr


def listing(command, work: Path, timeout: float):
    """Formats the decoder declares through `formats`; None when it declares none."""
    directory = Path(tempfile.mkdtemp(prefix="formats-", dir=work))
    try:
        process = subprocess.run([*command, "formats"], cwd=directory, stdin=subprocess.DEVNULL,
                                 capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if process.returncode != 0:
        return None
    declared = {"decode": set(), "encode": set()}
    for line in process.stdout.decode("utf-8", "replace").splitlines():
        words = line.split()
        if len(words) == 2 and words[0] in declared and words[1] in tc.FORMATS:
            declared[words[0]].add(words[1])
    if not declared["decode"] and not declared["encode"]:
        return None
    return declared


def execute(case, command, work: Path, timeout: float):
    """Runs one call and returns the case's record, compared and judged in t27."""
    directory = Path(tempfile.mkdtemp(prefix="case-", dir=work))
    paths = {}
    for name, data in case["files"].items():
        path = directory / f"{name}.bin"
        path.write_bytes(data)
        paths[name] = str(path)
    options = [option.format(**paths) for option in case["options"]]
    if case["op"] == "decode":
        paths["values"], paths["scales"] = str(directory / "values.out"), str(directory / "scales.out")
        call = ["decode", case["format"], str(case["count"]), paths["input"], paths["values"], paths["scales"]]
    else:
        paths["output"] = str(directory / "output.out")
        call = ["encode", case["format"], str(case["count"]), paths["values"], paths["scales"], paths["output"]]
    ended, exit_code, stderr = _run([*command, *call, *options], directory, timeout)
    error_file = directory / "error"
    error_text = error_file.read_bytes() if error_file.is_file() else None
    error = tc.error_status(error_text) if error_text is not None else 0
    value_diff = scale_diff = 0
    state = tc.FLAGS_EQUAL
    record = {key: case[key] for key in ("file", "id", "class", "op", "format")}
    record["expect"] = "reject:" + tc.error_token(case["expected"]) if case["expected"] else case["op"]
    if not ended and exit_code == 0 and case["expected"] == 0:
        if case["op"] == "decode":
            values = _read(paths["values"])
            value_diff, first = tc.compare_bytes(values, case["values"])
            record["values"] = _difference(value_diff, first, values, case["values"], signed=True)
            scales = _read(paths["scales"])
            scale_diff, first = tc.compare_words(scales, case["scale_width"], case["scales"])
            record["scales"] = _word_difference(scale_diff, first, scales, case["scale_width"], case["scales"])
            flags_file = directory / "flags"
            reported = flags_file.is_file()
            parsed, flags = tc.parse_flags(flags_file.read_bytes()) if reported else (0, [0] * tc.FLAG_COUNT)
            state = tc.flag_state(reported, parsed, flags, case["flags"])
            record["flags"] = ("equal", "unreported", "differ")[state]
            if state == tc.FLAGS_DIFFER:
                record["flags_expected"] = _flag_map(case["flags"])
                record["flags_reported"] = _flag_map(flags) if parsed == 0 else "malformed"
        else:
            output = _read(paths["output"])
            value_diff, first = tc.compare_bytes(output, case["output"])
            record["output"] = _difference(value_diff, first, output, case["output"], signed=False)
    outcome = tc.verdict(case["expected"], ended, exit_code, error, value_diff, scale_diff, state)
    record["outcome"] = tc.outcome_token(outcome)
    record["exit"] = None if ended else exit_code
    if error_text is not None and exit_code != 0:
        record["error"] = tc.error_token(error) if error else "unrecognized"
    record["fails"] = None  # set by the caller from the policy
    shutil.rmtree(directory, ignore_errors=True)
    return record, outcome, stderr


def _read(path) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def _difference(total: int, first: int, actual: bytes, expected: bytes, signed: bool):
    result = {"differ": total}
    if total:
        def at(data):
            if first >= len(data):
                return None
            byte = data[first]
            return byte - 256 if signed and byte > 127 else byte
        result.update(first=first, expected=at(expected), actual=at(actual), actual_bytes=len(actual))
    return result


def _word_difference(total: int, first: int, actual: bytes, width: int, expected):
    result = {"differ": total}
    if total:
        whole = len(actual) // width
        word = int.from_bytes(actual[first * width:(first + 1) * width], "little") if first < whole else None
        result.update(first=first, expected=expected[first] if first < len(expected) else None, actual=word,
                      actual_words=whole)
    return result


def _flag_map(flags):
    return {token: flags[slot] for slot, token in enumerate(tc.flag_tokens()) if flags[slot]}


# ---- the run -------------------------------------------------------------------
def load_vectors(paths):
    documents = []
    for path in paths:
        path = Path(path)
        if path.is_dir():
            documents += load_vectors(sorted(path.glob("formats_*.json")))
            continue
        try:
            data = path.read_bytes()
            document = json.loads(data)
        except (OSError, ValueError) as error:
            raise RunError(f"cannot read vectors {path}: {error}") from error
        if "vectors" not in document or "constants" not in document or "errors" not in document["constants"]:
            raise RunError(f"{path} is not a formats_*.json conformance file")
        documents.append((path, hashlib.sha256(data).hexdigest(), document))
    return documents


def run(decoder: str, vectors=None, formats=None, fail_on="mismatch", timeout=60.0, jobs=None):
    """Returns (report, summary Markdown)."""
    if fail_on not in tc.FAIL_ON:
        raise RunError(f"--fail-on must be one of {', '.join(tc.FAIL_ON)}")
    policy = tc.FAIL_ON[fail_on]
    if formats:
        unknown = sorted(set(formats) - set(tc.FORMATS))
        if unknown:
            raise RunError(f"unknown formats {', '.join(unknown)}; known: {', '.join(tc.FORMATS)}")
    documents = load_vectors(vectors or default_vectors())
    if not documents:
        raise RunError("no vector files: pass --vectors (an installed package does not ship conformance/)")
    names = [path.name for path, _, _ in documents]
    if len(set(names)) != len(names):
        raise RunError("two vector files share a name")
    command = decoder_command(decoder)
    started, clock = datetime.now(timezone.utc), time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="ternary-check-"))
    try:
        declared = listing(command, work, timeout)
        supported = declared or {"decode": set(tc.FORMATS), "encode": set()}
        cases, not_run, sources = [], [], []
        for path, digest, document in documents:
            constants = document["constants"]
            file_cases, file_not_run = cases_of(document, path.name, constants["errors"], constants["flags"])
            sources.append({"file": path.name, "sha256": digest, "spec_path": document.get("spec_path"),
                            "vectors": len(document["vectors"])})
            not_run += file_not_run
            for case in file_cases:
                reason = None
                if formats and case["format"] not in formats:
                    reason = "filtered"
                elif case["format"] not in supported[case["op"]]:
                    reason = "unsupported"
                if reason:
                    not_run.append({key: case[key] for key in ("file", "id", "class", "op", "format")}
                                   | {"reason": reason})
                else:
                    cases.append(case)
        workers = max(1, jobs or min(8, os.cpu_count() or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda case: execute(case, command, work, timeout), cases))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    records, stderr_tails = [], {}
    counts = {tc.outcome_token(o): 0 for o in range(tc.OUTCOME_COUNT)}
    by_format = {}
    failures = 0
    for record, outcome, stderr in results:
        record["fails"] = tc.fails(outcome, policy)
        failures += record["fails"]
        counts[record["outcome"]] += 1
        row = by_format.setdefault(record["format"], {tc.outcome_token(o): 0 for o in range(tc.OUTCOME_COUNT)})
        row[record["outcome"]] += 1
        records.append(record)
        if record["fails"] and stderr.strip():
            stderr_tails[f"{record['file']}#{record['id']}:{record['op']}"] = stderr.strip().splitlines()[-STDERR_LINES:]
    reasons = {}
    for entry in not_run:
        reasons[entry["reason"]] = reasons.get(entry["reason"], 0) + 1
    report = {
        "schema": SCHEMA,
        "contract": CONTRACT,
        "fail_on": fail_on,
        "formats_filter": sorted(formats) if formats else None,
        "decoder_formats": {"source": "listing" if declared else "assumed",
                            "decode": sorted(supported["decode"], key=tc.FORMATS.index),
                            "encode": sorted(supported["encode"], key=tc.FORMATS.index)},
        "vectors": sources,
        "summary": {"cases": len(records), "failures": failures, "passed": failures == 0,
                    "outcomes": counts, "by_format": {name: by_format[name] for name in tc.FORMATS if name in by_format},
                    "not_run": dict(sorted(reasons.items()))},
        "cases": records,
        "not_run": not_run,
        "run": {
            "note": "Run metadata: excluded from the reproducible content; compare reports without this block.",
            "decoder": decoder,
            "started_utc": started.isoformat(timespec="seconds"),
            "seconds": round(time.monotonic() - clock, 2),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "jobs": workers,
            "timeout_seconds": timeout,
            "stderr_of_failures": stderr_tails,
        },
    }
    return report, summary_markdown(report)


def reproducible(report):
    """The report without its run block: equal for equal decoders and vectors."""
    return {key: value for key, value in report.items() if key != "run"}


def summary_markdown(report) -> str:
    s = report["summary"]
    verdict = "PASS" if s["passed"] else "FAIL"
    lines = [f"## Ternary Check: {verdict}", "",
             f"{s['cases']} calls of the CLI contract (`{report['contract']}`) from "
             f"{sum(v['vectors'] for v in report['vectors'])} vectors in {len(report['vectors'])} files; "
             f"{s['failures']} fail under `--fail-on {report['fail_on']}`. "
             f"Decoder formats: {report['decoder_formats']['source']}.", ""]
    shown = [name for name in s["outcomes"] if s["outcomes"][name]]
    if shown:
        lines += ["| format | " + " | ".join(shown) + " |", "|---|" + "---|" * len(shown)]
        for name, row in s["by_format"].items():
            lines.append(f"| {name} | " + " | ".join(str(row[o]) for o in shown) + " |")
        lines.append("| **all** | " + " | ".join(f"**{s['outcomes'][o]}**" for o in shown) + " |")
        lines.append("")
    if s["not_run"]:
        note = " (container: GGUF and safetensors header vectors are outside contract v1)" \
            if "container" in s["not_run"] else ""
        lines.append("Not run: " + ", ".join(f"{count} {reason}" for reason, count in s["not_run"].items())
                     + note + ".")
        lines.append("")
    failing = [c for c in report["cases"] if c["fails"]]
    if failing:
        lines += ["### Failing calls", "", "| file | vector | op | format | expected | outcome | detail |",
                  "|---|---|---|---|---|---|---|"]
        for c in failing[:40]:
            lines.append(f"| {c['file']} | `{c['id']}` | {c['op']} | {c['format']} | {c['expect']} | "
                         f"{c['outcome']} | {_detail(c)} |")
        if len(failing) > 40:
            lines.append(f"| ... | {len(failing) - 40} more in the JSON report | | | | | |")
        lines.append("")
    return "\n".join(lines) + "\n"


def _detail(case) -> str:
    parts = []
    for key in ("values", "scales", "output"):
        d = case.get(key)
        if d and d["differ"]:
            parts.append(f"{key}: {d['differ']} differ, first at {d['first']} "
                         f"(expected {d['expected']}, got {d['actual']})")
    if case.get("flags") == "differ":
        parts.append(f"flags: expected {case.get('flags_expected')}, got {case.get('flags_reported')}")
    if "error" in case:
        parts.append(f"error file: {case['error']}")
    if case["exit"] is None:
        parts.append("stopped by a signal or the time limit")
    elif case["exit"] and "error" not in case:
        parts.append(f"exit {case['exit']} without an error file")
    return "; ".join(parts)


def _main_run(argv) -> int:
    parser = argparse.ArgumentParser(prog="trinity-memory ternary-check run",
                                     description="Run conformance/formats_*.json against a decoder "
                                                 "that implements ternary-check/CONTRACT.md.")
    parser.add_argument("--decoder", required=True, help="decoder command; the contract arguments are appended")
    parser.add_argument("--vectors", nargs="+", help="formats_*.json files or directories (default: this "
                                                     "checkout's conformance/formats_*.json)")
    parser.add_argument("--formats", help="comma- or space-separated contract format names to run")
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    parser.add_argument("--summary", type=Path, help="append the Markdown summary here")
    parser.add_argument("--fail-on", default="mismatch", choices=sorted(tc.FAIL_ON))
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per decoder call")
    parser.add_argument("--jobs", type=int, help="parallel decoder calls (default: CPUs, at most 8)")
    args = parser.parse_args(argv)
    formats = [name for name in args.formats.replace(",", " ").split()] if args.formats else None
    try:
        report, summary = run(args.decoder, args.vectors, formats, args.fail_on, args.timeout, args.jobs)
    except RunError as error:
        print(f"ternary-check: {error}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=1, ensure_ascii=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(summary)
    sys.stdout.write(summary)
    return 0 if report["summary"]["passed"] else 1


def command(argv) -> int:
    """`trinity-memory ternary-check SUBCOMMAND ...`."""
    if argv[:1] == ["run"]:
        return _main_run(argv[1:])
    if argv[:1] in (["formats"], ["decode"], ["encode"]):
        return tc.command(argv)
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2
