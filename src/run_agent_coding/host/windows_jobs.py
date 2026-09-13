"""Windows command ownership: assign a suspended process before allowing execution."""

from __future__ import annotations

import ctypes
import importlib
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any, BinaryIO, cast


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        *[
            (name, wintypes.DWORD)
            for name in (
                "dwX",
                "dwY",
                "dwXSize",
                "dwYSize",
                "dwXCountChars",
                "dwYCountChars",
                "dwFillAttribute",
                "dwFlags",
            )
        ],
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class WindowsJobProcess:
    kind = "windows_job"
    pid: int

    def __init__(self, command: str, cwd: Path, output: BinaryIO, *, identity: str) -> None:
        self.identity = "Local\\run-agent-" + identity
        if sys.platform != "win32":
            raise RuntimeError("Windows job processes require Windows")
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, result, arguments in (
            ("CreateJobObjectW", wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
            (
                "SetInformationJobObject",
                wintypes.BOOL,
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
            ),
            (
                "InitializeProcThreadAttributeList",
                wintypes.BOOL,
                [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p],
            ),
            (
                "UpdateProcThreadAttribute",
                wintypes.BOOL,
                [
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.c_size_t,
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                ],
            ),
            ("DeleteProcThreadAttributeList", None, [ctypes.c_void_p]),
            (
                "CreateProcessW",
                wintypes.BOOL,
                [
                    wintypes.LPCWSTR,
                    wintypes.LPWSTR,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    wintypes.BOOL,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    wintypes.LPCWSTR,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                ],
            ),
            (
                "QueryInformationJobObject",
                wintypes.BOOL,
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
            ),
            ("TerminateJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
            ("ResumeThread", wintypes.DWORD, [wintypes.HANDLE]),
            ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
        ):
            function = getattr(self._api, name)
            function.restype, function.argtypes = result, arguments
        self._winapi = importlib.import_module("_winapi")
        self._job = self._api.CreateJobObjectW(None, self.identity)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        self._process: int | None = None
        self._thread: int | None = None
        try:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            self._check(
                self._api.SetInformationJobObject(
                    self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
            )
            msvcrt = importlib.import_module("msvcrt")
            with open(os.devnull, "rb") as input_file:
                handles = []
                try:
                    current = self._winapi.GetCurrentProcess()
                    for file in (input_file, output):
                        handles.append(
                            self._winapi.DuplicateHandle(
                                current,
                                msvcrt.get_osfhandle(file.fileno()),
                                current,
                                0,
                                True,
                                self._winapi.DUPLICATE_SAME_ACCESS,
                            )
                        )
                    shell = os.environ.get("COMSPEC") or str(
                        Path(os.environ["SYSTEMROOT"]) / "System32" / "cmd.exe"
                    )
                    self._create(shell, command, cwd, handles)
                finally:
                    for handle in handles:
                        self._winapi.CloseHandle(handle)
        except BaseException:
            if self._process is not None:
                self._winapi.TerminateProcess(self._process, 1)
                self._winapi.WaitForSingleObject(self._process, 5000)
            self.close()
            raise

    def _create(self, shell: str, command: str, cwd: Path, handles: list[int]) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows job processes require Windows")
        size = ctypes.c_size_t()
        self._api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        self._check(
            self._api.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size))
        )
        try:
            inherited = (wintypes.HANDLE * len(handles))(*handles)
            jobs = (wintypes.HANDLE * 1)(self._job)
            # JOB_LIST makes membership atomic with creation. There is no unowned
            # suspended process interval if the parent dies inside CreateProcessW.
            for key, values in ((0x20002, inherited), (0x2000D, jobs)):
                self._check(
                    self._api.UpdateProcThreadAttribute(
                        attributes,
                        0,
                        key,
                        values,
                        ctypes.sizeof(values),
                        None,
                        None,
                    )
                )
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = (
                subprocess.STARTF_USESTDHANDLES | subprocess.STARTF_USESHOWWINDOW
            )
            startup.StartupInfo.wShowWindow = subprocess.SW_HIDE
            startup.StartupInfo.hStdInput = handles[0]
            startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = handles[1]
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            info = _ProcessInfo()
            line = ctypes.create_unicode_buffer(
                f'{subprocess.list2cmdline([shell])} /d /s /c "{command}"'
            )
            self._check(
                self._api.CreateProcessW(
                    shell,
                    line,
                    None,
                    None,
                    True,
                    0x4 | 0x80000 | subprocess.CREATE_NO_WINDOW,
                    None,
                    str(cwd),
                    ctypes.byref(startup),
                    ctypes.byref(info),
                )
            )
            self._process, self._thread, self.pid = info.hProcess, info.hThread, info.dwProcessId
        finally:
            self._api.DeleteProcThreadAttributeList(attributes)

    def resume(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows job processes require Windows")
        if self._thread is None:
            raise RuntimeError("Process thread is not suspended")
        if self._api.ResumeThread(self._thread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        self._winapi.CloseHandle(self._thread)
        self._thread = None

    @staticmethod
    def _check(result: Any) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows job processes require Windows")
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def poll(self) -> int | None:
        if sys.platform != "win32":
            raise RuntimeError("Windows job processes require Windows")
        if self._winapi.WaitForSingleObject(self._process, 0) == self._winapi.WAIT_TIMEOUT:
            return None
        return cast(int, self._winapi.GetExitCodeProcess(self._process))

    def active_count(self) -> int:
        if sys.platform != "win32":
            raise RuntimeError("Windows job processes require Windows")
        info = _Accounting()
        self._check(
            self._api.QueryInformationJobObject(
                self._job, 1, ctypes.byref(info), ctypes.sizeof(info), None
            )
        )
        return int(info.ActiveProcesses)

    def terminate(self, *, force: bool) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows job processes require Windows")
        # Hidden Windows jobs have no console for CTRL_BREAK. Terminate the owned
        # job directly; never report a graceful signal that was not delivered.
        self._check(self._api.TerminateJobObject(self._job, 1))

    def close(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows job processes require Windows")
        if self._thread is not None:
            self._winapi.CloseHandle(self._thread)
            self._thread = None
        if self._process is not None:
            self._winapi.CloseHandle(self._process)
            self._process = None
        if self._job:
            self._check(self._api.CloseHandle(self._job))
            self._job = None
