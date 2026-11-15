"""Lifecycle helpers for subprocess groups owned by this application (POSIX)."""

import atexit
import os
import signal
import subprocess
import threading

_active = set()
_lock = threading.RLock()
_installed = False


def terminate_process(process, grace=5):
    """Stop the entire owned session, including descendants after leader exit."""
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        # Leader exit does not imply descendant exit. Always clean the group.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    finally:
        with _lock:
            _active.discard(process)


def cleanup_processes():
    with _lock:
        processes = list(_active)
    for process in processes:
        try:
            terminate_process(process, grace=1)
        except (OSError, subprocess.TimeoutExpired):
            pass


def install_process_cleanup():
    """Entrypoints install this before executing CLI calls, including in threads."""
    global _installed
    if _installed:
        return
    _installed = True
    atexit.register(cleanup_processes)

    def shutdown(signum, frame):
        cleanup_processes()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, shutdown)


def run_isolated(command, *, input, timeout, **options):
    options.pop("check", None)
    options.pop("capture_output", None)
    with _lock:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, **options,
        )
        _active.add(process)
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        # Also executes on TimeoutExpired, KeyboardInterrupt, or ordinary failure.
        terminate_process(process, grace=1)
        # Descendants must never hold an unbounded pipe-drain operation open.
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
