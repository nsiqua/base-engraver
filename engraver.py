"""Core engraving logic: wraps text around the outer wall of the urn base and cuts it in.

Supports both:
- "cat":  Domestic ShortHair_Complete_Blank.stl (full cat urn + base, text symmetrical to the cat's face)
- "base": Base with Screw2.stl (standalone threaded base ring)

Supports dynamic 3D scaling based on the cat's live weight (kg), keeping the engraved
text at a true 10 mm height (or capped to fit when the scaled base wall is < 10.6 mm tall)
and a true 1 mm depth along the surface normal of the scaled urn.

All units are millimetres.
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
CAT_STL = Path(__file__).with_name("Domestic ShortHair_Complete_Blank.stl")
FONT_FILE = Path(r"C:\Windows\Fonts\arial.ttf")
CAP_HEIGHT = 10.0          # mm, max height of letters (standard capital 'H' height = 10 mm)
DEPTH = 1.0                # mm, cut depth measured along the surface normal (uniform, incl. rounded top edge)
MAX_CHARS = 15
OVERSHOOT = 1.0            # mm the cutter sticks out beyond the wall (clean cut)
EDGE_MARGIN = 0.3          # mm, minimum margin from top/bottom faces of the scaled base
REFINE_LEN = 0.4           # mm, max edge length before wrapping (smooth curvature)
FRONT_ANGLE = 0.0          # radians; 0 = text centred on the +Y side of Base with Screw2.stl

# Weight -> Capacity -> Scale parameters
ORIG_CAPACITY_ML = 467.2   # measured usable chamber volume of the unscaled Cat Urn (Z >= 11.8 mm)
ML_PER_KG = 44.0           # 1 in^3/lb (36.12 mL/kg) + ~20% buffer for ash bag / comfortable fit
ML_PER_FL_OZ = 29.5735     # US fluid ounce
MIN_WEIGHT_KG = 1.5        # ~3.3 lbs
MAX_WEIGHT_KG = 12.0       # ~26.5 lbs
DEFAULT_WEIGHT_KG = 4.5    # ~10 lbs (typical adult Domestic Shorthair cat)

_STL_DTYPE = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
_FONT_PROPS = FontProperties(fname=str(FONT_FILE))
_CMAP = TTFont(str(FONT_FILE))["cmap"].getBestCmap()
_LOCK = threading.Lock()


class EngraveError(ValueError):
    """Raised for user-input problems (shown to the user)."""


def sizing_for_weight(weight_kg: float | None) -> dict:
    """Compute required capacity (mL, fl oz) and linear scale factor s for a given cat weight (kg)."""
    if weight_kg is None:
        w = DEFAULT_WEIGHT_KG
    else:
        try:
            w = float(weight_kg)
        except (TypeError, ValueError):
            raise EngraveError("Invalid cat weight.")
    if w < MIN_WEIGHT_KG or w > MAX_WEIGHT_KG:
        raise EngraveError(f"Cat weight must be between {MIN_WEIGHT_KG} kg and {MAX_WEIGHT_KG} kg.")
    cap_ml = w * ML_PER_KG
    cap_oz = cap_ml / ML_PER_FL_OZ
    scale = float((cap_ml / ORIG_CAPACITY_ML) ** (1.0 / 3.0))
    return {
        "weightKg": round(w, 2),
        "weightLbs": round(w * 2.20462, 1),
        "capacityMl": round(cap_ml, 1),
        "capacityOz": round(cap_oz, 1),
        "scale": scale,
    }


# ---------------------------------------------------------------- base & cat models
class Base:
    def __init__(self, base_path: Path, cat_path: Path):
        t = time.time()
        tm = trimesh.load(str(base_path), process=True)
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
        self.orig_height = self.z_max - self.z_min
        self._outer_profile(v[:, 2], r)
        self.preview = self._preview_shape()
        print(f"[base] loaded {self.manifold.num_tri():,} triangles in {time.time() - t:.1f}s; "
              f"R={self.r_outer:.3f} Z={self.z_min:.3f}..{self.z_max:.3f}")

        # Load Cat Urn if present
        self.has_cat = cat_path.exists()
        if self.has_cat:
            self._load_cat(cat_path, len(faces))

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

    def _load_cat(self, cat_path: Path, n_base_tris: int) -> None:
        """Load the full Cat Urn STL, extract non-base shells untouched, detect face symmetry,
        and build a lightweight decimated cat mesh for fast browser previewing.
        """
        t0 = time.time()
        with open(cat_path, "rb") as f:
            head = f.read(84)
            n_total = struct.unpack("<I", head[80:84])[0]
            raw = np.fromfile(f, dtype=_STL_DTYPE, count=n_total)

        # In Domestic ShortHair_Complete_Blank.stl:
        #   indices [0 : 3,858,582]         -> cat body & fragments
        #   indices [3,858,582 : 7,538,308] -> exact base ring (rotated -90° in XY, shifted +(128, 128, 5.797501))
        #   indices [7,538,308 : 7,538,564] -> 256-tri inner plug disc
        n_disc = 256
        n_cat_body = n_total - n_base_tris - n_disc
        self.cat_body_records = raw[:n_cat_body].copy()
        self.cat_disc_records = raw[n_cat_body + n_base_tris:].copy()
        self.cat_center_xy = np.array([128.0, 128.0], dtype=np.float32)
        self.cat_z_shift = float(-self.z_min)  # +5.79750109 mm so base bottom is at Z = 0

        # Detect the cat's face direction from the two ear tips + muzzle protrusion
        v0 = self.cat_body_records["v"][:, 0, :].astype(np.float64)
        v0[:, 0] -= 128.0
        v0[:, 1] -= 128.0
        z = v0[:, 2]
        self.cat_orig_height = float(z.max())
        top = v0[z > z.max() - 12.0]
        c = np.array([top[top[:, 0].argmin(), :2], top[top[:, 0].argmax(), :2]])
        for _ in range(30):
            lab = np.argmin(((top[:, None, :2] - c[None]) ** 2).sum(-1), axis=1)
            c = np.array([top[lab == k, :2].mean(0) for k in range(2)])
        tips = np.array([top[lab == k][top[lab == k][:, 2].argmax()] for k in range(2)])
        mid = tips.mean(0)
        ear_vec = tips[1, :2] - tips[0, :2]
        ear_vec /= np.linalg.norm(ear_vec)
        n = np.array([-ear_vec[1], ear_vec[0]])  # perpendicular in XY
        # Pick the side where the forehead/nose (Z 100..120) extends furthest from the base axis
        forehead = v0[(z > 100.0) & (z < 120.0), :2]
        if (forehead @ n).max() < (forehead @ (-n)).max():
            n = -n
        # Where the head's mid-plane (through `mid` along `n`) intersects the base outer wall (R = r_outer)
        b_coef = float(mid[:2] @ n)
        c_coef = float(mid[:2] @ mid[:2] - self.r_outer ** 2)
        hit = mid[:2] + (-b_coef + np.sqrt(b_coef * b_coef - c_coef)) * n
        self.cat_face_azimuth_deg = float(np.degrees(np.arctan2(n[1], n[0])))
        self.cat_text_azimuth_rad = float(np.arctan2(hit[1], hit[0]))
        # In Base local frame (before the -90° XY rotation that maps Base -> Cat):
        # A point at angle `theta` in `wrap()` has Base XY (-r sin(theta), r cos(theta)),
        # which maps to Cat XY (r cos(theta), r sin(theta)), i.e. Cat azimuth = `theta`!
        self.cat_front_angle = float(self.cat_text_azimuth_rad)

        # Build a seamless vertex-clustered preview mesh of the cat body + disc (centered at XY=(0,0), Z>=0)
        self.cat_preview_records = self._decimate_cat_for_preview(
            np.concatenate([self.cat_body_records, self.cat_disc_records]), grid=0.85
        )
        print(f"[cat] loaded {len(self.cat_body_records):,} cat tris + {len(self.cat_disc_records)} disc tris, "
              f"face azimuth={self.cat_face_azimuth_deg:+.2f}°, preview={len(self.cat_preview_records):,} tris "
              f"in {time.time() - t0:.1f}s")

    @staticmethod
    def _decimate_cat_for_preview(records: np.ndarray, grid: float = 0.85) -> np.ndarray:
        """Fast vectorized vertex-clustering decimation for smooth browser previewing."""
        v = records["v"].copy()
        v[:, :, 0] -= 128.0
        v[:, :, 1] -= 128.0
        q = np.round(v / grid).astype(np.int64)
        flat_q = q.reshape(-1, 3)
        flat_v = v.reshape(-1, 3).astype(np.float64)
        keys = (flat_q[:, 0] + 1000) * 1_000_000 + (flat_q[:, 1] + 1000) * 1_000 + (flat_q[:, 2] + 1000)
        _, inv = np.unique(keys, return_inverse=True)
        counts = np.bincount(inv)
        avg_v = np.column_stack([np.bincount(inv, flat_v[:, i]) / counts for i in range(3)]).astype(np.float32)

        tri_idx = inv.reshape(-1, 3)
        keep = (tri_idx[:, 0] != tri_idx[:, 1]) & (tri_idx[:, 1] != tri_idx[:, 2]) & (tri_idx[:, 2] != tri_idx[:, 0])
        tri_idx = tri_idx[keep]
        s_tri = np.sort(tri_idx, axis=1)
        _, u_idx = np.unique(s_tri, axis=0, return_index=True)
        tri_idx = tri_idx[np.sort(u_idx)]

        tri_v = avg_v[tri_idx]
        n = np.cross(tri_v[:, 1] - tri_v[:, 0], tri_v[:, 2] - tri_v[:, 0])
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        n = np.divide(n, ln, out=np.zeros_like(n), where=ln > 0)
        rec = np.zeros(len(tri_v), dtype=_STL_DTYPE)
        rec["n"], rec["v"] = n, tri_v
        return rec


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


def _text_polygons(text: str, target_cap_height: float = CAP_HEIGHT) -> list[np.ndarray]:
    """Flat text outline (mm).

    Standard scale sets capital 'H' height = target_cap_height (up to 10 mm). If the text's
    total vertical span (including descenders like 'y' or accents like 'Õ') exceeds
    target_cap_height, it is uniformly scaled down so the total height is target_cap_height.
    Centred on x=0.
    """
    big = 1000.0  # render large so curves are finely flattened, then scale down
    cap = TextPath((0, 0), "H", size=big, prop=_FONT_PROPS).get_extents().height
    scale = target_cap_height / cap
    path = TextPath((0, 0), text, size=big, prop=_FONT_PROPS)
    raw = [p[:-1] for p in path.to_polygons(closed_only=True) if len(p) > 3]
    if not raw:
        raise EngraveError("Text contains no printable characters.")
    all_raw = np.vstack(raw)
    span_y = (all_raw[:, 1].max() - all_raw[:, 1].min()) * scale
    if span_y > target_cap_height:
        scale *= target_cap_height / span_y
    polys = [p * scale for p in raw]
    allp = np.vstack(polys)
    cx = (allp[:, 0].min() + allp[:, 0].max()) / 2
    return [p - [cx, 0.0] for p in polys]


def build_cutter(text: str, base: Base, front_angle: float = FRONT_ANGLE,
                 scale_factor: float = 1.0) -> m3d.Manifold:
    """Build the 3D text cutter in the unscaled Base coordinate frame such that, after
    the base is scaled by `scale_factor`, the engraved letters have a physical height
    of up to 10 mm (capped to fit the scaled base wall) and a physical depth of 1 mm.
    """
    scaled_wall_height = base.orig_height * scale_factor
    max_physical_height = min(CAP_HEIGHT, scaled_wall_height - 2.0 * EDGE_MARGIN)
    if max_physical_height <= 1.0:
        raise EngraveError("Scaled base is too small for engraving.")

    # Convert physical text dimensions into the unscaled Base coordinate space
    eff_cap_height = max_physical_height / scale_factor
    eff_depth = DEPTH / scale_factor
    eff_overshoot = OVERSHOOT / scale_factor
    eff_refine = REFINE_LEN / scale_factor
    eff_margin = EDGE_MARGIN / scale_factor

    polys = _text_polygons(text, target_cap_height=eff_cap_height)
    allp = np.vstack(polys)

    # vertical placement: center the text's vertical bounding box on the base height
    z_mid = (base.z_min + base.z_max) / 2
    y_mid = (allp[:, 1].min() + allp[:, 1].max()) / 2
    y_shift = z_mid - y_mid
    lo, hi = allp[:, 1].min() + y_shift, allp[:, 1].max() + y_shift
    if lo < base.z_min + eff_margin - 1e-4 or hi > base.z_max - eff_margin + 1e-4:
        raise EngraveError("Text is too tall to fit on the side of the base.")

    width = allp[:, 0].max() - allp[:, 0].min()
    if width / base.r_outer > np.pi * 1.5:
        raise EngraveError("Text is too wide to fit around the base.")

    section = m3d.CrossSection(polys, m3d.FillRule.NonZero)
    solid = section.extrude(eff_depth + eff_overshoot).refine_to_length(eff_refine)

    R = base.r_outer

    def wrap(v: np.ndarray) -> np.ndarray:
        # x: along the text, y: up, t: 0..eff_depth+eff_overshoot through the wall (t=eff_depth is the surface)
        x, y, t = v[:, 0], v[:, 1], v[:, 2]
        theta = front_angle + x / R          # arc length measured on the outer wall
        z = y + y_shift
        # point on the actual surface (follows the rounded top edge) ...
        r_s = np.interp(z, base.prof_z, base.prof_r)
        dr = np.interp(z, base.prof_z, base.prof_dr)
        # ... offset along the surface normal, so the cut is DEPTH deep everywhere after scaling
        n_len = np.sqrt(1.0 + dr * dr)
        n_r, n_z = 1.0 / n_len, -dr / n_len
        s = t - eff_depth
        rad = r_s + s * n_r
        out = np.empty_like(v)
        out[:, 0] = -rad * np.sin(theta)     # reads left-to-right when viewed from front_angle
        out[:, 1] = rad * np.cos(theta)
        out[:, 2] = z + s * n_z
        return out

    return solid.warp_batch(wrap)


# ---------------------------------------------------------------- STL export
def _manifold_to_records(man: m3d.Manifold) -> np.ndarray:
    mesh = man.to_mesh()
    v = np.asarray(mesh.vert_properties, dtype=np.float32)[:, :3]
    f = np.asarray(mesh.tri_verts, dtype=np.int64)
    tri = v[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, ln, out=np.zeros_like(n), where=ln > 0)
    rec = np.zeros(len(f), dtype=_STL_DTYPE)
    rec["n"], rec["v"] = n, tri
    return rec


def _records_to_stl_bytes(rec: np.ndarray, header_text: str) -> bytes:
    buf = io.BytesIO()
    buf.write(header_text.encode("ascii", "replace")[:80].ljust(80, b" "))
    buf.write(struct.pack("<I", len(rec)))
    buf.write(rec.tobytes())
    return buf.getvalue()


def _transform_base_records_to_cat_space(rec: np.ndarray, z_shift: float, xy_shift: float = 0.0,
                                         scale_factor: float = 1.0) -> np.ndarray:
    """Map Base with Screw2.stl into Cat STL space, scaled uniformly by `scale_factor`
    around the base bottom-center (so Z >= 0 sits on the build plate and XY is centered at xy_shift).
    """
    out = rec.copy()
    s = np.float32(scale_factor)
    vx, vy, vz = out["v"][:, :, 0].copy(), out["v"][:, :, 1].copy(), out["v"][:, :, 2].copy()
    out["v"][:, :, 0] = s * vy + xy_shift
    out["v"][:, :, 1] = -s * vx + xy_shift
    out["v"][:, :, 2] = s * (vz + np.float32(z_shift))
    nx, ny = out["n"][:, 0].copy(), out["n"][:, 1].copy()
    out["n"][:, 0] = ny
    out["n"][:, 1] = -nx
    return out


def _scale_cat_records(rec: np.ndarray, scale_factor: float, orig_center_xy: float = 128.0,
                       target_center_xy: float = 128.0) -> np.ndarray:
    """Uniformly scale Cat STL records by `scale_factor` around (orig_center_xy, orig_center_xy, 0),
    placing the result at (target_center_xy, target_center_xy, 0). Unit normals are unchanged by uniform scaling.
    """
    if abs(scale_factor - 1.0) < 1e-6 and abs(orig_center_xy - target_center_xy) < 1e-6:
        return rec
    out = rec.copy()
    s = np.float32(scale_factor)
    out["v"][:, :, 0] = s * (out["v"][:, :, 0] - orig_center_xy) + target_center_xy
    out["v"][:, :, 1] = s * (out["v"][:, :, 1] - orig_center_xy) + target_center_xy
    out["v"][:, :, 2] = s * out["v"][:, :, 2]
    return out


def _rotate_cat_records_to_front(rec: np.ndarray, angle_rad: float, center_xy: float = 0.0) -> np.ndarray:
    """Rotate records in XY around (center_xy, center_xy) by angle_rad."""
    out = rec.copy()
    c, s = np.float32(np.cos(angle_rad)), np.float32(np.sin(angle_rad))
    vx = out["v"][:, :, 0] - center_xy
    vy = out["v"][:, :, 1] - center_xy
    out["v"][:, :, 0] = c * vx - s * vy + center_xy
    out["v"][:, :, 1] = s * vx + c * vy + center_xy
    nx, ny = out["n"][:, 0].copy(), out["n"][:, 1].copy()
    out["n"][:, 0] = c * nx - s * ny
    out["n"][:, 1] = s * nx + c * ny
    return out


# ---------------------------------------------------------------- public API
_base: Base | None = None


def get_base() -> Base:
    global _base
    with _LOCK:
        if _base is None:
            _base = Base(BASE_STL, CAT_STL)
        return _base


def engrave(text: str, preview: bool = False, model: str = "cat",
            weight_kg: float | None = None) -> tuple[str, bytes, dict]:
    text = normalise_text(text)
    sizing = sizing_for_weight(weight_kg)
    scale_factor = sizing["scale"]

    base = get_base()
    if model == "cat" and not base.has_cat:
        raise EngraveError(f"Cat model file not found: {CAT_STL.name}")

    t = time.time()
    front_angle = base.cat_front_angle if model == "cat" else FRONT_ANGLE
    cutter = build_cutter(text, base, front_angle=front_angle, scale_factor=scale_factor)
    target = base.preview if preview else base.manifold
    with _LOCK:
        result = target - cutter
        base_rec = _manifold_to_records(result)

    if model == "cat":
        if preview:
            # In preview: centre at (0, 0), Z >= 0, scaled by `scale_factor`, rotated to face +Y
            base_cat = _transform_base_records_to_cat_space(
                base_rec, z_shift=base.cat_z_shift, xy_shift=0.0, scale_factor=scale_factor
            )
            cat_prev = _scale_cat_records(
                base.cat_preview_records, scale_factor=scale_factor, orig_center_xy=0.0, target_center_xy=0.0
            )
            combined = np.concatenate([cat_prev, base_cat])
            rot_to_plus_y = (np.pi / 2) - base.cat_text_azimuth_rad
            combined = _rotate_cat_records_to_front(combined, rot_to_plus_y, center_xy=0.0)
            data = _records_to_stl_bytes(combined, f"Preview Cat Urn: {text} ({sizing['weightKg']}kg)")
            n_tris = len(combined)
        else:
            # Full export: scaled around (128, 128, 0) so it stays centered on the build plate at Z >= 0
            base_cat = _transform_base_records_to_cat_space(
                base_rec, z_shift=base.cat_z_shift, xy_shift=128.0, scale_factor=scale_factor
            )
            cat_body = _scale_cat_records(
                base.cat_body_records, scale_factor=scale_factor, orig_center_xy=128.0, target_center_xy=128.0
            )
            cat_disc = _scale_cat_records(
                base.cat_disc_records, scale_factor=scale_factor, orig_center_xy=128.0, target_center_xy=128.0
            )
            total_tris = len(cat_body) + len(base_cat) + len(cat_disc)
            buf = io.BytesIO()
            buf.write(f"Engraved Cat Urn: {text} ({sizing['weightKg']}kg)".encode("ascii", "replace")[:80].ljust(80, b" "))
            buf.write(struct.pack("<I", total_tris))
            buf.write(cat_body.tobytes())
            buf.write(base_cat.tobytes())
            buf.write(cat_disc.tobytes())
            data = buf.getvalue()
            n_tris = total_tris
    else:
        # Standalone base: scale uniformly around (0, 0, z_min) so its bottom stays at z_min * scale_factor
        if abs(scale_factor - 1.0) > 1e-6:
            base_rec = base_rec.copy()
            base_rec["v"] *= np.float32(scale_factor)
        data = _records_to_stl_bytes(base_rec, f"Engraved base: {text} ({sizing['weightKg']}kg)")
        n_tris = len(base_rec)

    print(f"[engrave] {model} {'preview' if preview else 'full'} '{text}' "
          f"({sizing['weightKg']}kg -> {sizing['capacityMl']}mL, scale={scale_factor:.3f}): "
          f"{n_tris:,} tris, {len(data) / 1e6:.1f} MB, {time.time() - t:.1f}s")
    return text, data, sizing


if __name__ == "__main__":  # quick CLI test:  python engraver.py "Charlie" [cat|base] [weight_kg]
    import sys
    arg_text = sys.argv[1] if len(sys.argv) > 1 else "Charlie"
    arg_model = sys.argv[2] if len(sys.argv) > 2 else "cat"
    arg_weight = float(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_WEIGHT_KG
    name, stl, info = engrave(arg_text, model=arg_model, weight_kg=arg_weight)
    prefix = "Cat_Urn" if arg_model == "cat" else "Base"
    out = Path(__file__).with_name(f"{prefix}_{name.replace(' ', '_')}_{info['weightKg']}kg.stl")
    out.write_bytes(stl)
    print("wrote", out, info)
