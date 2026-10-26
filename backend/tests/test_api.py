from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from PIL import Image

from backend.animation import AnimationFailed
from backend.app import create_app
from backend.processors import GfpganProcessor


class FakeAnimationProcessor:
    def __init__(self) -> None:
        self.driving_paths: list[Path] = []
        self.driving_bytes: list[bytes] = []

    def status(self) -> dict[str, object]:
        return {"mode": "liveportrait", "ready": True, "message": "test animation ready"}

    def animate(self, source: Path, driving: Path, destination: Path):
        assert source.is_file()
        assert driving.is_file()
        self.driving_paths.append(driving)
        self.driving_bytes.append(driving.read_bytes())
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"test video")
        return type("Result", (), {"message": "test animation complete"})()


class BlockingAnimationProcessor(FakeAnimationProcessor):
    """Holds every animation until ``release`` is set, recording peak concurrency."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.started = threading.Semaphore(0)
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def animate(self, source: Path, driving: Path, destination: Path):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.started.release()
        try:
            self.release.wait(5)
            return super().animate(source, driving, destination)
        finally:
            with self._lock:
                self.active -= 1


class FailingAnimationProcessor(FakeAnimationProcessor):
    def animate(self, source: Path, driving: Path, destination: Path):
        super().animate(source, driving, destination)
        raise AnimationFailed("动态生成失败，请检查本机 LivePortrait 运行环境。")


def png_upload(name: str = "portrait.png") -> tuple[io.BytesIO, str]:
    image_bytes = io.BytesIO()
    Image.new("RGB", (24, 16), color=(30, 80, 180)).save(image_bytes, format="PNG")
    image_bytes.seek(0)
    return image_bytes, name


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.storage_dir = Path(tempdir.name)
        self.motion_dir = self.storage_dir / "motions"
        self.motion_dir.mkdir()
        (self.motion_dir / "d0.mp4").write_bytes(b"demo video")

    def make_app(self, **overrides: Any):
        config = {
            "TESTING": True,
            "STORAGE_DIR": self.storage_dir,
            "MOTION_DIR": self.motion_dir,
            "ANIMATION_PROCESSOR": FakeAnimationProcessor(),
            **overrides,
        }
        app = create_app(config)
        self.addCleanup(app.extensions["portrait_service"].close)
        return app

    def wait_for_job(self, client: Any, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            response = client.get(f"/api/jobs/{job_id}")
            self.assertEqual(response.status_code, 200)
            job = response.get_json()["job"]
            if job["status"] in {"completed", "failed"}:
                return job
            if time.monotonic() > deadline:
                self.fail(f"job {job_id} is still {job['status']}")
            time.sleep(0.01)

    def submit_animation(self, client: Any, **fields: Any) -> Any:
        return client.post(
            "/api/animate",
            data={"image": png_upload(), "motionId": "d0.mp4", **fields},
            content_type="multipart/form-data",
        )


class ApiHealthTest(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.animation_processor = FakeAnimationProcessor()
        self.app = self.make_app(ANIMATION_PROCESSOR=self.animation_processor)
        self.client = self.app.test_client()

    def test_health_reports_a_ready_api(self) -> None:
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("model", payload)
        self.assertEqual(payload["animation"]["mode"], "liveportrait")
        self.assertEqual(payload["jobs"]["kinds"]["animate"], {"concurrency": 1, "queued": 0, "running": 0})

    def test_restore_rejects_an_unsupported_file(self) -> None:
        response = self.client.post(
            "/api/restore",
            data={"image": (io.BytesIO(b"not an image"), "notes.txt")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "unsupported_file")

    def test_restore_queues_a_job_that_produces_a_preview_result(self) -> None:
        response = self.client.post(
            "/api/restore",
            data={"image": png_upload(), "scale": "2"},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 202)
        submitted = response.get_json()["job"]
        self.assertEqual(submitted["kind"], "restore")
        self.assertIn(submitted["status"], {"queued", "running", "completed"})
        self.assertEqual(response.headers["Location"], f"/api/jobs/{submitted['id']}")

        job = self.wait_for_job(self.client, submitted["id"])
        self.assertEqual(job["status"], "completed")
        self.assertIsNone(job["error"])
        self.assertEqual(job["result"]["model"]["mode"], "preview")
        source = self.client.get(job["result"]["sourceUrl"])
        self.assertEqual(source.status_code, 200)
        source.close()
        result = self.client.get(job["result"]["resultUrl"])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.mimetype, "image/png")
        result.close()

    def test_motion_library_lists_named_local_samples_with_playable_previews(self) -> None:
        response = self.client.get("/api/motions")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["scope"], "local")
        self.assertEqual(
            payload["items"],
            [
                {
                    "id": "d0.mp4",
                    "label": "轻快笑容",
                    "description": "短促侧视与笑容",
                    "duration": "3 秒",
                    "previewUrl": "/media/motions/d0.mp4",
                }
            ],
        )
        preview = self.client.get(payload["items"][0]["previewUrl"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.mimetype, "video/mp4")
        preview.close()

    def test_animate_queues_a_job_that_produces_a_local_video(self) -> None:
        response = self.submit_animation(self.client)

        self.assertEqual(response.status_code, 202)
        job = self.wait_for_job(self.client, response.get_json()["job"]["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["message"], "test animation complete")
        result = self.client.get(job["result"]["resultUrl"])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.mimetype, "video/mp4")
        result.close()

    def test_animate_accepts_an_authorized_custom_driving_video(self) -> None:
        response = self.client.post(
            "/api/animate",
            data={
                "image": png_upload(),
                "drivingVideo": (io.BytesIO(b"custom video"), "my-motion.mp4"),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 202)
        job = self.wait_for_job(self.client, response.get_json()["job"]["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(self.animation_processor.driving_bytes[-1], b"custom video")
        self.assertFalse(self.animation_processor.driving_paths[-1].exists())

    def test_animate_rejects_an_unknown_motion(self) -> None:
        response = self.submit_animation(self.client, motionId="../outside.mp4")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_motion")

    def test_animate_rejects_an_invalid_image_before_starting_the_model(self) -> None:
        response = self.client.post(
            "/api/animate",
            data={"image": (io.BytesIO(b"not an image"), "portrait.png"), "motionId": "d0.mp4"},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_image")
        self.assertEqual(self.animation_processor.driving_paths, [])

    def test_health_allows_the_local_frontend_origin(self) -> None:
        response = self.client.get(
            "/api/health",
            headers={"Origin": "http://127.0.0.1:5173"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://127.0.0.1:5173")

    def test_health_allows_an_explicitly_configured_origin(self) -> None:
        configured_app = self.make_app(
            STORAGE_DIR=self.storage_dir / "configured-runtime",
            CORS_ORIGINS=("https://portrait-lab.example",),
        )
        response = configured_app.test_client().get(
            "/api/health",
            headers={"Origin": "https://portrait-lab.example"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"],
            "https://portrait-lab.example",
        )

    def test_restore_reports_an_unavailable_requested_model(self) -> None:
        unavailable_app = self.make_app(
            STORAGE_DIR=self.storage_dir / "unavailable-runtime",
            PROCESSOR=GfpganProcessor(self.storage_dir / "missing-model.pth"),
        )

        response = unavailable_app.test_client().post(
            "/api/restore",
            data={"image": png_upload()},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"]["code"], "model_unavailable")


class JobQueueApiTest(ApiTestCase):
    def test_an_unknown_job_is_not_found(self) -> None:
        client = self.make_app().test_client()

        for job_id in ("0" * 32, "not-a-job-id"):
            response = client.get(f"/api/jobs/{job_id}")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.get_json()["error"]["code"], "job_not_found")

    def test_restore_rejects_an_invalid_image_before_queueing(self) -> None:
        client = self.make_app().test_client()

        response = client.post(
            "/api/restore",
            data={"image": (io.BytesIO(b"not an image"), "portrait.png")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_image")
        self.assertEqual(list((self.storage_dir / "uploads").iterdir()), [])
        self.assertEqual(client.get("/api/health").get_json()["jobs"]["kinds"]["restore"]["queued"], 0)

    def test_animation_concurrency_limit_queues_extra_jobs_in_order(self) -> None:
        processor = BlockingAnimationProcessor()
        client = self.make_app(ANIMATION_PROCESSOR=processor, ANIMATION_CONCURRENCY=1).test_client()
        self.addCleanup(processor.release.set)

        job_ids = [self.submit_animation(client).get_json()["job"]["id"] for _ in range(3)]
        self.assertTrue(processor.started.acquire(timeout=5))

        jobs = [client.get(f"/api/jobs/{job_id}").get_json()["job"] for job_id in job_ids]
        self.assertEqual([job["status"] for job in jobs], ["running", "queued", "queued"])
        self.assertEqual([job["queuePosition"] for job in jobs], [None, 0, 1])
        animate_load = client.get("/api/health").get_json()["jobs"]["kinds"]["animate"]
        self.assertEqual((animate_load["running"], animate_load["queued"]), (1, 2))

        processor.release.set()
        for job_id in job_ids:
            self.assertEqual(self.wait_for_job(client, job_id)["status"], "completed")
        self.assertEqual(processor.max_active, 1)

    def test_a_long_animation_does_not_block_a_restoration(self) -> None:
        processor = BlockingAnimationProcessor()
        client = self.make_app(ANIMATION_PROCESSOR=processor).test_client()
        self.addCleanup(processor.release.set)
        animation_id = self.submit_animation(client).get_json()["job"]["id"]
        self.assertTrue(processor.started.acquire(timeout=5))

        restore = client.post("/api/restore", data={"image": png_upload()}, content_type="multipart/form-data")

        self.assertEqual(self.wait_for_job(client, restore.get_json()["job"]["id"])["status"], "completed")
        self.assertEqual(client.get(f"/api/jobs/{animation_id}").get_json()["job"]["status"], "running")

    def test_a_full_queue_rejects_new_jobs_and_discards_their_uploads(self) -> None:
        processor = BlockingAnimationProcessor()
        client = self.make_app(ANIMATION_PROCESSOR=processor, MAX_PENDING_JOBS=1).test_client()
        self.addCleanup(processor.release.set)
        first = self.submit_animation(client)
        self.assertEqual(first.status_code, 202)

        response = client.post(
            "/api/animate",
            data={"image": png_upload(), "drivingVideo": (io.BytesIO(b"private"), "mine.mp4")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()["error"]["code"], "queue_full")
        uploads = list((self.storage_dir / "uploads").iterdir())
        self.assertEqual([path.name.split("_")[0] for path in uploads], [first.get_json()["job"]["id"]])
        self.assertEqual(list((self.storage_dir / "private_drivers").rglob("*.mp4")), [])

    def test_a_failed_animation_reports_its_error_and_deletes_the_private_driving_video(self) -> None:
        processor = FailingAnimationProcessor()
        client = self.make_app(ANIMATION_PROCESSOR=processor).test_client()

        response = client.post(
            "/api/animate",
            data={"image": png_upload(), "drivingVideo": (io.BytesIO(b"private"), "mine.mov")},
            content_type="multipart/form-data",
        )

        job = self.wait_for_job(client, response.get_json()["job"]["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "animation_failed")
        self.assertIsNone(job["result"])
        self.assertFalse(processor.driving_paths[-1].exists())

    def test_job_state_survives_a_service_restart(self) -> None:
        blocked = BlockingAnimationProcessor()
        first_service = self.make_app(ANIMATION_PROCESSOR=blocked, ANIMATION_CONCURRENCY=1)
        first_client = first_service.test_client()
        self.addCleanup(blocked.release.set)
        interrupted = first_client.post(
            "/api/animate",
            data={"image": png_upload(), "drivingVideo": (io.BytesIO(b"private"), "mine.mp4")},
            content_type="multipart/form-data",
        ).get_json()["job"]["id"]
        self.assertTrue(blocked.started.acquire(timeout=5))
        waiting = self.submit_animation(first_client).get_json()["job"]["id"]

        # Simulate the first process crashing: the kernel would drop its owner lock.
        # A second service on the same storage then plays the API coming back.
        first_service.extensions["portrait_service"].jobs._owner_lock.release()
        restarted = self.make_app().test_client()

        job = restarted.get(f"/api/jobs/{interrupted}").get_json()["job"]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "interrupted")
        self.assertEqual(list((self.storage_dir / "private_drivers").rglob("*.mp4")), [])
        self.assertEqual(self.wait_for_job(restarted, waiting)["status"], "completed")

        # The stale worker finishing late must not overwrite the recorded outcome.
        with self.assertLogs("backend.jobs", level="INFO") as logs:
            blocked.release.set()
            first_service.extensions["portrait_service"].close()
        self.assertIn(f"Job {interrupted} was finalized by another process", "\n".join(logs.output))
        self.assertEqual(restarted.get(f"/api/jobs/{interrupted}").get_json()["job"]["status"], "failed")

        later = self.make_app().test_client()
        finished = later.get(f"/api/jobs/{waiting}").get_json()["job"]
        self.assertEqual(finished["status"], "completed")
        result = later.get(finished["result"]["resultUrl"])
        self.assertEqual(result.status_code, 200)
        result.close()

    def test_job_events_stream_updates_until_the_job_finishes(self) -> None:
        client = self.make_app().test_client()
        job_id = client.post(
            "/api/restore", data={"image": png_upload()}, content_type="multipart/form-data"
        ).get_json()["job"]["id"]

        response = client.get(f"/api/jobs/{job_id}/events")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/event-stream")
        events = [
            json.loads(line.removeprefix("data: "))["job"]
            for line in response.get_data(as_text=True).splitlines()
            if line.startswith("data: ")
        ]
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[-1]["status"], "completed")
        self.assertTrue(events[-1]["result"]["resultUrl"].startswith("/media/results/"))


if __name__ == "__main__":
    unittest.main()
