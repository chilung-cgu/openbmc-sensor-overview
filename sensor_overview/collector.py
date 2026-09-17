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

__all__ = [
    "CollectionError",
    "Collector",
    "DBusCollector",
    "ssh_command",
    "dbus_ssh_command",
    "DEFAULT_OPENBMC_PASSWORD",
]

DEFAULT_OPENBMC_PASSWORD = "0penBmc"

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



def _build_ssh_base(
    host: str,
    port: int = 22,
    identity: str | None = None,
    password: str | None = None,
) -> list[str]:
    _validate_host(host)
    _validate_port(port)
    if identity is not None:
        _validate_identity(identity)

    cmd: list[str] = []
    if password is not None:
        cmd += ["sshpass", "-p", password]

    cmd += ["ssh", "-T"]
    if password is None:
        cmd += ["-o", "BatchMode=yes"]
    else:
        cmd += [
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
        ]

    cmd += [
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        "-o", "ControlMaster=auto",
        "-o", "ControlPath=/tmp/.sensor_overview_ssh_%C",
        "-o", "ControlPersist=60s",
        "-p", str(port),
    ]
    if identity:
        cmd += ["-i", identity]
    return cmd


def dbus_ssh_command(
    host: str,
    remote_cmd: str,
    port: int = 22,
    identity: str | None = None,
    password: str | None = None,
) -> list[str]:
    if not isinstance(remote_cmd, str) or not remote_cmd.strip():
        raise ValueError("remote_cmd must be a non-empty string")
    args = _build_ssh_base(host, port=port, identity=identity, password=password)
    args += [host, remote_cmd]
    return args


def ssh_command(
    host: str,
    port: int = 22,
    identity: str | None = None,
    password: str | None = None,
) -> list[str]:
    args = _build_ssh_base(host, port=port, identity=identity, password=password)
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
        password: str | None = None,
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
        self.password = password
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

    def _run_subprocess_cmd(self, cmd: list[str], stop: threading.Event) -> tuple[int, str, str]:
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

        return (proc.returncode, stdout_data, stderr_data)

    def _collect_subprocess(self, stop: threading.Event) -> str:
        if stop.is_set():
            raise CollectionError("Collection cancelled by stop event")

        if self.host is not None:
            cmd = ssh_command(self.host, port=self.port, identity=self.identity, password=self.password)
        else:
            cmd = ["mfg-tool", "sensor-display"]

        rc, stdout_data, stderr_data = self._run_subprocess_cmd(cmd, stop)

        # If authentication failed (Permission denied) and no explicit password was given,
        # automatically attempt fallback with default OpenBMC password '0penBmc'.
        if rc != 0 and self.host is not None and self.password is None:
            if "Permission denied" in (stderr_data or ""):
                fallback_cmd = ssh_command(
                    self.host,
                    port=self.port,
                    identity=self.identity,
                    password=DEFAULT_OPENBMC_PASSWORD,
                )
                rc_fb, stdout_fb, stderr_fb = self._run_subprocess_cmd(fallback_cmd, stop)
                if rc_fb == 0:
                    self.password = DEFAULT_OPENBMC_PASSWORD
                    return stdout_fb

        if rc != 0:
            stderr_tail = (stderr_data or "")[-2000:].strip()
            if stderr_tail:
                msg = f"Command failed with exit code {rc}: {stderr_tail}"
            else:
                msg = f"Command failed with exit code {rc}"
            raise CollectionError(msg)

        return stdout_data


