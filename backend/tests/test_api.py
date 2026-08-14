from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

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


class ApiHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.motion_dir = Path(self.tempdir.name) / "motions"
        self.motion_dir.mkdir()
        (self.motion_dir / "d0.mp4").write_bytes(b"demo video")
        self.animation_processor = FakeAnimationProcessor()
        self.app = create_app(
            {
                "TESTING": True,
                "STORAGE_DIR": Path(self.tempdir.name),
                "MOTION_DIR": self.motion_dir,
                "ANIMATION_PROCESSOR": self.animation_processor,
            }
        )
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_health_reports_a_ready_api(self) -> None:
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("model", payload)
        self.assertEqual(payload["animation"]["mode"], "liveportrait")

    def test_restore_rejects_an_unsupported_file(self) -> None:
        response = self.client.post(
            "/api/restore",
            data={"image": (io.BytesIO(b"not an image"), "notes.txt")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "unsupported_file")

    def test_restore_creates_a_preview_result_for_a_valid_image(self) -> None:
        image_bytes = io.BytesIO()
        Image.new("RGB", (24, 16), color=(30, 80, 180)).save(image_bytes, format="PNG")
        image_bytes.seek(0)

        response = self.client.post(
            "/api/restore",
            data={"image": (image_bytes, "portrait.png"), "scale": "2"},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["job"]["status"], "completed")
        self.assertEqual(payload["model"]["mode"], "preview")
        result = self.client.get(payload["resultUrl"])
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

    def test_animate_creates_a_local_video_result(self) -> None:
        image_bytes = io.BytesIO()
        Image.new("RGB", (24, 16), color=(30, 80, 180)).save(image_bytes, format="PNG")
        image_bytes.seek(0)

        response = self.client.post(
            "/api/animate",
            data={"image": (image_bytes, "portrait.png"), "motionId": "d0.mp4"},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["job"]["status"], "completed")
        result = self.client.get(payload["resultUrl"])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.mimetype, "video/mp4")
        result.close()

    def test_animate_accepts_an_authorized_custom_driving_video(self) -> None:
        image_bytes = io.BytesIO()
        Image.new("RGB", (24, 16), color=(30, 80, 180)).save(image_bytes, format="PNG")
        image_bytes.seek(0)

        response = self.client.post(
            "/api/animate",
            data={
                "image": (image_bytes, "portrait.png"),
                "drivingVideo": (io.BytesIO(b"custom video"), "my-motion.mp4"),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["job"]["status"], "completed")
        self.assertEqual(self.animation_processor.driving_bytes[-1], b"custom video")
        self.assertFalse(self.animation_processor.driving_paths[-1].exists())

    def test_animate_rejects_an_unknown_motion(self) -> None:
        image_bytes = io.BytesIO()
        Image.new("RGB", (24, 16), color=(30, 80, 180)).save(image_bytes, format="PNG")
        image_bytes.seek(0)

        response = self.client.post(
            "/api/animate",
            data={"image": (image_bytes, "portrait.png"), "motionId": "../outside.mp4"},
            content_type="multipart/form-data",
        )

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

    def test_health_allows_the_local_frontend_origin(self) -> None:
        response = self.client.get(
            "/api/health",
            headers={"Origin": "http://127.0.0.1:5173"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://127.0.0.1:5173")

    def test_health_allows_an_explicitly_configured_origin(self) -> None:
        configured_app = create_app(
            {
                "TESTING": True,
                "CORS_ORIGINS": ("https://portrait-lab.example",),
            }
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
        unavailable_app = create_app(
            {
                "TESTING": True,
                "STORAGE_DIR": Path(self.tempdir.name) / "unavailable-runtime",
                "PROCESSOR": GfpganProcessor(Path(self.tempdir.name) / "missing-model.pth"),
            }
        )
        image_bytes = io.BytesIO()
        Image.new("RGB", (8, 8), color="white").save(image_bytes, format="PNG")
        image_bytes.seek(0)

        response = unavailable_app.test_client().post(
            "/api/restore",
            data={"image": (image_bytes, "portrait.png")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"]["code"], "model_unavailable")


if __name__ == "__main__":
    unittest.main()
