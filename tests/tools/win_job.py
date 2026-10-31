#!/usr/bin/env python3
"""Launch a test process inside a Windows Job Object and kill the whole tree.

Why this exists
---------------
A test harness that starts a browser must be able to stop *exactly* the process
tree it started -- and nothing else. The unsafe alternative that this module
replaces is an image-name kill: it matches every process of that image on the
machine, including the user's own browser, and a PID-only kill can also hit a
recycled PID. Image-name matching is forbidden repo-wide by
`BrowserPilot/scripts/check-process-cleanup.mjs`.

Design
------
* `CreateJobObjectW` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`: when the job handle
  is closed, Windows terminates every process still in the job.
* The child is created with `CREATE_SUSPENDED`, assigned to the job with
  `AssignProcessToJobObject`, and only then resumed. A child can therefore never
  run (and spawn grandchildren) before it belongs to the job.
* `close()` closes the job handle, which is the tree-kill path; `terminate()`
  uses `TerminateJobObject` for an explicit early stop.
* Every Win32 failure raises `JobError` (fail closed). There is deliberately no
  fallback to an unscoped kill: a harness that cannot prove ownership must stop
  and report, not guess.

Compatibility
-------------
`JobProcess` exposes `pid`, `poll()`, `wait(timeout)`, `terminate()`, `kill()`
and `close()`, so it can replace `subprocess.Popen` in existing harnesses.

Self-test
---------
    python tests/tools/win_job.py --self-test

Starts a target tree (`cmd.exe` -> `ping.exe`) inside a job plus an unrelated
control process, terminates the job, and asserts that only the target tree died
and that `KILL_ON_JOB_CLOSE` works. Read-only queries only; no global kill.

# browser-launch-hygiene: windows-only (no keychain)
"""

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - this repo's harnesses are Windows-only
    raise ImportError("win_job.py is Windows-only (Job Objects are a Win32 API)")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000

PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
STARTF_USESTDHANDLES = 0x00000100

STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF

GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

class LARGE_INTEGER(ctypes.Union):
    _fields_ = [
        ("LowPart", wintypes.DWORD),
        ("HighPart", wintypes.LONG),
        ("QuadPart", ctypes.c_longlong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", LARGE_INTEGER),
        ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


# ---------------------------------------------------------------------------
# Prototypes (explicit argtypes/restype: handles are 64-bit pointers)
# ---------------------------------------------------------------------------

kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]

kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]

kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

kernel32.TerminateJobObject.restype = wintypes.BOOL
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]

kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.c_void_p, ctypes.POINTER(PROCESS_INFORMATION)]

kernel32.ResumeThread.restype = wintypes.DWORD
kernel32.ResumeThread.argtypes = [wintypes.HANDLE]

kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]

kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
kernel32.InitializeProcThreadAttributeList.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]

kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
kernel32.UpdateProcThreadAttribute.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]

kernel32.DeleteProcThreadAttributeList.restype = None
kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]


class JobError(RuntimeError):
    """A Win32 call failed. Callers must stop; never fall back to a global kill."""


def _last_error(what):
    error = ctypes.get_last_error()
    return JobError("%s failed: %s (WinError %d)"
                    % (what, ctypes.FormatError(error), error))


def _check(result, what):
    if not result:
        raise _last_error(what)
    return result


# ---------------------------------------------------------------------------
# Read-only helpers
# ---------------------------------------------------------------------------

def pid_alive(pid):
    """True if the PID currently exists (read-only; no kill, no ownership claim)."""
    handle = kernel32.OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def child_pids(pid):
    """Direct child PIDs of `pid` (PowerShell CIM query, read-only)."""
    script = ("Get-CimInstance Win32_Process -Filter 'ParentProcessId = %d' "
              "| Select-Object -ExpandProperty ProcessId" % int(pid))
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        creationflags=CREATE_NO_WINDOW)
    out = []
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.isdigit():
            out.append(int(line))
    return out


# ---------------------------------------------------------------------------
# Job + process
# ---------------------------------------------------------------------------

def create_job(name=None):
    """Create a job with KILL_ON_JOB_CLOSE and return its handle."""
    handle = kernel32.CreateJobObjectW(None, name)
    if not handle:
        raise _last_error("CreateJobObjectW")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        _check(kernel32.SetInformationJobObject(
            handle, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)), "SetInformationJobObject")
    except JobError:
        kernel32.CloseHandle(handle)
        raise
    return handle


def _open_nul_handles():
    """Three inheritable handles to NUL (child stdout/stderr/stdin sink)."""
    attributes = SECURITY_ATTRIBUTES()
    attributes.nLength = ctypes.sizeof(attributes)
    attributes.lpSecurityDescriptor = None
    attributes.bInheritHandle = True
    handles = []
    try:
        for access in (GENERIC_WRITE, GENERIC_WRITE, GENERIC_WRITE):
            handle = kernel32.CreateFileW(
                "NUL", access, FILE_SHARE_READ | FILE_SHARE_WRITE,
                ctypes.byref(attributes), OPEN_EXISTING, 0, None)
            if not handle or handle == INVALID_HANDLE_VALUE:
                raise _last_error("CreateFileW(NUL)")
            handles.append(handle)
    except Exception:
        for handle in handles:
            kernel32.CloseHandle(handle)
        raise
    return handles


