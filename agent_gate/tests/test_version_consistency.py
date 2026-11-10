"""版本号一致性看护：pyproject.toml 与 agent_gate/__init__.py。

2026-11 AgentMoss 侧事故的同款防线：发版漏改包内 ``__version__``，
安装脚本会反复「升级」却永远升不上去。裁剪仓没有 TS，保留 Python 两处一致性。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "pyproject.toml 中找不到 version"
    return m.group(1)


def test_python_package_version_matches_pyproject():
    import agent_gate

    assert agent_gate.__version__ == _pyproject_version()
