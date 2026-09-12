"""Tests for the generic background-job infrastructure (store, context, worker)."""

import threading

import jobs
from jobs import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_QUEUED,
    STATE_RUNNING,
    JobContext,
    JobStore,
    JobWorker,
    new_job,
)

TERMINAL = {STATE_COMPLETED, STATE_FAILED}


def _store(tmp_path):
    return JobStore(str(tmp_path), terminal_states=TERMINAL)


# --------------------------------------------------------------------------- #
# JobStore
# --------------------------------------------------------------------------- #


def test_store_save_load_roundtrip(tmp_path):
    store = _store(tmp_path)
    job = new_job("rename_device", {"device_id": "abc"}, job_id="j1")
    store.save(job)
    loaded = store.load("j1")
    assert loaded["job_id"] == "j1"
    assert loaded["type"] == "rename_device"
    assert loaded["payload"] == {"device_id": "abc"}
    assert loaded["version"] == store.schema_version


def test_store_load_missing_returns_none(tmp_path):
    assert _store(tmp_path).load("nope") is None


def test_store_list_unfinished_filters_terminal(tmp_path):
    store = _store(tmp_path)
    store.save(new_job("t", {}, job_id="running") | {"state": STATE_RUNNING})
    store.save(new_job("t", {}, job_id="done") | {"state": STATE_COMPLETED})
    store.save(new_job("t", {}, job_id="failed") | {"state": STATE_FAILED})
    ids = {j["job_id"] for j in store.list_unfinished()}
    assert ids == {"running"}


def test_store_skips_unreadable_file(tmp_path):
    store = _store(tmp_path)
    store.save(new_job("t", {}, job_id="good"))
    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
    ids = {j["job_id"] for j in store.list_jobs()}
    assert ids == {"good"}


def test_store_delete(tmp_path):
    store = _store(tmp_path)
    store.save(new_job("t", {}, job_id="j"))
    store.delete("j")
    assert store.load("j") is None
    store.delete("j")  # deleting a missing job is a no-op


def test_store_atomic_write_leaves_no_tmp(tmp_path):
    store = _store(tmp_path)
    store.save(new_job("t", {}, job_id="j"))
    assert not list(tmp_path.glob("*.tmp"))


# --------------------------------------------------------------------------- #
# JobContext.progress throttling
# --------------------------------------------------------------------------- #


def test_progress_throttles_saves(tmp_path, monkeypatch):
    store = _store(tmp_path)
    job = new_job("t", {}, job_id="j")
    store.save(job)

    saves = {"n": 0}
    original = store.save
    store.save = lambda j: (saves.__setitem__("n", saves["n"] + 1), original(j))[1]

    clock = {"t": 100.0}
    monkeypatch.setattr(jobs.time, "monotonic", lambda: clock["t"])

    ctx = JobContext(job, store, throttle_seconds=0.5)
    ctx.progress(1, 10)  # first call: last_save is 0.0 -> writes
    ctx.progress(2, 10)  # same clock, within throttle -> skipped
    clock["t"] += 1.0
    ctx.progress(3, 10)  # throttle elapsed -> writes
    ctx.progress(10, 10)  # done >= total -> always writes
    assert saves["n"] == 3
    assert store.load("j")["progress"] == {"done": 10, "total": 10, "current": ""}


def test_log_persists_every_line(tmp_path):
    store = _store(tmp_path)
    job = new_job("t", {}, job_id="j")
    store.save(job)
    ctx = JobContext(job, store)
    ctx.log("STEP", "hello")
    ctx.log("STEP", "world")
    log = store.load("j")["log"]
    assert [entry["message"] for entry in log] == ["hello", "world"]
    assert all("ts" in entry for entry in log)


# --------------------------------------------------------------------------- #
# JobWorker
# --------------------------------------------------------------------------- #


def _worker(tmp_path):
    store = _store(tmp_path)
    worker = JobWorker(store)
    return store, worker


def test_worker_runs_handler_to_completion(tmp_path):
    store, worker = _worker(tmp_path)

    async def handler(job, ctx):
        ctx.progress(1, 1, current="x")
        return {"ok": True, "device": job["payload"]["device_id"]}

    worker.register("demo", handler)
    worker.start()
    job = new_job("demo", {"device_id": "d1"}, job_id="j")
    store.save(job)
    worker.enqueue(job)
    worker.queue.join()

    done = store.load("j")
    assert done["state"] == STATE_COMPLETED
    assert done["result"] == {"ok": True, "device": "d1"}
    assert done["progress"] == {"done": 1, "total": 1, "current": "x"}


def test_worker_marks_failure(tmp_path):
    store, worker = _worker(tmp_path)

    async def handler(job, ctx):
        raise ValueError("boom")

    worker.register("demo", handler)
    worker.start()
    job = new_job("demo", {}, job_id="j")
    store.save(job)
    worker.enqueue(job)
    worker.queue.join()

    done = store.load("j")
    assert done["state"] == STATE_FAILED
    assert done["error"] == "boom"
    assert done["log"][-1]["message"] == "boom"


def test_worker_unknown_type_fails_job(tmp_path):
    store, worker = _worker(tmp_path)
    worker.start()
    job = new_job("mystery", {}, job_id="j")
    store.save(job)
    worker.enqueue(job)
    worker.queue.join()

    done = store.load("j")
    assert done["state"] == STATE_FAILED
    assert "No handler" in done["error"]


def test_worker_runs_jobs_serially(tmp_path):
    store, worker = _worker(tmp_path)
    concurrent = {"max": 0, "cur": 0}
    guard = threading.Lock()

    async def handler(job, ctx):
        with guard:
            concurrent["cur"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["cur"])
        with guard:
            concurrent["cur"] -= 1
        return None

    worker.register("demo", handler)
    worker.start()
    for i in range(5):
        job = new_job("demo", {}, job_id=f"j{i}")
        store.save(job)
        worker.enqueue(job)
    worker.queue.join()
    assert concurrent["max"] == 1


# --------------------------------------------------------------------------- #
# reconcile_on_start
# --------------------------------------------------------------------------- #


def test_reconcile_fails_interrupted_jobs(tmp_path):
    store = _store(tmp_path)
    store.save(new_job("t", {}, job_id="queued") | {"state": STATE_QUEUED})
    store.save(new_job("t", {}, job_id="running") | {"state": STATE_RUNNING})
    store.save(new_job("t", {}, job_id="done") | {"state": STATE_COMPLETED})

    JobWorker(store).reconcile_on_start()

    assert store.load("queued")["state"] == STATE_FAILED
    assert store.load("running")["state"] == STATE_FAILED
    assert store.load("running")["error"] == "Interrupted by restart"
    assert store.load("done")["state"] == STATE_COMPLETED
