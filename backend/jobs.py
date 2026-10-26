from __future__ import annotations

import fcntl
import json
import logging
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from uuid import uuid4


LOGGER = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
TERMINAL_STATUSES = frozenset({COMPLETED, FAILED})

INTERRUPTED_MESSAGE = "服务在任务运行期间重启，任务已中断，请重新提交。"
UNEXPECTED_MESSAGE = "任务执行时发生意外错误。"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    result TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    updated_at REAL NOT NULL,
    owner TEXT
);
CREATE INDEX IF NOT EXISTS jobs_by_kind_and_status ON jobs (kind, status);
"""


class JobError(Exception):
    """An anticipated failure that is reported to the client as the job's error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class QueueFull(RuntimeError):
    """Raised when a job kind already holds the maximum number of pending jobs."""


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    status: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    created_at: float
    started_at: float | None
    finished_at: float | None
    updated_at: float
    owner: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class OwnerLock:
    """An exclusive ``flock`` held by a process for as long as it runs jobs.

    The kernel drops the lock when its process exits, even on SIGKILL, so a
    lock that someone else can take means the process that took it is gone.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._handle = path.open("ab")
        fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self._handle.closed:
            return
        self.path.unlink(missing_ok=True)
        fcntl.flock(self._handle, fcntl.LOCK_UN)
        self._handle.close()

    @staticmethod
    def is_held(path: Path) -> bool:
        """True while a live process holds the lock at ``path``; a stale lock file is removed."""
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            return False
        with handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            path.unlink(missing_ok=True)
            return False


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        status=row["status"],
        payload=json.loads(row["payload"]),
        result=json.loads(row["result"]) if row["result"] else None,
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        updated_at=row["updated_at"],
        owner=row["owner"],
    )


class JobStore:
    """SQLite table of jobs, so queued and finished work survives a restart.

    Every call opens its own short-lived connection: worker threads never share
    one, and WAL mode lets status reads proceed while a worker is writing.
    Status changes are guarded with ``WHERE status = ...`` so a job is claimed
    and finalized at most once, even if two processes point at the same file.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with self._schema_lock:
                if not self._schema_ready:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.executescript(SCHEMA)
                    self._schema_ready = True
            with connection:
                yield connection
        finally:
            connection.close()

    def insert_if_capacity(
        self,
        job_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        limit: int,
        now: float,
    ) -> bool:
        """Admit a queued job only while its kind has fewer than ``limit`` pending jobs.

        The capacity check and the insert are one statement, so concurrent
        submissions cannot both slip past the limit.
        """
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs (id, kind, status, payload, created_at, updated_at)
                SELECT ?, ?, ?, ?, ?, ?
                WHERE (SELECT COUNT(*) FROM jobs WHERE kind = ? AND status IN (?, ?)) < ?
                """,
                (job_id, kind, QUEUED, json.dumps(dict(payload)), now, now, kind, QUEUED, RUNNING, limit),
            )
            return cursor.rowcount == 1

    def get(self, job_id: str) -> Job | None:
        if not self.path.exists():
            return None
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job_from_row(row) if row else None

    def with_status(self, status: str) -> list[Job]:
        if not self.path.exists():
            return []
        with self._transaction() as connection:
            rows = connection.execute("SELECT * FROM jobs WHERE status = ? ORDER BY rowid", (status,)).fetchall()
        return [_job_from_row(row) for row in rows]

    def claim(self, job_id: str, *, now: float, owner: str | None = None) -> Job | None:
        """Move a queued job to running for ``owner``; ``None`` means someone else already took it."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, started_at = ?, updated_at = ?, owner = ? WHERE id = ? AND status = ?",
                (RUNNING, now, now, owner, job_id, QUEUED),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job_from_row(row)

    def finish(
        self,
        job_id: str,
        status: str,
        *,
        now: float,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Record the outcome of a running job; ``False`` if it was already finalized."""
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, result = ?, error_code = ?, error_message = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    status,
                    json.dumps(dict(result)) if result is not None else None,
                    error_code,
                    error_message,
                    now,
                    now,
                    job_id,
                    RUNNING,
                ),
            )
            return cursor.rowcount == 1

    def queue_position(self, job_id: str) -> int:
        """Number of jobs of the same kind that were queued before this one."""
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM jobs AS ahead, jobs AS target
                WHERE target.id = ? AND ahead.kind = target.kind
                  AND ahead.status = ? AND ahead.rowid < target.rowid
                """,
                (job_id, QUEUED),
            ).fetchone()
        return int(row[0])

    def active_counts(self) -> dict[tuple[str, str], int]:
        if not self.path.exists():
            return {}
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT kind, status, COUNT(*) FROM jobs WHERE status IN (?, ?) GROUP BY kind, status",
                (QUEUED, RUNNING),
            ).fetchall()
        return {(row[0], row[1]): int(row[2]) for row in rows}


