from __future__ import annotations

import io
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters
from PIL import Image

from backend.mcp_server import build_server
from backend.processors import PreviewProcessor
from backend.service import PortraitService
from backend.tests.test_api import BlockingAnimationProcessor, FakeAnimationProcessor


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The SDK logs every anticipated ToolError at INFO; these tests trigger several on purpose.
logging.getLogger("mcp").setLevel(logging.WARNING)


def write_png(path: Path) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (24, 16), color=(30, 80, 180)).save(buffer, format="PNG")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue())
    return buffer.getvalue()


def tool_payload(result: Any) -> Any:
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def tool_error(result: Any) -> str:
    assert result.is_error, result
    return result.content[0].text


class McpToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        self.motion_dir = self.root / "motions"
        self.motion_dir.mkdir()
        (self.motion_dir / "d0.mp4").write_bytes(b"demo video")
        self.photo = self.root / "inbox" / "portrait.png"
        self.photo_bytes = write_png(self.photo)

    def make_server(self, animation_processor: Any | None = None) -> Any:
        self.service = PortraitService(
            storage_dir=self.root / "runtime",
            motion_dir=self.motion_dir,
            processor=PreviewProcessor(),
            animation_processor=animation_processor or FakeAnimationProcessor(),
        )
        self.addCleanup(self.service.close)
        return build_server(self.service)

    async def test_exposes_the_portrait_lab_tools_with_typed_inputs(self) -> None:
        async with Client(self.make_server()) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}

        self.assertEqual(
            set(tools),
            {"health_check", "list_motions", "restore_photo", "animate_portrait", "get_job"},
        )
        restore_schema = tools["restore_photo"].input_schema
        self.assertEqual(restore_schema["required"], ["image_path"])
        self.assertEqual((restore_schema["properties"]["scale"]["minimum"], restore_schema["properties"]["scale"]["maximum"]), (1, 4))
        self.assertIn("consent_confirmed", tools["animate_portrait"].input_schema["required"])
        self.assertTrue(tools["health_check"].annotations.read_only_hint)
        self.assertFalse(tools["restore_photo"].annotations.read_only_hint)

    async def test_health_check_reports_models_and_queue_load(self) -> None:
        async with Client(self.make_server()) as client:
            health = tool_payload(await client.call_tool("health_check", {}))

        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["model"]["mode"], "preview")
        self.assertEqual(health["animation"]["mode"], "liveportrait")
        self.assertEqual(health["jobs"]["kinds"]["animate"]["concurrency"], 1)

    async def test_list_motions_returns_ids_that_animate_portrait_accepts(self) -> None:
        async with Client(self.make_server()) as client:
            motions = tool_payload(await client.call_tool("list_motions", {}))

        self.assertEqual(motions["count"], 1)
        self.assertEqual(motions["items"][0]["id"], "d0.mp4")
        self.assertEqual(motions["items"][0]["label"], "轻快笑容")
        self.assertEqual(Path(motions["items"][0]["path"]), (self.motion_dir / "d0.mp4").resolve())

    async def test_restore_photo_waits_for_the_result_and_reports_progress(self) -> None:
        progress: list[tuple[float, float | None, str | None]] = []

        async def on_progress(value: float, total: float | None, message: str | None) -> None:
            progress.append((value, total, message))

        async with Client(self.make_server()) as client:
            result = await client.call_tool(
                "restore_photo", {"image_path": str(self.photo), "scale": 2}, progress_callback=on_progress
            )

        job = tool_payload(result)
        self.assertFalse(result.is_error)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["model"]["mode"], "preview")
        restored = Path(job["result"]["resultPath"])
        self.assertTrue(restored.is_file())
        with Image.open(restored) as opened:
            self.assertEqual(opened.format, "PNG")
        self.assertEqual(self.photo.read_bytes(), self.photo_bytes)
        self.assertNotIn("next", job)
        self.assertEqual(progress[-1][:2], (2, 2))
        self.assertEqual([value for value, _, _ in progress], sorted({value for value, _, _ in progress}))

    async def test_restore_photo_explains_unusable_inputs(self) -> None:
        notes = self.root / "inbox" / "notes.txt"
        notes.write_text("not an image")

        async with Client(self.make_server()) as client:
            relative = await client.call_tool("restore_photo", {"image_path": "portrait.png"})
            missing = await client.call_tool("restore_photo", {"image_path": str(self.root / "absent.png")})
            unsupported = await client.call_tool("restore_photo", {"image_path": str(notes)})
            out_of_range = await client.call_tool("restore_photo", {"image_path": str(self.photo), "scale": 9})

        self.assertIn("[invalid_path]", tool_error(relative))
        self.assertIn("[missing_file]", tool_error(missing))
        self.assertIn("[unsupported_file]", tool_error(unsupported))
        self.assertTrue(out_of_range.is_error)

    async def test_animate_portrait_requires_confirmed_consent(self) -> None:
        async with Client(self.make_server()) as client:
            result = await client.call_tool(
                "animate_portrait",
                {"image_path": str(self.photo), "motion_id": "d0.mp4", "consent_confirmed": False},
            )

        self.assertIn("[consent_required]", tool_error(result))
        self.assertEqual(self.service.jobs.stats()["kinds"]["animate"]["queued"], 0)

    async def test_animate_portrait_returns_a_job_that_get_job_follows_to_completion(self) -> None:
        processor = BlockingAnimationProcessor()
        server = self.make_server(processor)
        self.addCleanup(processor.release.set)

        async with Client(server) as client:
            submitted = tool_payload(
                await client.call_tool(
                    "animate_portrait",
                    {"image_path": str(self.photo), "motion_id": "d0.mp4", "consent_confirmed": True},
                )
            )
            self.assertIn(submitted["status"], {"queued", "running"})
            self.assertIn("get_job", submitted["next"])

            still_running = tool_payload(
                await client.call_tool("get_job", {"job_id": submitted["id"], "wait_seconds": 0.1})
            )
            self.assertIn(still_running["status"], {"queued", "running"})

            processor.release.set()
            finished = tool_payload(await client.call_tool("get_job", {"job_id": submitted["id"], "wait_seconds": 5}))

        self.assertEqual(finished["status"], "completed")
        self.assertTrue(Path(finished["result"]["resultPath"]).is_file())

    async def test_a_private_driving_video_is_copied_for_one_job_and_then_deleted(self) -> None:
        processor = FakeAnimationProcessor()
        driving = self.root / "inbox" / "mine.mp4"
        driving.write_bytes(b"my motion")

        async with Client(self.make_server(processor)) as client:
            job = tool_payload(
                await client.call_tool(
                    "animate_portrait",
                    {
                        "image_path": str(self.photo),
                        "driving_video_path": str(driving),
                        "consent_confirmed": True,
                        "wait_seconds": 5,
                    },
                )
            )

        self.assertEqual(job["status"], "completed")
        self.assertEqual(processor.driving_bytes, [b"my motion"])
        self.assertNotEqual(processor.driving_paths[0], driving)
        self.assertFalse(processor.driving_paths[0].exists())
        self.assertEqual(driving.read_bytes(), b"my motion")

    async def test_get_job_reports_an_unknown_id(self) -> None:
        async with Client(self.make_server()) as client:
            result = await client.call_tool("get_job", {"job_id": "0" * 32, "wait_seconds": 0})

        self.assertIn("[job_not_found]", tool_error(result))


class McpStdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_server_answers_over_stdio(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            photo = root / "portrait.png"
            write_png(photo)
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "backend.mcp_server"],
                cwd=PROJECT_ROOT,
                env={
                    "PYTHONPATH": str(PROJECT_ROOT),
                    "MODEL_MODE": "preview",
                    "STORAGE_DIR": str(root / "runtime"),
                    "MOTION_DIR": str(root / "motions"),
                },
            )

            async with Client(parameters, read_timeout_seconds=30) as client:
                tools = {tool.name for tool in (await client.list_tools()).tools}
                health = tool_payload(await client.call_tool("health_check", {}))
                job = tool_payload(await client.call_tool("restore_photo", {"image_path": str(photo)}))

            self.assertIn("restore_photo", tools)
            self.assertEqual(health["model"]["mode"], "preview")
            self.assertEqual(job["status"], "completed")
            self.assertTrue(Path(job["result"]["resultPath"]).is_file())
            self.assertTrue((root / "runtime" / "mcp-jobs.sqlite3").is_file())


if __name__ == "__main__":
    unittest.main()
