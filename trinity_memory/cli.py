"""OS-only launcher for the native t27 command dispatcher."""
from pathlib import Path
import os
import subprocess
import sys
from . import _native as n

def _read_json(path): return n.strict_json(Path(path).read_bytes())

def main(argv=None):
    executable=n.executable()
    if argv is None:
        os.execv(str(executable),[str(executable),*sys.argv[1:]])
        return 2
    process=subprocess.run([str(executable),*map(str,argv)],capture_output=True)
    sys.stdout.write(process.stdout.decode("utf-8",errors="replace"))
    sys.stderr.write(process.stderr.decode("utf-8",errors="replace"))
    return process.returncode

if __name__=="__main__": main()
