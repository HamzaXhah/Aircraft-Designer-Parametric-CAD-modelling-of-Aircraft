#!/usr/bin/env python3
"""
Parametric aircraft / UAV fuselage generator for SolidWorks (pywin32 / COM)
============================================================================

A real fuselage is not a body of revolution. Looking at typical UAVs
(MQ-9, Bayraktar TB2, Heron, Aerosonde) and light aircraft, the shape has:

  * non-circular cross-sections - wider than tall or taller than wide, with
    a rounder top and a flatter bottom (a "superellipse" / rounded box)
  * a drooped nose      - the nose tip sits below the body centreline
  * an avionics / canopy hump on top of the forward fuselage
  * a constant (or nearly constant) mid-body for payload, fuel, wing box
  * an upswept tail cone - the bottom line sweeps up strongly towards
    the empennage while the top line stays almost straight, ending in a
    small elliptical tail-end section (tail boom / rudder post)

This script builds exactly that from a handful of parameters:

    longitudinal layout : L, nose / tail length fractions
    max section         : width W, height H, upper/lower superellipse exponents
    nose                : bluntness, droop
    canopy / hump       : start, length, height
    tail                : end width / height, upsweep

Pipeline
--------
1. Side-view top/bottom lines, plan-view half-width and section exponents
   are evaluated as smooth analytic functions of x.
2. Cross-section stations are distributed along x (clustered in the nose and
   tail where curvature is high).
3. SolidWorks: one reference plane per station (offset from the Right Plane),
   one closed superellipse spline per station, a point at the nose tip, and a
   loft through all of them -> one smooth solid.

Parametric workflow: change the parameters (in FuselageParams, a JSON file
via --config, or CLI flags) and re-run. Re-running in the same part deletes
the previous fuselage first, so the geometry always follows the parameters.

Coordinate system (consistent with wing_solidworks.py)
------------------------------------------------------
    X  along the fuselage, + aft      (nose tip at x = 0)
    Y  vertical, + up                 (y = 0 is the body reference line)
    Z  lateral (spanwise)
    Cross-sections lie in planes parallel to the Right Plane (YZ).

Usage
-----
    python fuselage_sw.py                              # defaults, builds in SolidWorks
    python fuselage_sw.py -L 4000 -W 450 -H 520
    python fuselage_sw.py --config fuselage.json       # parameters from JSON
    python fuselage_sw.py --dry-run --plot             # geometry check only
    python fuselage_sw.py --save C:\\work\\fuselage.SLDPRT
    python fuselage_sw.py --new-part                   # always start a new part
    python fuselage_sw.py --cad catia                  # build in CATIA V5 instead

Example fuselage.json (any subset of FuselageParams fields):
    {"length_mm": 4000, "max_width_mm": 420, "max_height_mm": 520,
     "nose_frac": 0.18, "tail_frac": 0.48, "nose_droop": -0.3,
     "canopy_height_mm": 70, "tail_upsweep": 0.6}

Requires: Windows, SolidWorks, pywin32, numpy (matplotlib optional for --plot)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, fields

import numpy as np


# =============================================================================
# 1. INPUT PARAMETERS  (edit here, or override with --config / CLI flags)
# =============================================================================
@dataclass
class FuselageParams:
    # --- overall size ------------------------------------------------------
    length_mm: float = 3500.0        # total length L (nose tip -> tail end)
    max_width_mm: float = 440.0      # max width  W (lateral, Z)
    max_height_mm: float = 500.0     # max height H (vertical, Y), excl. canopy

    # --- longitudinal layout (fractions of L) -------------------------------
    nose_frac: float = 0.20          # nose section: tip -> start of max section
    tail_frac: float = 0.45          # tail cone: end of max section -> tail end
    #                                  mid-body = 1 - nose_frac - tail_frac

    # --- nose ---------------------------------------------------------------
    nose_bluntness: float = 2.0      # 2 = elliptical nose, >2 blunter, <2 more pointed (>1)
    nose_droop: float = -0.25        # tip height as a fraction of H/2 (negative = below centreline)

    # --- cross-section shape (superellipse exponents, 2 = ellipse) ----------
    section_n_top: float = 2.3       # upper half at max section (larger = boxier)
    section_n_bottom: float = 3.0    # lower half at max section (larger = flatter floor)
    section_n_nose: float = 2.0      # exponent at the nose tip (blends to the values above)
    section_n_tail: float = 2.0      # exponent at the tail end

    # --- canopy / avionics hump on top -------------------------------------
    canopy_start_frac: float = 0.06  # start of the hump (fraction of L)
    canopy_length_frac: float = 0.30 # length of the hump (fraction of L)
    canopy_height_mm: float = 60.0   # extra height at the hump peak (0 = no hump)
    canopy_peak_pos: float = 0.40    # peak position within the hump (0..1)

    # --- tail ---------------------------------------------------------------
    tail_end_width_mm: float = 80.0  # width of the tail-end section (0 -> pointed tail)
    tail_end_height_mm: float = 120.0  # height of the tail-end section
    tail_upsweep: float = 0.55       # tail-end centre height as a fraction of H/2 (+ = up)
    tail_shape: float = 1.0          # 1 = smooth cubic taper, >1 keeps the body full longer

    # --- discretisation ------------------------------------------------------
    n_nose_stations: int = 9         # sections in the nose (excl. tip point)
    n_mid_stations: int = 4          # sections in the mid-body
    n_tail_stations: int = 8         # sections in the tail cone
    n_section_points: int = 40       # spline points around each section

    part_name: str = "Fuselage"


# =============================================================================
# 2. GEOMETRY LAWS (pure Python, no CAD needed)
# =============================================================================
def validate(p: FuselageParams) -> list[str]:
    notes = []
    if p.length_mm <= 0 or p.max_width_mm <= 0 or p.max_height_mm <= 0:
        raise ValueError("Length, width and height must be positive.")
    if not (0 < p.nose_frac < 1 and 0 < p.tail_frac < 1):
        raise ValueError("nose_frac and tail_frac must be in (0, 1).")
    if p.nose_frac + p.tail_frac > 1.0:
        raise ValueError("nose_frac + tail_frac must be <= 1.")
    if p.nose_bluntness <= 1.0:
        raise ValueError("nose_bluntness must be > 1.")
    if not -0.9 <= p.nose_droop <= 0.9:
        raise ValueError("nose_droop must be in [-0.9, 0.9].")
    for name in ("section_n_top", "section_n_bottom", "section_n_nose", "section_n_tail"):
        if not 1.5 <= getattr(p, name) <= 8.0:
            raise ValueError(f"{name} must be in [1.5, 8].")
    if p.tail_end_width_mm < 0 or p.tail_end_height_mm < 0:
        raise ValueError("Tail-end dimensions must be >= 0.")
    if (p.tail_end_width_mm == 0) != (p.tail_end_height_mm == 0):
        raise ValueError("Set both tail-end width and height to 0 for a pointed tail.")
    if p.tail_end_width_mm > p.max_width_mm or p.tail_end_height_mm > p.max_height_mm:
        raise ValueError("Tail-end section cannot exceed the max section.")
    top_end = p.tail_upsweep * p.max_height_mm / 2 + p.tail_end_height_mm / 2
    if top_end > p.max_height_mm / 2 + 1e-9:
        raise ValueError("tail_upsweep too large: tail end would stick out above the "
                         "fuselage top line. Reduce tail_upsweep or tail_end_height_mm.")
    if p.canopy_height_mm < 0:
        raise ValueError("canopy_height_mm must be >= 0.")
    if p.canopy_height_mm > 0:
        if p.canopy_start_frac < 0 or p.canopy_start_frac + p.canopy_length_frac > 1:
            raise ValueError("Canopy must lie within the fuselage length.")
        if not 0.05 <= p.canopy_peak_pos <= 0.95:
            raise ValueError("canopy_peak_pos must be in [0.05, 0.95].")
    if min(p.n_nose_stations, p.n_tail_stations) < 3 or p.n_mid_stations < 0:
        raise ValueError("Need >= 3 nose and tail stations, >= 0 mid stations.")
    if p.n_section_points < 16:
        raise ValueError("n_section_points must be >= 16.")
    if p.tail_shape < 0.5:
        raise ValueError("tail_shape must be >= 0.5.")

    Lt = p.tail_frac * p.length_mm
    rise = (p.max_height_mm / 2) + (p.tail_upsweep * p.max_height_mm / 2
                                    - p.tail_end_height_mm / 2)
    upsweep_deg = math.degrees(math.atan(1.5 * rise / Lt))   # max slope of the cubic
    if upsweep_deg > 22:
        notes.append(f"Max tail upsweep angle {upsweep_deg:.1f} deg > 22 deg: "
                     "flow separation likely, increase tail_frac.")
    fineness = p.length_mm / math.sqrt(p.max_width_mm * p.max_height_mm)
    if not 4.0 <= fineness <= 12.0:
        notes.append(f"Fineness ratio {fineness:.1f} is outside the usual 4-12 range.")
    return notes


def _nose_growth(s: np.ndarray, k: float) -> np.ndarray:
    """0 at the tip, 1 at the end of the nose; infinite slope at the tip (round)."""
    s = np.clip(s, 0.0, 1.0)
    return (1.0 - (1.0 - s) ** k) ** (1.0 / k)


def _tail_taper(t: np.ndarray, shape: float) -> np.ndarray:
    """1 at the start of the tail cone, 0 at the end; zero slope at both ends."""
    t = np.clip(t, 0.0, 1.0) ** shape
    return 1.0 - 3.0 * t ** 2 + 2.0 * t ** 3


def _canopy_bump(x: np.ndarray, p: FuselageParams) -> np.ndarray:
    """Smooth (C1) hump on the top line, peak = canopy_height_mm."""
    if p.canopy_height_mm <= 0 or p.canopy_length_frac <= 0:
        return np.zeros_like(x)
    x0 = p.canopy_start_frac * p.length_mm
    lc = p.canopy_length_frac * p.length_mm
    u = np.clip((x - x0) / lc, 0.0, 1.0)
    # warp u so the peak lands at canopy_peak_pos, then a sin^2 bump
    a = math.log(0.5) / math.log(p.canopy_peak_pos)
    return p.canopy_height_mm * np.sin(math.pi * u ** a) ** 2


@dataclass
class Layout:
    L: float
    W: float
    H: float
    Ln: float
    Lm: float
    Lt: float
    x_nose_end: float
    x_tail_start: float
    y_tip: float          # nose tip height
    y_tail_c: float       # tail-end centre height
    pointed_tail: bool


def derive_layout(p: FuselageParams) -> Layout:
    L, W, H = p.length_mm, p.max_width_mm, p.max_height_mm
    Ln, Lt = p.nose_frac * L, p.tail_frac * L
    Lm = L - Ln - Lt
    return Layout(L, W, H, Ln, Lm, Lt, Ln, Ln + Lm,
                  p.nose_droop * H / 2, p.tail_upsweep * H / 2,
                  p.tail_end_width_mm == 0)


def body_lines(p: FuselageParams, lay: Layout, x):
    """Return half-width, top, bottom, n_top, n_bottom at the stations x [mm]."""
    x = np.asarray(x, dtype=float)
    W2, H2 = lay.W / 2, lay.H / 2
    we2, he2 = p.tail_end_width_mm / 2, p.tail_end_height_mm / 2
    top_end, bot_end = lay.y_tail_c + he2, lay.y_tail_c - he2

    half_w = np.full_like(x, W2)
    top = np.full_like(x, H2)
    bot = np.full_like(x, -H2)
    n_top = np.full_like(x, p.section_n_top)
    n_bot = np.full_like(x, p.section_n_bottom)

    nose = x < lay.x_nose_end
    f = _nose_growth(x[nose] / lay.Ln, p.nose_bluntness)
    half_w[nose] = W2 * f
    top[nose] = lay.y_tip + (H2 - lay.y_tip) * f
    bot[nose] = lay.y_tip + (-H2 - lay.y_tip) * f
    n_top[nose] = p.section_n_nose + (p.section_n_top - p.section_n_nose) * f
    n_bot[nose] = p.section_n_nose + (p.section_n_bottom - p.section_n_nose) * f

    tail = x > lay.x_tail_start
    g = _tail_taper((x[tail] - lay.x_tail_start) / lay.Lt, p.tail_shape)
    half_w[tail] = we2 + (W2 - we2) * g
    top[tail] = top_end + (H2 - top_end) * g
    bot[tail] = bot_end + (-H2 - bot_end) * g
    n_top[tail] = p.section_n_tail + (p.section_n_top - p.section_n_tail) * g
    n_bot[tail] = p.section_n_tail + (p.section_n_bottom - p.section_n_tail) * g

    top = top + _canopy_bump(x, p) * np.clip(half_w / W2, 0, 1) ** 0.5
    return half_w, top, bot, n_top, n_bot


def station_positions(p: FuselageParams, lay: Layout) -> np.ndarray:
    """x of every cross-section (excluding the nose tip point)."""
    # nose: cosine clustering towards the tip, where the curvature is highest
    i = np.arange(1, p.n_nose_stations + 1)
    xs_nose = lay.Ln * (1.0 - np.cos(0.5 * math.pi * i / p.n_nose_stations))
    xs_nose[0] = max(xs_nose[0], 0.004 * lay.Ln)
    xs_mid = np.linspace(lay.x_nose_end, lay.x_tail_start, p.n_mid_stations + 2)[1:-1]
    # tail: denser near the end where the upsweep turns
    j = np.arange(1, p.n_tail_stations + 1)
    xs_tail = lay.x_tail_start + lay.Lt * np.sin(0.5 * math.pi * j / p.n_tail_stations)
    xs = np.concatenate([xs_nose, xs_mid, xs_tail])
    # make sure the canopy is resolved: add stations inside it if it is sparse
    if p.canopy_height_mm > 0:
        x0 = p.canopy_start_frac * lay.L
        xc = x0 + np.linspace(0, 1, 7)[1:-1] * p.canopy_length_frac * lay.L
        xs = np.concatenate([xs, xc])
    xs = np.unique(np.round(xs, 6))
    # drop near-duplicates (< 1 % of L apart), always keep the tail end
    keep = [xs[0]]
    for xv in xs[1:]:
        if xv - keep[-1] > 0.01 * lay.L:
            keep.append(xv)
    keep[-1] = lay.L
    if lay.pointed_tail:
        keep = keep[:-1]                  # tail end becomes a point
    return np.array(keep)


@dataclass
class Station:
    x: float
    half_w: float
    top: float
    bot: float
    n_top: float
    n_bot: float
    pts: np.ndarray        # (m, 3) closed loop, model coords [mm], first == last


def superellipse_section(x, half_w, top, bot, n_top, n_bot, m) -> np.ndarray:
    """Closed loop starting at the +Z side point, going over the top.
    Upper and lower halves have separate exponents; both meet the side point
    with a vertical tangent, so the loop is smooth."""
    yc, hb = 0.5 * (top + bot), 0.5 * (top - bot)
    th = np.linspace(0.0, 2.0 * math.pi, m + 1)      # last == first
    c, s = np.cos(th), np.sin(th)
    n = np.where(s >= 0, n_top, n_bot)
    z = half_w * np.sign(c) * np.abs(c) ** (2.0 / n)
    y = yc + hb * np.sign(s) * np.abs(s) ** (2.0 / n)
    pts = np.column_stack([np.full_like(z, x), y, z])
    pts[-1] = pts[0]
    return pts


def build_stations(p: FuselageParams, lay: Layout) -> list[Station]:
    xs = station_positions(p, lay)
    hw, top, bot, nt, nb = body_lines(p, lay, xs)
    return [Station(float(x), float(a), float(b), float(c), float(d), float(e),
                    superellipse_section(x, a, b, c, d, e, p.n_section_points))
            for x, a, b, c, d, e in zip(xs, hw, top, bot, nt, nb)]


def _superellipse_area(a, b, n):
    """Area of the full superellipse |z/a|^n + |y/b|^n <= 1."""
    return 4 * a * b * math.gamma(1 + 1 / n) ** 2 / math.gamma(1 + 2 / n)


def integrate_properties(p: FuselageParams, lay: Layout, n=3000):
    """Volume, wetted area, max cross-section area (numerical)."""
    x = np.linspace(0, lay.L, n)
    hw, top, bot, nt, nb = body_lines(p, lay, x)
    hb = 0.5 * (top - bot)
    area = np.array([0.5 * _superellipse_area(a, b, t) + 0.5 * _superellipse_area(a, b, q)
                     for a, b, t, q in zip(hw, hb, nt, nb)])
    perim = []
    for xi, a, t_, b_, t, q in zip(x, hw, top, bot, nt, nb):
        loop = superellipse_section(xi, a, t_, b_, t, q, 120)
        perim.append(np.linalg.norm(np.diff(loop[:, 1:], axis=0), axis=1).sum())
    perim = np.array(perim)
    vol = np.trapezoid(area, x)
    wet = np.trapezoid(perim, x)
    i = int(np.argmax(area))
    return dict(volume=vol, wetted=wet, max_area=area[i], x_max_area=x[i],
                max_top=float(top.max()))


# =============================================================================
# 3. SOLIDWORKS BUILDER (pywin32, late-bound COM)
# =============================================================================
SW_DOC_PART = 1
SW_DEFAULT_TEMPLATE_PART = 8          # swUserPreferenceStringValue_e
SW_REFPLANE_DISTANCE = 8              # swRefPlaneReferenceConstraint_Distance
SW_REFPLANE_FLIP = 256                # swRefPlaneReferenceConstraint_OptionFlip
SW_DELETE_ABSORBED_AND_CHILDREN = 3
SW_SAVEAS_SILENT = 1
MM = 1e-3                             # SolidWorks API works in metres

LOFT_NAME = "Fuselage_Loft"
PLANE_PREFIX = "Fus_Station_"
SKETCH_PREFIX = "Fus_Section_"
TIP_SKETCH = "Fus_NoseTip"
TAIL_SKETCH = "Fus_TailTip"


class SolidWorksFuselageBuilder:
    def __init__(self, new_part: bool = False):
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise SystemExit("pywin32 not found: pip install pywin32") from exc
        self.pythoncom = pythoncom
        self.win32 = win32com.client
        try:
            self.app = win32com.client.GetActiveObject("SldWorks.Application")
            print("Connected to running SolidWorks session.")
        except Exception:
            print("Starting SolidWorks ...")
            self.app = win32com.client.Dispatch("SldWorks.Application")
        self.app.Visible = True
        self.nothing = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        self.force_new = new_part
        self.model = None

    # ---- helpers ------------------------------------------------------------
    @staticmethod
    def _call(obj, name):
        """Call a zero-argument method that pywin32 may mistake for a property."""
        from win32com.client.dynamic import CDispatch
        if isinstance(obj, CDispatch):
            obj._FlagAsMethod(name)
        return getattr(obj, name)()

    def _dbl_array(self, values):
        return self.win32.VARIANT(self.pythoncom.VT_ARRAY | self.pythoncom.VT_R8,
                                  [float(v) for v in values])

    @staticmethod
    def _apply_transform(arr16, pts: np.ndarray) -> np.ndarray:
        """Model -> sketch coordinates with a MathTransform's ArrayData. Picks
        whichever matrix convention puts the points on the sketch plane."""
        a = np.array(arr16, dtype=float)
        R, t, s = a[:9].reshape(3, 3), a[9:12], a[12] if a[12] != 0 else 1.0
        c1 = (pts @ R) * s + t
        c2 = (pts @ R.T) * s + t
        return c1 if np.abs(c1[:, 2]).max() <= np.abs(c2[:, 2]).max() else c2

    def _features(self):
        feat = self._call(self.model, "FirstFeature")
        while feat is not None:
            yield feat
            feat = self._call(feat, "GetNextFeature")

    def _default_planes(self):
        planes = [f for f in self._features()
                  if self._call(f, "GetTypeName2") == "RefPlane"][:3]
        if len(planes) < 3:
            raise RuntimeError("Could not find the default reference planes.")
        return planes                                 # [Front, Top, Right]

    # ---- document -----------------------------------------------------------
    def open_part(self):
        model = None if self.force_new else self.app.ActiveDoc
        if model is not None and self._call(model, "GetType") == SW_DOC_PART:
            print(f"Using active part: {self._call(model, 'GetTitle')}")
        else:
            template = self.app.GetUserPreferenceStringValue(SW_DEFAULT_TEMPLATE_PART)
            if not template or not os.path.exists(template):
                raise RuntimeError("No default part template set. In SolidWorks: "
                                   "Tools > Options > Default Templates.")
            model = self.app.NewDocument(template, 0, 0, 0)
            if model is None:
                raise RuntimeError("SolidWorks failed to create a new part.")
            print("Created new part document.")
        self.attach(model)

    def attach(self, model):
        """Work in an existing part (used by aircraft_sw.py)."""
        self.model = model
        self.ext = model.Extension
        self.fm = model.FeatureManager
        self.skm = model.SketchManager
        self.right = self._default_planes()[2]

    def delete_existing(self):
        """Remove a previous fuselage so a re-run follows the new parameters."""
        names = [LOFT_NAME, "Fuselage"]               # "Fuselage" = old revolve version
        others = [f.Name for f in self._features()
                  if f.Name.startswith((PLANE_PREFIX, SKETCH_PREFIX))
                  or f.Name in (TIP_SKETCH, TAIL_SKETCH, "Fuselage_Profile")]
        # loft first, then sketches, then planes
        others.sort(key=lambda n: n.startswith(PLANE_PREFIX))
        removed = 0
        for name in names + others:
            for feat in self._features():
                if feat.Name == name:
                    self.model.ClearSelection2(True)
                    feat.Select2(False, 0)
                    self.ext.DeleteSelection2(SW_DELETE_ABSORBED_AND_CHILDREN)
                    removed += 1
                    break
        self.model.ClearSelection2(True)
        if removed:
            print(f"Deleted {removed} features of the previous fuselage.")

    def write_global_variables(self, p: FuselageParams, lay: Layout):
        """Record inputs and derived values in Tools > Equations (reference)."""
        vals = {
            "Fus_L": f"{lay.L:.4f}mm", "Fus_W": f"{lay.W:.4f}mm",
            "Fus_H": f"{lay.H:.4f}mm", "Fus_Ln": f"{lay.Ln:.4f}mm",
            "Fus_Lm": f"{lay.Lm:.4f}mm", "Fus_Lt": f"{lay.Lt:.4f}mm",
            "Fus_NoseDroop": f"{p.nose_droop:.4f}",
            "Fus_TailUpsweep": f"{p.tail_upsweep:.4f}",
            "Fus_CanopyH": f"{p.canopy_height_mm:.4f}mm",
            "Fus_TailEndW": f"{p.tail_end_width_mm:.4f}mm",
            "Fus_TailEndH": f"{p.tail_end_height_mm:.4f}mm",
            "Fus_Fineness": f"{lay.L / math.sqrt(lay.W * lay.H):.4f}",
        }
        try:
            eqm = self._call(self.model, "GetEquationMgr")
            for i in range(self._call(eqm, "GetCount") - 1, -1, -1):
                if any(eqm.Equation(i).startswith(f'"{n}"') for n in vals):
                    eqm.Delete(i)
            for n, v in vals.items():
                eqm.Add2(-1, f'"{n}" = {v}', False)
            self._call(eqm, "EvaluateAll")
        except Exception as exc:                      # not critical for geometry
            print(f"  [warn] global variables not written: {exc}")

    def offset_plane(self, dist_mm: float, name: str):
        """Plane parallel to the Right Plane at x = dist_mm (+X = aft)."""
        def make(flags):
            self.model.ClearSelection2(True)
            self.right.Select2(False, 0)
            return self.fm.InsertRefPlane(flags, dist_mm * MM, 0, 0, 0, 0)

        feat = make(SW_REFPLANE_DISTANCE)
        if feat is None:
            raise RuntimeError(f"Failed to create plane at x = {dist_mm:.2f} mm")
        tx = self._call(feat, "GetSpecificFeature2").Transform.ArrayData[9]
        if abs(tx - dist_mm * MM) > 1e-7:              # went to -X: flip it
            self.model.ClearSelection2(True)
            feat.Select2(False, 0)
            self._call(self.model, "EditDelete")
            feat = make(SW_REFPLANE_DISTANCE | SW_REFPLANE_FLIP)
        feat.Name = name
        return feat

    def _open_sketch(self, plane_feat, label):
        self.model.ClearSelection2(True)
        plane_feat.Select2(False, 0)
        self.skm.InsertSketch(True)
        sketch = self.skm.ActiveSketch
        if sketch is None:
            raise RuntimeError(f"Could not open a sketch for {label}")
        return sketch.ModelToSketchTransform.ArrayData

    def _close_sketch(self, name):
        self.skm.InsertSketch(True)
        feat = self.model.FeatureByPositionReverse(0)
        feat.Name = name
        return feat.Name

    def sketch_point(self, plane_feat, xyz_mm, name) -> str:
        T = self._open_sketch(plane_feat, name)
        q = self._apply_transform(T, np.array([xyz_mm]) * MM)[0]
        self.skm.AddToDB = True
        try:
            if self.skm.CreatePoint(q[0], q[1], 0.0) is None:
                raise RuntimeError(f"{name}: point creation failed")
        finally:
            self.skm.AddToDB = False
        return self._close_sketch(name)

    def sketch_section(self, plane_feat, st: Station, name) -> str:
        T = self._open_sketch(plane_feat, name)
        q = self._apply_transform(T, st.pts * MM)
        if np.abs(q[:, 2]).max() > 1e-6:
            raise RuntimeError(f"{name}: section points are not on the sketch plane")
        q[:, 2] = 0.0
        q[-1] = q[0]                                   # identical end points -> closed

        self.skm.AddToDB = True                        # no snapping / auto relations
        self.skm.DisplayWhenAdded = False
        try:
            seg = self.skm.CreateSpline2(self._dbl_array(q.ravel()), False)
            if seg is None:
                # fallback: two half splines meeting at the side points
                k = len(q) // 2
                ok1 = self.skm.CreateSpline2(self._dbl_array(q[:k + 1].ravel()), False)
                ok2 = self.skm.CreateSpline2(self._dbl_array(q[k:].ravel()), False)
                if ok1 is None or ok2 is None:
                    raise RuntimeError(f"{name}: section spline failed")
        finally:
            self.skm.AddToDB = False
            self.skm.DisplayWhenAdded = True
        return self._close_sketch(name)

    def loft(self, profiles):
        """profiles: list of (sketch name, select point in model mm)."""
        def attempt(items):
            self.model.ClearSelection2(True)
            for nm, xyz in items:
                v = np.asarray(xyz) * MM
                if not self.ext.SelectByID2(nm, "SKETCH", v[0], v[1], v[2],
                                            True, 1, self.nothing, 0):
                    raise RuntimeError(f"Could not select {nm} for the loft")
            return self.fm.InsertProtrusionBlend2(
                False, True, False, 1.0,     # closed, keep tangency, non-rational, tess
                0, 0, 1.0, 1.0, True, True,  # start/end constraints: none
                False, 0.0, 0.0, 0,          # thin off
                True, True, True, 0)         # merge, feat scope, auto select, guide infl.

        feat = attempt(profiles)
        if feat is None:
            # Some SolidWorks versions refuse a point profile chosen by sketch;
            # retry without the tip point(s) - the nose then starts at a tiny section.
            trimmed = [pr for pr in profiles if pr[0] not in (TIP_SKETCH, TAIL_SKETCH)]
            if len(trimmed) != len(profiles):
                print("  [warn] loft to the tip point failed; retrying without it.")
                feat = attempt(trimmed)
        if feat is None:
            raise RuntimeError("Loft failed. Open the part and check the section "
                               "sketches (closed profiles, connector alignment).")
        feat.Name = LOFT_NAME
        return feat

    def build(self, p: FuselageParams, lay: Layout, stations: list[Station],
              save_path: str | None = None):
        self.open_part()
        self.delete_existing()
        self.app.CommandInProgress = True              # speeds up API calls
        try:
            self.write_global_variables(p, lay)
            profiles = []
            tip = (0.0, lay.y_tip, 0.0)
            profiles.append((self.sketch_point(self.right, tip, TIP_SKETCH), tip))
            for i, st in enumerate(stations):
                print(f"  station {i:2d}: x = {st.x:8.1f}  W = {2 * st.half_w:7.1f}  "
                      f"top = {st.top:7.1f}  bot = {st.bot:7.1f}  "
                      f"n = {st.n_top:.2f}/{st.n_bot:.2f}")
                plane = self.offset_plane(st.x, f"{PLANE_PREFIX}{i}")
                name = self.sketch_section(plane, st, f"{SKETCH_PREFIX}{i}")
                profiles.append((name, st.pts[0]))     # select at the +Z side point
            if lay.pointed_tail:
                plane = self.offset_plane(lay.L, f"{PLANE_PREFIX}end")
                end = (lay.L, lay.y_tail_c, 0.0)
                profiles.append((self.sketch_point(plane, end, TAIL_SKETCH), end))
            print("  lofting ...")
            self.loft(profiles)
            self._hide_construction()
            self.model.ForceRebuild3(False)
        finally:
            self.app.CommandInProgress = False
        self.model.ClearSelection2(True)
        self.model.ShowNamedView2("*Isometric", 7)
        self._call(self.model, "ViewZoomtofit2")
        self._report_mass_props()
        if save_path:
            err = self.model.SaveAs3(os.path.abspath(save_path), 0, SW_SAVEAS_SILENT)
            print(f"  saved to {save_path}" if err == 0 else f"  [warn] save error {err}")
        print("Done.")

    def _hide_construction(self):
        """Hide the station planes so the model view stays clean."""
        try:
            self.model.ClearSelection2(True)
            for f in self._features():
                if f.Name.startswith(PLANE_PREFIX):
                    f.Select2(True, 0)
            self._call(self.model, "BlankRefGeom")
            self.model.ClearSelection2(True)
        except Exception as exc:
            print(f"  [warn] could not hide station planes: {exc}")

    def _report_mass_props(self):
        try:
            mp = self._call(self.ext, "CreateMassProperty")
            print(f"  SolidWorks volume    : {mp.Volume:10.4f} m^3")
            print(f"  SolidWorks area      : {mp.SurfaceArea:10.4f} m^2")
        except Exception as exc:
            print(f"  [warn] mass properties unavailable: {exc}")


# =============================================================================
# 4. OUTPUT HELPERS
# =============================================================================
def print_summary(p: FuselageParams, lay: Layout, stations, props, notes):
    print("\n=== Fuselage definition ===")
    for k, v in asdict(p).items():
        print(f"  {k:22s} {v}")
    print("\n=== Derived ===")
    print(f"  nose / mid / tail      {lay.Ln:.1f} / {lay.Lm:.1f} / {lay.Lt:.1f} mm")
    print(f"  fineness L/sqrt(WH)    {lay.L / math.sqrt(lay.W * lay.H):.2f}")
    print(f"  nose tip height        {lay.y_tip:.1f} mm")
    print(f"  tail-end centre height {lay.y_tail_c:.1f} mm")
    print(f"  max top (with canopy)  {props['max_top']:.1f} mm")
    print(f"  max cross-section      {props['max_area'] / 1e6:.4f} m^2 at x = "
          f"{props['x_max_area']:.0f} mm")
    print(f"  volume (numeric)       {props['volume'] * 1e-9:.4f} m^3")
    print(f"  wetted area (numeric)  {props['wetted'] * 1e-6:.4f} m^2")
    print(f"  loft sections          {len(stations)} + nose tip"
          f"{' + tail tip' if lay.pointed_tail else ''}")
    for n in notes:
        print(f"  NOTE: {n}")
    print()


def plot_fuselage(p: FuselageParams, lay: Layout, stations, path: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x = np.linspace(0, lay.L, 600)
    hw, top, bot, _, _ = body_lines(p, lay, x)
    fig = plt.figure(figsize=(14, 9))
    ax1 = fig.add_subplot(2, 2, (1, 2))
    ax1.plot(x, top, "k-", x, bot, "k-")
    for st in stations:
        ax1.plot([st.x, st.x], [st.bot, st.top], color="0.75", lw=0.6)
    ax1.set_title("Side view (Y up)")
    ax1.set_aspect("equal")
    ax1.grid(alpha=0.3)

    ax2 = fig.add_subplot(2, 2, 3)
    ax2.plot(x, hw, "k-", x, -hw, "k-")
    for st in stations:
        ax2.plot([st.x, st.x], [-st.half_w, st.half_w], color="0.75", lw=0.6)
    ax2.set_title("Top view")
    ax2.set_aspect("equal")
    ax2.grid(alpha=0.3)

    ax3 = fig.add_subplot(2, 2, 4)
    cmap = plt.get_cmap("viridis")
    for i, st in enumerate(stations):
        ax3.plot(st.pts[:, 2], st.pts[:, 1], color=cmap(i / max(1, len(stations) - 1)),
                 lw=0.8)
    ax3.set_title("Cross-sections (front view, nose = dark)")
    ax3.set_aspect("equal")
    ax3.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    print(f"Preview saved to {path}")


# =============================================================================
# 5. MAIN
# =============================================================================
def load_params(args) -> FuselageParams:
    p = FuselageParams()
    if args.config:
        with open(args.config) as fh:
            cfg = json.load(fh)
        valid = {f.name for f in fields(FuselageParams)}
        for k, v in cfg.items():
            if k not in valid:
                raise KeyError(f"Unknown parameter in config: {k}")
            setattr(p, k, v)
    for flag, attr in (("length", "length_mm"), ("width", "max_width_mm"),
                       ("height", "max_height_mm")):
        if getattr(args, flag) is not None:
            setattr(p, attr, getattr(args, flag))
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description="Parametric fuselage generator for SolidWorks "
                                             "and CATIA V5")
    ap.add_argument("--cad", choices=["solidworks", "catia"], default="solidworks",
                    help="CAD system to build in (default: %(default)s)")
    ap.add_argument("--config", help="JSON file with FuselageParams overrides")
    ap.add_argument("-L", "--length", type=float, help="fuselage length [mm]")
    ap.add_argument("-W", "--width", type=float, help="max width [mm]")
    ap.add_argument("-H", "--height", type=float, help="max height [mm]")
    ap.add_argument("--save", help="save the part to this .SLDPRT / .CATPart path")
    ap.add_argument("--new-part", action="store_true",
                    help="always create a new part instead of using the active one")
    ap.add_argument("--dry-run", action="store_true", help="geometry only, no CAD")
    ap.add_argument("--plot", action="store_true", help="save fuselage_preview.png")
    args = ap.parse_args(argv)

    p = load_params(args)
    notes = validate(p)
    lay = derive_layout(p)
    stations = build_stations(p, lay)
    props = integrate_properties(p, lay)
    print_summary(p, lay, stations, props, notes)

    if args.plot:
        plot_fuselage(p, lay, stations, "fuselage_preview.png")
    if args.dry_run:
        return 0
    if sys.platform != "win32":
        print("CAD automation needs Windows; use --dry-run elsewhere.")
        return 1
    if args.cad == "catia":
        import catia_builder as C
        saved = C.build_component(
            "fuselage", lambda cp: C.build_fuselage(cp, p, lay, stations), p, args.save,
            reuse="never" if args.new_part else "project")
        return 0 if saved else 1
    SolidWorksFuselageBuilder(new_part=args.new_part).build(p, lay, stations, args.save)
    return 0


if __name__ == "__main__":
    sys.exit(main())
