"""System information collector: OS, kernel, CPU, memory, disk."""

import os
import platform
from typing import Optional

import psutil


class SystemInfoCollector:
    """Collects OS-level information."""

    @staticmethod
    def collect() -> dict:
        try:
            return {
                "os": {
                    "system": platform.system().lower(),
                    "distribution": _get_distribution(),
                    "kernel": platform.release(),
                    "hostname": platform.node(),
                    "machine": platform.machine(),
                },
                "resources": {
                    "cpu_count": os.cpu_count() or 0,
                    "cpu_percent": round(psutil.cpu_percent(interval=0.1), 1),
                    "memory_total_gb": round(psutil.virtual_memory().total / (1024 ** 3), 1),
                    "memory_percent": psutil.virtual_memory().percent,
                    "disk_total_gb": round(
                        psutil.disk_usage("/").total / (1024 ** 3), 1
                    ),
                    "disk_percent": psutil.disk_usage("/").percent,
                },
            }
        except Exception:
            return {"os": {}, "resources": {}}


def _get_distribution() -> str:
    """Detect Linux distribution name."""
    try:
        if os.path.exists("/etc/os-release"):
            with open("/etc/os-release") as f:
                info = {}
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        info[k] = v.strip('"')
                name = info.get("NAME", "")
                version = info.get("VERSION_ID", "")
                return f"{name} {version}".strip()
    except Exception:
        pass
    return platform.platform()
