"""Cancellable host subprocesses for one evaluation batch.

Container removal deliberately uses the standard subprocess module so cancellation
never prevents cleanup. No provider credentials are stored in this registry.
"""
from __future__ import annotations
import subprocess as _subprocess
import threading
import time

_cancelled = threading.Event()
_lock = threading.Lock()
_processes = set()


def __getattr__(name):
    return getattr(_subprocess, name)


def reset():
    _cancelled.clear()


def cancel_all():
    _cancelled.set()
    with _lock:
        processes = list(_processes)
    for process in processes:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass


class Popen(_subprocess.Popen):
    """Preserve subprocess.Popen's runtime typing and process API."""
    def __init__(self, *args, **kwargs):
        with _lock:
            if _cancelled.is_set():
                raise _subprocess.SubprocessError('Evaluation interrupted')
            super().__init__(*args, **kwargs)
            _processes.difference_update(p for p in list(_processes) if p.poll() is not None)
            _processes.add(self)


def run(*popenargs, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    if input is not None:
        if kwargs.get('stdin') is not None:
            raise ValueError('stdin and input arguments may not both be used')
        kwargs['stdin'] = _subprocess.PIPE
    if capture_output:
        if kwargs.get('stdout') is not None or kwargs.get('stderr') is not None:
            raise ValueError('stdout and stderr arguments may not be used with capture_output')
        kwargs['stdout'] = kwargs['stderr'] = _subprocess.PIPE
    process = Popen(*popenargs, **kwargs)
    deadline = None if timeout is None else time.monotonic() + timeout
    first = True
    try:
        while True:
            if _cancelled.is_set():
                raise _subprocess.SubprocessError('Evaluation interrupted')
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise _subprocess.TimeoutExpired(process.args, timeout)
            try:
                stdout, stderr = process.communicate(input if first else None,
                    timeout=min(0.2, remaining) if remaining is not None else 0.2)
                break
            except _subprocess.TimeoutExpired:
                first = False
        if check and process.returncode:
            raise _subprocess.CalledProcessError(process.returncode, process.args, output=stdout, stderr=stderr)
        return _subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    except BaseException as exc:
        process.kill()
        # Reap the host CLI; container removal is handled by the batch owner.
        try:
            stdout, stderr = process.communicate(timeout=1)
            if isinstance(exc, _subprocess.TimeoutExpired):
                exc.output, exc.stderr = stdout, stderr
        except (_subprocess.TimeoutExpired, OSError):
            pass
        raise
    finally:
        with _lock:
            _processes.discard(process)
