from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from PIL import Image
from werkzeug.utils import secure_filename

from backend.animation import AnimationFailed, AnimationUnavailable, LivePortraitProcessor
from backend.jobs import COMPLETED, FAILED, Job, JobError, JobManager, JobStore, QueueFull
from backend.motions import motion_sort_key
from backend.processors import GfpganProcessor, InvalidImage, ModelUnavailable, PreviewProcessor


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mov", "webm"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_DRIVING_VIDEO_BYTES = 64 * 1024 * 1024
RESTORE = "restore"
ANIMATE = "animate"
JOB_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


def runtime_config() -> dict[str, Any]:
    """Settings shared by the HTTP API and the MCP server, read from the environment."""
    return {
        "STORAGE_DIR": Path(os.getenv("STORAGE_DIR", PROJECT_ROOT / "backend" / "runtime")),
        "MOTION_DIR": Path(
            os.getenv(
                "MOTION_DIR",
                PROJECT_ROOT / ".runtime" / "liveportrait" / "assets" / "examples" / "driving",
            )
        ),
        "MODEL_MODE": os.getenv("MODEL_MODE", "preview").strip().lower(),
        "MODEL_PATH": Path(os.getenv("GFPGAN_MODEL_PATH", PROJECT_ROOT / "models" / "GFPGANv1.4.pth")),
        "LIVEPORTRAIT_ROOT": Path(os.getenv("LIVEPORTRAIT_ROOT", PROJECT_ROOT / ".runtime" / "liveportrait")),
        "LIVEPORTRAIT_PYTHON": Path(
            os.getenv("LIVEPORTRAIT_PYTHON", PROJECT_ROOT / ".venv-liveportrait" / "bin" / "python")
        ),
        "LIVEPORTRAIT_WEIGHTS": Path(
            os.getenv(
                "LIVEPORTRAIT_WEIGHTS_DIR",
                PROJECT_ROOT / ".runtime" / "liveportrait" / "pretrained_weights",
            )
        ),
        # Defaults to <STORAGE_DIR>/jobs.sqlite3 when unset.
        "JOB_DB_PATH": Path(os.environ["JOB_DB_PATH"]) if os.getenv("JOB_DB_PATH") else None,
        "RESTORE_CONCURRENCY": int(os.getenv("RESTORE_CONCURRENCY", "2")),
        "ANIMATION_CONCURRENCY": int(os.getenv("ANIMATION_CONCURRENCY", "1")),
        "MAX_PENDING_JOBS": int(os.getenv("MAX_PENDING_JOBS", "16")),
        "PROCESSOR": None,
        "ANIMATION_PROCESSOR": None,
    }


