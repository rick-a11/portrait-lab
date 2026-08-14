from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MotionPresentation:
    label: str
    description: str
    duration: str
    order: int


# The official LivePortrait sample identifiers are opaque. Keep their names in
# one place so the API and UI can expose a useful, stable local motion library.
MOTION_PRESENTATIONS: dict[str, MotionPresentation] = {
    "d0.mp4": MotionPresentation("轻快笑容", "短促侧视与笑容", "3 秒", 10),
    "d18.mp4": MotionPresentation("柔和眨眼", "慢速眨眼与自然口型", "7 秒", 20),
    "d19.mp4": MotionPresentation("平稳口播", "正脸、节奏平稳的口型", "8 秒", 30),
    "d14.mp4": MotionPresentation("自然近景", "轻微头部律动与正脸表情", "18 秒", 40),
    "d10.mp4": MotionPresentation("从容讲述", "自然口播，表情变化温和", "15 秒", 50),
    "d13.mp4": MotionPresentation("竖版日常", "竖版近景、自然口播", "12 秒", 60),
    "d11.mp4": MotionPresentation("轻快表达", "节奏轻快、表情较丰富", "9 秒", 70),
    "d9.mp4": MotionPresentation("细微律动", "轻微口型与脸部表情", "20 秒", 80),
    "d3.mp4": MotionPresentation("严肃情绪", "皱眉与克制的情绪变化", "12 秒", 90),
    "d12.mp4": MotionPresentation("趣味表情", "夸张口型与表情变化", "7 秒", 100),
    "d6.mp4": MotionPresentation("高表现力", "夸张表情与清晰口型", "34 秒", 110),
    "d20.mp4": MotionPresentation("强口型演绎", "口型和表情变化更明显", "7 秒", 120),
}


def motion_sort_key(path: Path) -> tuple[int, str]:
    presentation = MOTION_PRESENTATIONS.get(path.name)
    return (presentation.order if presentation else 1_000, path.name.casefold())


def motion_payload(path: Path, preview_url: str) -> dict[str, str]:
    presentation = MOTION_PRESENTATIONS.get(path.name)
    if presentation:
        return {
            "id": path.name,
            "label": presentation.label,
            "description": presentation.description,
            "duration": presentation.duration,
            "previewUrl": preview_url,
        }
    return {
        "id": path.name,
        "label": f"本机视频 · {path.stem}",
        "description": "本机可用的驱动素材",
        "duration": "视频",
        "previewUrl": preview_url,
    }
