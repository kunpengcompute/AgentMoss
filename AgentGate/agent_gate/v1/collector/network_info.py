"""Network information collector: interfaces, active connections, public IP detection."""

import os
import socket


class NetworkInfoCollector:
    """Collects network interface and connection summary."""

    @staticmethod
    def collect() -> dict:
        interfaces = _get_interfaces()
        active_connections = _count_active_connections()
        has_public_ip = _check_public_ip(interfaces)

        return {
            "network": {
                "interfaces": interfaces,
                "active_connections": active_connections,
                "has_public_ip": has_public_ip,
            }
        }


def _get_interfaces() -> list[str]:
    """Get list of non-loopback network interfaces."""
    ifaces = []
    try:
        for name in os.listdir("/sys/class/net/"):
            if name != "lo":
                ifaces.append(name)
    except Exception:
        pass
    return ifaces


def _count_active_connections() -> int:
    """Count active network connections."""
    count = 0
    try:
        # TCP connections
        with open("/proc/net/tcp") as f:
            lines = f.readlines()[1:]  # Skip header
            count = len(lines)
        with open("/proc/net/tcp6") as f:
            lines = f.readlines()[1:]
            count += len(lines)
    except Exception:
        pass
    return count


def _check_public_ip(interfaces: list[str]) -> bool:
    """Check if any interface has a public IP."""
    private_prefixes = ("10.", "172.16.", "172.17.", "172.18.",
                        "172.19.", "172.20.", "172.21.", "172.22.",
                        "172.23.", "172.24.", "172.25.", "172.26.",
                        "172.27.", "172.28.", "172.29.", "172.30.",
                        "172.31.", "192.168.", "127.")
    try:
        addrs = socket.getaddrinfo(socket.gethostname(), None)
        for addr in addrs:
            ip = addr[4][0]
            if not any(ip.startswith(p) for p in private_prefixes):
                return True
    except Exception:
        pass
    return False
