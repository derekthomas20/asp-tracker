"""Draw the Rxcurve favicon PNGs into site/static/.

Draws the same shapes as site/static/favicon.svg (navy rounded square, the
curve in light teal, a white dot) at 16, 32 and 180 pixels, using only the
standard library. Run it again only if the logo changes; the PNGs are kept in
the repository and copied into public/ by scripts/build_site.py.

Usage (from the repository root):
    .venv\\Scripts\\python scripts/make_favicons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "site" / "static"

NAVY = (0x0F, 0x2A, 0x3D)
LIGHT_TEAL = (0x5F, 0xD3, 0xC6)
WHITE = (0xFF, 0xFF, 0xFF)

# The curve "M4 24 C 10 24, 12 9, 18 9 S 25 15, 28 7" in the 32x32 viewBox.
# "S" mirrors the previous control point (12, 9) around (18, 9) to (24, 9).
CURVES = [((4, 24), (10, 24), (12, 9), (18, 9)),
          ((18, 9), (24, 9), (25, 15), (28, 7))]
STROKE_HALF = 1.5  # stroke-width 3
DOT = (28, 7, 2.8)
CORNER = 7  # rx of the rounded square
SAMPLES = 4  # per pixel side, for smooth edges


def bezier_points(p0, p1, p2, p3, steps=48):
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append((u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                    u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]))
    return out


SEGMENTS = []
for c in CURVES:
    pts = bezier_points(*c)
    SEGMENTS += list(zip(pts, pts[1:]))


def near_curve(x, y):
    """True if (x, y) is within the stroke. Distance to segments gives round caps."""
    for (ax, ay), (bx, by) in SEGMENTS:
        if x < min(ax, bx) - STROKE_HALF or x > max(ax, bx) + STROKE_HALF:
            continue
        if y < min(ay, by) - STROKE_HALF or y > max(ay, by) + STROKE_HALF:
            continue
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        ex, ey = ax + t * dx - x, ay + t * dy - y
        if ex * ex + ey * ey <= STROKE_HALF * STROKE_HALF:
            return True
    return False


def in_square(x, y, corner):
    cx = min(max(x, corner), 32 - corner)
    cy = min(max(y, corner), 32 - corner)
    return (x - cx) ** 2 + (y - cy) ** 2 <= corner * corner


def color_at(x, y, corner):
    """Color of the topmost shape at viewBox point (x, y), or None outside."""
    if (x - DOT[0]) ** 2 + (y - DOT[1]) ** 2 <= DOT[2] ** 2:
        return WHITE
    if not in_square(x, y, corner):
        return None
    return LIGHT_TEAL if near_curve(x, y) else NAVY


def render(size, corner):
    rows = []
    n = SAMPLES * SAMPLES
    for py in range(size):
        row = bytearray([0])  # PNG filter type 0
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    x = (px + (sx + 0.5) / SAMPLES) * 32 / size
                    y = (py + (sy + 0.5) / SAMPLES) * 32 / size
                    c = color_at(x, y, corner)
                    if c:
                        r += c[0]; g += c[1]; b += c[2]; a += 1
            if a:
                row += bytes([round(r / a), round(g / a), round(b / a), round(255 * a / n)])
            else:
                row += bytes(4)
        rows.append(bytes(row))
    return rows


def write_png(path, size, rows):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def main():
    # Apple rounds home-screen icons itself, so its icon is a plain square.
    for name, size, corner in [("favicon-16.png", 16, CORNER), ("favicon-32.png", 32, CORNER),
                               ("apple-touch-icon.png", 180, 0)]:
        write_png(STATIC / name, size, render(size, corner))
        print("Wrote", (STATIC / name).relative_to(STATIC.parent.parent))


if __name__ == "__main__":
    main()
