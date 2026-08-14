from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from flask import Flask, abort, jsonify, request, send_from_directory, url_for
from flask_cors import CORS
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from backend.animation import AnimationFailed, AnimationUnavailable, LivePortraitProcessor
from backend.motions import motion_payload, motion_sort_key
from backend.processors import GfpganProcessor, InvalidImage, ModelUnavailable, PreviewProcessor


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mov", "webm"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_DRIVING_VIDEO_BYTES = 64 * 1024 * 1024
DEFAULT_CORS_ORIGINS = tuple(
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    ).split(",")
    if origin.strip()
)


def create_app(overrides: Mapping[str, Any] | None = None) -> Flask:
    """Create the local image-restoration API without loading model weights yet."""
    app = Flask(__name__)
    app.config.from_mapping(
        STORAGE_DIR=PROJECT_ROOT / "backend" / "runtime",
        MOTION_DIR=Path(
            os.getenv(
                "MOTION_DIR",
                PROJECT_ROOT / ".runtime" / "liveportrait" / "assets" / "examples" / "driving",
            )
        ),
        MODEL_MODE=os.getenv("MODEL_MODE", "preview").strip().lower(),
        MODEL_PATH=Path(os.getenv("GFPGAN_MODEL_PATH", PROJECT_ROOT / "models" / "GFPGANv1.4.pth")),
        LIVEPORTRAIT_ROOT=Path(os.getenv("LIVEPORTRAIT_ROOT", PROJECT_ROOT / ".runtime" / "liveportrait")),
        LIVEPORTRAIT_PYTHON=Path(
            os.getenv("LIVEPORTRAIT_PYTHON", PROJECT_ROOT / ".venv-liveportrait" / "bin" / "python")
        ),
        LIVEPORTRAIT_WEIGHTS=Path(
            os.getenv(
                "LIVEPORTRAIT_WEIGHTS_DIR",
                PROJECT_ROOT / ".runtime" / "liveportrait" / "pretrained_weights",
            )
        ),
        CORS_ORIGINS=DEFAULT_CORS_ORIGINS,
        MAX_CONTENT_LENGTH=80 * 1024 * 1024,
        PROCESSOR=None,
        ANIMATION_PROCESSOR=None,
    )
    if overrides:
        app.config.update(overrides)
    CORS(
        app,
        resources={
            r"/api/*": {"origins": app.config["CORS_ORIGINS"]},
            r"/media/*": {"origins": app.config["CORS_ORIGINS"]},
        },
    )
    if app.config["PROCESSOR"]:
        processor = app.config["PROCESSOR"]
    elif app.config["MODEL_MODE"] == "gfpgan":
        processor = GfpganProcessor(Path(app.config["MODEL_PATH"]))
    else:
        processor = PreviewProcessor()
    app.extensions["processor"] = processor
    if app.config["ANIMATION_PROCESSOR"]:
        animation_processor = app.config["ANIMATION_PROCESSOR"]
    else:
        animation_processor = LivePortraitProcessor(
            Path(app.config["LIVEPORTRAIT_ROOT"]),
            Path(app.config["LIVEPORTRAIT_PYTHON"]),
            Path(app.config["LIVEPORTRAIT_WEIGHTS"]),
        )
    app.extensions["animation_processor"] = animation_processor

    def error(message: str, code: str, status: int) -> tuple[Any, int]:
        return jsonify({"error": {"code": code, "message": message}}), status

    def upload_size(uploaded: Any) -> int:
        """Measure a multipart upload without consuming its stream."""
        stream = uploaded.stream
        position = stream.tell()
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(position)
        return size

    @app.get("/api/health")
    def health() -> tuple[dict[str, Any], int]:
        return (
            jsonify(
                {
                    "status": "ok",
                    "model": processor.status(),
                    "animation": animation_processor.status(),
                }
            ),
            200,
        )

    @app.post("/api/restore")
    def restore() -> tuple[Any, int]:
        uploaded = request.files.get("image")
        if uploaded is None or not uploaded.filename:
            return error("请上传一张图片。", "missing_file", 400)

        extension = uploaded.filename.rsplit(".", 1)[-1].lower() if "." in uploaded.filename else ""
        if extension not in ALLOWED_IMAGE_EXTENSIONS:
            return error("只支持 JPG、PNG 或 WebP 图片。", "unsupported_file", 400)
        if upload_size(uploaded) > MAX_IMAGE_BYTES:
            return error("图片不能超过 15 MB。", "file_too_large", 413)

        try:
            scale = int(request.form.get("scale", "2"))
        except ValueError:
            return error("放大倍率必须是数字。", "invalid_scale", 400)
        if not 1 <= scale <= 4:
            return error("放大倍率需要介于 1 到 4 之间。", "invalid_scale", 400)

        storage_dir = Path(app.config["STORAGE_DIR"])
        upload_dir = storage_dir / "uploads"
        result_dir = storage_dir / "results"
        upload_dir.mkdir(parents=True, exist_ok=True)
        result_dir.mkdir(parents=True, exist_ok=True)

        job_id = uuid4().hex
        safe_stem = Path(secure_filename(uploaded.filename)).stem or "image"
        source_name = f"{job_id}_{safe_stem}.{extension}"
        result_name = f"{job_id}.png"
        source_path = upload_dir / source_name
        result_path = result_dir / result_name
        uploaded.save(source_path)

        try:
            outcome = processor.restore(source_path, result_path, scale=scale)
        except InvalidImage as exc:
            return error(str(exc), "invalid_image", 422)
        except ModelUnavailable as exc:
            return error(str(exc), "model_unavailable", 503)

        return (
            jsonify(
                {
                    "job": {"id": job_id, "status": "completed"},
                    "model": {**processor.status(), "mode": outcome.mode},
                    "message": outcome.message,
                    "sourceUrl": url_for("media", category="uploads", filename=source_name),
                    "resultUrl": url_for("media", category="results", filename=result_name),
                }
            ),
            201,
        )

    @app.post("/api/animate")
    def animate() -> tuple[Any, int]:
        uploaded = request.files.get("image")
        if uploaded is None or not uploaded.filename:
            return error("请上传一张照片。", "missing_file", 400)

        extension = uploaded.filename.rsplit(".", 1)[-1].lower() if "." in uploaded.filename else ""
        if extension not in ALLOWED_IMAGE_EXTENSIONS:
            return error("只支持 JPG、PNG 或 WebP 图片。", "unsupported_file", 400)
        if upload_size(uploaded) > MAX_IMAGE_BYTES:
            return error("图片不能超过 15 MB。", "file_too_large", 413)

        requested_motion = request.form.get("motionId", "").strip()
        uploaded_driving = request.files.get("drivingVideo")
        has_custom_driving = uploaded_driving is not None and bool(uploaded_driving.filename)
        if requested_motion and has_custom_driving:
            return error("一次只能选择一种驱动视频。", "ambiguous_motion", 400)
        if not requested_motion and not has_custom_driving:
            return error("请选择本机示例或上传一个驱动视频。", "missing_motion", 400)

        motion_path: Path | None = None
        custom_extension = ""
        if has_custom_driving:
            custom_extension = (
                uploaded_driving.filename.rsplit(".", 1)[-1].lower() if "." in uploaded_driving.filename else ""
            )
            if custom_extension not in ALLOWED_VIDEO_EXTENSIONS:
                return error("自定义驱动视频只支持 MP4、MOV 或 WebM。", "unsupported_driving_video", 400)
            if upload_size(uploaded_driving) > MAX_DRIVING_VIDEO_BYTES:
                return error("驱动视频不能超过 64 MB。", "driving_video_too_large", 413)
        else:
            if Path(requested_motion).name != requested_motion:
                return error("请选择一个有效的本地驱动素材。", "invalid_motion", 400)
            motion_path = Path(app.config["MOTION_DIR"]) / requested_motion
            if not motion_path.is_file() or motion_path.suffix.lstrip(".").lower() not in ALLOWED_VIDEO_EXTENSIONS:
                return error("所选驱动素材不可用。", "invalid_motion", 400)

        storage_dir = Path(app.config["STORAGE_DIR"])
        upload_dir = storage_dir / "uploads"
        animation_dir = storage_dir / "animations"
        upload_dir.mkdir(parents=True, exist_ok=True)
        animation_dir.mkdir(parents=True, exist_ok=True)

        job_id = uuid4().hex
        safe_stem = Path(secure_filename(uploaded.filename)).stem or "image"
        source_name = f"{job_id}_{safe_stem}.{extension}"
        source_path = upload_dir / source_name
        job_dir = animation_dir / job_id
        result_path = job_dir / "result.mp4"
        uploaded.save(source_path)

        try:
            with Image.open(source_path) as opened:
                opened.verify()
        except (OSError, UnidentifiedImageError):
            return error("文件不是可解析的图片。", "invalid_image", 422)

        if has_custom_driving:
            private_driving_dir = storage_dir / "private_drivers" / job_id
            private_driving_dir.mkdir(parents=True, exist_ok=True)
            motion_path = private_driving_dir / f"driving.{custom_extension}"
            uploaded_driving.save(motion_path)

        try:
            assert motion_path is not None
            outcome = animation_processor.animate(source_path, motion_path, result_path)
        except InvalidImage as exc:
            return error(str(exc), "invalid_image", 422)
        except AnimationUnavailable as exc:
            return error(str(exc), "animation_unavailable", 503)
        except AnimationFailed as exc:
            return error(str(exc), "animation_failed", 502)
        finally:
            if has_custom_driving and motion_path is not None:
                motion_path.unlink(missing_ok=True)

        return (
            jsonify(
                {
                    "job": {"id": job_id, "status": "completed"},
                    "animation": animation_processor.status(),
                    "message": outcome.message,
                    "resultUrl": url_for("media", category="animations", filename=f"{job_id}/result.mp4"),
                }
            ),
            201,
        )

    @app.get("/api/motions")
    def motions() -> Any:
        motion_dir = Path(app.config["MOTION_DIR"])
        paths = []
        if motion_dir.is_dir():
            for path in motion_dir.iterdir():
                if path.is_file() and path.suffix.lstrip(".").lower() in ALLOWED_VIDEO_EXTENSIONS:
                    paths.append(path)
        items = [
            motion_payload(path, url_for("media", category="motions", filename=path.name))
            for path in sorted(paths, key=motion_sort_key)
        ]
        return jsonify({"scope": "local", "items": items})

    @app.get("/media/<category>/<path:filename>")
    def media(category: str, filename: str) -> Any:
        if category == "motions":
            media_dir = Path(app.config["MOTION_DIR"])
        elif category in {"uploads", "results", "animations"}:
            media_dir = Path(app.config["STORAGE_DIR"]) / category
        else:
            abort(404)
        return send_from_directory(media_dir, filename)

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_: RequestEntityTooLarge) -> tuple[Any, int]:
        return error("图片和驱动视频总大小不能超过 80 MB。", "file_too_large", 413)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=True)
