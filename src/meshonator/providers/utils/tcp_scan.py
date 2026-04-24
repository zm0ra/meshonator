from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Iterable


def expand_targets(single_hosts: Iterable[str], cidrs: Iterable[str]) -> list[str]:
    out: set[str] = set(single_hosts)
    for cidr in cidrs:
        net = ipaddress.ip_network(cidr, strict=False)
        for ip in net.hosts():
            out.add(str(ip))
    return sorted(out)


def tcp_is_open(host: str, port: int, timeout: float = 1.5) -> bool:
    return tcp_probe(host, port, timeout=timeout)["is_open"]


def tcp_probe(host: str, port: int, timeout: float = 1.5) -> dict:
    started = time.monotonic()
    code = None
    error = None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            code = sock.connect_ex((host, port))
        except OSError as exc:
            error = str(exc)
            code = None
    latency_ms = round((time.monotonic() - started) * 1000.0, 2)
    is_open = code == 0
    reason = "open" if is_open else ("socket_error" if error else f"connect_ex_{code}")
    return {
        "is_open": is_open,
        "connect_ex_code": code,
        "reason": reason,
        "error": error,
        "latency_ms": latency_ms,
        "timeout_s": timeout,
    }
