#!/usr/bin/env python3
"""
Complete parametric aircraft for SolidWorks and CATIA V5: wing + fuselage + tail
================================================================================

Reads ONE configuration file (aircraft_config.json) that defines every
geometric parameter of the three components, each with its own [min, max]
range, plus cross-component constraints. It then

1. validates every parameter against its range (and the component scripts'
   own sanity checks),
2. integrates the components automatically:
     * fuselage  - nose tip at the origin, X aft, Y up
     * wing      - root LE at x = wing_x_frac * L, set at wing_incidence_deg;
                   its height is solved so the root airfoil always lies inside
                   the fuselage (wing_vertical_pos: -1 low ... +1 high wing)
     * tail      - placed so its aft-most root trailing edge sits at the
                   fuselage end (+ tail_overhang_frac); the tail arm follows
                   from that and the tail is re-sized with the volume
                   coefficients on that arm (iterated to convergence); its
                   height is solved so the root fits inside the tail cone
3. evaluates every constraint expression from the configuration on the
   parameters and the derived geometry (tail arm, wing-tail gap, root fits, ...)
4. builds the aircraft in a single part only if nothing is violated - in
   SolidWorks (default) or CATIA V5 (--cad catia): wing loft + mirror,
   fuselage loft, tail lofts, all merged into one solid body. Re-running in
   the same part deletes the previous aircraft first, so the model always
   follows the configuration.

The geometry itself comes from wing_solidworks.py, fuselage_sw.py and
tail_sw.py, which still work on their own. The CATIA builder is in
catia_builder.py and uses exactly the same geometry.

Usage
-----
    python aircraft_sw.py                               # build from aircraft_config.json
    python aircraft_sw.py --cad catia                   # build in CATIA V5 instead
    python aircraft_sw.py --check                       # validate + report only
    python aircraft_sw.py --dry-run --plot              # report + aircraft_preview.png
    python aircraft_sw.py --set wing.span_mm=3400 --set tail.configuration=v_tail
    python aircraft_sw.py --config my_uav.json --save C:\\work\\uav.SLDPRT
    python aircraft_sw.py --cad catia --save C:\\work\\uav.CATPart
    python aircraft_sw.py --new-part                    # never reuse the active part

Requires: Windows, SolidWorks or CATIA V5, pywin32, numpy
          (matplotlib optional for --plot)
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import io
import json
import math
import operator
import os
import re
import sys
from dataclasses import asdict, dataclass, fields, replace

import numpy as np

import fuselage_sw as F
import tail_sw as T
import wing_solidworks as W

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "aircraft_config.json")


# =============================================================================
# 1. PARAMETER SETS
# =============================================================================
@dataclass
class IntegrationParams:
    wing_x_frac: float = 0.30          # wing root LE x / fuselage length
    wing_vertical_pos: float = 0.0     # -1 low wing, 0 mid, +1 high wing
    wing_incidence_deg: float = 0.0    # wing setting angle (+ nose up)
    tail_overhang_frac: float = 0.0    # aft root TE beyond the fuselage end / root chord
    tail_vertical_pos: float = 0.0     # -1 lowest, 0 middle, +1 highest root position


SECTIONS = {
    "wing": W.WingParams,
    "fuselage": F.FuselageParams,
    "tail": T.TailParams,
    "integration": IntegrationParams,
}

# Fields that are not free parameters in the integrated aircraft.
DERIVED_FIELDS = {
    "wing": {"mirror": "the aircraft wing is always mirrored",
             "part_name": "not geometric"},
    "fuselage": {"part_name": "not geometric"},
    "tail": {"tail_arm_mm": "derived from the wing and tail positions",
             "origin_x_mm": "derived from integration.tail_overhang_frac",
             "origin_y_mm": "derived from integration.tail_vertical_pos",
             "part_name": "not geometric"},
    "integration": {},
}


class ConfigError(Exception):
    def __init__(self, errors):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass
class Config:
    params: dict          # section -> dataclass instance
    specs: dict           # section -> {name: spec dict}
    constraints: list     # list of constraint dicts


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_spec(section, name, spec, default, errors):
    """Validate one parameter entry; returns the typed value (or None)."""
    where = f"{section}.{name}"
    if not isinstance(spec, dict) or "value" not in spec:
        errors.append(f"{where}: entry must be an object with a 'value'.")
        return None
    v = spec["value"]
    if isinstance(default, bool):
        if not isinstance(v, bool):
            errors.append(f"{where}: must be true/false.")
            return None
        return v
    if isinstance(default, str):
        if not isinstance(v, str):
            errors.append(f"{where}: must be text.")
            return None
        if "choices" in spec and v not in spec["choices"]:
            errors.append(f"{where} = '{v}' is not one of {spec['choices']}.")
        if "pattern" in spec and not re.search(spec["pattern"], v):
            errors.append(f"{where} = '{v}' does not match the pattern {spec['pattern']}.")
        return v
    # numeric
    if not _is_number(v):
        errors.append(f"{where}: value must be a number.")
        return None
    lo, hi = spec.get("min"), spec.get("max")
    if not (_is_number(lo) and _is_number(hi)):
        errors.append(f"{where}: numeric parameters need numeric 'min' and 'max'.")
        return None
    if lo > hi:
        errors.append(f"{where}: min {lo} > max {hi}.")
        return None
    if isinstance(default, int):
        if float(v) != int(v):
            errors.append(f"{where} = {v} must be an integer.")
            return None
        v = int(v)
    else:
        v = float(v)
    if not lo <= v <= hi:
        errors.append(f"{where} = {v:g} is outside its range [{lo:g}, {hi:g}].")
    return v


def dump_config(raw: dict) -> str:
    """Configuration as JSON text with one parameter / constraint per line."""
    out, keys = ["{"], list(raw)
    for i, k in enumerate(keys):
        end = "," if i < len(keys) - 1 else ""
        v = raw[k]
        if k in SECTIONS and isinstance(v, dict) and v:
            out.append(f'  "{k}": {{')
            names = list(v)
            w = max(len(n) for n in names) + 3
            for j, n in enumerate(names):
                c = "," if j < len(names) - 1 else ""
                out.append(f"    {(json.dumps(n) + ':'):<{w}} {json.dumps(v[n])}{c}")
            out.append("  }" + end)
        elif isinstance(v, list):
            out.append(f'  "{k}": [')
            for j, item in enumerate(v):
                c = "," if j < len(v) - 1 else ""
                out.append(f"    {json.dumps(item)}{c}")
            out.append("  ]" + end)
        elif isinstance(v, dict) and isinstance(v.get("variables"), list):   # sweep block
            out.append(f'  "{k}": {{')
            items = [(n, x) for n, x in v.items() if n != "variables"]
            for n, x in items:
                out.append(f"    {json.dumps(n)}: {json.dumps(x)},")
            out.append('    "variables": [')
            for j, item in enumerate(v["variables"]):
                c = "," if j < len(v["variables"]) - 1 else ""
                out.append(f"      {json.dumps(item)}{c}")
            out.append("    ]")
            out.append("  }" + end)
        else:
            out.append(f'  "{k}": {json.dumps(v)}{end}')
        if i < len(keys) - 1:
            out.append("")
    out.append("}")
    return "\n".join(out) + "\n"


def load_config(path: str, overrides: list[str] = ()) -> Config:
    with open(path) as fh:
        raw = json.load(fh)
    return config_from_dict(raw, overrides)


def config_from_dict(raw: dict, overrides: list[str] = ()) -> Config:
    """Validate a configuration already loaded as a dict (used by the GUI)."""
    raw = copy.deepcopy(raw)
    errors = []

    # --set section.name=value overrides (still checked against min/max)
    for item in overrides:
        m = re.fullmatch(r"\s*(\w+)\.(\w+)\s*=\s*(.*)", item)
        if not m:
            errors.append(f"--set '{item}': use section.name=value")
            continue
        sec, name, text = m.groups()
        try:
            val = json.loads(text)
        except json.JSONDecodeError:
            val = text
        if sec not in raw or name not in raw.get(sec, {}):
            errors.append(f"--set '{item}': unknown parameter {sec}.{name}")
            continue
        raw[sec][name]["value"] = val

    params, specs = {}, {}
    for sec, cls in SECTIONS.items():
        block = raw.get(sec)
        if not isinstance(block, dict):
            errors.append(f"Section '{sec}' is missing.")
            continue
        defaults = cls()
        names = {f.name for f in fields(cls)}
        free = names - set(DERIVED_FIELDS[sec])
        for name in block:
            if name in DERIVED_FIELDS[sec]:
                errors.append(f"{sec}.{name} cannot be set: {DERIVED_FIELDS[sec][name]}.")
            elif name not in names:
                errors.append(f"{sec}.{name}: unknown parameter.")
        for name in sorted(free - set(block)):
            errors.append(f"{sec}.{name} is missing (every parameter must be defined "
                          "with value/min/max).")
        values = {}
        for name in free & set(block):
            v = _check_spec(sec, name, block[name], getattr(defaults, name), errors)
            if v is not None:
                values[name] = v
        params[sec] = replace(defaults, **values)
        specs[sec] = block
    params["wing"] = replace(params["wing"], mirror=True) if "wing" in params else None

    constraints = raw.get("constraints", [])
    for c in constraints:
        for key in ("name", "expr", "min", "max"):
            if key not in c:
                errors.append(f"constraint {c.get('name', '?')}: '{key}' missing.")
        if _is_number(c.get("min")) and _is_number(c.get("max")) and c["min"] > c["max"]:
            errors.append(f"constraint {c['name']}: min > max.")
    if errors:
        raise ConfigError(errors)
    return Config(params, specs, constraints)


# =============================================================================
# 2. SAFE CONSTRAINT EXPRESSIONS
# =============================================================================
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow}
_FUNCS = {"sqrt": math.sqrt, "abs": abs, "min": min, "max": max,
          "tan": lambda d: math.tan(math.radians(d)),
          "cos": lambda d: math.cos(math.radians(d)),
          "sin": lambda d: math.sin(math.radians(d))}


def safe_eval(expr: str, ns: dict) -> float:
    """Arithmetic on group.name variables only - no arbitrary Python."""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and _is_number(n.value):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            group = ns.get(n.value.id)
            if group is None or n.attr not in group:
                raise NameError(f"unknown variable {n.value.id}.{n.attr}")
            val = group[n.attr]
            if not _is_number(val):
                raise TypeError(f"{n.value.id}.{n.attr} is not numeric")
            return val
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in _FUNCS and not n.keywords):
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError(f"unsupported syntax: {ast.dump(n)[:60]}")
    return float(ev(ast.parse(expr, mode="eval")))


# =============================================================================
# 3. INTEGRATED GEOMETRY
# =============================================================================
@dataclass
class Aircraft:
    wp: W.WingParams
    fp: F.FuselageParams
    tp: T.TailParams            # with tail_arm / origin filled in
    ip: IntegrationParams
    pf: W.Planform
    wing_sections: list         # placed W.Section (right side)
    fus_lay: F.Layout
    stations: list
    tail_lay: T.TailLayout
    tail_sections: list
    derived: dict
    notes: list


def _airfoil(file, naca, n):
    af = W.load_airfoil(file) if file else W.naca4(naca)
    return W.resample(af, n) if n > 0 else af


def _thickness_ratio(af) -> float:
    xs = np.linspace(0, 1, 400)
    up = af.upper[np.argsort(af.upper[:, 0])]
    lo = af.lower[np.argsort(af.lower[:, 0])]
    return float((np.interp(xs, up[:, 0], up[:, 1]) - np.interp(xs, lo[:, 0], lo[:, 1])).max())


def _attached(lay: T.TailLayout):
    """Tail panels rooted on the fuselage centreline at the tail origin height."""
    return [pan for pan in lay.panels
            if abs(pan.root_le[1]) < 1e-9 and abs(pan.root_le[2]) < 1e-9]


def integrate(wp, fp, tp, ip) -> Aircraft:
    errors, notes = [], []
    for label, fn, arg in (("wing", W.validate, wp), ("fuselage", F.validate, fp),
                           ("tail", T.validate, tp)):
        try:
            notes += [f"{label}: {n}" for n in (fn(arg) or [])]
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
    if errors:
        raise ConfigError(errors)

    d = {}
    margin = 0.01 * fp.max_height_mm        # keep surfaces off tangency with the skin

    # ---- fuselage (reference body) -----------------------------------------
    flay = F.derive_layout(fp)
    stations = F.build_stations(fp, flay)
    xs_dense = np.linspace(0, flay.L, 1500)
    _, top_d, bot_d, _, _ = F.body_lines(fp, flay, xs_dense)

    def lines(x):
        _, top, bot, _, _ = F.body_lines(fp, flay, np.atleast_1d(x))
        return top, bot

    # ---- wing ----------------------------------------------------------------
    pf = W.derive_planform(wp)
    waf = _airfoil(wp.airfoil_file, wp.naca, wp.n_points_per_surface)
    secs = W.build_sections(wp, pf, waf)
    inc = math.radians(ip.wing_incidence_deg)
    ci, si, xa = math.cos(inc), math.sin(inc), 0.25 * pf.c_root

    def rot(pts):
        dx = pts[:, 0] - xa
        return np.column_stack([xa + dx * ci + pts[:, 1] * si,
                                -dx * si + pts[:, 1] * ci, pts[:, 2]])

    root_up, root_lo = rot(secs[0].upper), rot(secs[0].lower)
    loop = np.vstack([root_up, root_lo])
    xr = np.linspace(loop[:, 0].min(), loop[:, 0].max(), 300)
    su = root_up[np.argsort(root_up[:, 0])]
    sl = root_lo[np.argsort(root_lo[:, 0])]
    yu_i, yl_i = np.interp(xr, su[:, 0], su[:, 1]), np.interp(xr, sl[:, 0], sl[:, 1])
    y_up, y_lo = np.maximum(yu_i, yl_i), np.minimum(yu_i, yl_i)

    x_w = ip.wing_x_frac * flay.L
    top_w, bot_w = lines(x_w + xr)
    y_hi = float((top_w - y_up).min()) - margin
    y_lo_ = float((bot_w - y_lo).max()) + margin
    fit_w = y_hi - y_lo_
    y_w = 0.5 * (y_hi + y_lo_) + ip.wing_vertical_pos * 0.5 * fit_w
    shift = np.array([x_w, y_w, 0.0])
    wing_sections = [W.Section(s.z, s.chord, s.twist_deg, rot(s.upper) + shift,
                               rot(s.lower) + shift, s.sharp_te) for s in secs]
    wing_pts = np.vstack([np.vstack([s.upper, s.lower]) for s in wing_sections])
    x_ac_w = x_w + pf.x_le_mac + 0.25 * pf.mac

    d.update(
        wing_area_mm2=pf.area, wing_root_chord_mm=pf.c_root, wing_tip_chord_mm=pf.c_tip,
        wing_mac_mm=pf.mac, wing_sweep_le_deg=pf.sweep_le_deg,
        wing_root_le_x_mm=x_w, wing_root_te_x_mm=x_w + pf.c_root,
        wing_ac_x_mm=x_ac_w, wing_root_y_mm=y_w,
        wing_root_thickness_mm=float((y_up - y_lo).max()),
        wing_root_fit_mm=fit_w,
        fuselage_height_at_wing_mm=float((top_w - bot_w).min()),
        wing_te_max_x_mm=float(wing_pts[:, 0].max()),
        wing_tip_y_mm=float(wing_pts[:, 1].max()),
        fuselage_nose_end_x_mm=flay.x_nose_end, fuselage_tail_start_x_mm=flay.x_tail_start,
        fuselage_fineness=flay.L / math.sqrt(flay.W * flay.H),
        fuselage_top_max_mm=float(top_d.max()), fuselage_bottom_min_mm=float(bot_d.min()),
    )

    # ---- tail: x placement and sizing on the resulting arm (fixed point) ------
    taf = _airfoil(tp.airfoil_file, tp.naca, tp.n_points_per_surface)
    wref = T.WingRef(pf.area, pf.mac, pf.span)
    lt, ox, converged = 3.0 * pf.mac, 0.0, False
    for _ in range(200):
        lay = T.derive_layout(replace(tp, tail_arm_mm=lt), wref)
        att = _attached(lay)
        if not att:
            raise ConfigError(["tail: no surface is rooted on the fuselage centreline."])
        ref = max(att, key=lambda p: p.root_le[0] + p.c_root)
        ox = flay.L + ip.tail_overhang_frac * ref.c_root - (ref.root_le[0] + ref.c_root)
        lt_new = ox - x_ac_w
        if lt_new <= 0.2 * pf.mac:
            raise ConfigError([
                f"tail arm would be {lt_new:.0f} mm: the tail does not fit behind the wing. "
                "Make the fuselage longer, move the wing forward (integration.wing_x_frac) "
                "or reduce the tail size."])
        if abs(lt_new - lt) < 1e-6:
            converged = True
            break
        lt = 0.5 * (lt + lt_new)
    if not converged:
        notes.append("tail placement iteration did not fully converge.")

    # ---- tail: vertical placement inside the tail cone -----------------------
    t_c = _thickness_ratio(taf)
    y_min, y_max, overlap = -math.inf, math.inf, 1.0
    for pan in att:
        x0 = ox + pan.root_le[0]
        x1 = x0 + pan.c_root
        a, b = max(x0, 0.0), min(x1, flay.L)
        overlap = min(overlap, max(0.0, b - a) / pan.c_root)
        if b <= a:
            continue
        top_t, bot_t = lines(np.linspace(a, b, 80))
        m = margin
        if pan.plane_kind == "front":           # horizontal-ish: needs its thickness
            m += 0.5 * t_c * pan.c_root / abs(math.cos(math.radians(pan.dihedral_deg)))
        y_max = min(y_max, float((top_t - m).min()))
        y_min = max(y_min, float((bot_t + m).max()))
    if not math.isfinite(y_min) or not math.isfinite(y_max):
        y_min = y_max = 0.0
    fit_t = y_max - y_min
    oy = 0.5 * (y_max + y_min) + ip.tail_vertical_pos * 0.5 * fit_t

    tp_final = replace(tp, tail_arm_mm=lt, origin_x_mm=ox, origin_y_mm=oy)
    tlay = T.derive_layout(tp_final, wref)
    tail_sections = T.all_sections(tp_final, tlay, taf)
    tpts = np.vstack([np.vstack([s.upper, s.lower])
                      for _, _, ss in tail_sections for s in ss])
    sh, sv, vh, vv = T.effective_volumes(tlay)

    d.update(
        tail_arm_mm=lt, tail_origin_x_mm=ox, tail_origin_y_mm=oy,
        tail_s_h_mm2=sh, tail_s_v_mm2=sv, tail_v_angle_deg=tlay.v_angle_deg,
        tail_vh_effective=vh, tail_vv_effective=vv,
        tail_vh_ratio=vh / tp.vh if tp.h_area_mm2 == 0 else 1.0,
        tail_vv_ratio=vv / tp.vv if tp.v_area_mm2 == 0 else 1.0,
        tail_le_min_x_mm=float(tpts[:, 0].min()), tail_te_max_x_mm=float(tpts[:, 0].max()),
        tail_span_mm=float(2 * np.abs(tpts[:, 2]).max()),
        tail_height_mm=float(np.abs(tpts[:, 1] - oy).max()),
        tail_root_fit_mm=fit_t, tail_root_overlap_frac=overlap,
        tail_below_fuselage_mm=float(bot_d.min() - tpts[:, 1].min()),
        wing_tail_gap_mm=float(tpts[:, 0].min() - wing_pts[:, 0].max()),
        aircraft_length_mm=float(max(flay.L, tpts[:, 0].max())),
        aircraft_span_mm=pf.span,
        aircraft_height_mm=float(max(top_d.max(), tpts[:, 1].max(), wing_pts[:, 1].max())
                                 - min(bot_d.min(), tpts[:, 1].min(), wing_pts[:, 1].min())),
    )
    return Aircraft(wp, fp, tp_final, ip, pf, wing_sections, flay, stations,
                    tlay, tail_sections, d, notes)


def evaluate_constraints(cfg: Config, ac: Aircraft):
    """[(constraint, value or None, ok, message)]"""
    ns = {sec: {k: v for k, v in asdict(p).items()} for sec, p in cfg.params.items()}
    ns["derived"] = ac.derived
    out = []
    for c in cfg.constraints:
        try:
            v = safe_eval(c["expr"], ns)
            ok = c["min"] - 1e-12 <= v <= c["max"] + 1e-12
            msg = "" if ok else (f"{c['name'].replace('_', ' ').capitalize()} = {v:.4g} is "
                                 f"outside the allowed range {c['min']:g} to {c['max']:g}. "
                                 f"{c.get('description', '')}")
        except Exception as exc:
            v, ok, msg = None, False, f"{c['name']}: cannot evaluate '{c['expr']}': {exc}"
        out.append((c, v, ok, msg))
    return out


# Hard geometric requirements that hold regardless of the configured constraints.
def hard_checks(ac: Aircraft) -> list[str]:
    d, errs = ac.derived, []
    if d["wing_root_fit_mm"] < 0:
        errs.append(f"wing root airfoil does not fit inside the fuselage "
                    f"(short by {-d['wing_root_fit_mm']:.1f} mm): increase the fuselage "
                    "height, move the wing to a thicker part of the fuselage, or use a "
                    "thinner / shorter root section.")
    if d["tail_root_fit_mm"] < 0:
        errs.append(f"tail root does not fit inside the tail cone (short by "
                    f"{-d['tail_root_fit_mm']:.1f} mm): increase fuselage.tail_end_height_mm, "
                    "reduce integration.tail_overhang_frac or use a thinner tail section.")
    if d["tail_root_overlap_frac"] <= 0:
        errs.append("tail root is not attached to the fuselage.")
    if d["wing_tail_gap_mm"] <= 0:
        errs.append("tail overlaps the wing in x.")
    if ac.pf.span / 2 <= ac.fp.max_width_mm / 2:
        errs.append("wing span is smaller than the fuselage width.")
    return errs


# =============================================================================
# 4. SOLIDWORKS BUILD
# =============================================================================
AIRCRAFT_PREFIXES = ("Wing_", "Fus_", "Fuselage_", "Tail_")
EQ_PREFIX = "AC_"

# CAD systems the aircraft can be built in: key -> (display name, part extension)
CAD_SYSTEMS = {
    "solidworks": ("SolidWorks", ".SLDPRT"),
    "catia": ("CATIA V5", ".CATPart"),
}
DEFAULT_CAD = "solidworks"


def cad_label(cad: str) -> str:
    return CAD_SYSTEMS[cad][0]


def cad_extension(cad: str) -> str:
    return CAD_SYSTEMS[cad][1]


CAD_PROGIDS = {"solidworks": "SldWorks.Application", "catia": "CATIA.Application"}


def cad_installed(cad: str) -> bool:
    """True if the CAD system is registered for COM automation (Windows)."""
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, CAD_PROGIDS[cad]))
        return True
    except (ImportError, OSError, KeyError):
        return False


def make_builder(cad: str = DEFAULT_CAD, new_part: bool = False):
    """Builder for the chosen CAD system. Both have the same interface:
    build(cfg, ac, save_path) -> saved, save_as(path), close(), .model"""
    if cad not in CAD_SYSTEMS:
        raise ValueError(f"Unknown CAD system '{cad}'. Choose from: {', '.join(CAD_SYSTEMS)}")
    if cad == "catia":
        from catia_builder import CatiaAircraftBuilder
        return CatiaAircraftBuilder(new_part=new_part)
    return SolidWorksAircraftBuilder(new_part=new_part)


def equation_values(cfg: Config, ac: Aircraft) -> dict:
    """Every free numeric parameter and derived value as {AC_<group>_<name>: value}.
    Written to the part for reference (SolidWorks global variables / CATIA
    parameters)."""
    vals = {}
    for sec, p in cfg.params.items():
        for k, v in asdict(p).items():
            if _is_number(v) and k not in DERIVED_FIELDS[sec]:
                vals[f"{EQ_PREFIX}{sec}_{k}"] = v
    for k, v in ac.derived.items():
        vals[f"{EQ_PREFIX}derived_{k}"] = v
    return vals


class SolidWorksAircraftBuilder:
    label = "SolidWorks"
    extension = ".SLDPRT"

    def __init__(self, new_part: bool = False):
        with contextlib.redirect_stdout(io.StringIO()):
            self.wb = W.SolidWorksWingBuilder(visible=True)
            self.fb = F.SolidWorksFuselageBuilder()
            self.tb = T.SolidWorksTailBuilder()
        self.app = self.wb.app
        self.force_new = new_part
        self._call = W.SolidWorksWingBuilder._call_com_method

    def _features(self):
        feat = self._call(self.model, "FirstFeature")
        while feat is not None:
            yield feat
            feat = self._call(feat, "GetNextFeature")

    def _ours(self):
        return [f for f in self._features() if f.Name.startswith(AIRCRAFT_PREFIXES)]

    def open_part(self):
        model = None if self.force_new else self.app.ActiveDoc
        reuse = False
        if model is not None and self._call(model, "GetType") == F.SW_DOC_PART:
            self.model = model
            reuse = bool(self._ours())
        if reuse:
            print(f"Rebuilding aircraft in active part: {self._call(model, 'GetTitle')}")
        else:
            template = self.app.GetUserPreferenceStringValue(F.SW_DEFAULT_TEMPLATE_PART)
            if not template or not os.path.exists(template):
                raise RuntimeError("No default part template set. In SolidWorks: "
                                   "Tools > Options > Default Templates.")
            model = self.app.NewDocument(template, 0, 0, 0)
            if model is None:
                raise RuntimeError("SolidWorks failed to create a new part.")
            print("Created new part document.")
        self.model = model
        for b in (self.wb, self.fb, self.tb):
            b.attach(model)

    def delete_existing(self):
        """Delete every aircraft feature, last one first."""
        removed = 0
        while True:
            ours = self._ours()
            if not ours:
                break
            feat = ours[-1]
            self.model.ClearSelection2(True)
            feat.Select2(False, 0)
            if not self.model.Extension.DeleteSelection2(F.SW_DELETE_ABSORBED_AND_CHILDREN):
                raise RuntimeError(f"Could not delete old feature {feat.Name}")
            removed += 1
        self.model.ClearSelection2(True)
        if removed:
            print(f"Deleted {removed} features of the previous aircraft.")

    def write_equations(self, cfg: Config, ac: Aircraft):
        vals = equation_values(cfg, ac)
        try:
            eqm = self._call(self.model, "GetEquationMgr")
            for i in range(self._call(eqm, "GetCount") - 1, -1, -1):
                if eqm.Equation(i).startswith(f'"{EQ_PREFIX}'):
                    eqm.Delete(i)
            for n, v in vals.items():
                eqm.Add2(-1, f'"{n}" = {float(v):.6g}', False)
            self._call(eqm, "EvaluateAll")
        except Exception as exc:
            print(f"  [warn] global variables not written: {exc}")

    def build_wing(self, ac: Aircraft):
        wb, names = self.wb, []
        for i, sec in enumerate(ac.wing_sections):
            plane = wb.front if i == 0 else wb.offset_plane(sec.z, f"Wing_Station_{i}")
            names.append(wb.sketch_section(plane, sec, f"Wing_Section_{i}"))
        wb.loft(names, ac.wing_sections)
        wb.mirror_body()

    def build_fuselage(self, ac: Aircraft):
        fb, lay = self.fb, ac.fus_lay
        tip = (0.0, lay.y_tip, 0.0)
        profiles = [(fb.sketch_point(fb.right, tip, F.TIP_SKETCH), tip)]
        for i, st in enumerate(ac.stations):
            plane = fb.offset_plane(st.x, f"{F.PLANE_PREFIX}{i}")
            profiles.append((fb.sketch_section(plane, st, f"{F.SKETCH_PREFIX}{i}"), st.pts[0]))
        if lay.pointed_tail:
            plane = fb.offset_plane(lay.L, f"{F.PLANE_PREFIX}end")
            end = (lay.L, lay.y_tail_c, 0.0)
            profiles.append((fb.sketch_point(plane, end, F.TAIL_SKETCH), end))
        fb.loft(profiles)

    def build_tail(self, ac: Aircraft):
        tb = self.tb
        for tag, _, secs in ac.tail_sections:
            names = [tb.sketch_section(sec, f"{T.SKETCH_PREFIX}{tag}_{k}")
                     for k, sec in enumerate(secs)]
            tb.loft(names, secs, f"{T.LOFT_PREFIX}{tag}")

    def hide_planes(self):
        try:
            self.model.ClearSelection2(True)
            for f in self._ours():
                if self._call(f, "GetTypeName2") == "RefPlane":
                    f.Select2(True, 0)
            self._call(self.model, "BlankRefGeom")
            self.model.ClearSelection2(True)
        except Exception as exc:
            print(f"  [warn] could not hide construction planes: {exc}")

    def build(self, cfg: Config, ac: Aircraft, save_path: str | None = None):
        self.open_part()
        self.delete_existing()
        self.app.CommandInProgress = True
        try:
            print("  wing ...")
            self.build_wing(ac)
            print("  fuselage ...")
            self.build_fuselage(ac)
            print("  tail ...")
            self.build_tail(ac)
            self.hide_planes()
            self.write_equations(cfg, ac)
            self.model.ForceRebuild3(False)
        finally:
            self.app.CommandInProgress = False
        self.model.ClearSelection2(True)
        self.model.ShowNamedView2("*Isometric", 7)
        self._call(self.model, "ViewZoomtofit2")

        bodies = self.model.GetBodies2(W.SW_SOLID_BODY, False) or []
        print(f"  solid bodies         : {len(bodies)}")
        if len(bodies) != 1:
            print("  [warn] components did not merge into one body - check the "
                  "intersections in SolidWorks.")
        try:
            mp = self._call(self.model.Extension, "CreateMassProperty")
            print(f"  SolidWorks volume    : {mp.Volume:.5f} m^3")
            print(f"  SolidWorks area      : {mp.SurfaceArea:.4f} m^2")
        except Exception as exc:
            print(f"  [warn] mass properties unavailable: {exc}")
        saved = True
        if save_path:
            saved = self.save_as(save_path)
        print("Done.")
        return saved

    def save_as(self, path, tries=3) -> bool:
        """SaveAs3 occasionally returns a generic error right after a heavy rebuild;
        retry a few times before giving up."""
        import time
        path = os.path.abspath(path)
        for k in range(tries):
            err = self.model.SaveAs3(path, 0, W.SW_SAVEAS_SILENT)
            if err == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
                print(f"  saved to {path}")
                return True
            time.sleep(1.0 + k)
        print(f"  [error] could not save {path} (SolidWorks save error {err}; "
              "is the file open or locked by another program?)")
        with contextlib.suppress(OSError):
            if os.path.exists(path) and os.path.getsize(path) == 0:
                os.remove(path)
        return False

    def close(self):
        """Close the part document that was just built."""
        title = self._call(self.model, "GetTitle")
        self.app.CloseDoc(title)
        self.model = None


# =============================================================================
# 5. REPORT AND PREVIEW
# =============================================================================
def print_report(cfg: Config, ac: Aircraft, results):
    print("\n=== Parameters (value  [min, max]) ===")
    for sec, p in cfg.params.items():
        print(f"  [{sec}]")
        for k, v in asdict(p).items():
            if k in DERIVED_FIELDS[sec]:
                continue
            spec = cfg.specs[sec][k]
            rng = (f"[{spec['min']:g}, {spec['max']:g}]" if "min" in spec
                   else f"{spec.get('choices', spec.get('pattern', ''))}")
            print(f"    {k:22s} {str(v):>14s}  {rng}")
    print("\n=== Derived geometry ===")
    for k, v in ac.derived.items():
        print(f"  {k:30s} {v:14.4f}")
    print("\n=== Constraints ===")
    for c, v, ok, _ in results:
        val = f"{v:10.4f}" if v is not None else "       n/a"
        print(f"  {'OK  ' if ok else 'FAIL'} {c['name']:28s} {val}  "
              f"[{c['min']:g}, {c['max']:g}]")
    for n in ac.notes:
        print(f"  NOTE: {n}")
    print()


def plot_aircraft(ac: Aircraft, path: str):
    """Three-view drawing (top, side, front) of the aircraft, saved as an image."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(15, 10))
    gs = fig.add_gridspec(2, 2, width_ratios=[1.25, 1])
    views = [(fig.add_subplot(gs[1, 1]), 2, 1, "Front view: Z, Y"),
             (fig.add_subplot(gs[0, 1]), 0, 1, "Side view: X, Y"),
             (fig.add_subplot(gs[:, 0]), 0, 2, "Top view: X, Z")]

    groups = []                                           # (color, [loops])
    for sgn in (1, -1):
        groups.append(("tab:blue", [np.vstack([s.upper, s.lower[1:]]) * [1, 1, sgn]
                                    for s in ac.wing_sections]))
    for _, _, secs in ac.tail_sections:
        groups.append(("tab:orange", [np.vstack([s.upper, s.lower[1:]]) for s in secs]))

    for col, loops in groups:
        both = np.vstack(loops)
        for ax, i, j, _ in views:
            h = T._hull(both[:, [i, j]])
            ax.fill(h[:, 0], h[:, 1], color=col, alpha=0.6, lw=0.6, ec="k")

    # fuselage silhouettes (non-convex, drawn exactly)
    x = np.linspace(0, ac.fus_lay.L, 400)
    hw, top, bot, _, _ = F.body_lines(ac.fp, ac.fus_lay, x)
    views[1][0].fill(np.r_[x, x[::-1]], np.r_[top, bot[::-1]], color="tab:gray",
                     alpha=0.5, ec="k", lw=0.6)
    views[2][0].fill(np.r_[x, x[::-1]], np.r_[hw, -hw[::-1]], color="tab:gray",
                     alpha=0.5, ec="k", lw=0.6)
    sec = max(ac.stations, key=lambda s: s.half_w * (s.top - s.bot))
    views[0][0].fill(sec.pts[:, 2], sec.pts[:, 1], color="tab:gray", alpha=0.5,
                     ec="k", lw=0.6)

    for ax, i, j, title in views:
        ax.set_title(title, fontsize=10)
        ax.set_aspect("equal")
        ax.grid(alpha=0.3)
        if j == 2:
            ax.invert_yaxis()
    fig.suptitle(f"Aircraft ({ac.tp.configuration} tail)", fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    print(f"Preview saved to {path}")


# =============================================================================
# 6. MAIN
# =============================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="Parametric aircraft (wing + fuselage + tail) "
                                             "for SolidWorks and CATIA V5")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="aircraft configuration JSON")
    ap.add_argument("--cad", choices=list(CAD_SYSTEMS), default=DEFAULT_CAD,
                    help="CAD system to build in (default: %(default)s)")
    ap.add_argument("--set", action="append", default=[], metavar="SECTION.NAME=VALUE",
                    help="override a parameter value (still checked against min/max)")
    ap.add_argument("--check", "--dry-run", dest="dry_run", action="store_true",
                    help="validate and report only, no CAD")
    ap.add_argument("--plot", action="store_true", help="save aircraft_preview.png")
    ap.add_argument("--save", help="save the part to this path (.SLDPRT for SolidWorks, "
                                   ".CATPart for CATIA)")
    ap.add_argument("--new-part", action="store_true",
                    help="always create a new part instead of rebuilding in the active one "
                         "(CATIA: instead of using an empty active part)")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config, args.set)
        p = cfg.params
        ac = integrate(p["wing"], p["fuselage"], p["tail"], p["integration"])
    except ConfigError as exc:
        print("\nConfiguration rejected - nothing was built:")
        for e in exc.errors:
            print(f"  - {e}")
        return 2

    results = evaluate_constraints(cfg, ac)
    print_report(cfg, ac, results)
    errors = hard_checks(ac) + [msg for _, _, ok, msg in results if not ok]
    if args.plot:
        plot_aircraft(ac, "aircraft_preview.png")
    if errors:
        print("Configuration rejected - nothing was built:")
        for e in errors:
            print(f"  - {e}")
        return 2
    print("All parameters and constraints satisfied.")
    if args.dry_run:
        return 0
    if sys.platform != "win32":
        print(f"{cad_label(args.cad)} automation needs Windows; use --check elsewhere.")
        return 1
    print(f"Building in {cad_label(args.cad)} ...")
    saved = make_builder(args.cad, new_part=args.new_part).build(cfg, ac, args.save)
    return 0 if saved else 1


if __name__ == "__main__":
    sys.exit(main())
