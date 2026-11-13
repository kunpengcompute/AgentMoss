"""Opt-in check of the delivered Moss wheel's DeepAgents integration."""

import os
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

import pytest


@pytest.mark.skipif(not os.getenv("AGENT_MOSS_UNIFIED_WHEEL"), reason="unified wheel not supplied")
def test_moss_wheel_contains_aio_plugin_and_optional_dependencies():
    with ZipFile(Path(os.environ["AGENT_MOSS_UNIFIED_WHEEL"])) as wheel:
        names = set(wheel.namelist())
        assert "agent_moss/engine/coordinator.py" in names
        assert "agentmoss_deepagents/integration.py" in names
        assert "agentmoss_deepagents/aio_backend.py" in names
        assert not any("/tests/" in name for name in names)
        metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(wheel.read(metadata_path))
    assert metadata["Name"] == "agent-moss"
    assert "deepagents-aio" in metadata.get_all("Provides-Extra", [])
    requirements = metadata.get_all("Requires-Dist", [])
    assert any("deepagents==0.7.32" in value and 'extra == "deepagents-aio"' in value for value in requirements)
    assert any("agent-sandbox==0.0.30" in value and 'extra == "deepagents-aio"' in value for value in requirements)


@pytest.mark.skipif(not os.getenv("AGENT_MOSS_UNIFIED_WHEEL"), reason="unified wheel not supplied")
def test_moss_wheel_includes_plugin_third_party_licenses():
    with ZipFile(Path(os.environ["AGENT_MOSS_UNIFIED_WHEEL"])) as wheel:
        for filename in ("agent-infra-sandbox-Apache-2.0.txt", "THIRD_PARTY_NOTICES.md"):
            matches = [name for name in wheel.namelist() if ".dist-info/licenses/" in name and name.endswith(filename)]
            assert len(matches) == 1, f"Missing third-party license: {filename}"
            assert wheel.read(matches[0]).strip()
