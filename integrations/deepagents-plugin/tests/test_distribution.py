"""Opt-in checks against the actual built wheel, including a no-AIO local import."""

import os
import subprocess
import sys
import textwrap
import zipfile
from email.parser import BytesParser

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

pytestmark = pytest.mark.skipif(not os.getenv("AGENTMOSS_PLUGIN_WHEEL"), reason="built wheel not configured")


def test_wheel_contains_version_optional_dependency_and_licenses():
    with zipfile.ZipFile(os.environ["AGENTMOSS_PLUGIN_WHEEL"]) as wheel:
        names = wheel.namelist()
        metadata = BytesParser().parsebytes(wheel.read(next(n for n in names if n.endswith(".dist-info/METADATA"))))
        assert metadata["Version"] == "0.6.0"
        python_versions = SpecifierSet(metadata["Requires-Python"])
        assert "3.11" in python_versions and "3.10" not in python_versions
        deepagents = next(
            Requirement(d) for d in metadata.get_all("Requires-Dist") if Requirement(d).name == "deepagents"
        )
        assert all(version in deepagents.specifier for version in ("0.7.32", "0.7.33", "0.7.37"))
        assert all(version not in deepagents.specifier for version in ("0.7.31", "0.8.0"))
        aio = [Requirement(d) for d in metadata.get_all("Requires-Dist") if Requirement(d).name == "agent-sandbox"]
        assert len(aio) == 1 and all(version in aio[0].specifier for version in ("0.0.30", "0.0.31"))
        assert aio[0].marker.evaluate({"extra": "aio"})
        assert not aio[0].marker.evaluate({"extra": ""})
        assert any(n.endswith("agent-infra-sandbox-Apache-2.0.txt") for n in names)
        assert any(n.endswith("THIRD_PARTY_NOTICES.md") for n in names)
        assert not any("/tests/" in n or "__pycache__" in n for n in names)


def test_installed_wheel_local_mode_without_optional_aio_sdk(tmp_path):
    with zipfile.ZipFile(os.environ["AGENTMOSS_PLUGIN_WHEEL"]) as wheel:
        wheel.extractall(tmp_path / "site")
    (tmp_path / "workspace").mkdir()
    script = textwrap.dedent("""
        import asyncio
        import importlib.abc
        import pathlib
        import sys
        site = pathlib.Path(sys.argv[1])
        sys.path.insert(0, str(site))
        class NoAIO(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'agent_sandbox' or fullname.startswith('agent_sandbox.'):
                    raise ModuleNotFoundError('optional SDK absent', name='agent_sandbox')
        sys.meta_path.insert(0, NoAIO())
        import agentmoss_deepagents
        from agentmoss_deepagents import DeepAgentsIntegration
        assert pathlib.Path(agentmoss_deepagents.__file__).is_relative_to(site)
        integration = DeepAgentsIntegration.load({
            'backend': 'local', 'gate': {'enabled': False},
            'local': {'root_dir': sys.argv[2]},
        })
        assert integration.backend.execute('printf local-wheel-ok').exit_code == 0
        asyncio.run(integration.aclose())
        try:
            from agentmoss_deepagents import AIOSandboxBackend
        except ImportError as error:
            assert "pip install -e '/path/to/AgentMoss[deepagents-aio]'" in str(error)
        else:
            raise AssertionError('AIO import unexpectedly succeeded')
        print('wheel local mode works without optional AIO SDK')
    """)
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(tmp_path / "site"), str(tmp_path / "workspace")],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
