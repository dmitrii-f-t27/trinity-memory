"""OS-only launcher for the native t27 command dispatcher, and the ternary-check runner."""
from pathlib import Path
import os
import subprocess
import sys
from . import _native as n

def _read_json(path): return n.strict_json(Path(path).read_bytes())

def main(argv=None):
    arguments=sys.argv[1:] if argv is None else [str(a) for a in argv]
    if arguments[:1]==["ternary-check"]:
        # Python glue around the t27 contract functions (ternary-check/CONTRACT.md).
        from .ternary_check_run import command
        return command(arguments[1:])
    executable=n.executable()
    if argv is None:
        os.execv(str(executable),[str(executable),*sys.argv[1:]])
        return 2
    process=subprocess.run([str(executable),*map(str,argv)],capture_output=True)
    sys.stdout.write(process.stdout.decode("utf-8",errors="replace"))
    sys.stderr.write(process.stderr.decode("utf-8",errors="replace"))
    return process.returncode

if __name__=="__main__": main()
