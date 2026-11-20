#!/usr/bin/env python3
"""
Parametric tail (empennage) generator for SolidWorks (pywin32 / COM)
====================================================================

Builds a complete tail from a handful of parameters, in one of seven
configurations:

    conventional   low-set horizontal stabiliser + single vertical fin
    t_tail         stabiliser on top of the fin
    cruciform      stabiliser part-way up the fin
    h_tail         stabiliser with twin endplate fins at its tips
    v_tail         two inclined surfaces (ruddervators)
    inverted_v     V-tail pointing down (A-tail)
    y_tail         V-tail plus a ventral fin (MQ-9 style)

Sizing (realistic by construction)
----------------------------------
The surfaces are sized with tail volume coefficients from the wing you
already defined in wing_solidworks.py (or a --wing-config JSON):

    S_h = V_h * S_w * MAC_w / l_t          (pitch)
    S_v = V_v * S_w * b_w   / l_t          (yaw)

Typical values: V_h = 0.4-0.7, V_v = 0.02-0.05. For V / inverted-V / Y tails
the Purser-Campbell rule turns S_h and S_v into one pair of inclined panels:

    S_V = S_h + S_v,        dihedral = atan( sqrt(S_v / S_h) )

so every configuration gives the same pitch and yaw authority. Instead of
sizing, you can also give h_area_mm2 / v_area_mm2 directly.

Each surface is a straight-tapered, swept trapezoid (AR, taper, c/4 sweep, like
the wing) with a symmetric NACA 4-digit or .dat airfoil section.

Pipeline
--------
1. Wing reference (S_w, MAC_w, b_w) -> S_h, S_v -> panels for the chosen
   configuration (span, chords, sweep, dihedral, position).
2. Each panel = root + tip airfoil section. Panels inclined less than 60 deg
   from horizontal are cut by planes parallel to the Front Plane (XY); steeper
   ones (fins) by planes parallel to the Top Plane (XZ). The section is
   stretched by 1/cos(dihedral) in the cut plane, so the section normal to
   the panel is exactly the chosen airfoil.
3. SolidWorks: one sketch per section (upper + lower spline), one two-profile
   loft per panel side, all merged into a single body.

Parametric workflow: change the parameters (TailParams, --config JSON or CLI
flags) and re-run. Re-running in the same part deletes the previous tail
(all features named "Tail_*") first.

Coordinate system (consistent with wing_solidworks.py / fuselage_sw.py)
------------------------------------------------------------------------
    X  aft      x = 0 at the tail's pitch aerodynamic centre (c/4 of the
                stabiliser / V-tail MAC) = the end of the tail arm, so in the
                aircraft the part goes at x = x_wing_AC + l_t
    Y  up       y = 0 at the root of the fin / V-tail (fuselage reference line)
    Z  spanwise (right side +Z, left side mirrored)
    origin_x_mm / origin_y_mm move the whole tail, e.g. onto the fuselage
    tail cone when building into the fuselage part.

Usage
-----
    python tail_sw.py                                  # conventional, builds in SolidWorks
    python tail_sw.py -c v_tail
    python tail_sw.py -c t_tail --config tail.json
    python tail_sw.py -c y_tail --wing-config wing.json --dry-run --plot
    python tail_sw.py --list                           # show the configurations
    python tail_sw.py -c h_tail --cad catia            # build in CATIA V5 instead
    python tail_sw.py --new-part --save C:\\work\\tail.SLDPRT

Example tail.json (any subset of TailParams fields):
    {"configuration": "t_tail", "tail_arm_mm": 1600, "vh": 0.6, "vv": 0.05,
     "h_aspect_ratio": 4.0, "v_sweep_deg": 35, "naca": "0012"}

Requires: Windows, SolidWorks, pywin32, numpy (matplotlib optional for --plot)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, fields, replace

import numpy as np

from wing_solidworks import (Airfoil, WingParams, check_resample_points, derive_planform,
                             load_airfoil, naca4, resample)


CONFIGURATIONS = {
    "conventional": "low-set horizontal stabiliser + single vertical fin",
    "t_tail":       "stabiliser on top of the fin",
    "cruciform":    "stabiliser part-way up the fin (stab_height_frac)",
    "h_tail":       "stabiliser with twin endplate fins at its tips",
    "v_tail":       "two inclined surfaces (ruddervators), Purser-Campbell sized",
    "inverted_v":   "V-tail pointing down (A-tail)",
    "y_tail":       "V-tail plus a ventral fin (ventral_fin_frac)",
}


# =============================================================================
# 1. INPUT PARAMETERS  (edit here, or override with --config / CLI flags)
# =============================================================================
@dataclass
class TailParams:
    configuration: str = "conventional"   # one of CONFIGURATIONS

    # --- sizing from the wing (wing_solidworks.py / --wing-config) ---------
    tail_arm_mm: float = 0.0       # wing AC -> tail AC distance l_t; 0 -> 3 x wing MAC
    vh: float = 0.50               # horizontal tail volume coefficient S_h*l_t/(S_w*MAC_w)
    vv: float = 0.04               # vertical tail volume coefficient   S_v*l_t/(S_w*b_w)
    h_area_mm2: float = 0.0        # > 0: use this S_h instead of vh sizing
    v_area_mm2: float = 0.0        # > 0: use this S_v instead of vv sizing (all fins together)

    # --- horizontal stabiliser (also used for the V-tail panels) -----------
    h_aspect_ratio: float = 4.5    # b_h^2 / S_h   (full span, both sides)
    h_taper_ratio: float = 0.6     # c_tip / c_root
    h_sweep_deg: float = 8.0       # quarter-chord sweep
    h_dihedral_deg: float = 0.0    # stabiliser dihedral (ignored for V-tails)
    h_incidence_deg: float = 0.0   # setting angle, + nose up (typically 0 .. -3)

    # --- vertical fin ------------------------------------------------------
    v_aspect_ratio: float = 1.6    # h^2 / S_v   (per fin)
    v_taper_ratio: float = 0.55
    v_sweep_deg: float = 30.0      # quarter-chord sweep

    # --- configuration specific -----------------------------------------
    v_tail_angle_deg: float = 0.0  # V/inv-V/Y panel angle from horizontal; 0 -> Purser-Campbell
    stab_height_frac: float = 0.5  # cruciform: stabiliser height / fin height
    fin_below_frac: float = 0.3    # h_tail: share of each endplate fin below the stabiliser
    ventral_fin_frac: float = 0.3  # y_tail: share of S_v carried by the ventral fin
    fin_x_offset_mm: float = 0.0   # move the fin(s) aft (+) / forward (-) from the default spot

    # --- section and placement -------------------------------------------
    naca: str = "0010"             # symmetric NACA 4-digit section for all surfaces
    airfoil_file: str = ""         # Selig/Lednicer .dat instead of the NACA section
    n_points_per_surface: int = 0  # 0 = airfoil points as-is; > 0 = cosine resample
    origin_x_mm: float = 0.0       # shift of the whole tail in the part
    origin_y_mm: float = 0.0

    part_name: str = "Tail"


# =============================================================================
# 2. GEOMETRY (pure Python, no CAD needed)
# =============================================================================
@dataclass
class WingRef:
    area: float     # mm^2
    mac: float      # mm
    span: float     # mm


@dataclass
class Panel:
    """One straight-tapered lifting surface (one side if mirrored).

    The panel axis starts at root_le and points along (0, sin d, cos d) with
    d = dihedral_deg: 0 = horizontal to the right, 90 = vertical up, -90 = down.
    """
    name: str
    root_le: tuple          # (x, y, z) of the root leading edge [mm]
    c_root: float
    c_tip: float
    span: float             # true length along the panel axis [mm]
    sweep_deg: float        # quarter-chord sweep
    dihedral_deg: float
    incidence_deg: float = 0.0
    mirror: bool = False    # also build the mirror image (z -> -z)

    def chord(self, s):
        return self.c_root + (self.c_tip - self.c_root) * s / self.span

    def x_le(self, s):
        """LE x relative to the root LE (straight c/4 line)."""
        return s * math.tan(math.radians(self.sweep_deg)) + 0.25 * (self.c_root - self.chord(s))

    def axis_point(self, s):
        d = math.radians(self.dihedral_deg)
        return (self.root_le[1] + s * math.sin(d), self.root_le[2] + s * math.cos(d))

    @property
    def area(self):         # one side
        return 0.5 * (self.c_root + self.c_tip) * self.span

    @property
    def taper(self):
        return self.c_tip / self.c_root

    @property
    def mac(self):
        lam = self.taper
        return (2.0 / 3.0) * self.c_root * (1 + lam + lam ** 2) / (1 + lam)

    @property
    def s_mac(self):
        lam = self.taper
        return self.span / 3.0 * (1 + 2 * lam) / (1 + lam)

    @property
    def x_ac(self):
        """Absolute x of the aerodynamic centre (c/4 of the MAC)."""
        return self.root_le[0] + self.x_le(self.s_mac) + 0.25 * self.mac

    @property
    def sweep_le_deg(self):
        return math.degrees(math.atan2(self.x_le(self.span), self.span))

    @property
    def plane_kind(self):
        """'front' (cut by z = const) for shallow panels, 'top' (y = const) for fins."""
        return "front" if abs(math.cos(math.radians(self.dihedral_deg))) >= 0.5 - 1e-9 else "top"

    def moved_x(self, x_root_le):
        return replace(self, root_le=(x_root_le, self.root_le[1], self.root_le[2]))


def trapezoid(area, aspect_ratio, taper):
    """Span and chords of a trapezoid with the given area and AR = b^2 / S."""
    b = math.sqrt(aspect_ratio * area)
    cr = 2.0 * area / (b * (1.0 + taper))
    return b, cr, taper * cr


@dataclass
class TailLayout:
    config: str
    wing: WingRef
    tail_arm: float
    s_h: float              # sized horizontal area (requirement)
    s_v: float              # sized vertical area (requirement)
    v_angle_deg: float      # V-tail panel angle (0 if not a V-type tail)
    panels: list


def validate(p: TailParams) -> list[str]:
    notes = []
    if p.configuration not in CONFIGURATIONS:
        raise ValueError(f"Unknown configuration '{p.configuration}'. "
                         f"Choose from: {', '.join(CONFIGURATIONS)}")
    for name in ("h_aspect_ratio", "v_aspect_ratio", "h_taper_ratio", "v_taper_ratio"):
        if getattr(p, name) <= 0:
            raise ValueError(f"{name} must be > 0.")
    if p.h_area_mm2 < 0 or p.v_area_mm2 < 0 or p.tail_arm_mm < 0:
        raise ValueError("h_area_mm2, v_area_mm2 and tail_arm_mm must be >= 0.")
    if p.h_area_mm2 == 0 and p.vh <= 0:
        raise ValueError("vh must be > 0 (or give h_area_mm2).")
    if p.v_area_mm2 == 0 and p.vv <= 0:
        raise ValueError("vv must be > 0 (or give v_area_mm2).")
    if abs(p.h_sweep_deg) >= 60 or abs(p.v_sweep_deg) >= 70:
        raise ValueError("h_sweep_deg must be within +-60 deg and v_sweep_deg within +-70 deg.")
    if abs(p.h_dihedral_deg) > 30:
        raise ValueError("h_dihedral_deg must be within +-30 deg.")
    if p.v_tail_angle_deg and not 15 <= p.v_tail_angle_deg <= 60:
        raise ValueError("v_tail_angle_deg must be in [15, 60] (or 0 for automatic).")
    if not 0.2 <= p.stab_height_frac <= 0.9:
        raise ValueError("stab_height_frac must be in [0.2, 0.9].")
    if not 0.0 < p.fin_below_frac < 0.6:
        raise ValueError("fin_below_frac must be in (0, 0.6).")
    if not 0.0 < p.ventral_fin_frac < 0.6:
        raise ValueError("ventral_fin_frac must be in (0, 0.6).")
    if len(p.naca) != 4 or not p.naca.isdigit():
        raise ValueError("naca must be a 4-digit code, e.g. '0010'.")
    check_resample_points(p.n_points_per_surface)
    if p.naca[:2] != "00" and not p.airfoil_file:
        notes.append(f"NACA {p.naca} is cambered; tail surfaces are normally symmetric.")
    if not 3.0 <= p.h_aspect_ratio <= 6.0:
        notes.append(f"Stabiliser AR {p.h_aspect_ratio} outside the usual 3-6.")
    if not 1.0 <= p.v_aspect_ratio <= 2.2:
        notes.append(f"Fin AR {p.v_aspect_ratio} outside the usual 1-2.")
    if p.configuration == "t_tail" and p.v_taper_ratio < 0.65:
        notes.append("T-tail fins usually have taper >= 0.7 to carry the stabiliser.")
    return notes


def wing_reference(wing_config: str | None) -> WingRef:
    wp = WingParams()
    if wing_config:
        with open(wing_config) as fh:
            for k, v in json.load(fh).items():
                if k in {f.name for f in fields(WingParams)}:
                    setattr(wp, k, v)
    pf = derive_planform(wp)
    return WingRef(pf.area, pf.mac, pf.span)


def derive_layout(p: TailParams, wing: WingRef) -> TailLayout:
    cfg = p.configuration
    lt = p.tail_arm_mm if p.tail_arm_mm > 0 else 3.0 * wing.mac
    s_h = p.h_area_mm2 if p.h_area_mm2 > 0 else p.vh * wing.area * wing.mac / lt
    s_v = p.v_area_mm2 if p.v_area_mm2 > 0 else p.vv * wing.area * wing.span / lt

    def stab(area, y=0.0, dihedral=p.h_dihedral_deg, name="HStab"):
        """Mirrored stabiliser with its AC at x = 0."""
        b, cr, ct = trapezoid(area, p.h_aspect_ratio, p.h_taper_ratio)
        pan = Panel(name, (0.0, y, 0.0), cr, ct, b / 2, p.h_sweep_deg, dihedral,
                    p.h_incidence_deg, mirror=True)
        return pan.moved_x(-pan.x_ac)

    def fin(area, name, dihedral=90.0):
        """Single fin rooted at the origin height (y = 0) on the centreline."""
        h, cr, ct = trapezoid(area, p.v_aspect_ratio, p.v_taper_ratio)
        return Panel(name, (0.0, 0.0, 0.0), cr, ct, h, p.v_sweep_deg, dihedral)

    def align_mid_chord(f: Panel, s_f: float, x_mid: float) -> Panel:
        """Place fin so its chord at s_f has mid-chord at x_mid (+ fin offset)."""
        x0 = x_mid - 0.5 * f.chord(s_f) - f.x_le(s_f) + p.fin_x_offset_mm
        return f.moved_x(x0)

    def make_panels(s_h, s_v):
        v_angle = 0.0
        if cfg == "conventional":
            h = stab(s_h)
            v = fin(s_v, "VFin")
            panels = [h, v.moved_x(v.root_le[0] - v.x_ac + p.fin_x_offset_mm)]

        elif cfg in ("t_tail", "cruciform"):
            v = fin(s_v, "VFin")
            frac = 1.0 if cfg == "t_tail" else p.stab_height_frac
            s_j = frac * v.span
            h = stab(s_h, y=v.axis_point(s_j)[0])
            v = align_mid_chord(v, s_j, h.root_le[0] + 0.5 * h.c_root)
            panels = [h, v]

        elif cfg == "h_tail":
            h = stab(s_h)
            y_t, z_t = h.axis_point(h.span)
            x_mid = h.root_le[0] + h.x_le(h.span) + 0.5 * h.c_tip
            # each endplate fin carries S_v / 2 and has height sqrt(AR * S_v / 2); it is
            # split at the stabiliser into an upper and a lower part, both tapering
            # from the same root chord to taper * root, so the area is exactly S_v / 2
            hf, cr, ct = trapezoid(s_v / 2, p.v_aspect_ratio, p.v_taper_ratio)
            up = Panel("VFinUpper", (0.0, y_t, z_t), cr, ct, (1 - p.fin_below_frac) * hf,
                       p.v_sweep_deg, 90.0, 0.0, mirror=True)
            lo = Panel("VFinLower", (0.0, y_t, z_t), cr, ct, p.fin_below_frac * hf,
                       p.v_sweep_deg, -90.0, 0.0, mirror=True)
            panels = [h, align_mid_chord(up, 0.0, x_mid), align_mid_chord(lo, 0.0, x_mid)]

        else:                                      # V-type tails
            s_v_v = s_v * (1 - p.ventral_fin_frac) if cfg == "y_tail" else s_v
            v_angle = p.v_tail_angle_deg or math.degrees(math.atan(math.sqrt(s_v_v / s_h)))
            sign = -1.0 if cfg == "inverted_v" else 1.0
            panels = [stab(s_h + s_v_v, dihedral=sign * v_angle, name="VTail")]
            if cfg == "y_tail":
                vf = fin(s_v * p.ventral_fin_frac, "VentralFin", dihedral=-90.0)
                panels.append(vf.moved_x(vf.root_le[0] - vf.x_ac + p.fin_x_offset_mm))
        return panels, v_angle

    panels, v_angle = make_panels(s_h, s_v)
    lay = TailLayout(cfg, wing, lt, s_h, s_v, v_angle, panels)

    # The nominal sizing assumes every surface sits exactly at the tail arm. Where
    # the configuration moves a surface (fin under a T-tail, fins at the stabiliser
    # tips, fin offset) re-size it on its real arm so the volume coefficients hold.
    # V-type tails are not iterated: all their surfaces sit at the arm, and a fixed
    # V angle cannot meet both targets anyway.
    if cfg not in ("v_tail", "inverted_v", "y_tail"):
        for _ in range(30):
            _, _, vh, vv = effective_volumes(lay)
            # a surface moved ahead of the wing (arm <= 0) cannot be re-sized; it
            # keeps its nominal size and the volume constraints report it
            kh = p.vh / vh if p.h_area_mm2 == 0 and vh > 0 else 1.0
            kv = p.vv / vv if p.v_area_mm2 == 0 and vv > 0 else 1.0
            if abs(kh - 1) < 1e-6 and abs(kv - 1) < 1e-6:
                break
            s_h, s_v = s_h * kh, s_v * kv
            panels, v_angle = make_panels(s_h, s_v)
            lay = TailLayout(cfg, wing, lt, s_h, s_v, v_angle, panels)
    return lay


def effective_volumes(lay: TailLayout):
    """Purser-Campbell projected areas and the resulting volume coefficients."""
    sh = sv = vh = vv = 0.0
    for pan in lay.panels:
        a = pan.area * (2 if pan.mirror else 1)
        c2 = math.cos(math.radians(pan.dihedral_deg)) ** 2
        arm = lay.tail_arm + pan.x_ac
        sh += a * c2
        sv += a * (1 - c2)
        vh += a * c2 * arm / (lay.wing.area * lay.wing.mac)
        vv += a * (1 - c2) * arm / (lay.wing.area * lay.wing.span)
    return sh, sv, vh, vv


# -----------------------------------------------------------------------------
# 3-D sections
# -----------------------------------------------------------------------------
@dataclass
class Section:
    plane: str            # "front" (z = offset) or "top" (y = offset)
    offset: float         # plane position [mm]
    chord: float
    upper: np.ndarray     # (n, 3) model coords [mm], TE -> LE
    lower: np.ndarray     # (m, 3) model coords [mm], LE -> TE
    sharp_te: bool


def panel_sections(pan: Panel, af: Airfoil, ox: float, oy: float, side: int) -> list[Section]:
    """Root and tip section of one panel side (side = +1 right, -1 mirrored)."""
    d = math.radians(pan.dihedral_deg)
    cd, sd = math.cos(d), math.sin(d)
    eps = math.radians(pan.incidence_deg)
    ce, se = math.cos(eps), math.sin(eps)
    kind = pan.plane_kind
    out = []
    for s in (0.0, pan.span):
        c = pan.chord(s)
        x_le = ox + pan.root_le[0] + pan.x_le(s)
        ya, za = pan.axis_point(s)
        ya += oy

        def tf(uv):
            dx = uv[:, 0] - 0.25                     # incidence about c/4, + nose up
            xi = 0.25 + dx * ce + uv[:, 1] * se
            eta = -dx * se + uv[:, 1] * ce
            x = x_le + xi * c
            if kind == "front":                      # cut by z = za: stretch by 1/cos
                y = ya + eta * c / cd
                z = np.full_like(x, za)
            else:                                    # cut by y = ya: stretch by 1/sin
                y = np.full_like(x, ya)
                z = za - eta * c / sd
            return np.column_stack([x, y, side * z])

        offset = side * za if kind == "front" else ya
        out.append(Section(kind, offset, c, tf(af.upper), tf(af.lower), af.sharp_te))
    return out


def panel_sides(pan: Panel):
    """(tag, side) pairs for the lofts of a panel."""
    return [(f"{pan.name}_R", 1), (f"{pan.name}_L", -1)] if pan.mirror else [(pan.name, 1)]


def all_sections(p: TailParams, lay: TailLayout, af: Airfoil):
    """[(loft tag, panel index, [root, tip])] for every panel side."""
    return [(tag, i, panel_sections(pan, af, p.origin_x_mm, p.origin_y_mm, side))
            for i, pan in enumerate(lay.panels) for tag, side in panel_sides(pan)]


# =============================================================================
# 3. SOLIDWORKS BUILDER (pywin32, late-bound COM)
# =============================================================================
SW_DOC_PART = 1
SW_DEFAULT_TEMPLATE_PART = 8          # swUserPreferenceStringValue_e
SW_REFPLANE_DISTANCE = 8              # swRefPlaneReferenceConstraint_Distance
SW_REFPLANE_FLIP = 256                # swRefPlaneReferenceConstraint_OptionFlip
SW_DELETE_ABSORBED_AND_CHILDREN = 3
SW_SOLID_BODY = 0
SW_SAVEAS_SILENT = 1
MM = 1e-3                             # SolidWorks API works in metres

PREFIX = "Tail_"
PLANE_PREFIX = PREFIX + "Plane_"
SKETCH_PREFIX = PREFIX + "Sk_"
LOFT_PREFIX = PREFIX + "Loft_"


class SolidWorksTailBuilder:
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
        self._planes = {}

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
        front, top, _ = self._default_planes()
        self.base = {"front": front, "top": top}
        self._planes = {}

    def delete_existing(self):
        """Remove a previous tail so a re-run follows the new parameters."""
        names = [f.Name for f in self._features() if f.Name.startswith(PREFIX)]
        # lofts first, then sketches, then planes
        order = {LOFT_PREFIX: 0, SKETCH_PREFIX: 1, PLANE_PREFIX: 2}
        names.sort(key=lambda n: next((v for k, v in order.items() if n.startswith(k)), 0))
        removed = 0
        for name in names:
            for feat in self._features():
                if feat.Name == name:
                    self.model.ClearSelection2(True)
                    feat.Select2(False, 0)
                    self.ext.DeleteSelection2(SW_DELETE_ABSORBED_AND_CHILDREN)
                    removed += 1
                    break
        self.model.ClearSelection2(True)
        if removed:
            print(f"Deleted {removed} features of the previous tail.")

    def write_global_variables(self, lay: TailLayout):
        """Record inputs and derived values in Tools > Equations (reference)."""
        sh, sv, vh, vv = effective_volumes(lay)
        vals = {
            "Tail_Arm": f"{lay.tail_arm:.4f}mm",
            "Tail_Sh": f"{sh:.4f}", "Tail_Sv": f"{sv:.4f}",
            "Tail_Vh": f"{vh:.5f}", "Tail_Vv": f"{vv:.5f}",
            "Tail_VAngle": f"{lay.v_angle_deg:.4f}",
        }
        for pan in lay.panels:
            vals[f"Tail_{pan.name}_Span"] = f"{pan.span:.4f}mm"
            vals[f"Tail_{pan.name}_Croot"] = f"{pan.c_root:.4f}mm"
            vals[f"Tail_{pan.name}_Ctip"] = f"{pan.c_tip:.4f}mm"
        try:
            eqm = self._call(self.model, "GetEquationMgr")
            for i in range(self._call(eqm, "GetCount") - 1, -1, -1):
                if eqm.Equation(i).startswith('"Tail_'):
                    eqm.Delete(i)
            for n, v in vals.items():
                eqm.Add2(-1, f'"{n}" = {v}', False)
            self._call(eqm, "EvaluateAll")
        except Exception as exc:                      # not critical for geometry
            print(f"  [warn] global variables not written: {exc}")

    def plane(self, kind: str, dist_mm: float):
        """Plane parallel to the Front (kind='front', z = dist) or Top (y = dist)
        plane. Re-uses planes already made at the same position."""
        if abs(dist_mm) < 1e-6:
            return self.base[kind]
        key = (kind, round(dist_mm, 5))
        if key in self._planes:
            return self._planes[key]
        idx = 11 if kind == "front" else 10           # translation term z / y

        def make(flags):
            self.model.ClearSelection2(True)
            self.base[kind].Select2(False, 0)
            return self.fm.InsertRefPlane(flags, abs(dist_mm) * MM, 0, 0, 0, 0)

        feat = make(SW_REFPLANE_DISTANCE)
        if feat is None:
            raise RuntimeError(f"Failed to create {kind} plane at {dist_mm:.2f} mm")
        t = self._call(feat, "GetSpecificFeature2").Transform.ArrayData[idx]
        if abs(t - dist_mm * MM) > 1e-7:              # went the wrong way: flip it
            self.model.ClearSelection2(True)
            feat.Select2(False, 0)
            self._call(self.model, "EditDelete")
            feat = make(SW_REFPLANE_DISTANCE | SW_REFPLANE_FLIP)
        feat.Name = f"{PLANE_PREFIX}{len(self._planes)}"
        self._planes[key] = feat
        return feat

    def sketch_section(self, sec: Section, name: str) -> str:
        self.model.ClearSelection2(True)
        self.plane(sec.plane, sec.offset).Select2(False, 0)
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

        self.skm.AddToDB = True                        # no snapping / auto relations
        self.skm.DisplayWhenAdded = False
        try:
            if self.skm.CreateSpline2(self._dbl_array(up.ravel()), False) is None:
                raise RuntimeError(f"{name}: upper spline failed")
            if self.skm.CreateSpline2(self._dbl_array(lo.ravel()), False) is None:
                raise RuntimeError(f"{name}: lower spline failed")
            if not sec.sharp_te:                       # close a blunt trailing edge
                a, b = lo[-1], up[0]
                self.skm.CreateLine(a[0], a[1], 0.0, b[0], b[1], 0.0)
        finally:
            self.skm.AddToDB = False
            self.skm.DisplayWhenAdded = True

        self.skm.InsertSketch(True)                    # exit sketch
        feat = self.model.FeatureByPositionReverse(0)
        feat.Name = name
        return feat.Name

    def loft(self, names: list[str], sections: list[Section], feat_name: str):
        self.model.ClearSelection2(True)
        for nm, sec in zip(names, sections):
            te = sec.upper[0] * MM                     # pick near TE -> aligned connectors
            if not self.ext.SelectByID2(nm, "SKETCH", te[0], te[1], te[2],
                                        True, 1, self.nothing, 0):
                raise RuntimeError(f"Could not select {nm} for the loft")
        feat = self.fm.InsertProtrusionBlend2(
            False, True, False, 1.0,       # closed, keep tangency, non-rational, tess
            0, 0, 1.0, 1.0, True, True,    # start/end constraints: none
            False, 0.0, 0.0, 0,            # thin off
            True, True, True, 0)           # merge, feat scope, auto select, guide infl.
        if feat is None:
            raise RuntimeError(f"Loft {feat_name} failed. Open the part and check its "
                               "section sketches.")
        feat.Name = feat_name
        return feat

    def build(self, p: TailParams, lay: TailLayout, sections, save_path: str | None = None):
        self.open_part()
        self.delete_existing()
        self.app.CommandInProgress = True              # speeds up API calls
        try:
            self.write_global_variables(lay)
            for tag, _, secs in sections:
                print(f"  {tag:14s} root c = {secs[0].chord:7.1f}  tip c = {secs[1].chord:7.1f}"
                      f"  ({secs[0].plane} planes)")
                names = [self.sketch_section(sec, f"{SKETCH_PREFIX}{tag}_{k}")
                         for k, sec in enumerate(secs)]
                self.loft(names, secs, f"{LOFT_PREFIX}{tag}")
            self._hide_construction()
            self.model.ForceRebuild3(False)
        finally:
            self.app.CommandInProgress = False
        self.model.ClearSelection2(True)
        self.model.ShowNamedView2("*Isometric", 7)
        self._call(self.model, "ViewZoomtofit2")
        self._report()
        if save_path:
            err = self.model.SaveAs3(os.path.abspath(save_path), 0, SW_SAVEAS_SILENT)
            print(f"  saved to {save_path}" if err == 0 else f"  [warn] save error {err}")
        print("Done.")

    def _hide_construction(self):
        try:
            self.model.ClearSelection2(True)
            for f in self._features():
                if f.Name.startswith(PLANE_PREFIX):
                    f.Select2(True, 0)
            self._call(self.model, "BlankRefGeom")
            self.model.ClearSelection2(True)
        except Exception as exc:
            print(f"  [warn] could not hide construction planes: {exc}")

    def _report(self):
        try:
            bodies = self.model.GetBodies2(SW_SOLID_BODY, False) or []
            print(f"  solid bodies in part : {len(bodies)}")
            mp = self._call(self.ext, "CreateMassProperty")
            print(f"  SolidWorks volume    : {mp.Volume * 1e9:12.1f} mm^3 (whole part)")
            print(f"  SolidWorks area      : {mp.SurfaceArea * 1e6:12.1f} mm^2 (whole part)")
        except Exception as exc:
            print(f"  [warn] mass properties unavailable: {exc}")


# =============================================================================
# 4. OUTPUT HELPERS
# =============================================================================
def print_summary(p: TailParams, lay: TailLayout, af: Airfoil, notes):
    print("\n=== Tail definition ===")
    for k, v in asdict(p).items():
        print(f"  {k:22s} {v}")
    w = lay.wing
    print("\n=== Wing reference ===")
    print(f"  S_w = {w.area:.1f} mm^2   MAC_w = {w.mac:.2f} mm   b_w = {w.span:.1f} mm")
    print(f"  tail arm l_t = {lay.tail_arm:.1f} mm "
          f"({lay.tail_arm / w.mac:.2f} x MAC)")
    print(f"\n=== {lay.config}: {CONFIGURATIONS[lay.config]} ===")
    print(f"  sized S_h {lay.s_h:10.1f} mm^2     S_v {lay.s_v:10.1f} mm^2")
    if lay.v_angle_deg:
        print(f"  V-panel angle          {lay.v_angle_deg:.2f} deg")
    print(f"  airfoil                {af.name}")
    print(f"  {'panel':12s} {'area*':>10s} {'span':>8s} {'c_root':>8s} {'c_tip':>8s} "
          f"{'MAC':>8s} {'LEsweep':>8s} {'dihed':>6s} {'x_AC':>8s}")
    for pan in lay.panels:
        n = 2 if pan.mirror else 1
        print(f"  {pan.name:12s} {n * pan.area:10.1f} {pan.span:8.1f} {pan.c_root:8.1f} "
              f"{pan.c_tip:8.1f} {pan.mac:8.1f} {pan.sweep_le_deg:8.2f} "
              f"{pan.dihedral_deg:6.1f} {pan.x_ac:8.1f}")
    print("  (* both sides; span is per side along the panel; x_AC from the tail origin)")
    sh, sv, vh, vv = effective_volumes(lay)
    print(f"  projected S_h {sh:9.1f} mm^2 -> V_h = {vh:.3f}  (target {p.vh})")
    print(f"  projected S_v {sv:9.1f} mm^2 -> V_v = {vv:.4f} (target {p.vv})")
    for n in notes:
        print(f"  NOTE: {n}")
    print()


def _hull(pts: np.ndarray) -> np.ndarray:
    """2-D convex hull (monotone chain), for filled three-view outlines."""
    pts = np.unique(np.round(pts, 9), axis=0)
    if len(pts) < 3:
        return pts

    def half(seq):
        h = []
        for q in seq:
            while len(h) >= 2 and ((h[-1][0] - h[-2][0]) * (q[1] - h[-2][1])
                                   - (h[-1][1] - h[-2][1]) * (q[0] - h[-2][0])) <= 0:
                h.pop()
            h.append(q)
        return h

    lower, upper = half(pts), half(pts[::-1])
    return np.array(lower[:-1] + upper[:-1])


def plot_tail(lay: TailLayout, sections, path: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(14, 9))
    ax3 = fig.add_subplot(2, 2, 1, projection="3d")
    views = [(fig.add_subplot(2, 2, 2), 2, 1, "Front view (from behind): Z, Y"),
             (fig.add_subplot(2, 2, 3), 0, 1, "Side view: X, Y"),
             (fig.add_subplot(2, 2, 4), 0, 2, "Top view: X, Z")]
    colors = plt.get_cmap("tab10")
    allpts = []
    for _, idx, secs in sections:
        col = colors(idx)
        loops = [np.vstack([s.upper, s.lower[1:]]) for s in secs]
        allpts.extend(loops)
        X = np.array([lp[:, 0] for lp in loops])
        Y = np.array([lp[:, 1] for lp in loops])
        Z = np.array([lp[:, 2] for lp in loops])
        ax3.plot_surface(Z, X, Y, color=col, alpha=0.85, linewidth=0, shade=True)
        for lp in loops:
            ax3.plot(lp[:, 2], lp[:, 0], lp[:, 1], "k-", lw=0.6)
        both = np.vstack(loops)
        for ax, i, j, _ in views:
            h = _hull(both[:, [i, j]])
            ax.fill(h[:, 0], h[:, 1], color=col, alpha=0.55, lw=0.8, ec="k")
    pts = np.vstack(allpts)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    ctr, rng = 0.5 * (lo + hi), 0.5 * (hi - lo).max()
    ax3.set_xlim(ctr[2] - rng, ctr[2] + rng)
    ax3.set_ylim(ctr[0] + rng, ctr[0] - rng)
    ax3.set_zlim(ctr[1] - rng, ctr[1] + rng)
    ax3.set_box_aspect((1, 1, 1))
    ax3.set_xlabel("Z")
    ax3.set_ylabel("X")
    ax3.set_zlabel("Y")
    ax3.view_init(elev=22, azim=-130)
    ax3.set_title(f"{lay.config}: {CONFIGURATIONS[lay.config]}", fontsize=10)
    for ax, i, j, title in views:
        ax.set_title(title, fontsize=10)
        ax.set_aspect("equal")
        ax.grid(alpha=0.3)
        ax.axhline(0, color="0.6", lw=0.5)
        ax.axvline(0, color="0.6", lw=0.5)
        if j == 2:
            ax.invert_yaxis()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"Preview saved to {path}")


# =============================================================================
# 5. MAIN
# =============================================================================
def load_params(args) -> TailParams:
    p = TailParams()
    if args.config:
        with open(args.config) as fh:
            cfg = json.load(fh)
        valid = {f.name for f in fields(TailParams)}
        for k, v in cfg.items():
            if k not in valid:
                raise KeyError(f"Unknown parameter in config: {k}")
            setattr(p, k, v)
    if args.configuration:
        p.configuration = args.configuration
    if args.tail_arm is not None:
        p.tail_arm_mm = args.tail_arm
    if args.airfoil:
        p.airfoil_file = args.airfoil
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description="Parametric tail generator for SolidWorks "
                                             "and CATIA V5")
    ap.add_argument("--cad", choices=["solidworks", "catia"], default="solidworks",
                    help="CAD system to build in (default: %(default)s)")
    ap.add_argument("-c", "--configuration", choices=list(CONFIGURATIONS),
                    help="tail configuration")
    ap.add_argument("--config", help="JSON file with TailParams overrides")
    ap.add_argument("--wing-config", help="JSON file with the WingParams used for sizing")
    ap.add_argument("--tail-arm", type=float, help="tail arm l_t [mm]")
    ap.add_argument("--airfoil", help="airfoil .dat file (Selig or Lednicer)")
    ap.add_argument("--save", help="save the part to this .SLDPRT / .CATPart path")
    ap.add_argument("--new-part", action="store_true",
                    help="always create a new part instead of using the active one")
    ap.add_argument("--dry-run", action="store_true", help="geometry only, no CAD")
    ap.add_argument("--plot", action="store_true", help="save tail_preview.png")
    ap.add_argument("--list", action="store_true", help="list the configurations and exit")
    args = ap.parse_args(argv)

    if args.list:
        for k, v in CONFIGURATIONS.items():
            print(f"  {k:13s} {v}")
        return 0

    p = load_params(args)
    notes = validate(p)
    wing = wing_reference(args.wing_config)
    lay = derive_layout(p, wing)
    af = load_airfoil(p.airfoil_file) if p.airfoil_file else naca4(p.naca)
    if p.n_points_per_surface > 0:
        af = resample(af, p.n_points_per_surface)
    sections = all_sections(p, lay, af)
    print_summary(p, lay, af, notes)

    if args.plot:
        plot_tail(lay, sections, "tail_preview.png")
    if args.dry_run:
        return 0
    if sys.platform != "win32":
        print("CAD automation needs Windows; use --dry-run elsewhere.")
        return 1
    if args.cad == "catia":
        import catia_builder as C
        saved = C.build_component(
            "tail", lambda cp: C.build_tail(cp, sections), p, args.save,
            reuse="never" if args.new_part else "project")
        return 0 if saved else 1
    SolidWorksTailBuilder(new_part=args.new_part).build(p, lay, sections, args.save)
    return 0


if __name__ == "__main__":
    sys.exit(main())
