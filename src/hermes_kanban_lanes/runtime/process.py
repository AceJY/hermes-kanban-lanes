"""Executor process lifecycle: spawn, wait, group scan, termination proof.

The term "process group" here means the group created with
``start_new_session=True`` at spawn time (``pgid == pid``).  A process group id
stays valid while any member survives, so the group can still be scanned after
the recorded leader has exited.

Fail-closed: an unreadable start fingerprint or an unreadable process table is
*unknown*, never proof of death.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PROCESS_TABLE_SCAN = "ps -axo pid=,ppid=,pgid=,stat=,comm=,args="


def pid_alive(pid: int | None) -> bool:
    if not pid or int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start_fingerprint(pid: int | None) -> str | None:
    """``ps -o lstart=`` start time; ``None`` when it cannot be read."""

    if not pid or int(pid) <= 0:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip()
    return f"ps-lstart:{value}" if value else None


def process_group_members(pgid: int | None) -> dict[str, Any]:
    """Scan the real process table for live non-zombie members of ``pgid``."""

    empty = {
        "pgid": int(pgid) if pgid else None,
        "method": PROCESS_TABLE_SCAN,
        "members": [],
        "live_members": [],
        "live_pids": [],
        "zombie_pids": [],
    }
    if not pgid:
        return {**empty, "available": False, "error": "no pgid witness"}
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,stat=,comm=,args="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {**empty, "available": False, "error": type(exc).__name__}
    if result.returncode != 0:
        return {**empty, "available": False, "error": f"ps exited {result.returncode}"}

    members: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split(None, 5)
        if len(parts) < 4:
            continue
        try:
            row_pid, row_ppid, row_pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if row_pgid != int(pgid):
            continue
        stat = parts[3]
        members.append(
            {
                "pid": row_pid,
                "ppid": row_ppid,
                "pgid": row_pgid,
                "stat": stat,
                "zombie": stat.upper().startswith("Z"),
                "comm": parts[4] if len(parts) > 4 else "",
                "args": parts[5] if len(parts) > 5 else "",
            }
        )
    live = [member for member in members if not member["zombie"]]
    return {
        **empty,
        "available": True,
        "members": members,
        "live_members": live,
        "live_pids": [member["pid"] for member in live],
        "zombie_pids": [member["pid"] for member in members if member["zombie"]],
        "error": None,
    }


def prove_terminated(
    pid: int | None,
    fingerprint: str | None,
    pgid: int | None,
    *,
    alive_fn: Callable[[int | None], bool] = pid_alive,
    fingerprint_fn: Callable[[int | None], str | None] = start_fingerprint,
    members_fn: Callable[[int | None], dict[str, Any]] = process_group_members,
) -> tuple[bool, dict[str, Any]]:
    """Decide whether a recorded run's whole process tree is gone.

    Terminated requires *both*: the recorded leader is gone (or its start
    fingerprint no longer matches) **and** the recorded group has no live
    (non-zombie) member.
    """

    alive = bool(pid) and bool(alive_fn(int(pid)))
    current = fingerprint_fn(int(pid)) if alive and pid else None
    group = (
        members_fn(int(pgid))
        if pgid
        else {
            "available": False,
            "pgid": None,
            "method": PROCESS_TABLE_SCAN,
            "members": [],
            "live_members": [],
            "live_pids": [],
            "zombie_pids": [],
            "error": "no pgid witness",
        }
    )
    group_checked = bool(pgid) and group.get("available") is True
    group_live_pids = list(group.get("live_pids") or []) if group_checked else []
    descendant_pids = [
        member_pid
        for member_pid in group_live_pids
        if not pid or int(member_pid) != int(pid)
    ]
    group_alive = bool(group_live_pids) if group_checked else None

    if not pid:
        terminated, reason = False, "no-pid-witness"
    elif not pgid:
        terminated, reason = False, "no-pgid-witness"
    elif not group_checked:
        terminated, reason = False, "process-group-unreadable"
    elif not alive:
        if group_alive:
            terminated, reason = False, "leader-dead-descendants-alive"
        else:
            terminated, reason = True, "pid-not-alive"
    elif fingerprint is None:
        terminated, reason = False, "recorded-fingerprint-unavailable"
    elif current is None:
        terminated, reason = False, "current-fingerprint-unreadable"
    elif current == fingerprint:
        terminated, reason = False, "owner-alive-fingerprint-match"
    elif group_alive:
        terminated, reason = False, "pid-reused-descendants-alive"
    else:
        terminated, reason = True, "pid-reused-fingerprint-mismatch"

    return terminated, {
        "pid": pid,
        "alive": alive,
        "pgid": int(pgid) if pgid else None,
        "recorded_fingerprint": fingerprint,
        "current_fingerprint": current,
        "fingerprint_matched": bool(
            alive and fingerprint is not None and current == fingerprint
        ),
        "process_group_checked": group_checked,
        "process_group_method": group.get("method", PROCESS_TABLE_SCAN),
        "process_group_alive": group_alive,
        "group_live_pids": group_live_pids,
        "descendants_alive": bool(descendant_pids) if group_checked else None,
        "descendant_pids": descendant_pids,
        "terminated": terminated,
        "reason": reason,
    }


@dataclass
class ManagedChild:
    """One executor process started in its own session/process group."""

    name: str
    argv: list[str]
    env: dict[str, str]
    cwd: str
    stdout_path: str
    stderr_path: str
    process: subprocess.Popen | None = field(default=None, repr=False)
    pid: int | None = None
    pgid: int | None = None
    fingerprint: str | None = None
    argv_sha256: str = ""
    started_at: float = 0.0
    _stdout_handle: Any = field(default=None, repr=False)
    _stderr_handle: Any = field(default=None, repr=False)

    @property
    def returncode(self) -> int | None:
        if self.process is None:
            return None
        return self.process.poll()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int | None:
        if self.process is None:
            return None
        try:
            return self.process.wait(timeout=max(0.0, float(timeout)))
        except subprocess.TimeoutExpired:
            return None

    def close_streams(self) -> None:
        for handle in (self._stdout_handle, self._stderr_handle):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        self._stdout_handle = None
        self._stderr_handle = None

    def stderr_tail(self, limit: int = 4000) -> str:
        try:
            with open(self.stderr_path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return ""
        return text[-limit:]


def spawn(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: str,
    stdout_path: str,
    stderr_path: str,
    name: str = "executor",
) -> ManagedChild:
    """Start ``argv`` in a new session and record pid/pgid/fingerprint."""

    Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
    Path(stderr_path).parent.mkdir(parents=True, exist_ok=True)
    stdout_handle = open(stdout_path, "ab")
    stderr_handle = open(stderr_path, "ab")
    try:
        process = subprocess.Popen(
            [str(part) for part in argv],
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
    except OSError:
        stdout_handle.close()
        stderr_handle.close()
        raise
    child = ManagedChild(
        name=name,
        argv=[str(part) for part in argv],
        env=dict(env),
        cwd=str(cwd),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        process=process,
        pid=process.pid,
        pgid=process.pid,
        argv_sha256=hashlib.sha256(
            "\x00".join(str(part) for part in argv).encode("utf-8")
        ).hexdigest(),
        started_at=time.time(),
        _stdout_handle=stdout_handle,
        _stderr_handle=stderr_handle,
    )
    deadline = time.monotonic() + 0.5
    while child.fingerprint is None and time.monotonic() < deadline:
        child.fingerprint = start_fingerprint(child.pid)
        if child.fingerprint is None:
            time.sleep(0.02)
    return child


def _kill_group(pgid: int | None, sig: int) -> None:
    if not pgid:
        return
    try:
        os.killpg(int(pgid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def terminate(
    child: ManagedChild,
    *,
    grace_seconds: float = 5.0,
    kill_grace_seconds: float = 3.0,
    members_fn: Callable[[int | None], dict[str, Any]] = process_group_members,
    alive_fn: Callable[[int | None], bool] = pid_alive,
    fingerprint_fn: Callable[[int | None], str | None] = start_fingerprint,
) -> dict[str, Any]:
    """SIGTERM -> grace -> SIGKILL the whole group, then prove it is gone."""

    pgid = child.pgid or child.pid
    killed = False
    if child.process is not None and child.process.poll() is None:
        _kill_group(pgid, signal.SIGTERM)
        child.wait(grace_seconds)
        if child.process.poll() is None:
            _kill_group(pgid, signal.SIGKILL)
            killed = True
            child.wait(kill_grace_seconds)
    else:
        # Leader already exited; descendants may still be alive.
        group = members_fn(pgid)
        if group.get("available") and group.get("live_pids"):
            _kill_group(pgid, signal.SIGTERM)

    deadline = time.monotonic() + max(0.1, kill_grace_seconds)
    group = members_fn(pgid)
    while time.monotonic() < deadline and group.get("available") and group.get("live_pids"):
        _kill_group(pgid, signal.SIGKILL)
        killed = True
        child.wait(0.1)
        time.sleep(0.05)
        group = members_fn(pgid)

    terminated, witness = prove_terminated(
        child.pid,
        child.fingerprint,
        pgid,
        alive_fn=alive_fn,
        fingerprint_fn=fingerprint_fn,
        members_fn=members_fn,
    )
    child.close_streams()
    return {
        "terminated": terminated,
        "residual_pids": list(witness.get("group_live_pids") or []),
        "killed": killed,
        "witness": witness,
    }


__all__ = [
    "ManagedChild",
    "PROCESS_TABLE_SCAN",
    "pid_alive",
    "process_group_members",
    "prove_terminated",
    "spawn",
    "start_fingerprint",
    "terminate",
]
