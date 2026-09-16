from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import signal
import subprocess
import threading
import time

__all__ = ["CollectionError", "Collector", "ssh_command"]

DISALLOWED_SHELL_CHARS = set(";`&|*?~<>^()${}\"'\\")


class CollectionError(Exception):
    """Raised when sensor data collection fails."""
    pass


def _validate_host(host: str) -> None:
    if not isinstance(host, str) or not host.strip():
        raise ValueError("host must be a non-empty string")
    if host.startswith("-"):
        raise ValueError("host cannot start with '-'")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in host):
        raise ValueError("host cannot contain whitespace or control characters")
    if any(c in DISALLOWED_SHELL_CHARS for c in host):
        raise ValueError("host contains disallowed shell characters")

    if "@" in host:
        parts = host.split("@")
        if len(parts) != 2:
            raise ValueError("host cannot contain multiple '@' symbols")
        user, target = parts
        if not user or not target:
            raise ValueError("username and host cannot be empty")
        if user.startswith("-"):
            raise ValueError("user cannot start with '-'")
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", user):
            raise ValueError(f"Invalid characters in user: {user}")
    else:
        target = host

    if target.startswith("-"):
        raise ValueError("target host cannot start with '-'")

    if target.startswith("[") and target.endswith("]"):
        inner = target[1:-1]
    else:
        inner = target

    ip_part = inner.split("%")[0] if "%" in inner else inner
    try:
        ipaddress.ip_address(ip_part)
        return
    except ValueError:
        pass

    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", inner):
        raise ValueError(f"Invalid hostname or alias: {inner}")


def _validate_port(port: int) -> None:
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ValueError("port must be an integer between 1 and 65535")


def _validate_identity(identity: str) -> None:
    if not isinstance(identity, str) or not identity.strip() or identity.startswith("-") or "\0" in identity:
        raise ValueError("identity must be a valid file path and cannot start with '-'")


def ssh_command(host: str, port: int = 22, identity: str | None = None) -> list[str]:
    _validate_host(host)
    _validate_port(port)
    if identity is not None:
        _validate_identity(identity)

    args = [
        "ssh",
        "-T",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        "-p", str(port),
    ]
    if identity:
        args += ["-i", identity]
    args += [host, "mfg-tool sensor-display"]
    return args


def _close_pipes(proc: subprocess.Popen) -> None:
    for pipe in (proc.stdout, proc.stderr, proc.stdin):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _kill_process_group(proc: subprocess.Popen) -> None:
    # 1. Send SIGTERM to process group
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass

    # 2. Wait up to 0.2s for graceful exit
    try:
        proc.wait(timeout=0.2)
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        if proc.poll() is not None:
            _close_pipes(proc)

    # 3. Escalate to SIGKILL if child is still running or ignored SIGTERM
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass

    # 4. Final wait and reap
    try:
        proc.wait(timeout=0.5)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
            proc.wait(timeout=0.2)
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            pass
    finally:
        _close_pipes(proc)


class Collector:
    def __init__(
        self,
        *,
        host: str | None = None,
        file: str | None = None,
        demo: bool = False,
        local: bool = False,
        port: int = 22,
        identity: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        active_sources = sum([bool(host), bool(file), bool(demo), bool(local)])
        if active_sources != 1:
            raise ValueError("Exactly one source (host, file, demo, local) must be specified")

        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")

        _validate_port(port)

        if host is not None:
            _validate_host(host)
            if identity is not None:
                _validate_identity(identity)

        if file is not None:
            if not isinstance(file, str) or not file.strip():
                raise ValueError("file must be a non-empty string path")

        self.host = host
        self.file = file
        self.demo = demo
        self.local = local
        self.port = port
        self.identity = identity
        self.timeout = float(timeout)
        self._demo_cycle = 0

    def collect(self, stop: threading.Event) -> str:
        if self.demo:
            return self._collect_demo(stop)
        elif self.file is not None:
            return self._collect_file(stop)
        else:
            return self._collect_subprocess(stop)

    def _collect_demo(self, stop: threading.Event) -> str:
        if stop.is_set():
            raise CollectionError("Collection cancelled by stop event")

        round_idx = self._demo_cycle % 5
        self._demo_cycle += 1

        anchor = {"status": "ok", "value": 4500.0}
        fanboard = {"status": "ok", "value": 5000.0}
        medusa = {"status": "ok", "value": 12.0}

        data: dict[str, dict] = {
            "SENTINEL_DOME_SLOT_1_FAN1": anchor,
            "FANBOARD0_FAN_SPEED": fanboard,
            "MEDUSA_MB1_VOLTAGE": medusa,
        }

        if round_idx == 0:
            data["SENTINEL_DOME_SLOT_1_CPU_TEMP"] = {"status": "ok", "value": 42.0}
        elif round_idx == 1:
            data["SENTINEL_DOME_SLOT_1_CPU_TEMP"] = {"status": "critical", "value": 98.5}
        elif round_idx == 2:
            data["SENTINEL_DOME_SLOT_1_CPU_TEMP"] = {"status": "unavailable", "value": None}
        elif round_idx == 3:
            pass  # Simulates missing sensor while anchor remains
        elif round_idx == 4:
            data["SENTINEL_DOME_SLOT_1_CPU_TEMP"] = {"status": "ok", "value": 43.0}

        return json.dumps(data)

    def _collect_file(self, stop: threading.Event) -> str:
        if stop.is_set():
            raise CollectionError("Collection cancelled by stop event")

        try:
            with open(self.file, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeError) as e:
            raise CollectionError(f"Failed to read file '{self.file}': {e}") from e

    def _collect_subprocess(self, stop: threading.Event) -> str:
        if stop.is_set():
            raise CollectionError("Collection cancelled by stop event")

        if self.host is not None:
            cmd = ssh_command(self.host, port=self.port, identity=self.identity)
        else:
            cmd = ["mfg-tool", "sensor-display"]

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            raise CollectionError(f"Executable not found: {cmd[0]}") from e
        except OSError as e:
            raise CollectionError(f"Failed to execute command {cmd[0]}: {e}") from e

        deadline = time.monotonic() + self.timeout
        cancelled = False
        timed_out = False
        stdout_data = ""
        stderr_data = ""

        while True:
            if stop.is_set():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break

            wait_slice = min(0.1, max(0.01, remaining))
            try:
                stdout_data, stderr_data = proc.communicate(timeout=wait_slice)
                break
            except subprocess.TimeoutExpired:
                continue
            except (UnicodeDecodeError, OSError) as e:
                _kill_process_group(proc)
                raise CollectionError(f"I/O or decode error while communicating with command: {e}") from e

        if cancelled or timed_out:
            _kill_process_group(proc)
            if cancelled:
                raise CollectionError("Collection cancelled by stop event")
            else:
                raise CollectionError(f"Collection timed out after {self.timeout}s")

        if proc.returncode != 0:
            stderr_tail = (stderr_data or "")[-2000:].strip()
            if stderr_tail:
                msg = f"Command failed with exit code {proc.returncode}: {stderr_tail}"
            else:
                msg = f"Command failed with exit code {proc.returncode}"
            raise CollectionError(msg)

        return stdout_data
