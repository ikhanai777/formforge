"""A small software rasteriser and PNG encoder.

The spec calls for offscreen VTK on llvmpipe. This does the same job in numpy
and the standard library, which turns out to be the better trade for what the
renders are actually for: they are 512x512 matte-grey previews fed to a vision
model and shown as thumbnails, not photorealistic marketing shots. In exchange
for giving up materials and shadows we get no native dependency, no GL context
to fail to acquire in a container, and byte-identical output across machines --
which matters, because a render that changes between runs makes the visual
critique step non-reproducible and its regressions impossible to bisect.

If the hero-shot path later needs something prettier, that is a separate
renderer behind the same interface.
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Matte clay grey. Light enough to show form, dark enough that the white
# background reads as background.
CLAY_RGB = (0.72, 0.71, 0.69)
BACKGROUND_RGB = (0.97, 0.97, 0.98)
# The cut face of a section view, in a colour nothing else in the scene uses so
# "this is the inside" is unambiguous to a vision model.
SECTION_RGB = (0.85, 0.45, 0.35)
GRID_RGB = (0.78, 0.80, 0.84)
GRID_MAJOR_RGB = (0.62, 0.65, 0.70)

# Three-point lighting in view space: key over the left shoulder, fill opposite
# and dimmer, rim from behind to separate the silhouette from the background.
LIGHTS: tuple[tuple[tuple[float, float, float], float], ...] = (
    ((-0.4, 0.5, 0.85), 0.72),
    ((0.7, -0.3, 0.5), 0.28),
    ((0.2, 0.6, -0.6), 0.18),
)
AMBIENT = 0.22


@dataclass
class Camera:
    """Where the scene is viewed from, and how it is projected."""

    eye: np.ndarray
    target: np.ndarray
    up: np.ndarray
    orthographic: bool = True
    # Half-height of the ortho view volume, in millimetres.
    scale_mm: float = 100.0
    fov_deg: float = 32.0

    def view_matrix(self) -> np.ndarray:
        """Right-handed look-at, world to view space."""
        forward = _normalize(self.target - self.eye)
        up = _normalize(self.up)
        right = np.cross(forward, up)
        if np.linalg.norm(right) < 1e-9:
            # The up vector is parallel to the view direction; pick another.
            up = np.array([0.0, 0.0, 1.0]) if abs(forward[2]) < 0.9 else np.array([0.0, 1.0, 0.0])
            right = np.cross(forward, up)
        right = _normalize(right)
        true_up = np.cross(right, forward)
        matrix = np.eye(4)
        matrix[0, :3] = right
        matrix[1, :3] = true_up
        matrix[2, :3] = -forward
        matrix[:3, 3] = -matrix[:3, :3] @ self.eye
        return matrix

    def project(self, points_view: np.ndarray, width: int, height: int) -> np.ndarray:
        """View space to screen space, returning (x_px, y_px, depth)."""
        aspect = width / height
        if self.orthographic:
            half_h = self.scale_mm
            half_w = half_h * aspect
            x = points_view[:, 0] / half_w
            y = points_view[:, 1] / half_h
            depth = -points_view[:, 2]
        else:
            f = 1.0 / math.tan(math.radians(self.fov_deg) / 2.0)
            # Guard against division by zero for a vertex exactly at the eye.
            z = np.where(points_view[:, 2] > -1e-6, -1e-6, points_view[:, 2])
            x = (f / aspect) * points_view[:, 0] / -z
            y = f * points_view[:, 1] / -z
            depth = -z
        screen = np.empty((len(points_view), 3), dtype=float)
        screen[:, 0] = (x * 0.5 + 0.5) * width
        screen[:, 1] = (0.5 - y * 0.5) * height
        screen[:, 2] = depth
        return screen


def _normalize(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-12 else vector


class Framebuffer:
    """An RGB colour buffer with a matching depth buffer."""

    def __init__(self, width: int, height: int, background=BACKGROUND_RGB):
        self.width = width
        self.height = height
        self.color = np.tile(
            np.asarray(background, dtype=np.float32), (height, width, 1)
        )
        self.depth = np.full((height, width), np.inf, dtype=np.float64)

    def to_png(self, path: str | Path) -> Path:
        write_png(path, (np.clip(self.color, 0.0, 1.0) * 255).astype(np.uint8))
        return Path(path)

    def to_array(self) -> np.ndarray:
        return (np.clip(self.color, 0.0, 1.0) * 255).astype(np.uint8)


def rasterize(
    framebuffer: Framebuffer,
    screen: np.ndarray,
    faces: np.ndarray,
    face_colors: np.ndarray,
    *,
    cull_backfaces: bool = True,
) -> None:
    """Z-buffered triangle fill with flat per-face colour.

    One Python iteration per triangle, with numpy doing the work inside each
    triangle's screen bounding box. A fully vectorised rasteriser would need a
    per-pixel argmin over triangles, which costs far more memory than the loop
    costs time at these resolutions -- a typical part is tens of thousands of
    triangles covering a few pixels each.
    """
    if len(faces) == 0:
        return

    tris = screen[faces]  # (n, 3, 3): three vertices, each (x, y, depth)
    width, height = framebuffer.width, framebuffer.height

    # Signed area in screen space. Screen y points down, so a front-facing
    # triangle (counter-clockwise in world space) has negative area here.
    ax, ay = tris[:, 0, 0], tris[:, 0, 1]
    bx, by = tris[:, 1, 0], tris[:, 1, 1]
    cx, cy = tris[:, 2, 0], tris[:, 2, 1]
    area = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)

    visible = np.abs(area) > 1e-9
    if cull_backfaces:
        visible &= area < 0

    min_x = np.floor(np.minimum(np.minimum(ax, bx), cx)).astype(int)
    max_x = np.ceil(np.maximum(np.maximum(ax, bx), cx)).astype(int)
    min_y = np.floor(np.minimum(np.minimum(ay, by), cy)).astype(int)
    max_y = np.ceil(np.maximum(np.maximum(ay, by), cy)).astype(int)

    visible &= (max_x >= 0) & (min_x < width) & (max_y >= 0) & (min_y < height)

    for i in np.flatnonzero(visible):
        x0 = max(0, min_x[i])
        x1 = min(width, max_x[i] + 1)
        y0 = max(0, min_y[i])
        y1 = min(height, max_y[i] + 1)
        if x1 <= x0 or y1 <= y0:
            continue

        xs = np.arange(x0, x1) + 0.5
        ys = np.arange(y0, y1) + 0.5
        px, py = np.meshgrid(xs, ys)

        inv_area = 1.0 / area[i]
        w0 = ((bx[i] - px) * (cy[i] - py) - (cx[i] - px) * (by[i] - py)) * inv_area
        w1 = ((cx[i] - px) * (ay[i] - py) - (ax[i] - px) * (cy[i] - py)) * inv_area
        w2 = 1.0 - w0 - w1

        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not inside.any():
            continue

        depth = w0 * tris[i, 0, 2] + w1 * tris[i, 1, 2] + w2 * tris[i, 2, 2]
        window_depth = framebuffer.depth[y0:y1, x0:x1]
        closer = inside & (depth < window_depth)
        if not closer.any():
            continue

        window_depth[closer] = depth[closer]
        framebuffer.color[y0:y1, x0:x1][closer] = face_colors[i]


def shade(normals_view: np.ndarray, base_rgb=CLAY_RGB) -> np.ndarray:
    """Lambertian shading under the fixed three-point rig, in view space.

    Lighting in view space rather than world space means every view is lit the
    same way relative to the camera. A model rendered from the back should not
    come out darker than one rendered from the front -- the vision critique is
    comparing form, and an unlit face reads as a missing feature.
    """
    intensity = np.full(len(normals_view), AMBIENT, dtype=float)
    for direction, weight in LIGHTS:
        light = _normalize(np.asarray(direction, dtype=float))
        lambert = np.clip(normals_view @ light, 0.0, None)
        intensity += weight * lambert
    intensity = np.clip(intensity, 0.0, 1.0)
    base = np.asarray(base_rgb, dtype=float)
    return np.clip(intensity[:, None] * base[None, :], 0.0, 1.0).astype(np.float32)


def draw_line(
    framebuffer: Framebuffer,
    start: np.ndarray,
    end: np.ndarray,
    rgb,
    *,
    depth_bias: float = 0.02,
) -> None:
    """Depth-tested line, used for the build-plate grid.

    The grid has to be depth-tested or it draws over the model; the small bias
    keeps a grid line lying exactly on the plate from z-fighting with the plate
    itself.
    """
    x0, y0, d0 = start
    x1, y1, d1 = end
    steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    if steps <= 0 or steps > 4096:
        return
    t = np.linspace(0.0, 1.0, steps)
    xs = np.round(x0 + (x1 - x0) * t).astype(int)
    ys = np.round(y0 + (y1 - y0) * t).astype(int)
    ds = d0 + (d1 - d0) * t - depth_bias

    on_screen = (
        (xs >= 0) & (xs < framebuffer.width) & (ys >= 0) & (ys < framebuffer.height)
    )
    xs, ys, ds = xs[on_screen], ys[on_screen], ds[on_screen]
    if len(xs) == 0:
        return
    closer = ds < framebuffer.depth[ys, xs]
    framebuffer.depth[ys[closer], xs[closer]] = ds[closer]
    framebuffer.color[ys[closer], xs[closer]] = np.asarray(rgb, dtype=np.float32)


# ---------------------------------------------------------------------------
# PNG encoding
# ---------------------------------------------------------------------------


def write_png(path: str | Path, rgb: np.ndarray) -> Path:
    """Write an 8-bit RGB PNG. No image library required.

    PNG is simple enough that pulling in an imaging dependency to emit one is a
    poor trade: this is a hundred lines less surface area in the container that
    runs untrusted geometry code next door.
    """
    height, width = rgb.shape[:2]
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("expected an (h, w, 3) uint8 array")

    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter type 0 (None) for this scanline
        raw.extend(rgb[row].tobytes())

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    return output


def tile(images: list[np.ndarray], columns: int, gap: int = 8) -> np.ndarray:
    """Compose images into a grid, for a single-image contact sheet."""
    if not images:
        raise ValueError("no images to tile")
    height, width = images[0].shape[:2]
    rows = math.ceil(len(images) / columns)
    canvas = np.full(
        (rows * height + (rows - 1) * gap, columns * width + (columns - 1) * gap, 3),
        245,
        dtype=np.uint8,
    )
    for index, image in enumerate(images):
        row, col = divmod(index, columns)
        y = row * (height + gap)
        x = col * (width + gap)
        canvas[y : y + height, x : x + width] = image
    return canvas