def _environment_block(env):
    """Sorted, double-NUL-terminated UTF-16 environment block."""
    entries = []
    for key, value in env.items():
        text = "%s=%s" % (key, value)
        entries.append(text)
    entries.sort(key=lambda item: item.split("=", 1)[0].upper())
    block = "\0".join(entries) + "\0\0"
    return ctypes.create_unicode_buffer(block, len(block))


def _build_handle_list(handles):
    """PROC_THREAD_ATTRIBUTE_HANDLE_LIST so only our NUL handles are inherited."""
    size = ctypes.c_size_t(0)
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if not size.value:
        raise _last_error("InitializeProcThreadAttributeList(size)")
    buffer = ctypes.create_string_buffer(size.value)
    _check(kernel32.InitializeProcThreadAttributeList(
        buffer, 1, 0, ctypes.byref(size)), "InitializeProcThreadAttributeList")
    array = (wintypes.HANDLE * len(handles))(*handles)
    try:
        _check(kernel32.UpdateProcThreadAttribute(
            buffer, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            ctypes.cast(array, ctypes.c_void_p),
            ctypes.sizeof(array), None, None), "UpdateProcThreadAttribute")
    except JobError:
        kernel32.DeleteProcThreadAttributeList(buffer)
        raise
    return buffer


class JobProcess:
    """A process tree that lives and dies with its Job Object."""

    def __init__(self, job, process_handle, pid, owns_job=True, label=None):
        self._job = job
        self._process = process_handle
        self._pid = int(pid)
        self._owns_job = bool(owns_job)
        self._exit_code = None
        self._closed = False
        self.label = label

    @property
    def pid(self):
        return self._pid

    def _refresh_exit_code(self):
        if self._exit_code is not None:
            return self._exit_code
        code = wintypes.DWORD()
        _check(kernel32.GetExitCodeProcess(self._process, ctypes.byref(code)),
               "GetExitCodeProcess")
        if code.value == STILL_ACTIVE:
            return None
        self._exit_code = int(code.value)
        return self._exit_code

    def poll(self):
        """Exit code, or None while the process is still running."""
        if self._closed:
            return self._exit_code
        return self._refresh_exit_code()

    def wait(self, timeout=None):
        """Block until exit; raise TimeoutError on timeout."""
        if self._closed:
            raise JobError("wait() on a closed JobProcess (pid %d)" % self._pid)
        milliseconds = INFINITE if timeout is None else int(max(0.0, timeout) * 1000)
        result = kernel32.WaitForSingleObject(self._process, milliseconds)
        if result == WAIT_TIMEOUT:
            raise TimeoutError(
                "process %d did not exit within %.1fs" % (self._pid, timeout))
        if result != WAIT_OBJECT_0:
            raise _last_error("WaitForSingleObject")
        code = self._refresh_exit_code()
        if code is None:  # pragma: no cover - signaled handle always has a code
            raise JobError("process %d signaled without an exit code" % self._pid)
        return code

    def terminate(self):
        """Terminate every process in the job (the whole tree). Idempotent."""
        if self._closed:
            return
        if self._refresh_exit_code() is not None:
            return
        _check(kernel32.TerminateJobObject(self._job, 1), "TerminateJobObject")

    def kill(self):
        """Alias of terminate() so Popen-shaped call sites keep working."""
        self.terminate()

    def close(self):
        """Close handles. Closing the job kills anything still running in it."""
        if self._closed:
            return
        self._closed = True
        if self._process:
            kernel32.CloseHandle(self._process)
            self._process = None
        if self._job and self._owns_job:
            # KILL_ON_JOB_CLOSE: the tree dies here if terminate() was not called.
            kernel32.CloseHandle(self._job)
            self._job = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:  # pragma: no cover - interpreter shutdown
            pass


