from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from threading import RLock
from types import ModuleType

from PIL import Image, ImageOps, UnidentifiedImageError


class InvalidImage(ValueError):
    """Raised when an uploaded file cannot be decoded as an image."""


class ModelUnavailable(RuntimeError):
    """Raised when the optional GFPGAN runtime or a model weight is unavailable."""


def prepare_gfpgan_runtime() -> None:
    """Bridge a small API move in modern torchvision releases.

    GFPGAN 1.3.8 pulls BasicSR 1.4.2, which imports
    ``torchvision.transforms.functional_tensor``.  torchvision 0.17 moved
    ``rgb_to_grayscale`` to ``torchvision.transforms.functional``.  Supplying
    the old module name here keeps the upstream model untouched while allowing
    the isolated Python 3.10 runtime to use a maintained Torch build.
    """

    module_name = "torchvision.transforms.functional_tensor"
    if module_name in sys.modules:
        return

    try:
        from torchvision.transforms.functional import rgb_to_grayscale
    except ImportError as exc:
        raise ModelUnavailable("GFPGAN 运行时缺少 torchvision 组件。") from exc

    compatibility_module = ModuleType(module_name)
    compatibility_module.rgb_to_grayscale = rgb_to_grayscale
    sys.modules[module_name] = compatibility_module


@dataclass(frozen=True)
class ProcessingResult:
    mode: str
    message: str


class PreviewProcessor:
    """Normalize a valid image for an honest end-to-end preview flow.

    This adapter intentionally does not claim to restore a face. It lets the
    upload and delivery interface be tested while model weights are unavailable.
    """

    def status(self) -> dict[str, object]:
        return {
            "mode": "preview",
            "ready": True,
            "message": "预览模式会规范化图片，不会把结果标记为 AI 修复。",
        }

    def restore(self, source: Path, destination: Path, *, scale: int) -> ProcessingResult:
        try:
            with Image.open(source) as opened:
                opened.verify()
            with Image.open(source) as opened:
                normalized = ImageOps.exif_transpose(opened)
                if normalized.mode not in {"RGB", "RGBA"}:
                    normalized = normalized.convert("RGB")
                destination.parent.mkdir(parents=True, exist_ok=True)
                normalized.save(destination, format="PNG", optimize=True)
        except (OSError, UnidentifiedImageError) as exc:
            raise InvalidImage("文件不是可解析的图片。") from exc

        return ProcessingResult(
            mode="preview",
            message="预览处理完成；配置 GFPGAN 权重后可启用真实修复。",
        )


class GfpganProcessor:
    """Lazy adapter for the optional GFPGAN runtime.

    The public interface matches ``PreviewProcessor``. Imports and model loading
    happen only when an actual restoration is requested, so a missing legacy
    dependency cannot stop the local API from starting.
    """

    def __init__(self, model_path: Path, *, device: str | None = None) -> None:
        self.model_path = Path(model_path)
        self.device = device or os.getenv("GFPGAN_DEVICE", "cpu")
        self._restorers: dict[int, object] = {}
        self._lock = RLock()

    def status(self) -> dict[str, object]:
        missing_dependencies = [
            package for package in ("gfpgan", "cv2") if find_spec(package) is None
        ]
        if missing_dependencies:
            return {
                "mode": "gfpgan",
                "ready": False,
                "message": f"缺少模型依赖：{', '.join(missing_dependencies)}。",
            }
        if not self.model_path.is_file():
            return {
                "mode": "gfpgan",
                "ready": False,
                "message": f"未找到模型权重：{self.model_path.name}。",
            }
        return {
            "mode": "gfpgan",
            "ready": True,
            "message": f"GFPGAN 模型已就绪（{self.device}）。",
        }

    def restore(self, source: Path, destination: Path, *, scale: int) -> ProcessingResult:
        status = self.status()
        if not status["ready"]:
            raise ModelUnavailable(str(status["message"]))

        try:
            import cv2

            prepare_gfpgan_runtime()
            from gfpgan import GFPGANer
        except ImportError as exc:
            raise ModelUnavailable("GFPGAN 运行时无法导入。") from exc

        source_image = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if source_image is None:
            raise InvalidImage("文件不是可解析的图片。")

        with self._lock:
            restorer = self._restorers.get(scale)
            if restorer is None:
                restorer = GFPGANer(
                    model_path=str(self.model_path),
                    upscale=scale,
                    arch="clean",
                    channel_multiplier=2,
                    bg_upsampler=None,
                    device=self.device,
                )
                self._restorers[scale] = restorer
            _, _, restored_image = restorer.enhance(
                source_image,
                has_aligned=False,
                only_center_face=False,
                paste_back=True,
                weight=1.0,
            )

        if restored_image is None:
            raise InvalidImage("图片中没有可输出的修复结果。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(destination), restored_image):
            raise RuntimeError("无法写入修复结果。")

        return ProcessingResult(mode="gfpgan", message="GFPGAN 修复完成。")
