"""Core engraving logic: wraps text around the outer wall of the urn base and cuts it in.

All units are millimetres. The base is assumed to be a ring centred on the Z axis
(verified for "Base with Screw2.stl": centre (0, 0), outer radius 49.68, Z -5.80..6.00).
"""
from __future__ import annotations

import io
import struct
import threading
import time
from pathlib import Path

import numpy as np
import trimesh
import manifold3d as m3d
from fontTools.ttLib import TTFont
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextPath

# ---------------------------------------------------------------- settings
BASE_STL = Path(__file__).with_name("Base with Screw2.stl")
FONT_FILE = Path(r"C:\Windows\Fonts\arial.ttf")
CAP_HEIGHT = 10.0      # mm, height of capital letters (user choice "B")
DEPTH = 1.0            # mm, cut depth measured along the surface normal (uniform, incl. rounded top edge)
MAX_CHARS = 15
OVERSHOOT = 1.0        # mm the cutter sticks out beyond the wall (clean cut)
EDGE_MARGIN = 0.05     # mm, letters must stay at least this far from top/bottom faces
REFINE_LEN = 0.4       # mm, max edge length before wrapping (smooth curvature)
FRONT_ANGLE = 0.0      # radians; 0 = text centred on the +Y side

_FONT_PROPS = FontProperties(fname=str(FONT_FILE))
_CMAP = TTFont(str(FONT_FILE))["cmap"].getBestCmap()
_LOCK = threading.Lock()


class EngraveError(ValueError):
    """Raised for user-input problems (shown to the user)."""


# ---------------------------------------------------------------- base model
class Base:
    def __init__(self, path: Path):
        t = time.time()
        tm = trimesh.load(str(path), process=True)
        verts = np.asarray(tm.vertices, dtype=np.float32)
        faces = np.asarray(tm.faces, dtype=np.uint32)
        self.manifold = m3d.Manifold(m3d.Mesh(vert_properties=verts, tri_verts=faces))
        if self.manifold.status() != m3d.Error.NoError:
            raise RuntimeError(f"Base mesh is not manifold: {self.manifold.status()}")

        v = verts.astype(np.float64)
        r = np.hypot(v[:, 0], v[:, 1])
        self.r_outer = float(r.max())
        self.z_min = float(v[:, 2].min())
        self.z_max = float(v[:, 2].max())
        self._outer_profile(v[:, 2], r)
        self.preview = self._preview_shape()
        print(f"[base] loaded {self.manifold.num_tri():,} triangles in {time.time() - t:.1f}s; "
              f"R={self.r_outer:.3f} Z={self.z_min:.3f}..{self.z_max:.3f}")

    def _outer_profile(self, z: np.ndarray, r: np.ndarray) -> None:
        """Measure the outer wall profile r(z), including the rounded top edge.

        The mesh vertices lie on rings at discrete Z levels; the largest radius on each
        level is the true surface radius there. The result is resampled to a uniform
        0.01 mm grid, together with its slope dr/dz (used for the surface normal).
        """
        m = r > self.r_outer - 2.0
        zl = np.round(z[m], 3)
        order = np.argsort(zl, kind="stable")
        zl, rl = zl[order], r[m][order]
        levels, start = np.unique(zl, return_index=True)
        rmax = np.maximum.reduceat(rl, start)
        self.prof_z = np.arange(self.z_min, self.z_max + 1e-9, 0.01)
        self.prof_r = np.interp(self.prof_z, levels, rmax)
        # light smoothing before differentiating (5-sample moving average)
        k = np.ones(5) / 5
        smooth = np.convolve(np.pad(self.prof_r, 2, mode="edge"), k, mode="valid")
        self.prof_dr = np.gradient(smooth, self.prof_z)

    def _preview_shape(self) -> m3d.Manifold:
        """Light-weight revolved approximation of the base, for the browser preview only.

        Uses the exact measured outer wall profile prof_r(prof_z) (downsampled to every
        0.05 mm) so the preview outer wall matches the cutter surface with no residual film.
        """
        zc = self.prof_z[::5].copy()
        zc[-1] = self.z_max
        rmax = self.prof_r[::5].copy()
        rmin = np.full_like(zc, 29.85)
        inner = np.column_stack([rmin, zc])
        outer = np.column_stack([rmax, zc])[::-1]
        profile = m3d.CrossSection([np.vstack([inner, outer])], m3d.FillRule.NonZero)
        return m3d.Manifold.revolve(profile, 360)


