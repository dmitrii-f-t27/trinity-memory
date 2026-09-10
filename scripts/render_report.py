#!/usr/bin/env python3
"""File/FFI adapter for the executable t27 benchmark report renderer."""
from __future__ import annotations
import argparse
import ctypes as C
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from trinity_memory import _native as N


def render_report(data: dict) -> str:
    source = N.json_bytes(data)
    # HTML includes the source data, escaped strings, template, and WASM asset.
    output = N.buffer(len(source) * 6 + 131072)
    size = N.call('tm_report_benchmark_html', C.c_int64,
                  [C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t],
                  source, len(source), output, len(output))
    if size < 0:
        raise ValueError(f'Invalid benchmark report or native renderer failure ({size})')
    return bytes(output[:size]).decode('utf-8')


def validate_report(data: object) -> dict:
    render_report(data)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', type=Path, default=PROJECT_ROOT / 'reports/benchmark.json')
    parser.add_argument('--output', type=Path, default=PROJECT_ROOT / 'reports/index.html')
    args = parser.parse_args()
    try:
        source = args.input.read_bytes()
        data = N.strict_json(source)
        html = render_report(data)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(html, encoding='utf-8')
    except (OSError, ValueError, TypeError, N.NativeLibraryError) as error:
        parser.exit(1, f'Cannot render benchmark: {error}\n')
    print(f'Rendered {args.output}')


if __name__ == '__main__':
    main()
