from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from flask import Flask, Response, abort, jsonify, request, send_from_directory, stream_with_context, url_for
from flask_cors import CORS
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import RequestEntityTooLarge

from backend.jobs import Job
from backend.motions import motion_payload
from backend.service import PortraitService, SubmissionRejected, runtime_config


DEFAULT_CORS_ORIGINS = tuple(
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    ).split(",")
    if origin.strip()
)


class UploadedFile:
    """Adapt a multipart upload to the service's ``IncomingFile`` interface."""

    def __init__(self, storage: FileStorage) -> None:
        self._storage = storage

    @property
    def filename(self) -> str:
        return self._storage.filename or ""

    def size(self) -> int:
        """Measure a multipart upload without consuming its stream."""
        stream = self._storage.stream
        position = stream.tell()
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(position)
        return size

    def save(self, destination: Path) -> None:
        self._storage.save(destination)


def uploaded(field: str) -> UploadedFile | None:
    storage = request.files.get(field)
    return UploadedFile(storage) if storage is not None else None


def create_app(overrides: Mapping[str, Any] | None = None) -> Flask:
    """Create the local image-restoration API without loading model weights yet."""
    app = Flask(__name__)
    app.config.from_mapping(
        runtime_config(),
        CORS_ORIGINS=DEFAULT_CORS_ORIGINS,
        MAX_CONTENT_LENGTH=80 * 1024 * 1024,
        SSE_HEARTBEAT_SECONDS=15.0,
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
    service = PortraitService.from_config(app.config)
    app.extensions["portrait_service"] = service
    app.extensions["processor"] = service.processor
    app.extensions["animation_processor"] = service.animation_processor

    def error(message: str, code: str, status: int) -> tuple[Any, int]:
        return jsonify({"error": {"code": code, "message": message}}), status

    def media_url(media_path: str) -> str:
        category, filename = media_path.split("/", 1)
        return url_for("media", category=category, filename=filename)

    def job_body(job: Job) -> dict[str, Any]:
        return {"job": service.describe(job, media_url, "Url")}

    def accepted(job: Job) -> Response:
        response = jsonify(job_body(job))
        response.status_code = 202
        response.headers["Location"] = url_for("job_status", job_id=job.id)
        return response

    @app.get("/api/health")
    def health() -> tuple[Any, int]:
        return jsonify(service.health()), 200

    @app.post("/api/restore")
    def restore() -> Any:
        try:
            job = service.submit_restore(uploaded("image"), scale=request.form.get("scale", "2"))
        except SubmissionRejected as exc:
            return error(exc.message, exc.code, exc.status)
        return accepted(job)

    @app.post("/api/animate")
    def animate() -> Any:
        try:
            job = service.submit_animation(
                uploaded("image"),
                motion_id=request.form.get("motionId", ""),
                driving=uploaded("drivingVideo"),
            )
        except SubmissionRejected as exc:
            return error(exc.message, exc.code, exc.status)
        return accepted(job)

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id: str) -> Any:
        job = service.get_job(job_id)
        if job is None:
            return error("没有找到这个任务。", "job_not_found", 404)
        return jsonify(job_body(job)), 200

    @app.get("/api/jobs/<job_id>/events")
    def job_events(job_id: str) -> Any:
        """Push job updates as Server-Sent Events until the job finishes."""
        if service.get_job(job_id) is None:
            return error("没有找到这个任务。", "job_not_found", 404)
        heartbeat = float(app.config["SSE_HEARTBEAT_SECONDS"])

        def stream() -> Any:
            last_body: dict[str, Any] | None = None
            while True:
                version = service.jobs.version
                job = service.get_job(job_id)
                if job is None:
                    return
                body = job_body(job)
                if body != last_body:
                    yield f"event: job\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"
                    last_body = body
                if job.is_terminal:
                    return
                if service.jobs.wait_for_change(version, heartbeat) == version:
                    yield ": keep-alive\n\n"

        return Response(
            stream_with_context(stream()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/motions")
    def motions() -> Any:
        items = [
            motion_payload(path, url_for("media", category="motions", filename=path.name))
            for path in service.motion_paths()
        ]
        return jsonify({"scope": "local", "items": items})

    @app.get("/media/<category>/<path:filename>")
    def media(category: str, filename: str) -> Any:
        if category == "motions":
            media_dir = service.motion_dir
        elif category in {"uploads", "results", "animations"}:
            media_dir = service.storage_dir / category
        else:
            abort(404)
        return send_from_directory(media_dir, filename)

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_: RequestEntityTooLarge) -> tuple[Any, int]:
        return error("图片和驱动视频总大小不能超过 80 MB。", "file_too_large", 413)

    return app


if __name__ == "__main__":
    # The reloader would import the app twice and start two competing job worker pools.
    create_app().run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
