"""Read-only OS checks used by recovery; a PID alone never authorizes termination."""

from __future__ import annotations

import ctypes
import os
import re
import signal
from ctypes import wintypes
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from run_agent_coding.host.process_identity import machine_identity, process_identity


def _inspect_windows_job(name: str) -> dict[str, Any]:
    from run_agent_coding.host.windows_jobs import _Accounting

    if re.fullmatch(r"Local\\run-agent-[0-9a-f]{32}", name) is None:
        return {"empty": False, "reason": "Invalid job identity"}
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenJobObjectW.restype = wintypes.HANDLE
    api.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenJobObjectW(4, False, name)
    if not handle:
        code = ctypes.get_last_error()
        if code == 2:
            return {"empty": True, "reason": "Job no longer exists"}
        raise ctypes.WinError(code)
    try:
        accounting = _Accounting()
        if not api.QueryInformationJobObject(
            handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"empty": accounting.ActiveProcesses == 0,
                "reason": "Job membership inspected", "members": accounting.ActiveProcesses}
    finally:
        api.CloseHandle(handle)


def _inspect_unreleased_gate(intent: dict[str, Any]) -> dict[str, Any]:
    identity = intent.get("process_id", "")
    if not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{32}", identity) is None:
        return {"empty": False, "reason": "Missing launch identity"}
    if intent.get("kind") == "windows_job" and os.name == "nt":
        return _inspect_windows_job("Local\\run-agent-" + identity)
    if intent.get("kind") == "posix_group" and os.name == "posix":
        members = []
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                arguments = (path / "cmdline").read_bytes().split(b"\0")
            except FileNotFoundError:
                continue
            if len(arguments) > 2 and arguments[2] == identity.encode():
                members.append(int(path.name))
        return {"empty": not members, "reason": "Unreleased startup gate inspected",
                "members": members}
    return {"empty": False, "reason": "Launch platform differs from recovery host"}


def inspect_native_process(intent: dict[str, Any], native: dict[str, Any] | None) -> dict[str, Any]:
    if intent.get("launch_protocol") != "journal-gate-v1":
        return {"empty": False, "reason": "Unknown process launch protocol"}
    if intent.get("machine_identity") != machine_identity():
        return {"empty": False, "reason": "Process belongs to another machine"}
    try:
        if process_identity(intent["host_pid"]) == intent["host_identity"]:
            return {"empty": False, "reason": "Process owner is still alive"}
    except (OSError, KeyError, TypeError, ValueError):
        return {"empty": False, "reason": "Process owner identity cannot be verified"}
    try:
        if native is None:
            return _inspect_unreleased_gate(intent)
        pid, identity = native.get("pid"), native.get("native_identity")
        if not isinstance(pid, int) or pid <= 0 or not isinstance(identity, str):
            return {"empty": False, "reason": "Missing native process identity"}
        current = process_identity(pid)
        if current == identity:
            return {"empty": False, "reason": "Command process is still alive", "pid": pid}
        if native.get("kind") == "windows_job" and os.name == "nt":
            name = native.get("identity", "")
            if not isinstance(name, str):
                return {"empty": False, "reason": "Invalid job identity"}
            return _inspect_windows_job(name)
        if native.get("kind") == "posix_group" and os.name == "posix":
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if identity.split(":")[:2] != ["linux", boot]:
                return {"empty": True, "reason": "Same machine has rebooted since process start"}
            if current is not None and current != identity:
                return {"empty": False, "reason": "Process group identity was reused"}
            members = []
            for directory in Path("/proc").iterdir():
                if not directory.name.isdigit():
                    continue
                try:
                    body = (directory / "stat").read_text()
                except FileNotFoundError:
                    continue
                fields = body[body.rfind(")") + 2:].split()
                if int(fields[2]) == pid and fields[0] != "Z":
                    members.append(int(directory.name))
            return {"empty": not members, "reason": "Process group inspected", "members": members}
        return {"empty": False, "reason": "Process platform differs from recovery host"}
    except (OSError, ValueError, IndexError) as exc:
        return {"empty": False, "reason": f"Native process check failed: {exc}"}


def terminate_orphan(intent: dict[str, Any], native: dict[str, Any] | None) -> dict[str, Any]:
    check = inspect_native_process(intent, native)
    if check["empty"]:
        return check
    if (native is None or intent.get("machine_identity") != machine_identity()
            or intent.get("launch_protocol") != "journal-gate-v1"):
        return check
    if process_identity(intent["host_pid"]) == intent["host_identity"]:
        return check
    current = process_identity(native["pid"])
    if current is not None and current != native["native_identity"]:
        return {"empty": False, "reason": "Native process identity was reused"}
    if native.get("kind") == "windows_job" and os.name == "nt":
        name = native.get("identity", "")
        if re.fullmatch(r"Local\\run-agent-[0-9a-f]{32}", name) is None:
            return check
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        api.OpenJobObjectW.restype = wintypes.HANDLE
        api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenJobObjectW(4 | 8, False, name)
        if handle:
            try:
                if not api.TerminateJobObject(handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                api.CloseHandle(handle)
    elif native.get("kind") == "posix_group" and os.name == "posix":
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if native["native_identity"].split(":")[:2] != ["linux", boot]:
            return {"empty": False, "reason": "Process belongs to another host boot"}
        # Signal members through pidfds and recheck their group after opening each
        # descriptor. A reused numeric PID must never redirect a recovery signal.
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            return {"empty": False, "reason": "Recovery requires Linux pidfd support"}
        until = monotonic() + 5
        while monotonic() < until:
            current = process_identity(native["pid"])
            if current is not None and current != native["native_identity"]:
                return {"empty": False, "reason": "Process group identity was reused"}
            members = []
            for path in Path("/proc").iterdir():
                if not path.name.isdigit():
                    continue
                pid = int(path.name)
                try:
                    descriptor = getattr(os, "pidfd_open")(pid, 0)  # noqa: B009
                except ProcessLookupError:
                    continue
                try:
                    body = (path / "stat").read_text()
                    fields = body[body.rfind(")") + 2:].split()
                    if int(fields[2]) == native["pid"] and fields[0] != "Z":
                        members.append(pid)
                        getattr(signal, "pidfd_send_signal")(  # noqa: B009
                            descriptor, getattr(signal, "SIGKILL"), None, 0,  # noqa: B009
                        )
                except (FileNotFoundError, ProcessLookupError):
                    pass
                finally:
                    os.close(descriptor)
            if not members:
                break
            sleep(0.025)
    until = monotonic() + 5
    while monotonic() < until:
        check = inspect_native_process(intent, native)
        if check["empty"]:
            return {**check, "termination": "requested"}
        sleep(0.025)
    return check
