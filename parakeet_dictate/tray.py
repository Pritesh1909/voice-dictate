"""Tray icon: a microphone whose colour shows the state, plus the menu."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

COLORS = {
    "idle": (120, 124, 132),      # grey: waiting, model not in memory
    "ready": (40, 120, 220),      # blue: waiting, model loaded
    "recording": (220, 40, 40),   # red: listening
    "working": (235, 160, 20),    # amber: loading / transcribing
    "error": (150, 20, 20),
}


def icon_image(state: str, size: int = 64) -> Image.Image:
    c = COLORS.get(state, COLORS["idle"])
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([22 * s, 4 * s, 42 * s, 38 * s], radius=10 * s, fill=c)            # capsule
    d.arc([12 * s, 16 * s, 52 * s, 48 * s], start=0, end=180, fill=c, width=max(2, int(5 * s)))  # cradle
    d.rectangle([29.5 * s, 47 * s, 34.5 * s, 56 * s], fill=c)                              # stem
    d.rounded_rectangle([18 * s, 55 * s, 46 * s, 61 * s], radius=3 * s, fill=c)            # base
    if state == "error":
        d.ellipse([40 * s, 0, 64 * s, 24 * s], fill=(255, 255, 255))
        d.rectangle([50.5 * s, 4 * s, 53.5 * s, 15 * s], fill=c)
        d.rectangle([50.5 * s, 17 * s, 53.5 * s, 20 * s], fill=c)
    return img


def write_ico(path: Path) -> Path:
    if not path.exists():
        icon_image("ready", 256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    return path