# ---------------------------------------------------------------- text cutter
def normalise_text(text: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise EngraveError("Please enter some text.")
    if len(text) > MAX_CHARS:
        raise EngraveError(f"Text is too long ({len(text)} characters, max {MAX_CHARS}).")
    missing = sorted({c for c in text if c != " " and ord(c) not in _CMAP})
    if missing:
        raise EngraveError(f"Arial has no glyph for: {' '.join(missing)}")
    return text


def _text_polygons(text: str) -> list[np.ndarray]:
    """Flat text outline (mm).

    Standard scale sets capital 'H' height = CAP_HEIGHT (10 mm). If the text's
    total vertical span (including descenders like 'y' or accents like 'Õ') exceeds
    CAP_HEIGHT, it is uniformly scaled down so the total height is CAP_HEIGHT.
    Centred on x=0.
    """
    big = 1000.0  # render large so curves are finely flattened, then scale down
    cap = TextPath((0, 0), "H", size=big, prop=_FONT_PROPS).get_extents().height
    scale = CAP_HEIGHT / cap
    path = TextPath((0, 0), text, size=big, prop=_FONT_PROPS)
    raw = [p[:-1] for p in path.to_polygons(closed_only=True) if len(p) > 3]
    if not raw:
        raise EngraveError("Text contains no printable characters.")
    all_raw = np.vstack(raw)
    span_y = (all_raw[:, 1].max() - all_raw[:, 1].min()) * scale
    if span_y > CAP_HEIGHT:
        scale *= CAP_HEIGHT / span_y
    polys = [p * scale for p in raw]
    allp = np.vstack(polys)
    cx = (allp[:, 0].min() + allp[:, 0].max()) / 2
    return [p - [cx, 0.0] for p in polys]


def build_cutter(text: str, base: Base) -> m3d.Manifold:
    polys = _text_polygons(text)
    allp = np.vstack(polys)

    # vertical placement: center the text's vertical bounding box on the base height
    z_mid = (base.z_min + base.z_max) / 2
    y_mid = (allp[:, 1].min() + allp[:, 1].max()) / 2
    y_shift = z_mid - y_mid
    lo, hi = allp[:, 1].min() + y_shift, allp[:, 1].max() + y_shift
    if lo < base.z_min + EDGE_MARGIN or hi > base.z_max - EDGE_MARGIN:
        raise EngraveError("Text is too tall to fit on the side of the base.")

    width = allp[:, 0].max() - allp[:, 0].min()
    if width / base.r_outer > np.pi * 1.5:
        raise EngraveError("Text is too wide to fit around the base.")

    section = m3d.CrossSection(polys, m3d.FillRule.NonZero)
    solid = section.extrude(DEPTH + OVERSHOOT).refine_to_length(REFINE_LEN)

    R = base.r_outer

    def wrap(v: np.ndarray) -> np.ndarray:
        # x: along the text, y: up, t: 0..DEPTH+OVERSHOOT through the wall (t=DEPTH is the surface)
        x, y, t = v[:, 0], v[:, 1], v[:, 2]
        theta = FRONT_ANGLE + x / R          # arc length measured on the outer wall
        z = y + y_shift
        # point on the actual surface (follows the rounded top edge) ...
        r_s = np.interp(z, base.prof_z, base.prof_r)
        dr = np.interp(z, base.prof_z, base.prof_dr)
        # ... offset along the surface normal, so the cut is DEPTH deep everywhere
        n_len = np.sqrt(1.0 + dr * dr)
        n_r, n_z = 1.0 / n_len, -dr / n_len
        s = t - DEPTH
        rad = r_s + s * n_r
        out = np.empty_like(v)
        out[:, 0] = -rad * np.sin(theta)     # reads left-to-right when viewed from +Y
        out[:, 1] = rad * np.cos(theta)
        out[:, 2] = z + s * n_z
        return out

    return solid.warp_batch(wrap)


# ---------------------------------------------------------------- STL export
def to_stl_bytes(man: m3d.Manifold, name: str = "engraved") -> bytes:
    mesh = man.to_mesh()
    v = np.asarray(mesh.vert_properties, dtype=np.float32)[:, :3]
    f = np.asarray(mesh.tri_verts, dtype=np.int64)
    tri = v[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, ln, out=np.zeros_like(n), where=ln > 0)
    rec = np.zeros(len(f), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
    rec["n"], rec["v"] = n, tri
    buf = io.BytesIO()
    buf.write(f"Engraved base: {name}".encode("ascii", "replace")[:80].ljust(80, b" "))
    buf.write(struct.pack("<I", len(f)))
    buf.write(rec.tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------- public API
_base: Base | None = None


def get_base() -> Base:
    global _base
    with _LOCK:
        if _base is None:
            _base = Base(BASE_STL)
        return _base


def engrave(text: str, preview: bool = False) -> tuple[str, bytes]:
    text = normalise_text(text)
    base = get_base()
    t = time.time()
    cutter = build_cutter(text, base)
    target = base.preview if preview else base.manifold
    with _LOCK:
        result = target - cutter
        data = to_stl_bytes(result, text)
    print(f"[engrave] {'preview' if preview else 'full'} '{text}': "
          f"{result.num_tri():,} tris, {len(data) / 1e6:.1f} MB, {time.time() - t:.1f}s")
    return text, data


if __name__ == "__main__":  # quick CLI test:  python engraver.py "Charlie"
    import sys
    name, stl = engrave(sys.argv[1] if len(sys.argv) > 1 else "Charlie")
    out = Path(__file__).with_name(f"Base_{name.replace(' ', '_')}.stl")
    out.write_bytes(stl)
    print("wrote", out)
