"""Windows command ownership: assign a suspended process before allowing execution."""

from __future__ import annotations

import ctypes
import importlib
import os
import subprocess
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
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class WindowsJobProcess:
    kind = "windows_job"

    def __init__(
        self, command: str, cwd: Path, output: BinaryIO, *, identity: str
    ) -> None:
        self.identity = "Local\\run-agent-" + identity
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, result, arguments in (
            ("CreateJobObjectW", wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
            ("SetInformationJobObject", wintypes.BOOL,
             [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
            ("AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
            ("QueryInformationJobObject", wintypes.BOOL,
             [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]),
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
        thread: int | None = None
        try:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            self._check(self._api.SetInformationJobObject(
                self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ))
            msvcrt = importlib.import_module("msvcrt")
            startup = subprocess.STARTUPINFO()
            startup.dwFlags = subprocess.STARTF_USESTDHANDLES | subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            with open(os.devnull, "rb") as input_file:
                handles = []
                try:
                    current = self._winapi.GetCurrentProcess()
                    for file in (input_file, output):
                        handles.append(self._winapi.DuplicateHandle(
                            current, msvcrt.get_osfhandle(file.fileno()), current,
                            0, True, self._winapi.DUPLICATE_SAME_ACCESS,
                        ))
                    startup.hStdInput = handles[0]
                    startup.hStdOutput = startup.hStdError = handles[1]
                    startup.lpAttributeList = {"handle_list": handles}
                    shell = os.environ.get("COMSPEC") or str(
                        Path(os.environ["SYSTEMROOT"]) / "System32" / "cmd.exe"
                    )
                    self._process, thread, self.pid, _ = self._winapi.CreateProcess(
                        shell, f'{subprocess.list2cmdline([shell])} /d /s /c "{command}"',
                        None, None, True, 0x4 | subprocess.CREATE_NO_WINDOW,
                        None, str(cwd), startup,
                    )
                finally:
                    for handle in handles:
                        self._winapi.CloseHandle(handle)
            self._check(self._api.AssignProcessToJobObject(self._job, self._process))
            if self._api.ResumeThread(thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            if self._process is not None:
                self._winapi.TerminateProcess(self._process, 1)
                self._winapi.WaitForSingleObject(self._process, 5000)
            self.close()
            raise
        finally:
            if thread is not None:
                self._winapi.CloseHandle(thread)

    @staticmethod
    def _check(result: Any) -> None:
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def poll(self) -> int | None:
        if self._winapi.WaitForSingleObject(self._process, 0) == self._winapi.WAIT_TIMEOUT:
            return None
        return cast(int, self._winapi.GetExitCodeProcess(self._process))

    def active_count(self) -> int:
        info = _Accounting()
        self._check(self._api.QueryInformationJobObject(
            self._job, 1, ctypes.byref(info), ctypes.sizeof(info), None
        ))
        return int(info.ActiveProcesses)

    def terminate(self, *, force: bool) -> None:
        # Hidden Windows jobs have no console for CTRL_BREAK. Terminate the owned
        # job directly; never report a graceful signal that was not delivered.
        self._check(self._api.TerminateJobObject(self._job, 1))

    def close(self) -> None:
        if self._process is not None:
            self._winapi.CloseHandle(self._process)
            self._process = None
        if self._job:
            self._check(self._api.CloseHandle(self._job))
            self._job = None