class SubmissionRejected(Exception):
    """A request that fails validation before any job is queued."""

    def __init__(self, message: str, code: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class IncomingFile(Protocol):
    """A user-supplied file: a multipart upload over HTTP or a local path over MCP."""

    @property
    def filename(self) -> str: ...

    def size(self) -> int: ...

    def save(self, destination: Path) -> None: ...


@dataclass(frozen=True)
class LocalFile:
    path: Path

    @property
    def filename(self) -> str:
        return self.path.name

    def size(self) -> int:
        return self.path.stat().st_size

    def save(self, destination: Path) -> None:
        shutil.copyfile(self.path, destination)


def extension_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def iso_time(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class PortraitService:
    """Validation, staging, and background execution behind the API and MCP tools.

    Requests are validated and their files copied into UUID-named paths
    synchronously, so bad input still fails fast with a precise error. Only the
    model work itself is queued.
    """

    def __init__(
        self,
        *,
        storage_dir: Path,
        motion_dir: Path,
        processor: Any,
        animation_processor: Any,
        job_db_path: Path | None = None,
        restore_concurrency: int = 2,
        animation_concurrency: int = 1,
        max_pending_jobs: int = 16,
    ) -> None:
        self.storage_dir = Path(storage_dir)
        self.motion_dir = Path(motion_dir)
        self.processor = processor
        self.animation_processor = animation_processor
        self.jobs = JobManager(
            JobStore(Path(job_db_path) if job_db_path else self.storage_dir / "jobs.sqlite3"),
            {RESTORE: self._run_restore, ANIMATE: self._run_animation},
            concurrency={RESTORE: restore_concurrency, ANIMATE: animation_concurrency},
            max_pending=max_pending_jobs,
        )
        self.jobs.recover()

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> PortraitService:
        if config.get("PROCESSOR"):
            processor = config["PROCESSOR"]
        elif config["MODEL_MODE"] == "gfpgan":
            processor = GfpganProcessor(Path(config["MODEL_PATH"]))
        else:
            processor = PreviewProcessor()
        if config.get("ANIMATION_PROCESSOR"):
            animation_processor = config["ANIMATION_PROCESSOR"]
        else:
            animation_processor = LivePortraitProcessor(
                Path(config["LIVEPORTRAIT_ROOT"]),
                Path(config["LIVEPORTRAIT_PYTHON"]),
                Path(config["LIVEPORTRAIT_WEIGHTS"]),
            )
        return cls(
            storage_dir=config["STORAGE_DIR"],
            motion_dir=config["MOTION_DIR"],
            processor=processor,
            animation_processor=animation_processor,
            job_db_path=config.get("JOB_DB_PATH"),
            restore_concurrency=config.get("RESTORE_CONCURRENCY", 2),
            animation_concurrency=config.get("ANIMATION_CONCURRENCY", 1),
            max_pending_jobs=config.get("MAX_PENDING_JOBS", 16),
        )

    def close(self, timeout: float = 5.0) -> None:
        self.jobs.close(timeout)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "model": self.processor.status(),
            "animation": self.animation_processor.status(),
            "jobs": self.jobs.stats(),
        }

    def motion_paths(self) -> list[Path]:
        paths = []
        if self.motion_dir.is_dir():
            for path in self.motion_dir.iterdir():
                if path.is_file() and extension_of(path.name) in ALLOWED_VIDEO_EXTENSIONS:
                    paths.append(path)
        return sorted(paths, key=motion_sort_key)

    def get_job(self, job_id: str) -> Job | None:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            return None
        return self.jobs.get(job_id)

    def submit_restore(self, image: IncomingFile | None, *, scale: Any) -> Job:
        extension = self._image_extension(image, "请上传一张图片。")
        try:
            scale = int(scale)
        except (TypeError, ValueError):
            raise SubmissionRejected("放大倍率必须是数字。", "invalid_scale", 400) from None
        if not 1 <= scale <= 4:
            raise SubmissionRejected("放大倍率需要介于 1 到 4 之间。", "invalid_scale", 400)
        status = self.processor.status()
        if not status["ready"]:
            raise SubmissionRejected(str(status["message"]), "model_unavailable", 503)

        job_id = uuid4().hex
        source = self._stage_image(image, job_id, extension)
        result = self.storage_dir / "results" / f"{job_id}.png"
        return self._enqueue(
            RESTORE,
            job_id,
            {"source": str(source), "result": str(result), "scale": scale},
            staged=[source],
        )

    def submit_animation(
        self,
        image: IncomingFile | None,
        *,
        motion_id: str = "",
        driving: IncomingFile | None = None,
    ) -> Job:
        extension = self._image_extension(image, "请上传一张照片。")
        motion_id = (motion_id or "").strip()
        has_custom_driving = driving is not None and bool(driving.filename)
        if motion_id and has_custom_driving:
            raise SubmissionRejected("一次只能选择一种驱动视频。", "ambiguous_motion", 400)
        if not motion_id and not has_custom_driving:
            raise SubmissionRejected("请选择本机示例或上传一个驱动视频。", "missing_motion", 400)

        motion_path: Path | None = None
        driving_extension = ""
        if has_custom_driving:
            assert driving is not None
            driving_extension = extension_of(driving.filename)
            if driving_extension not in ALLOWED_VIDEO_EXTENSIONS:
                raise SubmissionRejected(
                    "自定义驱动视频只支持 MP4、MOV 或 WebM。", "unsupported_driving_video", 400
                )
            if driving.size() > MAX_DRIVING_VIDEO_BYTES:
                raise SubmissionRejected("驱动视频不能超过 64 MB。", "driving_video_too_large", 413)
        else:
            motion_path = self.motion_path(motion_id)
        status = self.animation_processor.status()
        if not status["ready"]:
            raise SubmissionRejected(str(status["message"]), "animation_unavailable", 503)

        job_id = uuid4().hex
        source = self._stage_image(image, job_id, extension)
        staged = [source]
        ephemeral: list[str] = []
        if has_custom_driving:
            assert driving is not None
            private_driving_dir = self.storage_dir / "private_drivers" / job_id
            private_driving_dir.mkdir(parents=True, exist_ok=True)
            motion_path = private_driving_dir / f"driving.{driving_extension}"
            driving.save(motion_path)
            staged.append(motion_path)
            ephemeral.append(str(motion_path))
        assert motion_path is not None
        result = self.storage_dir / "animations" / job_id / "result.mp4"
        return self._enqueue(
            ANIMATE,
            job_id,
            {"source": str(source), "driving": str(motion_path), "result": str(result), "ephemeral": ephemeral},
            staged=staged,
        )

    def motion_path(self, motion_id: str) -> Path:
        if Path(motion_id).name != motion_id:
            raise SubmissionRejected("请选择一个有效的本地驱动素材。", "invalid_motion", 400)
        path = self.motion_dir / motion_id
        if not path.is_file() or extension_of(path.name) not in ALLOWED_VIDEO_EXTENSIONS:
            raise SubmissionRejected("所选驱动素材不可用。", "invalid_motion", 400)
        return path

    def describe(self, job: Job, link: Callable[[str], str], suffix: str) -> dict[str, Any]:
        """Serialize a job; each result file becomes ``<name><suffix>: link(media path)``."""
        result = None
        if job.status == COMPLETED and job.result:
            result = {key: value for key, value in job.result.items() if key != "files"}
            for name, media_path in job.result.get("files", {}).items():
                result[f"{name}{suffix}"] = link(media_path)
        return {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "createdAt": iso_time(job.created_at),
            "startedAt": iso_time(job.started_at),
            "finishedAt": iso_time(job.finished_at),
            "queuePosition": self.jobs.queue_position(job),
            "error": {"code": job.error_code, "message": job.error_message} if job.status == FAILED else None,
            "result": result,
        }

    @staticmethod
    def _image_extension(image: IncomingFile | None, missing_message: str) -> str:
        if image is None or not image.filename:
            raise SubmissionRejected(missing_message, "missing_file", 400)
        extension = extension_of(image.filename)
        if extension not in ALLOWED_IMAGE_EXTENSIONS:
            raise SubmissionRejected("只支持 JPG、PNG 或 WebP 图片。", "unsupported_file", 400)
        if image.size() > MAX_IMAGE_BYTES:
            raise SubmissionRejected("图片不能超过 15 MB。", "file_too_large", 413)
        return extension

    def _stage_image(self, image: IncomingFile, job_id: str, extension: str) -> Path:
        upload_dir = self.storage_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = Path(secure_filename(image.filename)).stem or "image"
        source = upload_dir / f"{job_id}_{safe_stem}.{extension}"
        image.save(source)
        try:
            with Image.open(source) as opened:
                opened.verify()
        except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
            source.unlink(missing_ok=True)
            raise SubmissionRejected("文件不是可解析的图片。", "invalid_image", 422) from None
        return source

    def _enqueue(self, kind: str, job_id: str, payload: dict[str, Any], *, staged: list[Path]) -> Job:
        try:
            return self.jobs.submit(kind, payload, job_id=job_id)
        except QueueFull:
            for path in staged:
                path.unlink(missing_ok=True)
            raise SubmissionRejected(
                "任务队列已满，请等待当前任务完成后再提交。", "queue_full", 429
            ) from None

    def _media_path(self, path: Path) -> str:
        return path.relative_to(self.storage_dir).as_posix()

    def _run_restore(self, job: Job) -> dict[str, Any]:
        source = Path(job.payload["source"])
        result = Path(job.payload["result"])
        try:
            outcome = self.processor.restore(source, result, scale=int(job.payload["scale"]))
        except InvalidImage as exc:
            raise JobError("invalid_image", str(exc)) from exc
        except ModelUnavailable as exc:
            raise JobError("model_unavailable", str(exc)) from exc
        return {
            "model": {**self.processor.status(), "mode": outcome.mode},
            "message": outcome.message,
            "files": {"source": self._media_path(source), "result": self._media_path(result)},
        }

    def _run_animation(self, job: Job) -> dict[str, Any]:
        result = Path(job.payload["result"])
        try:
            outcome = self.animation_processor.animate(
                Path(job.payload["source"]), Path(job.payload["driving"]), result
            )
        except InvalidImage as exc:
            raise JobError("invalid_image", str(exc)) from exc
        except AnimationUnavailable as exc:
            raise JobError("animation_unavailable", str(exc)) from exc
        except AnimationFailed as exc:
            raise JobError("animation_failed", str(exc)) from exc
        return {
            "animation": self.animation_processor.status(),
            "message": outcome.message,
            "files": {"result": self._media_path(result)},
        }
