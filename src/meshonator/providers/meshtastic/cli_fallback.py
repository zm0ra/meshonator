from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class CliResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


class MeshtasticCliFallback:
    def __init__(self, timeout_s: float = 10.0) -> None:
        self.timeout_s = timeout_s

    def run(self, args: list[str]) -> CliResult:
        cmd = ["meshtastic", *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s, check=False)
        return CliResult(command=cmd, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    def run_json(self, args: list[str]) -> dict[str, Any]:
        result = self.run(args)
        if result.returncode != 0:
            raise RuntimeError(
                f"CLI command failed: {' '.join(result.command)} rc={result.returncode} stderr={result.stderr}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("CLI did not return JSON payload") from exc
