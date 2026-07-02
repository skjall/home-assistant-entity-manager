#!/usr/bin/env python3
"""Generic background-job infrastructure for long-running operations.

Long-running registry operations (renaming a device with many entities, batch
restructuring, device swaps) used to run synchronously inside the Flask request.
For large devices that exceeds the Ingress/proxy timeout: the client sees an
error toast while the work silently keeps running to completion with no visible
progress.

This module provides a small, Flask-agnostic building block to run such work as
a tracked background job instead:

* :class:`JobStore` persists jobs as atomic JSON files (like ``SwapJobStore``).
* :func:`new_job` creates the canonical job dict.
* :class:`JobContext` is handed to a job handler so it can report progress and
  log lines that get persisted (throttled) as the job runs.
* :class:`JobWorker` is a single, long-lived worker thread that runs queued jobs
  serially on one asyncio loop, guarded by a shared registry lock so registry
  mutations never overlap with concurrent request handlers.

The module knows nothing about Flask, Home Assistant or the concrete handlers;
those are injected. ``device_swap.SwapJobStore`` is a thin subclass of
:class:`JobStore`.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Generic job states. Swap jobs keep their own richer state machine and are only
# wrapped by a generic job whose state we do not surface to the swap UI.
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"

# States a generic job can no longer progress from (used for reconnect filtering).
TERMINAL_STATES: Set[str] = {STATE_COMPLETED, STATE_FAILED}

# Type of a job handler coroutine: ``async def handler(job, ctx) -> result``.
JobHandler = Callable[[Dict[str, Any], "JobContext"], Awaitable[Any]]


def iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


class JobStore:
    """Persists jobs as individual JSON files in ``storage_dir``.

    Writes are atomic (temp file + ``os.replace``) so a crash mid-write never
    corrupts a job and concurrent readers always see a complete old-or-new file.
    ``terminal_states`` is injected so different job families (generic vs. swap)
    can define which states count as finished.
    """

    def __init__(
        self,
        storage_dir: str,
        *,
        terminal_states: Set[str],
        schema_version: int = 1,
    ) -> None:
        """Create the store, ensuring ``storage_dir`` exists."""
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.terminal_states = set(terminal_states)
        self.schema_version = schema_version

    def _path(self, job_id: str) -> Path:
        """Return the on-disk path for ``job_id`` (basename only, defensively)."""
        safe = os.path.basename(job_id)
        return self.storage_dir / f"{safe}.json"

    def save(self, job: Dict[str, Any]) -> None:
        """Write a job atomically (temp file + ``os.replace``)."""
        job["version"] = self.schema_version
        path = self._path(job["job_id"])
        tmp = path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(job, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"Failed to save job {job.get('job_id')}: {e}")
            raise

    def load(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Load a job by id, or return ``None`` if missing/unreadable."""
        path = self._path(job_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load job {job_id}: {e}")
            return None

    def list_jobs(self) -> List[Dict[str, Any]]:
        """Return all readable jobs, skipping unreadable files."""
        jobs = []
        for path in sorted(self.storage_dir.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    jobs.append(json.load(f))
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Skipping unreadable job {path.name}: {e}")
        return jobs

    def list_unfinished(self) -> List[Dict[str, Any]]:
        """Return all jobs that are not in a terminal state (for reconnect UI)."""
        return [j for j in self.list_jobs() if j.get("state") not in self.terminal_states]

    def delete(self, job_id: str) -> None:
        """Remove a job file if it exists."""
        path = self._path(job_id)
        if path.exists():
            path.unlink()


# --------------------------------------------------------------------------- #
# Job schema + per-run context
# --------------------------------------------------------------------------- #


def new_job(job_type: str, payload: Dict[str, Any], *, job_id: str) -> Dict[str, Any]:
    """Build a fresh generic job dict in the ``queued`` state.

    ``payload`` carries everything the handler needs (already read from the Flask
    request in the request thread, since the worker thread has no request
    context). ``job_id`` is supplied by the caller (a uuid4 hex) so the caller
    can persist and enqueue it without a second lookup.
    """
    now = iso_now()
    return {
        "job_id": job_id,
        "type": job_type,
        "state": STATE_QUEUED,
        "progress": {"done": 0, "total": 0, "current": ""},
        "log": [],
        "payload": payload,
        "result": None,
        "error": None,
        "created": now,
        "updated": now,
    }


class JobContext:
    """Handed to a job handler to report progress and log lines.

    Progress updates are throttled: persisting on every entity of a large device
    would fsync dozens of times. We only write to disk when at least
    ``throttle_seconds`` passed since the last save, or when the job finished
    (``done >= total``), so the final state is always flushed. Log lines are
    always persisted (they are comparatively rare and carry the audit trail).
    """

    def __init__(
        self,
        job: Dict[str, Any],
        store: JobStore,
        *,
        throttle_seconds: float = 0.5,
    ) -> None:
        """Bind the context to a job and its store."""
        self.job = job
        self.store = store
        self._throttle = throttle_seconds
        self._last_save = 0.0

    def progress(self, done: int, total: int, current: str = "") -> None:
        """Update progress and persist it, throttled to avoid excessive writes."""
        self.job["progress"] = {"done": done, "total": total, "current": current}
        self.job["updated"] = iso_now()
        now = time.monotonic()
        if total <= 0 or done >= total or (now - self._last_save) >= self._throttle:
            self._last_save = now
            self.store.save(self.job)

    def log(self, step: str, message: str) -> None:
        """Append a ``{ts, step, message}`` log line and persist the job."""
        self.job.setdefault("log", []).append({"ts": iso_now(), "step": step, "message": message})
        self.job["updated"] = iso_now()
        self.store.save(self.job)


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #


class JobWorker:
    """Single background thread that runs queued jobs serially.

    A ``queue.Queue`` of job ids feeds one daemon thread which owns a single,
    long-lived asyncio loop. Serial execution is intentional: the jobs share
    ``restructurer`` state and mutate the same Home Assistant registry, so only
    one runs at a time. Requests keep serving concurrently (the dev server is
    threaded); ``load_structure`` rebuilds the restructurer by reassignment and
    handlers iterate over snapshots, so a concurrent read does not need a lock.

    Handlers are registered per job ``type`` and are plain coroutines
    ``async def handler(job, ctx) -> result``. Their return value becomes
    ``job["result"]``; a raised exception marks the job ``failed``.
    """

    def __init__(self, store: JobStore) -> None:
        """Create the worker bound to a store."""
        self.store = store
        self.handlers: Dict[str, JobHandler] = {}
        self.queue: "queue.Queue[str]" = queue.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    def register(self, job_type: str, handler: JobHandler) -> None:
        """Register the coroutine handler for a job ``type``."""
        self.handlers[job_type] = handler

    def start(self) -> None:
        """Start the worker thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="job-worker", daemon=True)
        self._thread.start()

    def enqueue(self, job: Dict[str, Any]) -> None:
        """Queue an already-persisted job for execution."""
        self.queue.put(job["job_id"])

    def reconcile_on_start(self) -> None:
        """Fail jobs left ``queued``/``running`` by a previous process.

        Generic jobs (device rename, batch execute, enable-all) are not safely
        resumable mid-flight, so an interrupted one is marked ``failed`` with a
        clear reason instead of showing an eternal spinner after a restart. Swap
        jobs live in their own store and stay resumable; this only touches this
        worker's store.
        """
        for job in self.store.list_jobs():
            if job.get("state") in (STATE_QUEUED, STATE_RUNNING):
                job["state"] = STATE_FAILED
                job["error"] = "Interrupted by restart"
                job["updated"] = iso_now()
                self.store.save(job)
                logger.info("Reconciled interrupted job %s -> failed", job.get("job_id"))

    def _run(self) -> None:
        """Worker thread main loop: own an asyncio loop and drain the queue."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        while True:
            job_id = self.queue.get()
            try:
                self._process(job_id)
            except Exception:  # noqa: BLE001 - the worker thread must never die
                logger.exception("Unexpected error processing job %s", job_id)
            finally:
                self.queue.task_done()

    def _process(self, job_id: str) -> None:
        """Load a queued job and dispatch its handler on the worker loop."""
        job = self.store.load(job_id)
        if job is None:
            logger.warning("Queued job %s vanished before execution", job_id)
            return
        handler = self.handlers.get(job.get("type"))
        if handler is None:
            job["state"] = STATE_FAILED
            job["error"] = f"No handler registered for job type '{job.get('type')}'"
            job["updated"] = iso_now()
            self.store.save(job)
            return
        self._loop.run_until_complete(self._dispatch(job, handler))

    async def _dispatch(self, job: Dict[str, Any], handler: JobHandler) -> None:
        """Run a handler, tracking ``running``/``completed``/``failed`` state."""
        job["state"] = STATE_RUNNING
        job["updated"] = iso_now()
        self.store.save(job)
        ctx = JobContext(job, self.store)
        try:
            job["result"] = await handler(job, ctx)
            job["state"] = STATE_COMPLETED
        except Exception as e:  # noqa: BLE001 - surface any handler failure as job state
            job["state"] = STATE_FAILED
            job["error"] = str(e)
            ctx.log("ERROR", str(e))
            logger.exception("Job %s (%s) failed", job.get("job_id"), job.get("type"))
        job["updated"] = iso_now()
        self.store.save(job)
