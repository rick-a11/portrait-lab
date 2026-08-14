from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Mapping


class AnimationUnavailable(RuntimeError):
    """Raised when the local LivePortrait runtime has not been prepared."""


class AnimationFailed(RuntimeError):
    """Raised when LivePortrait exits without producing a usable video."""


@dataclass(frozen=True)
class AnimationResult:
    message: str


class LivePortraitProcessor:
    """Run the official LivePortrait CLI in an isolated local environment.

    The model process is deliberately separate from Flask.  LivePortrait has a
    different Torch stack than GFPGAN, and starting it as a subprocess prevents
    both models from competing for one interpreter or one set of dependencies.
    """

    REQUIRED_WEIGHTS = (
        "liveportrait/base_models/appearance_feature_extractor.pth",
        "liveportrait/base_models/motion_extractor.pth",
        "liveportrait/base_models/spade_generator.pth",
        "liveportrait/base_models/warping_module.pth",
        "liveportrait/retargeting_models/stitching_retargeting_module.pth",
        "liveportrait/landmark.onnx",
        "insightface/models/buffalo_l/2d106det.onnx",
        "insightface/models/buffalo_l/det_10g.onnx",
    )

    def __init__(
        self,
        runtime_root: Path,
        python_path: Path,
        weights_dir: Path,
        *,
        timeout_seconds: int = 1_200,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.python_path = Path(python_path)
        self.weights_dir = Path(weights_dir)
        self.timeout_seconds = timeout_seconds
        self.environment = dict(environment or {})
        self._lock = RLock()

    def status(self) -> dict[str, object]:
        missing: list[str] = []
        if not self.python_path.is_file():
            missing.append("Python 3.10 动态环境")
        if not (self.runtime_root / "inference.py").is_file():
            missing.append("LivePortrait 源码")
        if shutil.which("ffmpeg") is None:
            missing.append("FFmpeg")
        if self.runtime_root.is_dir():
            missing.extend(
                relative for relative in self.REQUIRED_WEIGHTS if not (self.weights_dir / relative).is_file()
            )

        if missing:
            preview = "、".join(missing[:3])
            suffix = "等" if len(missing) > 3 else ""
            return {
                "mode": "liveportrait",
                "ready": False,
                "message": f"动态生成尚未就绪：缺少 {preview}{suffix}。",
            }
        return {
            "mode": "liveportrait",
            "ready": True,
            "message": "LivePortrait 动态模型已就绪（本机运行）。",
        }

    def animate(self, source: Path, driving: Path, destination: Path) -> AnimationResult:
        status = self.status()
        if not status["ready"]:
            raise AnimationUnavailable(str(status["message"]))
        if driving.suffix.lower() not in {".mp4", ".mov", ".webm"}:
            raise AnimationFailed("所选驱动素材不是受支持的视频文件。")

        destination.parent.mkdir(parents=True, exist_ok=True)
        expected_output = destination.parent / f"{source.stem}--{driving.stem}.mp4"
        command = [
            str(self.python_path),
            "inference.py",
            "--source",
            str(source.resolve()),
            "--driving",
            str(driving.resolve()),
            "--output-dir",
            str(destination.parent.resolve()),
            "--no-flag-use-half-precision",
        ]
        environment = os.environ.copy()
        environment.update(self.environment)
        environment.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        environment.setdefault("PYTHONUNBUFFERED", "1")

        try:
            with self._lock:
                completed = subprocess.run(
                    command,
                    cwd=self.runtime_root,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
        except subprocess.TimeoutExpired as exc:
            raise AnimationFailed("动态生成超时，请缩短驱动视频后重试。") from exc
        except OSError as exc:
            raise AnimationUnavailable("无法启动 LivePortrait 本地运行环境。") from exc

        if completed.returncode != 0:
            raise AnimationFailed("动态生成失败，请检查本机 LivePortrait 运行环境。")
        if not expected_output.is_file() or expected_output.stat().st_size == 0:
            raise AnimationFailed("动态生成没有产生可下载的视频。")
        if destination.exists():
            raise AnimationFailed("动态结果目标已存在，请重新提交任务。")

        expected_output.rename(destination)
        return AnimationResult(message="LivePortrait 动态短片已生成。")
