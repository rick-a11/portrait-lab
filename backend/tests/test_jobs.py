from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from backend.jobs import (
    COMPLETED,
    FAILED,
    QUEUED,
    RUNNING,
    Job,
    JobError,
    JobManager,
    JobStore,
    QueueFull,
)


class JobStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        self.store = JobStore(self.root / "jobs.sqlite3")

    def test_reads_before_the_first_job_do_not_create_the_database(self) -> None:
        self.assertIsNone(self.store.get("0" * 32))
        self.assertEqual(self.store.with_status(QUEUED), [])
        self.assertEqual(self.store.active_counts(), {})
        self.assertFalse(self.store.path.exists())

    def test_admission_stops_at_the_pending_limit_per_kind(self) -> None:
        admitted = [self.store.insert_if_capacity(f"r{index}", "restore", {}, limit=2, now=1.0) for index in range(3)]
        other_kind = self.store.insert_if_capacity("a0", "animate", {}, limit=2, now=1.0)

        self.assertEqual(admitted, [True, True, False])
        self.assertTrue(other_kind)
        self.assertEqual(self.store.active_counts(), {("restore", QUEUED): 2, ("animate", QUEUED): 1})

    def test_finished_jobs_free_their_pending_slot(self) -> None:
        self.store.insert_if_capacity("first", "restore", {}, limit=1, now=1.0)
        self.store.claim("first", now=2.0)
        self.store.finish("first", COMPLETED, now=3.0, result={"ok": True})

        self.assertTrue(self.store.insert_if_capacity("second", "restore", {}, limit=1, now=4.0))

    def test_a_job_is_claimed_and_finished_at_most_once(self) -> None:
        self.store.insert_if_capacity("job", "restore", {"scale": 2}, limit=5, now=1.0)

        claimed = self.store.claim("job", now=2.0)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual((claimed.status, claimed.started_at, claimed.payload), (RUNNING, 2.0, {"scale": 2}))
        self.assertIsNone(self.store.claim("job", now=3.0))

        self.assertTrue(self.store.finish("job", COMPLETED, now=4.0, result={"value": 1}))
        self.assertFalse(self.store.finish("job", FAILED, now=5.0, error_code="late", error_message="late"))
        finished = self.store.get("job")
        assert finished is not None
        self.assertEqual((finished.status, finished.result, finished.finished_at), (COMPLETED, {"value": 1}, 4.0))
        self.assertTrue(finished.is_terminal)

    def test_queue_position_counts_earlier_queued_jobs_of_the_same_kind(self) -> None:
        for job_id, kind in (("a1", "animate"), ("r1", "restore"), ("a2", "animate"), ("a3", "animate")):
            self.store.insert_if_capacity(job_id, kind, {}, limit=10, now=1.0)
        self.store.claim("a1", now=2.0)

        self.assertEqual(self.store.queue_position("a2"), 0)
        self.assertEqual(self.store.queue_position("a3"), 1)
        self.assertEqual(self.store.queue_position("r1"), 0)


class JobManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        self.store = JobStore(self.root / "jobs.sqlite3")

    def manager(self, handler, **options) -> JobManager:
        manager = JobManager(
            self.store,
            {"restore": handler},
            concurrency=options.pop("concurrency", {"restore": 1}),
            max_pending=options.pop("max_pending", 10),
            **options,
        )
        self.addCleanup(manager.close)
        return manager

    def test_a_submitted_job_runs_in_the_background_and_stores_its_result(self) -> None:
        manager = self.manager(lambda job: {"doubled": job.payload["value"] * 2})

        job = manager.submit("restore", {"value": 21})
        finished = manager.wait(job.id, timeout=5)

        assert finished is not None
        self.assertEqual(finished.status, COMPLETED)
        self.assertEqual(finished.result, {"doubled": 42})

    def test_an_anticipated_error_is_reported_with_its_code(self) -> None:
        def reject(_: Job):
            raise JobError("invalid_image", "文件不是可解析的图片。")

        manager = self.manager(reject)
        finished = manager.wait(manager.submit("restore", {}).id, timeout=5)

        assert finished is not None
        self.assertEqual((finished.status, finished.error_code), (FAILED, "invalid_image"))
        self.assertEqual(finished.error_message, "文件不是可解析的图片。")

    def test_an_unexpected_error_hides_its_details_from_clients(self) -> None:
        def crash(_: Job):
            raise RuntimeError("/private/path/that/should/not/leak")

        manager = self.manager(crash)
        with self.assertLogs("backend.jobs", level="ERROR"):
            finished = manager.wait(manager.submit("restore", {}).id, timeout=5)

        assert finished is not None
        self.assertEqual(finished.error_code, "internal_error")
        self.assertNotIn("private", finished.error_message or "")

    def test_ephemeral_inputs_are_deleted_whether_a_job_succeeds_or_fails(self) -> None:
        def maybe_fail(job: Job):
            if job.payload["fail"]:
                raise JobError("animation_failed", "failed")
            return {}

        manager = self.manager(maybe_fail)
        for fail in (False, True):
            private = self.root / f"private-{fail}.mp4"
            private.write_bytes(b"clip")
            job = manager.submit("restore", {"fail": fail, "ephemeral": [str(private)]})
            manager.wait(job.id, timeout=5)
            self.assertFalse(private.exists())

    def test_submissions_beyond_the_pending_limit_are_rejected(self) -> None:
        release = threading.Event()
        manager = self.manager(lambda job: release.wait(5) and {}, max_pending=2)
        self.addCleanup(release.set)

        manager.submit("restore", {})
        manager.submit("restore", {})
        with self.assertRaises(QueueFull):
            manager.submit("restore", {})
        load = manager.stats()["kinds"]["restore"]
        self.assertEqual(load["queued"] + load["running"], 2)

    def test_recover_fails_interrupted_jobs_and_resumes_queued_ones(self) -> None:
        private = self.root / "driving.mp4"
        private.write_bytes(b"clip")
        self.store.insert_if_capacity("crashed", "restore", {"ephemeral": [str(private)]}, limit=10, now=1.0)
        self.store.claim("crashed", now=2.0, owner="gone")
        # The dead process left its lock file behind, but nobody holds the lock any more.
        stale_lock = self.root / "jobs.sqlite3.owners" / "gone.lock"
        stale_lock.parent.mkdir()
        stale_lock.touch()
        self.store.insert_if_capacity("waiting", "restore", {}, limit=10, now=3.0)
        ran: list[str] = []

        manager = self.manager(lambda job: ran.append(job.id) or {"resumed": True})
        manager.recover()

        resumed = manager.wait("waiting", timeout=5)
        crashed = self.store.get("crashed")
        assert resumed is not None and crashed is not None
        self.assertEqual((resumed.status, resumed.result), (COMPLETED, {"resumed": True}))
        self.assertEqual((crashed.status, crashed.error_code), (FAILED, "interrupted"))
        self.assertFalse(private.exists())
        self.assertFalse(stale_lock.exists())
        self.assertEqual(ran, ["waiting"])

    def test_recover_leaves_jobs_of_a_live_process_running(self) -> None:
        # Two MCP clients each start a server on the same job table.
        release = threading.Event()
        started = threading.Event()
        private = self.root / "driving.mp4"
        private.write_bytes(b"clip")

        def hold(_: Job):
            started.set()
            release.wait(5)
            return {"done": True}

        first = self.manager(hold)
        self.addCleanup(release.set)
        job = first.submit("restore", {"ephemeral": [str(private)]})
        self.assertTrue(started.wait(5))

        second = self.manager(lambda _: {})
        second.recover()

        running = self.store.get(job.id)
        assert running is not None
        self.assertEqual((running.status, running.owner), (RUNNING, first.owner))
        self.assertTrue(private.exists())
        release.set()
        finished = first.wait(job.id, timeout=5)
        assert finished is not None
        self.assertEqual((finished.status, finished.result), (COMPLETED, {"done": True}))
        self.assertFalse(private.exists())

    def test_closing_releases_ownership_of_unfinished_jobs(self) -> None:
        release = threading.Event()
        started = threading.Event()

        def hold(_: Job):
            started.set()
            release.wait(5)
            return {}

        first = self.manager(hold)
        self.addCleanup(release.set)
        job = first.submit("restore", {})
        self.assertTrue(started.wait(5))
        first.close(timeout=0)

        self.manager(lambda _: {}).recover()

        interrupted = self.store.get(job.id)
        assert interrupted is not None
        self.assertEqual((interrupted.status, interrupted.error_code), (FAILED, "interrupted"))

    def test_wait_returns_the_latest_state_when_the_timeout_expires(self) -> None:
        release = threading.Event()
        started = threading.Event()

        def hold(_: Job):
            started.set()
            release.wait(5)
            return {}

        manager = self.manager(hold)
        self.addCleanup(release.set)
        job = manager.submit("restore", {})
        self.assertTrue(started.wait(5))

        latest = manager.wait(job.id, timeout=0.05)

        assert latest is not None
        self.assertEqual(latest.status, RUNNING)

    def test_closing_leaves_unstarted_jobs_queued_for_the_next_start(self) -> None:
        release = threading.Event()
        started = threading.Event()

        def hold(_: Job):
            started.set()
            release.wait(5)
            return {}

        manager = self.manager(hold)
        first = manager.submit("restore", {})
        self.assertTrue(started.wait(5))
        second = manager.submit("restore", {})

        manager.close(timeout=0)  # Stop taking new work while the first job is still running.
        release.set()
        manager.close()

        self.assertEqual(self.store.get(first.id).status, COMPLETED)
        self.assertEqual(self.store.get(second.id).status, QUEUED)


if __name__ == "__main__":
    unittest.main()
