"""Expose Portrait Lab to MCP clients such as Claude Desktop and Claude Code.

The server talks MCP over stdio and calls the same ``PortraitService`` as the
HTTP API, so uploads are validated, staged, queued, and cleaned up the same way.
Run it with ``scripts/run-mcp-server.sh`` or ``python -m backend.mcp_server``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated, Any, Callable

import anyio
import anyio.to_thread
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from backend.jobs import COMPLETED, FAILED, QUEUED, RUNNING, Job
from backend.motions import motion_payload
from backend.service import LocalFile, PortraitService, SubmissionRejected, runtime_config


MAX_WAIT_SECONDS = 300
POLL_INTERVAL_SECONDS = 0.5
# MCP progress must only increase, so it advances per stage rather than per poll.
PROGRESS_STAGES = {QUEUED: 0, RUNNING: 1, COMPLETED: 2, FAILED: 2}

INSTRUCTIONS = """\
Portrait Lab restores portrait photos with GFPGAN and animates them with LivePortrait, entirely on this machine.
Call health_check first to see which models are ready. Restorations usually finish within a minute, so
restore_photo waits for the result by default. Animations can take up to 20 minutes: animate_portrait returns a
job id right away, then call get_job (it can wait up to 300 seconds per call) until the status is completed or failed.
Only animate material the user owns or is authorized to use, and pass consent_confirmed=true only after they say so.
"""

ImagePath = Annotated[
    str,
    Field(description="Absolute path of a JPG, PNG, or WebP image on this machine (max 15 MB). It is copied, never modified."),
]
WaitSeconds = Annotated[
    float,
    Field(ge=0, le=MAX_WAIT_SECONDS, description="How long to wait for the job to finish before returning its current state."),
]


def local_file(raw_path: str, label: str) -> LocalFile:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise ToolError(f"[invalid_path] {label}需要使用绝对路径：{raw_path}")
    if not path.is_file():
        raise ToolError(f"[missing_file] 找不到{label}：{path}")
    return LocalFile(path)


def build_server(service: PortraitService) -> MCPServer:
    server = MCPServer("portrait-lab", title="Portrait Lab", instructions=INSTRUCTIONS)

    def describe(job: Job) -> dict[str, Any]:
        described = service.describe(job, lambda media_path: str((service.storage_dir / media_path).resolve()), "Path")
        if not job.is_terminal:
            described["next"] = (
                f"The job is still {job.status}. Call get_job with job_id={job.id!r} to wait for the result."
            )
        return described

    async def submit(action: Callable[[], Job]) -> Job:
        try:
            # Staging copies the files and writes SQLite; keep that off the event loop.
            return await anyio.to_thread.run_sync(action)
        except SubmissionRejected as exc:
            raise ToolError(f"[{exc.code}] {exc.message}") from None

    async def follow(job_id: str, wait_seconds: float, ctx: Context) -> dict[str, Any]:
        """Poll the job until it finishes or ``wait_seconds`` pass, reporting each new stage."""
        deadline = anyio.current_time() + min(max(wait_seconds, 0.0), MAX_WAIT_SECONDS)
        reported_stage = -1
        while True:
            job = service.get_job(job_id)
            if job is None:
                raise ToolError("[job_not_found] 没有找到这个任务。")
            described = describe(job)
            stage = PROGRESS_STAGES[job.status]
            if stage > reported_stage:
                reported_stage = stage
                position = described["queuePosition"]
                message = f"{job.kind} job {job.status}" + (
                    f" ({position} ahead in the queue)" if position else ""
                )
                await ctx.report_progress(stage, 2, message)
            remaining = deadline - anyio.current_time()
            if job.is_terminal or remaining <= 0:
                return described
            await anyio.sleep(min(POLL_INTERVAL_SECONDS, remaining))

    @server.tool(annotations=ToolAnnotations(title="Health check", read_only_hint=True))
    def health_check() -> dict[str, Any]:
        """Report whether the GFPGAN restorer and the LivePortrait animator are ready, plus current queue load."""
        return service.health()

    @server.tool(annotations=ToolAnnotations(title="List driving motions", read_only_hint=True))
    def list_motions() -> dict[str, Any]:
        """List the local driving clips whose id can be passed to animate_portrait as motion_id."""
        items = []
        for path in service.motion_paths():
            item = motion_payload(path, str(path.resolve()))
            item["path"] = item.pop("previewUrl")
            items.append(item)
        return {"count": len(items), "items": items}

    @server.tool(annotations=ToolAnnotations(title="Restore photo", read_only_hint=False, destructive_hint=False))
    async def restore_photo(
        image_path: ImagePath,
        ctx: Context,
        scale: Annotated[int, Field(ge=1, le=4, description="Upscale factor from 1 to 4.")] = 2,
        wait_seconds: WaitSeconds = 60,
    ) -> dict[str, Any]:
        """Restore a portrait photo with GFPGAN (or a clearly labeled preview when weights are missing).

        Returns the job; once completed, result.resultPath is the restored PNG on this machine.
        """
        image = local_file(image_path, "图片")
        job = await submit(lambda: service.submit_restore(image, scale=scale))
        return await follow(job.id, wait_seconds, ctx)

    @server.tool(
        annotations=ToolAnnotations(title="Animate portrait", read_only_hint=False, destructive_hint=False)
    )
    async def animate_portrait(
        image_path: ImagePath,
        consent_confirmed: Annotated[
            bool,
            Field(description="True only after the user confirms they own or may use both the photo and the driving clip."),
        ],
        ctx: Context,
        motion_id: Annotated[str, Field(description="A local driving clip id from list_motions.")] = "",
        driving_video_path: Annotated[
            str,
            Field(description="Absolute path of the user's own MP4, MOV, or WebM driving clip (max 64 MB); deleted after the job."),
        ] = "",
        wait_seconds: WaitSeconds = 0,
    ) -> dict[str, Any]:
        """Queue a LivePortrait animation of a portrait, driven by exactly one of motion_id or driving_video_path.

        Animations can take up to 20 minutes, so this returns the queued job immediately by default;
        poll get_job until it is completed, then open result.resultPath (an MP4 on this machine).
        """
        if not consent_confirmed:
            raise ToolError("[consent_required] 请先确认用户拥有照片和驱动素材的使用授权。")
        image = local_file(image_path, "照片")
        driving = local_file(driving_video_path, "驱动视频") if driving_video_path else None
        job = await submit(lambda: service.submit_animation(image, motion_id=motion_id, driving=driving))
        return await follow(job.id, wait_seconds, ctx)

    @server.tool(annotations=ToolAnnotations(title="Get job", read_only_hint=True))
    async def get_job(
        job_id: Annotated[str, Field(description="The id returned by restore_photo or animate_portrait.")],
        ctx: Context,
        wait_seconds: WaitSeconds = 30,
    ) -> dict[str, Any]:
        """Check a restore or animation job, waiting up to wait_seconds for it to finish (long polling)."""
        return await follow(job_id, wait_seconds, ctx)

    return server


def main() -> None:
    # stdout carries the MCP protocol, so every log line must go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s")
    config = runtime_config()
    if config["JOB_DB_PATH"] is None:
        # MCP jobs get a table of their own so the HTTP API's workers never pick them up. Servers
        # started by different MCP clients share it safely: recover() skips jobs whose owner is alive.
        config["JOB_DB_PATH"] = Path(config["STORAGE_DIR"]) / "mcp-jobs.sqlite3"
    service = PortraitService.from_config(config)
    try:
        build_server(service).run("stdio")
    finally:
        service.close(timeout=1.0)


if __name__ == "__main__":
    main()