class DBusCollector:
    """Collects OpenBMC sensor readings using standard D-Bus interfaces.

    Queries ObjectMapper for all objects implementing xyz.openbmc_project.Sensor.Value,
    then batches calls to org.freedesktop.DBus.ObjectManager.GetManagedObjects for each daemon.
    Falls back to org.freedesktop.DBus.Properties.GetAll when a daemon does not support ObjectManager.
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        local: bool = False,
        port: int = 22,
        identity: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
        per_daemon_timeout: float = 15.0,
        cmd_runner: Any = None,
    ) -> None:
        if host and local:
            raise ValueError("Cannot specify both host and local")
        if not host and not local:
            if cmd_runner is None:
                raise ValueError("Either host or local must be specified")
            local = True

        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        if not isinstance(per_daemon_timeout, (int, float)) or isinstance(per_daemon_timeout, bool) or not math.isfinite(per_daemon_timeout) or per_daemon_timeout <= 0:
            raise ValueError("per_daemon_timeout must be a positive finite number")

        _validate_port(port)
        if host is not None:
            _validate_host(host)
            if identity is not None:
                _validate_identity(identity)

        self.host = host
        self.local = local
        self.port = port
        self.identity = identity
        self.password = password
        self.timeout = max(float(timeout), 5.0)
        self.per_daemon_timeout = max(float(per_daemon_timeout), float(self.timeout))
        self.cmd_runner = cmd_runner

    def _exec(self, cmd: list[str], timeout: float) -> tuple[int, str, str]:
        if self.cmd_runner is not None:
            return self.cmd_runner(cmd, timeout)

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return (proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            raise TimeoutError(f"Command timed out after {timeout}s")
        except Exception as e:
            _kill_process_group(proc)
            raise CollectionError(f"Failed to execute command {cmd[0]}: {e}") from e

    def _make_cmd(self, remote_cmd: str, local_args: list[str]) -> list[str]:
        if self.host is not None:
            return dbus_ssh_command(
                self.host,
                remote_cmd,
                port=self.port,
                identity=self.identity,
                password=self.password,
            )
        return local_args

    def collect_sensors(self, stop: threading.Event) -> dict[str, Any]:
        if stop.is_set():
            raise CollectionError("Collection cancelled by stop event")

        from sensor_overview.model import (
            HealthState,
            Sensor,
            group_name,
            parse_dbus_managed_objects,
            parse_dbus_sensor,
        )

        subtree_call = (
            "busctl --json=pretty call xyz.openbmc_project.ObjectMapper "
            "/xyz/openbmc_project/object_mapper xyz.openbmc_project.ObjectMapper "
            "GetSubTree sias /xyz/openbmc_project/sensors 0 1 xyz.openbmc_project.Sensor.Value"
        )
        local_subtree = [
            "busctl", "--json=pretty", "call",
            "xyz.openbmc_project.ObjectMapper",
            "/xyz/openbmc_project/object_mapper",
            "xyz.openbmc_project.ObjectMapper",
            "GetSubTree", "sias", "/xyz/openbmc_project/sensors", "0", "1",
            "xyz.openbmc_project.Sensor.Value",
        ]
        cmd = self._make_cmd(subtree_call, local_subtree)

        try:
            rc, stdout, stderr = self._exec(cmd, timeout=self.timeout)
        except TimeoutError as e:
            raise CollectionError(f"D-Bus GetSubTree timed out after {self.timeout}s") from e
        except Exception as e:
            raise CollectionError(f"Failed to query ObjectMapper: {e}") from e

        # Auto-fallback to default password if SSH permission denied
        if rc != 0 and self.host is not None and self.password is None:
            if "Permission denied" in (stderr or ""):
                fallback_cmd = dbus_ssh_command(
                    self.host,
                    subtree_call,
                    port=self.port,
                    identity=self.identity,
                    password=DEFAULT_OPENBMC_PASSWORD,
                )
                rc_fb, stdout_fb, stderr_fb = self._exec(fallback_cmd, timeout=self.timeout)
                if rc_fb == 0:
                    self.password = DEFAULT_OPENBMC_PASSWORD
                    rc, stdout, stderr = rc_fb, stdout_fb, stderr_fb

        if rc != 0:
            raise CollectionError(f"ObjectMapper GetSubTree failed (exit {rc}): {stderr.strip()}")

        try:
            data = json.loads(stdout)
        except Exception as e:
            raise CollectionError(f"Failed to parse ObjectMapper response: {e}") from e

        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
            tree = data["data"][0]
        elif isinstance(data, dict):
            tree = data
        else:
            tree = {}

        if not tree or not isinstance(tree, dict):
            raise CollectionError("No sensor subtree found (empty ObjectMapper response)")

        service_to_paths: dict[str, list[str]] = {}
        for path, srv_map in tree.items():
            if isinstance(srv_map, dict):
                for srv in srv_map.keys():
                    service_to_paths.setdefault(srv, []).append(path)

        sensors: dict[str, Sensor] = {}
        sensors_lock = threading.Lock()

        def _query_service(srv: str) -> None:
            if stop.is_set():
                return

            paths = service_to_paths[srv]
            managed_call = f"busctl --json=pretty call {srv} /xyz/openbmc_project/sensors org.freedesktop.DBus.ObjectManager GetManagedObjects"
            local_managed = ["busctl", "--json=pretty", "call", srv, "/xyz/openbmc_project/sensors", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"]
            cmd = self._make_cmd(managed_call, local_managed)

            use_fallback = False
            try:
                rc, stdout, stderr = self._exec(cmd, timeout=self.per_daemon_timeout)
                if rc == 0:
                    mdata = json.loads(stdout)
                    if isinstance(mdata, dict) and "data" in mdata and isinstance(mdata["data"], list) and len(mdata["data"]) > 0:
                        objs = mdata["data"][0]
                    elif isinstance(mdata, dict):
                        objs = mdata
                    else:
                        objs = {}
                    parsed_sensors = parse_dbus_managed_objects(objs)
                    with sensors_lock:
                        for s in parsed_sensors.values():
                            sensors[s.name] = s
                    return
                else:
                    if "UnknownMethod" in stderr or "UnknownMethod" in stdout:
                        use_fallback = True
                    else:
                        with sensors_lock:
                            for p in paths:
                                sname = p.rstrip("/").split("/")[-1]
                                sensors[sname] = Sensor(
                                    name=sname,
                                    status="unavailable",
                                    health=HealthState.UNAVAILABLE,
                                    group=group_name(sname),
                                    raw_status=f"Daemon error: {stderr.strip() or ('exit code ' + str(rc))}",
                                    available=False,
                                    functional=False,
                                    path=p,
                                )
                        return
            except TimeoutError as te:
                with sensors_lock:
                    for p in paths:
                        sname = p.rstrip("/").split("/")[-1]
                        sensors[sname] = Sensor(
                            name=sname,
                            status="unavailable",
                            health=HealthState.UNAVAILABLE,
                            group=group_name(sname),
                            raw_status=f"Daemon timeout: {te}",
                            available=False,
                            functional=False,
                            path=p,
                        )
                return
            except Exception as e:
                with sensors_lock:
                    for p in paths:
                        sname = p.rstrip("/").split("/")[-1]
                        sensors[sname] = Sensor(
                            name=sname,
                            status="unavailable",
                            health=HealthState.UNAVAILABLE,
                            group=group_name(sname),
                            raw_status=f"Daemon call failed: {e}",
                            available=False,
                            functional=False,
                            path=p,
                        )
                return

            if use_fallback:
                for p in paths:
                    if stop.is_set():
                        return
                    sname = p.rstrip("/").split("/")[-1]
                    getall_call = f'busctl --json=pretty call {srv} {p} org.freedesktop.DBus.Properties GetAll s ""'
                    local_getall = ["busctl", "--json=pretty", "call", srv, p, "org.freedesktop.DBus.Properties", "GetAll", "s", ""]
                    cmd = self._make_cmd(getall_call, local_getall)
                    try:
                        rc_prop, out_prop, err_prop = self._exec(cmd, timeout=self.per_daemon_timeout)
                        if rc_prop == 0:
                            pdata = json.loads(out_prop)
                            if isinstance(pdata, dict) and "data" in pdata and isinstance(pdata["data"], list) and len(pdata["data"]) > 0:
                                props = pdata["data"][0]
                            elif isinstance(pdata, dict):
                                props = pdata
                            else:
                                props = {}
                            sensor = parse_dbus_sensor(p, props)
                            with sensors_lock:
                                sensors[sensor.name] = sensor
                        else:
                            with sensors_lock:
                                sensors[sname] = Sensor(
                                    name=sname,
                                    status="unavailable",
                                    health=HealthState.UNAVAILABLE,
                                    group=group_name(sname),
                                    raw_status=f"GetAll error: {err_prop.strip()}",
                                    available=False,
                                    functional=False,
                                    path=p,
                                )
                    except Exception as e:
                        with sensors_lock:
                            sensors[sname] = Sensor(
                                name=sname,
                                status="unavailable",
                                health=HealthState.UNAVAILABLE,
                                group=group_name(sname),
                                raw_status=f"GetAll failed: {e}",
                                available=False,
                                functional=False,
                                path=p,
                            )

        import concurrent.futures
        max_workers = min(8, len(service_to_paths)) if service_to_paths else 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_query_service, srv) for srv in sorted(service_to_paths.keys())]
            for future in concurrent.futures.as_completed(futures):
                if stop.is_set():
                    break
                try:
                    future.result()
                except Exception:
                    pass

        if stop.is_set():
            raise CollectionError("Collection cancelled by stop event")

        return sensors

    def collect(self, stop: threading.Event) -> str:
        sensors = self.collect_sensors(stop)
        data = {
            s.name: {
                "status": s.status,
                "health": str(s.health),
                "value": s.value,
                "path": s.path,
                "raw_status": s.raw_status,
            }
            for s in sensors.values()
        }
        return json.dumps(data)