Handler = Callable[[Job], Mapping[str, Any]]


class JobManager:
    """Run persisted jobs on small per-kind worker pools.

    Each kind gets its own pool so a 20-minute LivePortrait render never
    blocks a few-second restoration. Workers are daemon threads: stopping the
    service never waits for a model. Each running job records the process
    that claimed it, and that process holds an ``OwnerLock`` while it lives.
    ``recover()`` marks a running job interrupted only once its owner is gone,
    instead of retrying it, so a job that crashes the process cannot crash it
    again in a loop, and a second process sharing the table (two MCP clients,
    say) never interrupts the first one's live work.
    """

    def __init__(
        self,
        store: JobStore,
        handlers: Mapping[str, Handler],
        *,
        concurrency: Mapping[str, int],
        max_pending: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.handlers = dict(handlers)
        self.concurrency = {kind: max(1, int(concurrency.get(kind, 1))) for kind in self.handlers}
        self.max_pending = max(1, int(max_pending))
        self._clock = clock
        self._lock = threading.Lock()
        self._queues: dict[str, queue.Queue[str | None]] = {}
        self._workers: list[threading.Thread] = []
        self._closed = False
        self._changed = threading.Condition()
        self._version = 0
        self.owner = uuid4().hex
        self._owner_lock: OwnerLock | None = None

    @property
    def version(self) -> int:
        """Counter bumped on every state change made by this process."""
        with self._changed:
            return self._version

    def submit(self, kind: str, payload: Mapping[str, Any], *, job_id: str | None = None) -> Job:
        if kind not in self.handlers:
            raise ValueError(f"Unknown job kind: {kind}")
        job_id = job_id or uuid4().hex
        if not self.store.insert_if_capacity(job_id, kind, payload, limit=self.max_pending, now=self._clock()):
            raise QueueFull(kind)
        self._notify()
        self._dispatch(kind, job_id)
        job = self.store.get(job_id)
        assert job is not None
        return job

    def get(self, job_id: str) -> Job | None:
        return self.store.get(job_id)

    def queue_position(self, job: Job) -> int | None:
        return self.store.queue_position(job.id) if job.status == QUEUED else None

    def stats(self) -> dict[str, Any]:
        counts = self.store.active_counts()
        return {
            "maxPending": self.max_pending,
            "kinds": {
                kind: {
                    "concurrency": self.concurrency[kind],
                    "queued": counts.get((kind, QUEUED), 0),
                    "running": counts.get((kind, RUNNING), 0),
                }
                for kind in self.handlers
            },
        }

    def recover(self) -> None:
        """Resume queued jobs and fail running jobs whose process has exited."""
        for job in self.store.with_status(RUNNING):
            if job.owner and OwnerLock.is_held(self._owner_lock_path(job.owner)):
                continue
            if self.store.finish(
                job.id,
                FAILED,
                now=self._clock(),
                error_code="interrupted",
                error_message=INTERRUPTED_MESSAGE,
            ):
                self._discard_ephemeral(job)
        for job in self.store.with_status(QUEUED):
            if job.kind in self.handlers:
                self._dispatch(job.kind, job.id)
        self._notify()

    def wait_for_change(self, since: int, timeout: float) -> int:
        """Block until this process changes any job after ``since``, or ``timeout`` passes."""
        with self._changed:
            self._changed.wait_for(lambda: self._version != since, timeout=max(0.0, timeout))
            return self._version

    def wait(self, job_id: str, timeout: float) -> Job | None:
        """Return the job once it is finished, or its latest state after ``timeout``."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            version = self.version
            job = self.store.get(job_id)
            remaining = deadline - time.monotonic()
            if job is None or job.is_terminal or remaining <= 0:
                return job
            # Re-read at least once a second in case another process owns the job.
            self.wait_for_change(version, min(remaining, 1.0))

    def close(self, timeout: float = 5.0) -> None:
        """Stop the workers; jobs that have not started stay queued for the next start."""
        with self._lock:
            self._closed = True
            pools = [(pending, self.concurrency[kind]) for kind, pending in self._queues.items()]
            workers = list(self._workers)
        for pending, size in pools:
            for _ in range(size):
                pending.put(None)
        for worker in workers:
            worker.join(timeout)
        with self._lock:
            owner_lock, self._owner_lock = self._owner_lock, None
        if owner_lock is not None:
            # From here on, another process may treat this one's unfinished jobs as interrupted.
            owner_lock.release()

    def _owner_lock_path(self, owner: str) -> Path:
        return self.store.path.with_name(f"{self.store.path.name}.owners") / f"{owner}.lock"

    def _claim(self, job_id: str) -> Job | None:
        with self._lock:
            if self._closed:
                return None
            if self._owner_lock is None:
                self._owner_lock = OwnerLock(self._owner_lock_path(self.owner))
        return self.store.claim(job_id, now=self._clock(), owner=self.owner)

    def _dispatch(self, kind: str, job_id: str) -> None:
        with self._lock:
            if self._closed:
                return
            pending = self._queues.get(kind)
            if pending is None:
                pending = self._queues[kind] = queue.Queue()
                for index in range(self.concurrency[kind]):
                    worker = threading.Thread(
                        target=self._work,
                        args=(pending,),
                        name=f"portrait-lab-{kind}-{index + 1}",
                        daemon=True,
                    )
                    worker.start()
                    self._workers.append(worker)
        pending.put(job_id)

    def _work(self, pending: queue.Queue[str | None]) -> None:
        while True:
            job_id = pending.get()
            if job_id is None or self._closed:
                return
            try:
                self._execute(job_id)
            except Exception:
                # A storage failure must not silently shrink the pool.
                LOGGER.exception("Worker could not record job %s", job_id)

    def _execute(self, job_id: str) -> None:
        job = self._claim(job_id)
        if job is None:
            return
        self._notify()
        try:
            try:
                result = dict(self.handlers[job.kind](job))
            except JobError as exc:
                finalized = self.store.finish(
                    job.id, FAILED, now=self._clock(), error_code=exc.code, error_message=exc.message
                )
            except Exception:
                LOGGER.exception("Job %s failed unexpectedly", job.id)
                finalized = self.store.finish(
                    job.id, FAILED, now=self._clock(), error_code="internal_error", error_message=UNEXPECTED_MESSAGE
                )
            else:
                finalized = self.store.finish(job.id, COMPLETED, now=self._clock(), result=result)
            if not finalized:
                LOGGER.info("Job %s was finalized by another process; its outcome was discarded", job.id)
        finally:
            self._discard_ephemeral(job)
            self._notify()

    def _discard_ephemeral(self, job: Job) -> None:
        """Delete inputs that must not outlive their job, such as a private driving video.

        ``payload["ephemeral"]`` lists absolute paths; they are removed however
        the job ends, including when ``recover()`` marks it interrupted.
        """
        for path in job.payload.get("ephemeral", []):
            Path(path).unlink(missing_ok=True)

    def _notify(self) -> None:
        with self._changed:
            self._version += 1
            self._changed.notify_all()
