"""Card command execution: argv only, minimal environment, group timeout."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


# Bounded window for the *pre-return* proof that the command's process group is
# empty: SIGTERM grace, then SIGKILL grace.  A process that survives SIGKILL (or
# a check we cannot read) is never reported as a successful command.
GROUP_TERM_SECONDS = 2.0
GROUP_KILL_SECONDS = 2.0
_GROUP_POLL_SECONDS = 0.02


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    cwd: str
    exit_code: int
    timed_out: bool
    log: str
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "log": self.log,
            "duration_seconds": round(self.duration_seconds, 3),
        }


class CommandGroupError(RuntimeError):
    """The command's process group was not proven empty before returning.

    ``run_command`` writes the captured stdout/stderr logs *before* raising, so
    the caller still owns the evidence and can record an explicit failure.  A
    result is attached for callers that need the exit code/timings.
    """

    def __init__(self, reason: str, *, result: "CommandResult") -> None:
        super().__init__(f"process group not proven empty: {reason}")
        self.reason = reason
        self.result = result


def _group_status(pgid: int) -> tuple[bool, str]:
    """Is *any* member of ``pgid`` still observable?

    This is the single low-level process-observation seam used by this module.
    ``killpg(pgid, 0)`` performs the existence/permission check atomically in
    the kernel, so it also sees a surviving descendant after its leader exited.
    Fail-closed: a permission error or any other OS error is *unknown*, never
    proof that the group is empty.
    """

    try:
        os.killpg(int(pgid), 0)
    except ProcessLookupError:
        return False, "empty"
    except PermissionError:
        return True, "permission-denied"
    except OSError as exc:  # pragma: no cover - defensive: unusual kernel error
        return True, f"check-unavailable:{type(exc).__name__}"
    return True, "members-present"


def _wait_group_empty(pgid: int, seconds: float) -> tuple[bool, str]:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        alive, reason = _group_status(pgid)
        if not alive:
            return True, reason
        if time.monotonic() >= deadline:
            return False, reason
        time.sleep(_GROUP_POLL_SECONDS)


def _ensure_group_empty(pgid: int) -> tuple[bool, str]:
    """TERM -> bounded grace -> KILL -> bounded grace; returns the proof."""

    alive, reason = _group_status(pgid)
    if not alive:
        return True, reason
    _kill_group(pgid, signal.SIGTERM)
    empty, reason = _wait_group_empty(pgid, GROUP_TERM_SECONDS)
    if empty:
        return True, reason
    _kill_group(pgid, signal.SIGKILL)
    return _wait_group_empty(pgid, GROUP_KILL_SECONDS)


def run_command(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    log_path: str | os.PathLike[str] | None = None,
    stdout_log_path: str | os.PathLike[str] | None = None,
    stderr_log_path: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    capture: bool = False,
) -> CommandResult:
    """Run one card command.  Never a shell string; never inherits the env.

    ``capture`` keeps stdout and stderr separate in the returned result (for
    callers such as git plumbing that inspect the output); without it both
    streams are merged into the log file, unchanged.  ``log_path`` is optional
    so a caller that only needs the capture does not have to invent a file.

    ``stdout_log_path``/``stderr_log_path`` (capture mode only) write the two
    streams **raw and separately**, byte for byte, for W3 evidence.  Opening
    either file fails before the command is spawned, so a missing/unwritable
    log directory can never be mistaken for a successful run.
    """

    command = [str(part) for part in argv]
    if not command:
        raise ValueError("command argv must not be empty")
    if not capture and log_path is None:
        raise ValueError("log_path is required when capture is disabled")
    if log_path is not None and (
        stdout_log_path is not None or stderr_log_path is not None
    ):
        raise ValueError("log_path cannot be combined with separate stream logs")
    stream_logs = capture and (
        stdout_log_path is not None or stderr_log_path is not None
    )
    handle = None
    stdout_handle = None
    stderr_handle = None
    log_name = ""
    if log_path is not None:
        target = Path(log_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = open(target, "wb")
        log_name = target.name
    try:
        if stdout_log_path is not None:
            target = Path(stdout_log_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            stdout_handle = open(target, "wb")
        if stderr_log_path is not None:
            target = Path(stderr_log_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            stderr_handle = open(target, "wb")
    except OSError:
        for opened in (handle, stdout_handle, stderr_handle):
            if opened is not None:
                opened.close()
        raise
    started = time.monotonic()
    timed_out = False
    exit_code = -1
    stdout_bytes = b""
    stderr_bytes = b""
    group_empty = True
    group_reason = "empty"
    process: subprocess.Popen[bytes] | None = None
    try:
        if capture:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        else:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        if capture:
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=GROUP_TERM_SECONDS)
                except subprocess.TimeoutExpired:
                    _kill_group(process.pid, signal.SIGKILL)
                    try:
                        process.wait(timeout=GROUP_KILL_SECONDS)
                    except subprocess.TimeoutExpired:  # pragma: no cover - stuck leader
                        pass
                try:
                    stdout_bytes, stderr_bytes = process.communicate(
                        timeout=GROUP_KILL_SECONDS
                    )
                except subprocess.TimeoutExpired:  # pragma: no cover - escaped group
                    pass
                exit_code = (
                    process.returncode if process.returncode is not None else -1
                )
        else:
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(process.pid, signal.SIGTERM)
                try:
                    exit_code = process.wait(timeout=GROUP_TERM_SECONDS)
                except subprocess.TimeoutExpired:
                    _kill_group(process.pid, signal.SIGKILL)
                    try:
                        exit_code = process.wait(timeout=GROUP_KILL_SECONDS)
                    except subprocess.TimeoutExpired:  # pragma: no cover - stuck leader
                        exit_code = -1
    finally:
        if process is not None:
            # D10: the group must be proven empty *before* this call returns.
            # SIGKILL alone is not a witness: a leader may have exited while a
            # descendant still runs, and a descendant may ignore SIGTERM.
            group_empty, group_reason = _ensure_group_empty(process.pid)
        try:
            if handle is not None:
                if capture:
                    handle.write(stdout_bytes)
                    handle.write(stderr_bytes)
                handle.close()
            if stdout_handle is not None:
                stdout_handle.write(stdout_bytes)
                stdout_handle.close()
            if stderr_handle is not None:
                stderr_handle.write(stderr_bytes)
                stderr_handle.close()
        except OSError:
            for opened in (handle, stdout_handle, stderr_handle):
                if opened is not None:
                    try:
                        opened.close()
                    except OSError:
                        pass
            raise
    result = CommandResult(
        argv=tuple(command),
        cwd=str(cwd),
        exit_code=int(exit_code),
        timed_out=timed_out,
        log=log_name,
        duration_seconds=time.monotonic() - started,
        stdout=stdout_bytes.decode("utf-8", "replace"),
        stderr=stderr_bytes.decode("utf-8", "replace"),
    )
    if not group_empty:
        # The logs are already on disk; the caller must record an explicit
        # failure and must not treat this command as successful.
        raise CommandGroupError(group_reason, result=result)
    return result


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(int(pgid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


__all__ = ["CommandGroupError", "CommandResult", "run_command"]
