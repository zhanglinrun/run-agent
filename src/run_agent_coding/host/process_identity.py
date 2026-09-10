"""Native process identities distinguish an existing process from a reused PID."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path


def process_identity(pid: int) -> str | None:
    if pid <= 0:
        raise ValueError("Process PID must be positive")
    if os.name == "nt":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.c_void_p] * 4]
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = api.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: process no longer exists
                return None
            raise ctypes.WinError(error)
        try:
            if api.WaitForSingleObject(handle, 0) == 0:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            creation = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return f"windows:{pid}:{creation}"
        finally:
            api.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"linux:{boot}:{pid}:{fields[19]}"
    except FileNotFoundError:
        return None


def current_process_identity() -> str:
    identity = process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("Cannot establish host process identity")
    return identity


def machine_identity() -> str:
    if os.name == "nt":
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return f"windows:{value}"
    return "linux:" + Path("/etc/machine-id").read_text().strip()
