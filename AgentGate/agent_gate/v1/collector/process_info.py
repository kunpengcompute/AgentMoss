"""Process information collector: PID, parent process, capabilities, cgroup."""

import os


class ProcessInfoCollector:
    """Collects information about the current process."""

    @staticmethod
    def collect() -> dict:
        pid = os.getpid()
        ppid = os.getppid()
        name = ""
        capabilities: list[str] = []
        cgroup = ""

        try:
            with open(f"/proc/{pid}/comm") as f:
                name = f.read().strip()
        except Exception:
            pass

        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("CapEff:"):
                        caps_hex = line.split(":")[1].strip()
                        capabilities = _parse_capabilities(caps_hex)
                    elif line.startswith("Name:"):
                        if not name:
                            name = line.split(":")[1].strip()
        except Exception:
            pass

        try:
            with open(f"/proc/{pid}/cgroup") as f:
                cgroup = f.readline().strip()
        except Exception:
            pass

        return {
            "process": {
                "pid": pid,
                "ppid": ppid,
                "name": name,
                "capabilities": capabilities,
                "cgroup": cgroup,
            }
        }


def _parse_capabilities(caps_hex: str) -> list[str]:
    """Parse Linux capabilities from hex string."""
    CAP_NAMES = [
        "CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_DAC_READ_SEARCH", "CAP_FOWNER",
        "CAP_FSETID", "CAP_KILL", "CAP_SETGID", "CAP_SETUID",
        "CAP_SETPCAP", "CAP_LINUX_IMMUTABLE", "CAP_NET_BIND_SERVICE",
        "CAP_NET_BROADCAST", "CAP_NET_ADMIN", "CAP_NET_RAW",
        "CAP_IPC_LOCK", "CAP_IPC_OWNER", "CAP_SYS_MODULE",
        "CAP_SYS_RAWIO", "CAP_SYS_CHROOT", "CAP_SYS_PTRACE",
        "CAP_SYS_PACCT", "CAP_SYS_ADMIN", "CAP_SYS_BOOT",
        "CAP_SYS_NICE", "CAP_SYS_RESOURCE", "CAP_SYS_TIME",
        "CAP_SYS_TTY_CONFIG", "CAP_MKNOD", "CAP_LEASE",
        "CAP_AUDIT_WRITE", "CAP_AUDIT_CONTROL", "CAP_SETFCAP",
        "CAP_MAC_OVERRIDE", "CAP_MAC_ADMIN", "CAP_SYSLOG",
        "CAP_WAKE_ALARM", "CAP_BLOCK_SUSPEND", "CAP_AUDIT_READ",
        "CAP_PERFMON", "CAP_BPF", "CAP_CHECKPOINT_RESTORE",
    ]
    try:
        caps_int = int(caps_hex, 16)
        return [name for i, name in enumerate(CAP_NAMES) if caps_int & (1 << i)]
    except (ValueError, IndexError):
        return []
