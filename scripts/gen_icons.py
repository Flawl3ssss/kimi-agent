#!/usr/bin/env python3
"""Generate the launcher icons for the Android shell.

Pure stdlib (zlib + struct) so the build never depends on Pillow: each icon is
a solid rounded square with the agent's "K" mark drawn from simple geometry.
Sizes follow the adaptive-icon density ladder plus the legacy mipmap set.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

BG = (15, 17, 21)
FG = (94, 140, 255)
ACCENT = (255, 176, 32)

OUT_ROOT = Path(__file__).resolve().parent.parent / "android" / "app" / "src" / "main" / "res"
DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}
# Adaptive icons want a 108dp canvas with a 72dp safe zone.
ADAPTIVE = {
    "mdpi": 108,
    "hdpi": 162,
    "xhdpi": 216,
    "xxhdpi": 324,
    "xxxhdpi": 432,
}


def pixel(x: float, y: float, size: int) -> tuple[int, int, int]:
    """Background + a blocky 'K' with an accent dot."""
    u, v = x / size, y / size
    # rounded-square background
    pad = 0.08
    r = 0.22
    inside = True
    if u < pad or v < pad or u > 1 - pad or v > 1 - pad:
        inside = False
    else:
        # corner rounding
        cx = min(max(u, pad + r), 1 - pad - r)
        cy = min(max(v, pad + r), 1 - pad - r)
        if (u < pad + r or u > 1 - pad - r) and (v < pad + r or v > 1 - pad - r):
            if (u - cx) ** 2 + (v - cy) ** 2 > r * r:
                inside = False
    if not inside:
        return (0, 0, 0)  # transparent outside the shape

    # the K: stem plus two diagonals
    stem = 0.30 <= u <= 0.40 and 0.28 <= v <= 0.72
    arm = None
    if 0.40 < u < 0.72:
        t = (u - 0.40) / 0.32
        upper = 0.28 + t * 0.20          # 0.28 -> 0.48
        lower = 0.72 - t * 0.20          # 0.72 -> 0.52
        if abs(v - upper) < 0.055 or abs(v - lower) < 0.055:
            arm = True
    if stem or arm:
        return FG
    # accent: a cursor block under the letter
    if 0.30 <= u <= 0.70 and 0.78 <= v <= 0.84:
        return ACCENT
    return BG


def write_png(path: Path, size: int) -> None:
    rows = []
    for y in range(size):
        row = bytearray([0])  # filter type 0
        for x in range(size):
            rgb = pixel(x + 0.5, y + 0.5, size)
            # alpha: transparent outside the rounded square
            a = 0 if rgb == (0, 0, 0) else 255
            row += bytes((*rgb, a))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + tag + data
        return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main() -> None:
    # Only the legacy mipmap set: the manifest references @mipmap/ic_launcher,
    # and generating adaptive layers would need matching XML we do not use.
    written = 0
    for density, size in DENSITIES.items():
        out = OUT_ROOT / f"mipmap-{density}" / "ic_launcher.png"
        write_png(out, size)
        write_png(out.with_name("ic_launcher_round.png"), size)
        written += 2
    print(f"wrote {written} icons under {OUT_ROOT}")


if __name__ == "__main__":
    main()
