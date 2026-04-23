from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable


def expand_targets(single_hosts: Iterable[str], cidrs: Iterable[str]) -> list[str]:
    out: set[str] = set(single_hosts)
    for cidr in cidrs:
        net = ipaddress.ip_network(cidr, strict=False)
        for ip in net.hosts():
            out.add(str(ip))
    return sorted(out)


def tcp_is_open(host: str, port: int, timeout: float = 1.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0
