"""The Windows job-object half of process inspection.

Split out of ``process_probe`` because everything here needs ctypes and the kernel32 API
while the rest of that module is portable: the Linux paths use pidfds and signals, and
mixing the two meant every reader crossed a platform boundary to follow one flow.
"""

from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes
from typing import Any

JOB_NAME_PATTERN = r"Local\\run-agent-[0-9a-f]{32}"
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_QUERY_LIMITED = 4
ERROR_FILE_NOT_FOUND = 2


def inspect_windows_job(name: str) -> dict[str, Any]:
    """Whether a named job object still holds processes, or has gone entirely."""

    if sys.platform != "win32":
        raise RuntimeError("Windows job inspection requires Windows")
    if re.fullmatch(JOB_NAME_PATTERN, name) is None:
        return {"empty": False, "reason": "Invalid job identity"}
    api = _kernel32()
    handle = api.OpenJobObjectW(JOB_OBJECT_QUERY_LIMITED, False, name)
    if not handle:
        code = ctypes.get_last_error()
        if code == ERROR_FILE_NOT_FOUND:
            return {"empty": True, "reason": "Job no longer exists"}
        raise ctypes.WinError(code)
    try:
        return _membership(api, handle)
    finally:
        api.CloseHandle(handle)


def _kernel32() -> Any:
    """kernel32 with the argument types declared, so the calls are checked."""
    if sys.platform != "win32":
        raise RuntimeError("Windows job inspection requires Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenJobObjectW.restype = wintypes.HANDLE
    api.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    return api


def _membership(api: Any, handle: Any) -> dict[str, Any]:
    if sys.platform != "win32":
        raise RuntimeError("Windows job inspection requires Windows")
    from run_agent_coding.host.windows_jobs import _Accounting

    accounting = _Accounting()
    queried = api.QueryInformationJobObject(
        handle,
        JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
        ctypes.byref(accounting),
        ctypes.sizeof(accounting),
        None,
    )
    if not queried:
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "empty": accounting.ActiveProcesses == 0,
        "reason": "Job membership inspected",
        "members": accounting.ActiveProcesses,
    }
