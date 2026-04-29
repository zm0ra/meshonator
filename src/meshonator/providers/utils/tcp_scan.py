from __future__ import annotations

import ipaddress
import math
import platform
import re
import socket
import subprocess
import time
from collections.abc import Iterable
from shutil import which


def expand_targets(single_hosts: Iterable[str], cidrs: Iterable[str]) -> list[str]:
    out: set[str] = set(single_hosts)
    for cidr in cidrs:
        net = ipaddress.ip_network(cidr, strict=False)
        for ip in net.hosts():
            out.add(str(ip))
    return sorted(out)


def tcp_is_open(host: str, port: int, timeout: float = 1.5) -> bool:
    return tcp_probe(host, port, timeout=timeout)["is_open"]


def ping_is_online(host: str, timeout: float = 1.5) -> bool:
    return ping_probe(host, timeout=timeout)["is_online"]


def ping_probe(host: str, timeout: float = 1.5) -> dict:
    started = time.monotonic()
    ping_path = which("ping")
    if ping_path is None:
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        return {
            "is_online": False,
            "reason": "ping_not_installed",
            "error": "ping command not found",
            "latency_ms": latency_ms,
            "timeout_s": timeout,
            "method": "icmp",
        }

    system = platform.system().lower()
    if system == "darwin":
        wait_arg = str(max(1, int(math.ceil(timeout * 1000.0))))
    else:
        wait_arg = str(max(1, int(math.ceil(timeout))))

    proc = subprocess.run(
        [ping_path, "-c", "1", "-W", wait_arg, host],
        capture_output=True,
        text=True,
        check=False,
    )
    latency_ms = round((time.monotonic() - started) * 1000.0, 2)
    output = "\n".join(part for part in [proc.stdout.strip(), proc.stderr.strip()] if part).strip()
    match = re.search(r"time[=<]([0-9.]+)\s*ms", output)
    parsed_latency_ms = float(match.group(1)) if match else None
    is_online = proc.returncode == 0
    return {
        "is_online": is_online,
        "reason": "icmp_reply" if is_online else f"exit_{proc.returncode}",
        "error": None if is_online else (output or None),
        "latency_ms": parsed_latency_ms if parsed_latency_ms is not None else latency_ms,
        "timeout_s": timeout,
        "method": "icmp",
    }


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