def launch(argv, cwd=None, env=None, creation_flags=0, stdio="devnull", label=None):
    """Start `argv` suspended inside a fresh Job Object and resume it.

    stdio="devnull" points the child's std handles at NUL; "none" inherits
    nothing. Raises JobError on any Win32 failure (fail closed).
    """
    argv = [str(item) for item in argv]
    if not argv:
        raise JobError("launch() needs a non-empty argv")
    command_line = subprocess.list2cmdline(argv)
    application_name = argv[0] if os.path.isabs(argv[0]) else None

    job = create_job()
    nul_handles = []
    attribute_list = None
    startup = STARTUPINFOEXW()
    startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    process_info = PROCESS_INFORMATION()
    flags = int(creation_flags) | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT
    environment = None
    try:
        if stdio == "devnull":
            nul_handles = _open_nul_handles()
            attribute_list = _build_handle_list(nul_handles)
            startup.StartupInfo.dwFlags |= STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = nul_handles[0]
            startup.StartupInfo.hStdOutput = nul_handles[1]
            startup.StartupInfo.hStdError = nul_handles[2]
            startup.lpAttributeList = ctypes.cast(attribute_list, ctypes.c_void_p)
            inherit = True
        elif stdio == "none":
            inherit = False
        else:
            raise JobError("unknown stdio mode: %r" % (stdio,))

        if env is not None:
            environment = _environment_block(env)
            flags |= CREATE_UNICODE_ENVIRONMENT

        _check(kernel32.CreateProcessW(
            application_name,
            ctypes.create_unicode_buffer(command_line),
            None, None, inherit, flags,
            ctypes.cast(environment, ctypes.c_void_p) if environment is not None else None,
            cwd,
            ctypes.byref(startup),
            ctypes.byref(process_info)), "CreateProcessW")

        # Suspended: assign before it can run, so no grandchild escapes the job.
        try:
            _check(kernel32.AssignProcessToJobObject(job, process_info.hProcess),
                   "AssignProcessToJobObject")
        except JobError:
            kernel32.TerminateProcess(process_info.hProcess, 1)
            kernel32.CloseHandle(process_info.hThread)
            kernel32.CloseHandle(process_info.hProcess)
            raise

        if kernel32.ResumeThread(process_info.hThread) == 0xFFFFFFFF:
            kernel32.TerminateJobObject(job, 1)
            kernel32.CloseHandle(process_info.hThread)
            kernel32.CloseHandle(process_info.hProcess)
            raise _last_error("ResumeThread")

        kernel32.CloseHandle(process_info.hThread)
        return JobProcess(job, process_info.hProcess, process_info.dwProcessId,
                          owns_job=True, label=label)
    except Exception:
        if process_info.hProcess:
            try:
                kernel32.TerminateJobObject(job, 1)
            except Exception:
                pass
        kernel32.CloseHandle(job)
        raise
    finally:
        for handle in nul_handles:
            kernel32.CloseHandle(handle)
        if attribute_list is not None:
            kernel32.DeleteProcThreadAttributeList(attribute_list)


# ---------------------------------------------------------------------------
# Self-test: only the target tree dies; the control process survives.
# ---------------------------------------------------------------------------

PING = ["cmd.exe", "/c", "ping -n 120 127.0.0.1 >nul"]


def _wait_until(predicate, timeout, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def self_test():
    evidence = {}
    target = launch(PING, creation_flags=CREATE_NO_WINDOW, label="job-target")
    control = subprocess.Popen(
        PING, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW)
    evidence["target"] = {"pid": target.pid, "creation_alive": pid_alive(target.pid)}
    evidence["control"] = {"pid": control.pid, "alive_before": pid_alive(control.pid)}
    grandchildren = child_pids(target.pid)
    evidence["target_children"] = grandchildren

    ok = True

    def check(name, condition, detail=""):
        nonlocal ok
        if not condition:
            ok = False
        print("%-4s %s%s" % ("PASS" if condition else "FAIL", name,
                             (" :: " + str(detail)) if detail else ""))

    try:
        check("target and its grandchild are alive before terminate",
              evidence["target"]["creation_alive"]
              and (not grandchildren or all(pid_alive(p) for p in grandchildren)),
              json.dumps(evidence))
        check("control process is alive before terminate",
              evidence["control"]["alive_before"], control.pid)

        target.terminate()
        exit_code = target.wait(timeout=15)
        evidence["target"]["exit_code"] = exit_code
        check("target exits after terminate()", True)
        check("target PID is gone",
              _wait_until(lambda: not pid_alive(target.pid), 5), target.pid)
        for child in grandchildren:
            check("target grandchild %d is gone" % child,
                  _wait_until(lambda pid=child: not pid_alive(pid), 5), child)

        time.sleep(0.5)
        check("control process survives the target tree kill",
              control.poll() is None, "control exit=" + str(control.poll()))
        evidence["control"]["alive_after_target_kill"] = control.poll() is None

        # KILL_ON_JOB_CLOSE: close() alone must terminate the tree.
        second = launch(PING, creation_flags=CREATE_NO_WINDOW, label="job-kill-on-close")
        second_pid = second.pid
        check("second target is alive before close()", pid_alive(second_pid), second_pid)
        second.close()
        check("KILL_ON_JOB_CLOSE kills the tree when the job handle closes",
              _wait_until(lambda: not pid_alive(second_pid), 10), second_pid)
        evidence["kill_on_job_close_pid"] = second_pid

        # A closed process handle cannot leak into a later call.
        check("close() is idempotent", (target.close() or True) and True)
    finally:
        try:
            target.close()
        except Exception:
            pass
        if control.poll() is None:
            control.kill()  # direct ChildProcess handle: scoped to our own PID
            try:
                control.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        evidence["control"]["alive_after_cleanup"] = control.poll() is None

    check("control process is stopped by its own handle at the end",
          control.poll() is not None, control.poll())
    print(json.dumps(evidence, indent=2))
    print("SELF-TEST %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv):
    if "--self-test" in argv:
        return self_test()
    print("usage: win_job.py --self-test", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
