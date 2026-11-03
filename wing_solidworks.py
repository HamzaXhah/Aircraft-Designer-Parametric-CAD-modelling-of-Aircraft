#!/usr/bin/env python3
"""
Parametric wing generator for SolidWorks (pywin32 / COM API)
=============================================================

Builds a straight-tapered, swept, dihedral, twisted wing from

    AR, TR, span, sweep, dihedral, twist  +  airfoil coordinates

Pipeline
--------
1. Derive the planform: S, c_root, c_tip, LE line, MAC.
2. Load and normalise the airfoil (Selig or Lednicer .dat, or a built-in
   NACA 4-digit section for testing).
3. For n spanwise stations compute the 3-D section coordinates:
       scale to local chord -> rotate by local twist about the twist axis
       -> shift by sweep (x) and dihedral (y) -> place at span station (z)
4. Drive SolidWorks through COM:
       new part -> Global Variables (for reference) -> one reference plane
       per station (offset from Front Plane) -> one sketch per station
       (upper spline + lower spline + TE line if blunt) -> loft through all
       sections -> mirror about the root plane -> save.

Parametric workflow: change the parameters (in this file or a JSON config)
and re-run. The script is the single source of truth for the geometry.

Coordinate system in the SolidWorks part
----------------------------------------
    X  chordwise, + toward the trailing edge   (root LE at the origin)
    Y  vertical,  + up
    Z  spanwise,  + toward the tip (right wing; the left one is mirrored)
    Root airfoil lies on the Front Plane (XY).

Sign conventions
----------------
    sweep    > 0  : swept back
    dihedral > 0  : tip up
    twist    > 0  : tip nose-up (negative = washout); linear root -> tip

Requirements
------------
    Windows + SolidWorks, Python 3.8+
    pip install pywin32 numpy        (matplotlib optional, for --plot)

Usage
-----
    python wing_solidworks.py                          # defaults, builds in SolidWorks
    python wing_solidworks.py --airfoil sd7062.dat     # your airfoil file
    python wing_solidworks.py --config wing.json       # parameters from JSON
    python wing_solidworks.py --dry-run --plot         # geometry check only, no SolidWorks
    python wing_solidworks.py --save C:\\work\\wing.SLDPRT

Example wing.json (any subset of WingParams fields):
    {"aspect_ratio": 10, "taper_ratio": 0.45, "span_mm": 3000,
     "sweep_deg": 5, "sweep_ref": 0.25, "dihedral_deg": 4,
     "twist_deg": -3, "airfoil_file": "naca4415.dat", "n_stations": 6}
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, asdict, fields

import numpy as np


# =============================================================================
# 1. INPUT PARAMETERS  (edit here, or override with --config / CLI flags)
# =============================================================================
@dataclass
class WingParams:
    aspect_ratio: float = 10.0      # AR = b^2 / S
    taper_ratio: float = 0.7       # TR = c_tip / c_root
    span_mm: float = 200.0        # full tip-to-tip span b [mm]
    sweep_deg: float = 20.0        # sweep angle [deg]
    sweep_ref: float = 0.25        # chord fraction the sweep is measured on (0 = LE, 0.25 = c/4)
    dihedral_deg: float = 3.0      # dihedral angle [deg]
    twist_deg: float = -2.0        # tip twist relative to root [deg] (negative = washout)
    twist_axis: float = 0.25       # chord fraction about which sections are twisted
    n_stations: int = 10            # number of loft sections root -> tip (>= 2)
    airfoil_file: str = ""         # Selig/Lednicer .dat; empty -> NACA below
    naca: str = "2412"             # fallback NACA 4-digit section
    n_points_per_surface: int = 0  # 0 = use file points as-is; >0 = cosine resample
    mirror: bool = True            # mirror to create the full wing
    part_name: str = "Wing"


# =============================================================================
# 2. DERIVED PLANFORM
# =============================================================================
@dataclass
class Planform:
    span: float          # mm
    semi_span: float     # mm
    area: float          # mm^2 (full wing)
    c_root: float        # mm
    c_tip: float         # mm
    sweep_le_deg: float  # equivalent leading-edge sweep
    x_le_tip: float      # mm
    y_tip: float         # mm, vertical rise from dihedral
    mac: float           # mm
    z_mac: float         # mm, spanwise location of MAC
    x_le_mac: float      # mm


def validate(p: WingParams) -> None:
    if p.aspect_ratio <= 0 or p.span_mm <= 0:
        raise ValueError("AR and span must be positive.")
    if p.taper_ratio <= 0:
        raise ValueError("Taper ratio must be > 0.")
    if p.taper_ratio > 1:
        print("  [warn] TR > 1: reverse-tapered wing.")
    if p.n_stations < 2:
        raise ValueError("n_stations must be >= 2.")
    if abs(p.sweep_deg) >= 80 or abs(p.dihedral_deg) >= 45:
        raise ValueError("Sweep/dihedral out of a sensible range.")
    if not 0.0 <= p.sweep_ref <= 1.0 or not 0.0 <= p.twist_axis <= 1.0:
        raise ValueError("sweep_ref and twist_axis are chord fractions in [0, 1].")


def chord_at(p: WingParams, c_root: float, z: float, semi: float) -> float:
    return c_root * (1.0 - (1.0 - p.taper_ratio) * z / semi)


def x_le_at(p: WingParams, c_root: float, z: float, semi: float) -> float:
    """LE x-position. The reference line (sweep_ref chord fraction) is straight
    with angle sweep_deg; root LE at x = 0."""
    c = chord_at(p, c_root, z, semi)
    return z * math.tan(math.radians(p.sweep_deg)) + p.sweep_ref * (c_root - c)


def derive_planform(p: WingParams) -> Planform:
    b, lam, AR = p.span_mm, p.taper_ratio, p.aspect_ratio
    semi = b / 2.0
    S = b * b / AR
    cr = 2.0 * b / (AR * (1.0 + lam))          # from S = b*cr*(1+lam)/2
    ct = lam * cr
    x_tip = x_le_at(p, cr, semi, semi)
    sweep_le = math.degrees(math.atan2(x_tip, semi))
    y_tip = semi * math.tan(math.radians(p.dihedral_deg))
    mac = (2.0 / 3.0) * cr * (1 + lam + lam ** 2) / (1 + lam)
    z_mac = (b / 6.0) * (1 + 2 * lam) / (1 + lam)
    return Planform(b, semi, S, cr, ct, sweep_le, x_tip, y_tip, mac, z_mac,
                    x_le_at(p, cr, z_mac, semi))


# =============================================================================
# 3. AIRFOIL
#    Canonical form: unit chord, LE at (0, 0)
#      upper: TE -> LE   (x decreasing)
#      lower: LE -> TE   (x increasing), lower[0] == upper[-1] == LE
# =============================================================================
@dataclass
class Airfoil:
    name: str
    upper: np.ndarray   # (n, 2)
    lower: np.ndarray   # (m, 2)
    sharp_te: bool


def _dedupe(pts: np.ndarray, tol: float = 1e-7) -> np.ndarray:
    keep = [pts[0]]
    for q in pts[1:]:
        if np.linalg.norm(q - keep[-1]) > tol:
            keep.append(q)
    return np.array(keep)


def _finalise(name: str, upper: np.ndarray, lower: np.ndarray) -> Airfoil:
    """Normalise to unit chord with LE at the origin, tidy the TE."""
    le = upper[-1].copy()
    lower = lower.copy()
    lower[0] = le                                    # exact shared LE point
    x_te = max(upper[:, 0].max(), lower[:, 0].max())
    chord = x_te - le[0]
    upper = (upper - le) / chord
    lower = (lower - le) / chord
    upper, lower = _dedupe(upper), _dedupe(lower)

    gap = np.linalg.norm(upper[0] - lower[-1])
    sharp = gap < 1e-5
    if sharp:                                        # make the TE exactly closed
        te = 0.5 * (upper[0] + lower[-1])
        upper[0] = te
        lower[-1] = te
    return Airfoil(name, upper, lower, sharp)


def load_airfoil(path: str) -> Airfoil:
    rows, name = [], os.path.splitext(os.path.basename(path))[0]
    with open(path, "r", errors="ignore") as fh:
        for line in fh:
            parts = line.replace(",", " ").split()
            if len(parts) < 2:
                continue
            try:
                rows.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue                             # header / name line
    arr = np.array(rows, dtype=float)
    if len(arr) < 10:
        raise ValueError(f"Too few coordinates read from {path}")

    # Lednicer: first row holds point counts, then upper LE->TE, lower LE->TE
    if arr[0, 0] > 1.5 and arr[0, 1] > 1.5:
        nu, nl = int(arr[0, 0]), int(arr[0, 1])
        up = arr[1:1 + nu]
        lo = arr[1 + nu:1 + nu + nl]
        return _finalise(name, up[::-1], lo)

    return _split_loop(name, arr)


def _split_loop(name: str, arr: np.ndarray) -> Airfoil:
    """Selig-ordered loop: TE -> (one surface) -> LE -> (other surface) -> TE.
    The LE is taken as the minimum-x point."""
    i_le = int(np.argmin(arr[:, 0]))
    first, second = arr[:i_le + 1], arr[i_le:]
    if first[:, 1].mean() >= second[:, 1].mean():    # standard: upper first
        return _finalise(name, first, second)
    return _finalise(name, second[::-1], first[::-1])  # lower listed first


def naca4(code: str, n: int = 80) -> Airfoil:
    """NACA 4-digit section with closed TE, cosine spacing."""
    m, pp, t = int(code[0]) / 100, int(code[1]) / 10, int(code[2:]) / 100
    beta = np.linspace(0, math.pi, n)
    x = 0.5 * (1 - np.cos(beta))
    yt = 5 * t * (0.2969 * np.sqrt(x) - 0.1260 * x - 0.3516 * x ** 2
                  + 0.2843 * x ** 3 - 0.1036 * x ** 4)
    if m == 0 or pp == 0:
        yc, dyc = np.zeros_like(x), np.zeros_like(x)
    else:
        yc = np.where(x < pp, m / pp ** 2 * (2 * pp * x - x ** 2),
                      m / (1 - pp) ** 2 * (1 - 2 * pp + 2 * pp * x - x ** 2))
        dyc = np.where(x < pp, 2 * m / pp ** 2 * (pp - x),
                       2 * m / (1 - pp) ** 2 * (pp - x))
    th = np.arctan(dyc)
    up = np.column_stack([x - yt * np.sin(th), yc + yt * np.cos(th)])
    lo = np.column_stack([x + yt * np.sin(th), yc - yt * np.cos(th)])
    loop = np.vstack([up[::-1], lo[1:]])             # Selig order
    return _split_loop(f"NACA {code}", loop)


def resample(af: Airfoil, n: int) -> Airfoil:
    """Cosine re-spacing of each surface (linear interpolation in x)."""
    xs = 0.5 * (1 - np.cos(np.linspace(0, math.pi, n)))
    up = af.upper[::-1]                              # LE -> TE for interp
    up_s = up[np.argsort(up[:, 0])]
    lo_s = af.lower[np.argsort(af.lower[:, 0])]
    upper = np.column_stack([xs, np.interp(xs, up_s[:, 0], up_s[:, 1])])[::-1]
    lower = np.column_stack([xs, np.interp(xs, lo_s[:, 0], lo_s[:, 1])])
    return _finalise(af.name, upper, lower)


# =============================================================================
# 4. 3-D SECTIONS
# =============================================================================
@dataclass
class Section:
    z: float              # span station [mm]
    chord: float          # [mm]
    twist_deg: float
    upper: np.ndarray     # (n, 3) model coords [mm]
    lower: np.ndarray     # (m, 3) model coords [mm]
    sharp_te: bool


def build_section(p: WingParams, pf: Planform, af: Airfoil, z: float) -> Section:
    eta = z / pf.semi_span
    c = chord_at(p, pf.c_root, z, pf.semi_span)
    eps = math.radians(p.twist_deg) * eta           # linear twist
    x_le = x_le_at(p, pf.c_root, z, pf.semi_span)
    y_off = z * math.tan(math.radians(p.dihedral_deg))
    xa = p.twist_axis * c
    ce, se = math.cos(eps), math.sin(eps)

    def tf(pts2d: np.ndarray) -> np.ndarray:
        x, y = pts2d[:, 0] * c, pts2d[:, 1] * c       # scale
        dx = x - xa                                  # twist about axis
        xr = xa + dx * ce + y * se
        yr = -dx * se + y * ce                       # eps > 0 -> nose up
        return np.column_stack([xr + x_le, yr + y_off, np.full_like(xr, z)])

    return Section(z, c, math.degrees(eps), tf(af.upper), tf(af.lower), af.sharp_te)


def build_sections(p: WingParams, pf: Planform, af: Airfoil) -> list[Section]:
    zs = np.linspace(0.0, pf.semi_span, p.n_stations)
    return [build_section(p, pf, af, float(z)) for z in zs]


# =============================================================================
# 5. SOLIDWORKS BUILDER (pywin32, late-bound COM)
# =============================================================================
SW_DEFAULT_TEMPLATE_PART = 8          # swUserPreferenceStringValue_e
SW_REFPLANE_DISTANCE = 8              # swRefPlaneReferenceConstraint_Distance
SW_REFPLANE_FLIP = 256                # swRefPlaneReferenceConstraint_OptionFlip
SW_SOLID_BODY = 0                     # swBodyType_e
SW_SAVEAS_SILENT = 1                  # swSaveAsOptions_Silent
MM = 1e-3                             # SolidWorks API works in metres


class SolidWorksWingBuilder:
    def __init__(self, visible: bool = True):
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise SystemExit("pywin32 not found: pip install pywin32") from exc
        self.pythoncom = pythoncom
        self.win32 = win32com.client
        self.app = win32com.client.Dispatch("SldWorks.Application")
        self.app.Visible = visible
        self.nothing = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        self.model = None

    # ---- helpers ------------------------------------------------------------
    @staticmethod
    def _call_com_method(obj, name):
        """Call a zero-argument method without late-bound property ambiguity."""
        from win32com.client.dynamic import CDispatch

        # SolidWorks can expose no-argument methods as property getters. Then
        # obj.FirstFeature() calls the returned feature's default member and
        # raises DISP_E_MEMBERNOTFOUND. Flag the method before looking it up.
        # Generated (makepy) wrappers already know the correct invocation type.
        if isinstance(obj, CDispatch):
            obj._FlagAsMethod(name)
        return getattr(obj, name)()

    def _dbl_array(self, values) -> object:
        return self.win32.VARIANT(self.pythoncom.VT_ARRAY | self.pythoncom.VT_R8,
                                  [float(v) for v in values])

    @staticmethod
    def _apply_transform(arr16, pts: np.ndarray) -> np.ndarray:
        """Apply a SolidWorks MathTransform (ArrayData) to points.
        Layout: 9 rotation terms, 3 translation, 1 scale. Tries the row-vector
        convention first, then the transposed one, keeping whichever puts the
        points on the sketch plane (z ~ 0)."""
        a = np.array(arr16, dtype=float)
        R, t, s = a[:9].reshape(3, 3), a[9:12], a[12] if a[12] != 0 else 1.0
        cand1 = (pts @ R) * s + t
        cand2 = (pts @ R.T) * s + t
        return cand1 if np.abs(cand1[:, 2]).max() <= np.abs(cand2[:, 2]).max() else cand2

    def _default_planes(self):
        planes, feat = [], self._call_com_method(self.model, "FirstFeature")
        while feat is not None and len(planes) < 3:
            if self._call_com_method(feat, "GetTypeName2") == "RefPlane":
                planes.append(feat)
            feat = self._call_com_method(feat, "GetNextFeature")
        if len(planes) < 3:
            raise RuntimeError("Could not find the default reference planes.")
        return planes                                # [Front, Top, Right]

    # ---- document -----------------------------------------------------------
    def new_part(self):
        template = self.app.GetUserPreferenceStringValue(SW_DEFAULT_TEMPLATE_PART)
        if not template or not os.path.exists(template):
            raise RuntimeError(
                "No default part template set. In SolidWorks: Tools > Options > "
                "Default Templates.")
        self.model = self.app.NewDocument(template, 0, 0, 0)
        if self.model is None:
            raise RuntimeError("SolidWorks failed to create a new part.")
        self.ext = self.model.Extension
        self.fm = self.model.FeatureManager
        self.skm = self.model.SketchManager
        self.front = self._default_planes()[0]

    def add_global_variables(self, p: WingParams, pf: Planform):
        """Stored for reference/traceability in Tools > Equations."""
        eqs = [
            f'"AR" = {p.aspect_ratio}',
            f'"TR" = {p.taper_ratio}',
            f'"Span" = {p.span_mm}',
            f'"Sweep" = {p.sweep_deg}',
            f'"SweepRef" = {p.sweep_ref}',
            f'"Dihedral" = {p.dihedral_deg}',
            f'"Twist" = {p.twist_deg}',
            '"HalfSpan" = "Span" / 2',
            '"Area" = "Span" ^ 2 / "AR"',
            '"Croot" = 2 * "Span" / ( "AR" * ( 1 + "TR" ) )',
            '"Ctip" = "TR" * "Croot"',
            '"MAC" = 2 / 3 * "Croot" * ( 1 + "TR" + "TR" ^ 2 ) / ( 1 + "TR" )',
        ]
        try:
            mgr = self._call_com_method(self.model, "GetEquationMgr")
            for e in eqs:
                mgr.Add2(-1, e, True)
        except Exception as exc:                     # not critical for geometry
            print(f"  [warn] could not add global variables: {exc}")

    def offset_plane(self, dist_mm: float, name: str):
        def make(flags):
            self.model.ClearSelection2(True)
            self.front.Select2(False, 0)
            return self.fm.InsertRefPlane(flags, dist_mm * MM, 0, 0, 0, 0)

        feat = make(SW_REFPLANE_DISTANCE)
        if feat is None:
            raise RuntimeError(f"Failed to create plane at z = {dist_mm} mm")
        # make sure the plane went to +Z, otherwise flip it
        tz = self._call_com_method(feat, "GetSpecificFeature2").Transform.ArrayData[11]
        if abs(tz - dist_mm * MM) > 1e-7:
            self.model.ClearSelection2(True)
            feat.Select2(False, 0)
            self._call_com_method(self.model, "EditDelete")
            feat = make(SW_REFPLANE_DISTANCE | SW_REFPLANE_FLIP)
        feat.Name = name
        return feat

    def sketch_section(self, plane_feat, sec: Section, name: str) -> str:
        self.model.ClearSelection2(True)
        plane_feat.Select2(False, 0)
        self.skm.InsertSketch(True)
        sketch = self.skm.ActiveSketch
        if sketch is None:
            raise RuntimeError(f"Could not open a sketch for {name}")

        T = sketch.ModelToSketchTransform.ArrayData
        up = self._apply_transform(T, sec.upper * MM)
        lo = self._apply_transform(T, sec.lower * MM)
        if max(np.abs(up[:, 2]).max(), np.abs(lo[:, 2]).max()) > 1e-6:
            raise RuntimeError(f"{name}: section points are not on the sketch plane")
        up[:, 2] = 0.0
        lo[:, 2] = 0.0

        self.skm.AddToDB = True                      # no snapping / auto relations
        self.skm.DisplayWhenAdded = False
        try:
            if self.skm.CreateSpline2(self._dbl_array(up.ravel()), False) is None:
                raise RuntimeError(f"{name}: upper spline failed")
            if self.skm.CreateSpline2(self._dbl_array(lo.ravel()), False) is None:
                raise RuntimeError(f"{name}: lower spline failed")
            if not sec.sharp_te:                     # close a blunt trailing edge
                a, b = lo[-1], up[0]
                self.skm.CreateLine(a[0], a[1], 0.0, b[0], b[1], 0.0)
        finally:
            self.skm.AddToDB = False
            self.skm.DisplayWhenAdded = True

        self.skm.InsertSketch(True)                  # exit sketch
        feat = self.model.FeatureByPositionReverse(0)
        feat.Name = name
        return feat.Name

    def loft(self, sketch_names: list[str], sections: list[Section]):
        self.model.ClearSelection2(True)
        for nm, sec in zip(sketch_names, sections):
            te = sec.upper[0] * MM                   # pick near TE -> aligned connectors
            ok = self.ext.SelectByID2(nm, "SKETCH", te[0], te[1], te[2],
                                      True, 1, self.nothing, 0)
            if not ok:
                raise RuntimeError(f"Could not select {nm} for the loft")
        feat = self.fm.InsertProtrusionBlend2(
            False,   # Closed
            True,    # KeepTangency
            False,   # ForceNonRational
            1.0,     # TessToleranceFactor
            0, 0,    # Start/End matching: none
            1.0, 1.0,  # Start/End tangent length
            True, True,  # Start/End tangent dir
            False, 0.0, 0.0, 0,  # thin feature off
            True,    # Merge
            True,    # UseFeatScope
            True,    # UseAutoSelect
            0)       # GuideCurveInfluence
        if feat is None:
            raise RuntimeError("Loft failed. Open the part and check the section "
                               "sketches (closed profiles, connector alignment).")
        feat.Name = "Wing_Loft"
        return feat

    def mirror_body(self):
        bodies = self.model.GetBodies2(SW_SOLID_BODY, False)
        if not bodies:
            raise RuntimeError("No solid body to mirror.")
        self.model.ClearSelection2(True)
        self.front.Select2(False, 2)                 # mirror plane: mark 2
        sd = self._call_com_method(self.model.SelectionManager, "CreateSelectData")
        sd.Mark = 256                                # bodies to mirror: mark 256
        bodies[0].Select2(True, sd)
        feat = self.fm.InsertMirrorFeature2(True, False, True, False, 0)
        if feat is None:
            print("  [warn] mirror failed; half wing left in place.")
        else:
            feat.Name = "Wing_Mirror"

    def build(self, p: WingParams, pf: Planform, sections: list[Section],
              save_path: str | None = None):
        print("Connecting to SolidWorks ...")
        self.new_part()
        self.app.CommandInProgress = True            # speeds up API calls
        try:
            self.add_global_variables(p, pf)
            names = []
            for i, sec in enumerate(sections):
                print(f"  station {i}: z = {sec.z:8.2f} mm, c = {sec.chord:8.2f} mm, "
                      f"twist = {sec.twist_deg:+.2f} deg")
                plane = self.front if i == 0 else self.offset_plane(sec.z, f"Station_{i}")
                names.append(self.sketch_section(plane, sec, f"Section_{i}"))
            print("  lofting ...")
            self.loft(names, sections)
            if p.mirror:
                print("  mirroring ...")
                self.mirror_body()
            self.model.ForceRebuild3(False)
        finally:
            self.app.CommandInProgress = False
        self.model.ShowNamedView2("*Isometric", 7)
        self._call_com_method(self.model, "ViewZoomtofit2")

        if save_path:
            err = self.model.SaveAs3(os.path.abspath(save_path), 0, SW_SAVEAS_SILENT)
            print(f"  saved to {save_path}" if err == 0 else f"  [warn] save error {err}")
        print("Done.")


# =============================================================================
# 6. OUTPUT HELPERS
# =============================================================================
def print_summary(p: WingParams, pf: Planform, af: Airfoil):
    print("\n=== Wing definition ===")
    for k, v in asdict(p).items():
        print(f"  {k:22s} {v}")
    print("\n=== Derived planform ===")
    print(f"  airfoil                {af.name} ({'sharp' if af.sharp_te else 'blunt'} TE, "
          f"{len(af.upper)}+{len(af.lower)} pts)")
    print(f"  area S                 {pf.area / 1e6:.4f} m^2")
    print(f"  root chord             {pf.c_root:.2f} mm")
    print(f"  tip chord              {pf.c_tip:.2f} mm")
    print(f"  LE sweep (equivalent)  {pf.sweep_le_deg:.3f} deg")
    print(f"  tip LE x-offset        {pf.x_le_tip:.2f} mm")
    print(f"  tip rise (dihedral)    {pf.y_tip:.2f} mm")
    print(f"  MAC                    {pf.mac:.2f} mm at z = {pf.z_mac:.2f} mm, "
          f"x_LE = {pf.x_le_mac:.2f} mm\n")


def export_sections(sections: list[Section], folder: str):
    """X Y Z [mm] per station; usable with Insert > Curve > Curve Through XYZ Points."""
    os.makedirs(folder, exist_ok=True)
    for i, s in enumerate(sections):
        pts = np.vstack([s.upper, s.lower[1:]])      # TE -> upper -> LE -> lower -> TE
        np.savetxt(os.path.join(folder, f"section_{i}.txt"), pts, fmt="%.6f")
    print(f"Section coordinates written to {folder}/")


def plot_sections(sections: list[Section], pf: Planform, mirror: bool, path: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(12, 7))
    ax = fig.add_subplot(projection="3d")
    sides = [1, -1] if mirror else [1]
    for sgn in sides:
        for s in sections:
            pts = np.vstack([s.upper, s.lower[1:]])
            ax.plot(pts[:, 2] * sgn, pts[:, 0], pts[:, 1], "b-", lw=0.8)
        le = np.array([s.lower[0] for s in sections])
        te = np.array([s.upper[0] for s in sections])
        ax.plot(le[:, 2] * sgn, le[:, 0], le[:, 1], "k-", lw=1.2)
        ax.plot(te[:, 2] * sgn, te[:, 0], te[:, 1], "k-", lw=1.2)
    ax.set_xlabel("span Z [mm]")
    ax.set_ylabel("chord X [mm]")
    ax.set_zlabel("Y [mm]")
    ax.invert_yaxis()
    rng = pf.semi_span * (2 if mirror else 1)
    ax.set_box_aspect((rng, max(pf.c_root, pf.x_le_tip + pf.c_tip), rng * 0.15))
    ax.set_title("Wing sections (dry run)")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    print(f"Preview saved to {path}")


# =============================================================================
# 7. MAIN
# =============================================================================
def load_params(args) -> WingParams:
    p = WingParams()
    if args.config:
        with open(args.config) as fh:
            cfg = json.load(fh)
        valid = {f.name for f in fields(WingParams)}
        for k, v in cfg.items():
            if k not in valid:
                raise KeyError(f"Unknown parameter in config: {k}")
            setattr(p, k, v)
    if args.airfoil:
        p.airfoil_file = args.airfoil
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description="Parametric wing generator for SolidWorks")
    ap.add_argument("--config", help="JSON file with WingParams overrides")
    ap.add_argument("--airfoil", help="airfoil .dat file (Selig or Lednicer)")
    ap.add_argument("--save", help="save the part to this .SLDPRT path")
    ap.add_argument("--dry-run", action="store_true", help="geometry only, no SolidWorks")
    ap.add_argument("--plot", action="store_true", help="save a 3-D preview PNG")
    ap.add_argument("--export", metavar="DIR", help="write section XYZ files to DIR")
    args = ap.parse_args(argv)

    p = load_params(args)
    validate(p)
    pf = derive_planform(p)
    af = load_airfoil(p.airfoil_file) if p.airfoil_file else naca4(p.naca)
    if p.n_points_per_surface > 0:
        af = resample(af, p.n_points_per_surface)
    sections = build_sections(p, pf, af)
    print_summary(p, pf, af)

    if args.export:
        export_sections(sections, args.export)
    if args.plot:
        plot_sections(sections, pf, p.mirror, "wing_preview.png")
    if args.dry_run:
        return 0

    if sys.platform != "win32":
        print("SolidWorks automation needs Windows; use --dry-run elsewhere.")
        return 1
    SolidWorksWingBuilder(visible=True).build(p, pf, sections, args.save)
    return 0


if __name__ == "__main__":
    sys.exit(main())
