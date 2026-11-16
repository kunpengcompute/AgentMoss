"""Install the current repository source using the AIO service interpreter."""

import argparse
import shutil
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default="/opt/python3.12/bin/python3")
    parser.add_argument("--source", type=Path, default=Path("/opt/agentmoss-source/AgentMoss"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    python = args.python
    target = (
        Path(
            subprocess.check_output(
                [
                    python,
                    "-c",
                    "import sysconfig; print(sysconfig.get_paths()['purelib'])",
                ],
                text=True,
            ).strip()
        )
        / "app/api/v1"
    )
    if not (target / "shell.py").is_file():
        raise SystemExit("The selected Python environment does not contain the expected AIO shell API")
    subprocess.run([python, str(root / "patch_shell.py"), str(target / "shell.py")], check=True)
    subprocess.run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
            str(args.source),
        ],
        check=True,
    )
    shutil.copy2(root / "moss_guard.py", target / "moss_guard.py")
    subprocess.run(
        [
            python,
            "-c",
            "from app.api.v1.moss_guard import PROFILE; print('AgentMoss startup check passed')",
        ],
        check=True,
    )
    subprocess.run([python, "-m", "compileall", "-q", str(target / "shell.py")], check=True)


if __name__ == "__main__":
    main()
