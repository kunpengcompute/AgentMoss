"""Compatibility entry point for the CubeSandbox deployment check.

Setup and environment variables: ../../cubesandbox/README.md.
"""

import runpy
from pathlib import Path

if __name__ == "__main__":
    script = Path(__file__).resolve().parents[2] / "cubesandbox" / "verify.py"
    runpy.run_path(str(script), run_name="__main__")
