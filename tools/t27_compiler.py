"""Shared pinned-compiler and lexer checks for standalone RTL build tools.

Build infrastructure only. Rebuilds the host compiler from its verified checkout;
no generated decoder, container, storage or host runtime algorithm lives here.
"""
from __future__ import annotations
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def resolve_compiler(explicit: str | None) -> Path:
    value = explicit or os.environ.get('T27C') or os.environ.get('TRINITY_T27C')
    if not value and os.environ.get('T27_ROOT'):
        value = str(Path(os.environ['T27_ROOT']) / 'target/release/t27c')
    value = value or shutil.which('t27c')
    if not value:
        raise RuntimeError('Set --compiler or T27_ROOT to the checkout pinned in native/compiler.lock')
    return Path(value).expanduser().resolve()


def verify_compiler(compiler: Path, project_root: Path = ROOT) -> Path:
    """Match the main native build: exact pin, clean compiler inputs, rebuild."""
    compiler = compiler.resolve()
    if len(compiler.parents) < 3:
        raise RuntimeError('Compiler must be <pinned-checkout>/target/release/t27c')
    checkout = compiler.parents[2]
    if compiler != checkout / 'target/release/t27c':
        raise RuntimeError('Compiler must be <pinned-checkout>/target/release/t27c')
    if os.environ.get('CARGO_BUILD_TARGET'):
        raise RuntimeError('Unset CARGO_BUILD_TARGET: generation needs a host compiler')
    pin = (project_root / 'native/compiler.lock').read_text().strip()
    result = subprocess.run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'],
                            text=True, capture_output=True, timeout=30)
    if result.returncode or result.stdout.strip() != pin:
        raise RuntimeError(f'Compiler revision mismatch; expected {pin} at {checkout}')
    result = subprocess.run(['git', '-C', str(checkout), 'diff', '--quiet', 'HEAD', '--',
                             'bootstrap', 'Cargo.toml', 'Cargo.lock'],
                            text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Compiler checkout has modified tracked build inputs')
    result = subprocess.run(['cargo', '+1.94.0', 'build', '--locked', '--release',
                             '-p', 't27c', '--bin', 't27c', '--target-dir', 'target'],
                            cwd=checkout, text=True, capture_output=True, timeout=900)
    if result.returncode:
        raise RuntimeError(f'Pinned compiler rebuild failed:\n{result.stdout}\n{result.stderr}')
    if not compiler.is_file() or not os.access(compiler, os.X_OK):
        raise RuntimeError(f'Rebuilt compiler is not executable: {compiler}')
    return compiler


def check_lexer(compiler: Path, specs: Path) -> str:
    """lex-dropped can exit zero despite dropped characters: inspect its total."""
    result = subprocess.run([str(compiler), 'lex-dropped', '--specs-dir', str(specs)],
                            text=True, capture_output=True, timeout=180)
    if result.returncode or not re.search(r'^\s*0\s+TOTAL across 0 spec\(s\)\s*$',
                                         result.stdout, re.MULTILINE):
        raise RuntimeError(f'Lexer discarded source characters:\n{result.stdout}\n{result.stderr}')
    return result.stdout
