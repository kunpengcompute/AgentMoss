"""版本号一致性看护：pyproject.toml 与 agent_moss/__version__.py。

2026-11 事故：发版只改了 ``pyproject.toml``，漏改 ``agent_moss/__version__.py``，
安装脚本据此反复「升级」却永远升不上去。裁剪仓没有 TS，保留 Python 两处一致性。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "pyproject.toml 中找不到 version"
    return m.group(1)


def _module_version() -> str:
    text = (ROOT / "agent_moss" / "__version__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    assert m, "agent_moss/__version__.py 中找不到 __version__"
    return m.group(1)


def test_module_version_matches_pyproject():
    """模块内 __version__ 必须与打包版本一致。"""
    assert _module_version() == _pyproject_version()
