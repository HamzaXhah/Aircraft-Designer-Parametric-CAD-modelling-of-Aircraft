#!/usr/bin/env python3
"""
Aircraft Designer - CustomTkinter GUI for the parametric SolidWorks aircraft
============================================================================

Edit every wing, fuselage, tail and placement parameter of
aircraft_config.json in one window:

  * sliders + numeric entries limited to each parameter's [min, max]; typing
    is filtered as you type (only numbers, nothing beyond the limits) and
    out-of-range values are clamped on Enter / leaving the field
  * dropdowns for choices (tail layout), checked text fields (NACA codes)
    and a file picker for airfoil .dat files
  * live analysis of the complete aircraft after every change: all
    cross-component constraints, fit checks, derived geometry and a
    three-view preview; parameters involved in a violated rule are marked
  * parameters that do not apply to the chosen tail layout are disabled
  * open / save configurations, export a report or preview image, and build
    the model in SolidWorks (only when every rule is satisfied)
  * design sweep: press 'Iterate' on any parameter to vary it between a
    min and max with a number of iterations (or over a list of options);
    all other parameters stay fixed. Every generated design is checked
    against the parameter limits and all cross-component rules, and only
    valid designs are exported / built in SolidWorks.

Run:   python aircraft_gui.py [config.json]
Needs: customtkinter, matplotlib, numpy (+ pywin32 and SolidWorks to build)
"""

from __future__ import annotations

import ast
import contextlib
import copy
import ctypes
import glob
import hashlib
import io
import json
import math
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import types
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
import matplotlib
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.collections import PolyCollection
from matplotlib.colors import to_rgba
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.ticker import MaxNLocator
import numpy as np
from PIL import Image, ImageColor

import aircraft_sw as A
import aircraft_sweep as S
import fuselage_sw as F
import tail_sw as T


def _tune_customtkinter():
    """Remove CustomTkinter's forced idle flushes.

    CTkScrollbar._draw and CTkOptionMenu._draw end with canvas.update_idletasks(),
    and every widget's appearance-mode handler with update_idletasks(). In Tk that
    call is not local to the widget: it runs *all* pending layout and redraw work of
    the whole application, synchronously. A scrollbar redraws on every scroll step
    and every layout change, so scrolling and resizing re-laid-out and repainted the
    window several times per step, and a light / dark switch did it once per widget.
    Tk performs exactly the same work once in its normal idle pass right after the
    event, so the flushes only cost time."""
    try:
        from customtkinter.windows.widgets.core_widget_classes.ctk_base_class import (
            CTkBaseClass)
    except ImportError:                                   # other CTk layout: leave it
        return

    def skip_canvas_flush(cls):
        draw = cls._draw

        def _draw(self, no_color_updates=False):
            canvas = self._canvas
            if "update_idletasks" not in vars(canvas):    # this canvas only
                canvas.update_idletasks = lambda: None
            draw(self, no_color_updates)
        cls._draw = _draw

    for cls in (ctk.CTkScrollbar, ctk.CTkOptionMenu):
        if hasattr(cls, "_draw"):
            skip_canvas_flush(cls)

    def _set_appearance_mode(self, mode_string):
        super(CTkBaseClass, self)._set_appearance_mode(mode_string)
        self._draw()
    CTkBaseClass._set_appearance_mode = _set_appearance_mode


_tune_customtkinter()

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "Aircraft Designer"

# ---- colours (light, dark) ---------------------------------------------------
OK = ("#1f9d55", "#34d399")
ERR = ("#d64545", "#f87171")
WARN = ("#c27803", "#fbbf24")
MUTED = ("gray40", "gray62")
DISABLED = ("gray70", "gray35")
CARD = ("gray96", "gray16")
NAV_SEL = ("gray80", "gray25")
ACCENT = ("#2563eb", "#3b82f6")
ITER_BG = ("#e8f0fe", "#1c2b47")
TEX_COLORS = ("#1f2937", "#e5e7eb")

PAGES = [("wing", "Wing"), ("fuselage", "Fuselage"), ("tail", "Tail"),
         ("integration", "Placement"), ("constraints", "Constraints"),
         ("sweep", "Design sweep"), ("build", "Build & Export")]
PARAM_PAGES = ("wing", "fuselage", "tail", "integration")
PAGE_SUBTITLES = {
    "wing": "Planform, sweep, dihedral, twist and airfoil of the main wing",
    "fuselage": "Size, layout, nose, cross-section, canopy and tail cone of the body",
    "tail": "Empennage layout, sizing by tail volume coefficients, surfaces and airfoil",
    "integration": "Where the wing and tail sit on the fuselage",
    "constraints": "Cross-component rules - evaluated live, all must pass to build",
    "sweep": "Generate a family of designs by iterating selected parameters - "
             "only designs that pass every rule are kept",
    "build": "Check, export and build the complete aircraft in SolidWorks",
}

# Formulas shown under a parameter's description:
#   (mathtext formula, [(symbol, meaning), ...])
_SUPER = r"\left|\dfrac{y}{b}\right|^{n} + \left|\dfrac{z}{a}\right|^{n} = 1"
_L = ("L", "fuselage length")
_H = ("H", "max height")
FORMULAS = {
    ("wing", "aspect_ratio"): (r"AR = \dfrac{b^2}{S_w}", [("b", "span"), ("S_w", "wing area")]),
    ("wing", "taper_ratio"): (r"\lambda = \dfrac{c_\mathrm{tip}}{c_\mathrm{root}}",
                              [("c", "chord")]),
    ("fuselage", "nose_frac"): (r"\dfrac{L_\mathrm{nose}}{L}", [_L]),
    ("fuselage", "tail_frac"): (r"\dfrac{L_\mathrm{tail\ cone}}{L}", [_L]),
    ("fuselage", "nose_droop"): (r"\dfrac{y_\mathrm{tip}}{H/2}",
                                 [("y_\\mathrm{tip}", "nose tip height"), _H]),
    ("fuselage", "section_n_top"): (_SUPER, [("a", "half width"), ("b", "upper half height")]),
    ("fuselage", "section_n_bottom"): (_SUPER, [("a", "half width"),
                                                ("b", "lower half height")]),
    ("fuselage", "canopy_start_frac"): (r"\dfrac{x_\mathrm{hump}}{L}",
                                        [("x_\\mathrm{hump}", "hump start"), _L]),
    ("fuselage", "canopy_length_frac"): (r"\dfrac{L_\mathrm{hump}}{L}", [_L]),
    ("fuselage", "tail_upsweep"): (r"\dfrac{y_\mathrm{end}}{H/2}",
                                   [("y_\\mathrm{end}", "tail-end centre height"), _H]),
    ("tail", "vh"): (r"V_h = \dfrac{S_h\,l_t}{S_w\,\bar{c}_w}",
                     [("S_h", "stabiliser area"), ("l_t", "tail arm"),
                      ("S_w", "wing area"), ("\\bar{c}_w", "wing MAC")]),
    ("tail", "vv"): (r"V_v = \dfrac{S_v\,l_t}{S_w\,b_w}",
                     [("S_v", "fin area"), ("l_t", "tail arm"),
                      ("S_w", "wing area"), ("b_w", "wing span")]),
    ("tail", "h_aspect_ratio"): (r"AR_h = \dfrac{b_h^2}{S_h}",
                                 [("b_h", "stabiliser span"), ("S_h", "stabiliser area")]),
    ("tail", "h_taper_ratio"): (r"\lambda_h = \dfrac{c_\mathrm{tip}}{c_\mathrm{root}}",
                                [("c", "stabiliser chord")]),
    ("tail", "v_aspect_ratio"): (r"AR_v = \dfrac{h_v^2}{S_v}",
                                 [("h_v", "fin height"), ("S_v", "fin area")]),
    ("tail", "v_taper_ratio"): (r"\lambda_v = \dfrac{c_\mathrm{tip}}{c_\mathrm{root}}",
                                [("c", "fin chord")]),
    ("tail", "v_tail_angle_deg"): (r"\tan^2\Gamma = \dfrac{S_v}{S_h}",
                                   [("\\Gamma", "panel angle (Purser-Campbell)")]),
    ("tail", "stab_height_frac"): (r"\dfrac{h_\mathrm{stab}}{h_\mathrm{fin}}",
                                   [("h", "height above the fin root")]),
    ("integration", "wing_x_frac"): (r"\dfrac{x_\mathrm{LE}}{L}",
                                     [("x_\\mathrm{LE}", "wing root leading edge"), _L]),
    ("integration", "tail_overhang_frac"): (
        r"\dfrac{\Delta x_\mathrm{TE}}{c_\mathrm{root}}",
        [("\\Delta x_\\mathrm{TE}", "root trailing edge past the fuselage end"),
         ("c_\\mathrm{root}", "tail root chord")]),
}
# How derived values are written in the constraint formulas
DERIVED_TEX = {
    "tail_vh_ratio": r"\frac{V_{h,\,\mathrm{achieved}}}{V_{h,\,\mathrm{target}}}",
    "tail_vv_ratio": r"\frac{V_{v,\,\mathrm{achieved}}}{V_{v,\,\mathrm{target}}}",
}
DERIVED_WORDS = {
    "wing_mac_mm": "wing MAC", "wing_root_te_x_mm": "wing root TE position",
    "wing_tail_gap_mm": "wing to tail gap", "tail_root_overlap_frac":
    "share of tail root on fuselage", "tail_below_fuselage_mm": "tail below fuselage",
    "fuselage_height_at_wing_mm": "fuselage height at wing root",
    "tail_height_mm": "tail height", "tail_span_mm": "tail span",
}

GROUPS = {
    "wing": [
        ("Planform", ["span_mm", "aspect_ratio", "taper_ratio"]),
        ("Sweep & dihedral", ["sweep_deg", "sweep_ref", "dihedral_deg"]),
        ("Twist", ["twist_deg", "twist_axis"]),
        ("Airfoil", ["naca", "airfoil_file", "n_points_per_surface"]),
        ("Model resolution", ["n_stations"]),
    ],
    "fuselage": [
        ("Overall size", ["length_mm", "max_width_mm", "max_height_mm"]),
        ("Longitudinal layout", ["nose_frac", "tail_frac"]),
        ("Nose", ["nose_bluntness", "nose_droop"]),
        ("Cross-section shape", ["section_n_top", "section_n_bottom",
                                 "section_n_nose", "section_n_tail"]),
        ("Canopy / avionics hump", ["canopy_height_mm", "canopy_start_frac",
                                    "canopy_length_frac", "canopy_peak_pos"]),
        ("Tail cone", ["tail_end_width_mm", "tail_end_height_mm", "tail_upsweep",
                       "tail_shape"]),
        ("Model resolution", ["n_nose_stations", "n_mid_stations", "n_tail_stations",
                              "n_section_points"]),
    ],
    "tail": [
        ("Layout", ["configuration"]),
        ("Sizing", ["vh", "vv", "h_area_mm2", "v_area_mm2"]),
        ("Horizontal stabiliser / V panels", ["h_aspect_ratio", "h_taper_ratio",
                                              "h_sweep_deg", "h_dihedral_deg",
                                              "h_incidence_deg"]),
        ("Vertical fin", ["v_aspect_ratio", "v_taper_ratio", "v_sweep_deg"]),
        ("Layout specific", ["v_tail_angle_deg", "stab_height_frac", "fin_below_frac",
                             "ventral_fin_frac", "fin_x_offset_mm"]),
        ("Airfoil", ["naca", "airfoil_file", "n_points_per_surface"]),
    ],
    "integration": [
        ("Wing placement", ["wing_x_frac", "wing_vertical_pos", "wing_incidence_deg"]),
        ("Tail placement", ["tail_overhang_frac", "tail_vertical_pos"]),
    ],
}

LABELS = {
    "span_mm": "Wing span", "aspect_ratio": "Aspect ratio", "taper_ratio": "Taper ratio",
    "sweep_deg": "Sweep angle", "sweep_ref": "Sweep reference line",
    "dihedral_deg": "Dihedral", "twist_deg": "Tip twist", "twist_axis": "Twist axis",
    "naca": "NACA section", "airfoil_file": "Airfoil file",
    "n_points_per_surface": "Airfoil resample points", "n_stations": "Loft sections",
    "length_mm": "Length", "max_width_mm": "Max width", "max_height_mm": "Max height",
    "nose_frac": "Nose length", "tail_frac": "Tail-cone length",
    "nose_bluntness": "Nose bluntness", "nose_droop": "Nose droop",
    "section_n_top": "Upper section exponent", "section_n_bottom": "Lower section exponent",
    "section_n_nose": "Nose section exponent", "section_n_tail": "Tail section exponent",
    "canopy_start_frac": "Hump start", "canopy_length_frac": "Hump length",
    "canopy_height_mm": "Hump height", "canopy_peak_pos": "Hump peak position",
    "tail_end_width_mm": "Tail-end width", "tail_end_height_mm": "Tail-end height",
    "tail_upsweep": "Tail upsweep", "tail_shape": "Tail taper shape",
    "n_nose_stations": "Nose sections", "n_mid_stations": "Mid-body sections",
    "n_tail_stations": "Tail-cone sections", "n_section_points": "Points per section",
    "configuration": "Tail configuration", "vh": "Horizontal volume coeff. Vh",
    "vv": "Vertical volume coeff. Vv", "h_area_mm2": "Fixed stabiliser area",
    "v_area_mm2": "Fixed fin area", "h_aspect_ratio": "Aspect ratio",
    "h_taper_ratio": "Taper ratio", "h_sweep_deg": "Sweep (c/4)",
    "h_dihedral_deg": "Dihedral", "h_incidence_deg": "Incidence",
    "v_aspect_ratio": "Aspect ratio", "v_taper_ratio": "Taper ratio",
    "v_sweep_deg": "Sweep (c/4)", "v_tail_angle_deg": "V-panel angle",
    "stab_height_frac": "Stabiliser height (cruciform)",
    "fin_below_frac": "Fin share below stab (H-tail)",
    "ventral_fin_frac": "Ventral fin share (Y-tail)", "fin_x_offset_mm": "Fin x offset",
    "wing_x_frac": "Wing position", "wing_vertical_pos": "Wing height",
    "wing_incidence_deg": "Wing incidence", "tail_overhang_frac": "Tail overhang",
    "tail_vertical_pos": "Tail height",
}
UNIT_TEXT = {"-": "", "mm2": "mm²", "deg": "°"}
TAIL_CHOICES = {
    "conventional": "Conventional", "t_tail": "T-tail", "cruciform": "Cruciform",
    "h_tail": "H-tail (twin fins)", "v_tail": "V-tail", "inverted_v": "Inverted V (A-tail)",
    "y_tail": "Y-tail (V + ventral fin)",
}
V_TYPES = {"v_tail", "inverted_v", "y_tail"}


def pretty(name: str) -> str:
    return LABELS.get(name) or re.sub(r"_(mm2|mm|deg|frac)$", "", name).replace("_", " ").capitalize()


def fmt(v) -> str:
    if isinstance(v, int):
        return str(v)
    return f"{v:.6g}"


def text_fg():
    """The theme's normal label text colour (light, dark)."""
    return tuple(ctk.ThemeManager.theme["CTkLabel"]["text_color"])


def fit_lines(text: str, font, width: int, lines: int) -> str:
    """Word-wrap `text` to `width` px and cut it to `lines` lines, ending in '…'."""
    out, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if font.measure(trial) <= width or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = word
    out.append(cur)
    if len(out) <= lines:
        return "\n".join(out)
    last = out[lines - 1]
    while last and font.measure(last + " …") > width:
        last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
    return "\n".join(out[:lines - 1] + [last + " …"])


def fmt_lim(v) -> str:
    """Limits for display: 100000000 -> '100,000,000' instead of '1e+08'."""
    if abs(v) >= 1e5 and float(v) == int(v):
        return f"{int(v):,}"
    return fmt(v)


# ---- formulas rendered with matplotlib mathtext ------------------------------
# Each formula is drawn once as a coverage mask (white text, alpha = ink) and tinted
# for the light and dark theme. Masks are cached in memory and on disk, and are
# pre-rendered in a background thread at start-up, so pages open without waiting.
_TEX_SS = 3                                   # supersampling for crisp text
_TEX_CACHE = {}                               # (text, size, colors) -> CTkImage
_MASK_CACHE = {}                              # (text, size) -> PIL "L" image
_RENDER_LOCK = threading.Lock()               # matplotlib text layout is not thread safe
TEX_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                       "AircraftDesigner", "tex")


def _render_mask(text: str, size: float) -> Image.Image:
    prop = FontProperties(family=["Segoe UI", "DejaVu Sans"], size=size,
                          math_fontfamily="cm")
    # linespacing breaks the layout of tall math, so only use it between lines
    extra = {"linespacing": 1.5} if "\n" in text else {}
    w_in, h_in = 9.0, 2.5
    while True:
        fig = Figure(figsize=(w_in, h_in), dpi=72 * _TEX_SS)
        fig.patch.set_visible(False)
        canvas = FigureCanvasAgg(fig)
        fig.text(0.02, 0.5, text, fontproperties=prop, color="white", va="center", **extra)
        canvas.draw()
        alpha = np.asarray(canvas.buffer_rgba())[:, :, 3]
        ys, xs = np.nonzero(alpha)
        if xs.size == 0:
            return Image.new("L", (1, 1), 0)
        H, W = alpha.shape
        if xs.max() < W - 2 and ys.min() > 1 and ys.max() < H - 2:
            break
        w_in, h_in = w_in * 1.6, h_in * 1.6            # did not fit: render larger
    pad = 2 * _TEX_SS
    x0, x1 = max(xs.min() - pad, 0), min(xs.max() + pad + 1, W)
    y0, y1 = max(ys.min() - pad, 0), min(ys.max() + pad + 1, H)
    return Image.fromarray(np.ascontiguousarray(alpha[y0:y1, x0:x1]), "L")


def tex_mask(text: str, size: float) -> Image.Image:
    key = (text, size)
    mask = _MASK_CACHE.get(key)
    if mask is not None:
        return mask
    digest = hashlib.sha1(f"{text}|{size}|{_TEX_SS}|{matplotlib.__version__}"
                          .encode()).hexdigest()
    path = os.path.join(TEX_DIR, digest + ".png")
    try:
        with Image.open(path) as im:
            mask = im.convert("L")
    except (OSError, ValueError):
        with _RENDER_LOCK:
            mask = _MASK_CACHE.get(key) or _render_mask(text, size)
        try:
            os.makedirs(TEX_DIR, exist_ok=True)
            mask.save(path)
        except OSError:
            pass
    _MASK_CACHE[key] = mask
    return mask


def _tint(mask: Image.Image, color: str) -> Image.Image:
    img = Image.new("RGBA", mask.size, ImageColor.getrgb(color)[:3] + (255,))
    img.putalpha(mask)
    return img


def tex_image(text: str, size: float = 13, colors=TEX_COLORS):
    """CTkImage (light + dark) of text that may contain $math$ and newlines."""
    key = (text, size, colors)
    img = _TEX_CACHE.get(key)
    if img is None:
        mask = tex_mask(text, size)
        w, h = mask.size
        img = ctk.CTkImage(light_image=_tint(mask, colors[0]),
                           dark_image=_tint(mask, colors[1]),
                           size=(max(1, round(w / _TEX_SS)), max(1, round(h / _TEX_SS))))
        _TEX_CACHE[key] = img
    return img


def all_formula_texts(raw: dict) -> list:
    """(text, size) of every formula image the GUI shows, for pre-rendering."""
    out = []
    for tex, legend in FORMULAS.values():
        out.append((f"${tex}$", 14))
        if legend:
            out.append((legend_text(legend), 9.5))
    for c in raw.get("constraints", []):
        try:
            out.append((f"${expr_to_tex(c['expr'])}$", 12.5))
        except Exception:
            pass
    return out


def warm_formulas(items) -> threading.Thread:
    """Render formula masks in the background (pure matplotlib + PIL, no Tk)."""
    def work():
        for text, size in items:
            try:
                tex_mask(text, size)
            except Exception:
                pass
    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


def tex_label(parent, text, size=13, colors=TEX_COLORS, bg=None):
    """Label showing rendered math (light / dark aware)."""
    return LiteLabel(parent, image=tex_image(text, size, colors), bg=bg or CARD)


def legend_text(items, per_line=2) -> str:
    parts = [f"${sym}$  {words}" for sym, words in items]
    return "\n".join("      ".join(parts[i:i + per_line]) for i in range(0, len(parts), per_line))


def _words(text: str) -> str:
    return r"\mathrm{" + text.replace("-", " ").replace(" ", r"\ ") + "}"


def term_tex(sec: str, name: str) -> str:
    if sec == "derived":
        if name in DERIVED_TEX:
            return DERIVED_TEX[name]
        return _words(DERIVED_WORDS.get(name)
                      or re.sub(r"_(mm2|mm|deg|frac)$", "", name).replace("_", " "))
    words = pretty(name).lower()
    if sec in ("wing", "fuselage", "tail") and sec not in words:
        words = f"{sec} {words}"
    return _words(words)


def expr_to_tex(expr: str) -> str:
    """Constraint expression -> mathtext, e.g. a / sqrt(b * c) -> \\dfrac{a}{\\sqrt{b c}}."""
    def par(t, p, need):
        return rf"\left({t}\right)" if p < need else t

    def go(n, top):
        if isinstance(n, ast.Constant):
            return f"{n.value:g}", 9
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            return term_tex(n.value.id, n.attr), 9
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            t, p = go(n.operand, top)
            return "-" + par(t, p, 2), 2
        if isinstance(n, ast.BinOp):
            if isinstance(n.op, ast.Div):
                a, b = go(n.left, False)[0], go(n.right, False)[0]
                return (r"\dfrac" if top else r"\frac") + f"{{{a}}}{{{b}}}", 9
            if isinstance(n.op, ast.Pow):
                a, pa = go(n.left, False)
                return f"{{{par(a, pa, 9)}}}^{{{go(n.right, False)[0]}}}", 8
            (a, pa), (b, pb) = go(n.left, top), go(n.right, top)
            if isinstance(n.op, ast.Mult):
                return rf"{par(a, pa, 2)} \cdot {par(b, pb, 2)}", 2
            if isinstance(n.op, ast.Add):
                return f"{a} + {b}", 1
            if isinstance(n.op, ast.Sub):
                return f"{a} - {par(b, pb, 2)}", 1
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            args = [go(x, False)[0] for x in n.args]
            f = n.func.id
            if f == "sqrt":
                return rf"\sqrt{{{args[0]}}}", 9
            if f == "abs":
                return rf"\left|{args[0]}\right|", 9
            if f in ("min", "max", "tan", "sin", "cos"):
                return rf"\{f}\left({', '.join(args)}\right)", 9
        raise ValueError(f"cannot format {ast.dump(n)}")

    return go(ast.parse(expr, mode="eval").body, True)[0]


def fast_hull(pts: np.ndarray) -> np.ndarray:
    """2-D convex hull (monotone chain) for the preview outlines. Same algorithm as
    tail_sw._hull but on plain Python tuples, which is several times faster than
    indexing numpy rows inside the loop."""
    pts = np.unique(np.round(pts, 6), axis=0)
    if len(pts) < 3:
        return pts
    seq = [tuple(p) for p in pts.tolist()]

    def half(points):
        h = []
        for qx, qy in points:
            while len(h) >= 2 and ((h[-1][0] - h[-2][0]) * (qy - h[-2][1])
                                   - (h[-1][1] - h[-2][1]) * (qx - h[-2][0])) <= 0:
                h.pop()
            h.append((qx, qy))
        return h

    lower, upper = half(seq), half(seq[::-1])
    return np.array(lower[:-1] + upper[:-1])


def section_of_derived(name: str) -> set:
    if name.startswith("wing_tail"):
        return {"wing", "tail", "integration"}
    s = set()
    if name.startswith("wing"):
        s |= {"wing", "integration"}
    if name.startswith("tail"):
        s |= {"tail", "integration", "fuselage"}
    if "fuselage" in name or "fit" in name or "thickness" in name or "below" in name:
        s.add("fuselage")
    if name.startswith("aircraft"):
        s |= {"wing", "fuselage", "tail"}
    return s


# =============================================================================
# Small helpers
# =============================================================================
class Tooltip:
    """Hover help for any widget."""

    def __init__(self, widget, text_fn, delay=450):
        self.widget, self.text_fn, self.delay = widget, text_fn, delay
        self.tip, self.after_id = None, None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")

    def _enter(self, _=None):
        self._cancel()
        self.after_id = self.widget.after(self.delay, self._show)

    def _leave(self, _=None):
        self._cancel()
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None

    def _cancel(self):
        if self.after_id is not None:
            self.widget.after_cancel(self.after_id)
            self.after_id = None

    def _show(self):
        text = self.text_fn()
        if not text or self.tip is not None:
            return
        dark = ctk.get_appearance_mode() == "Dark"
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=text, justify="left", wraplength=380,
                 bg="#1f2937" if dark else "#fffbea", fg="#f3f4f6" if dark else "#1f2937",
                 relief="solid", bd=1, padx=9, pady=6, font=("Segoe UI", 9)).pack()


class LiteLabel(tk.Label):
    """Plain Tk label that follows CTk's light / dark mode and UI scaling.

    A CTkLabel is a frame + canvas + label with its own drawing code; for the many
    static texts of the parameter pages this single widget is far cheaper to create,
    lay out and recolour. Colours are (light, dark) pairs like everywhere in CTk;
    `bg` must be the colour of the surface the label sits on."""

    def __init__(self, master, text="", font=None, fg=MUTED, bg=CARD, image=None,
                 wraplength=0, **kw):
        super().__init__(master, text=text, bd=0, highlightthickness=0, padx=0, pady=0,
                         **kw)
        self._font, self._fg, self._bg = font, fg, bg
        self._img, self._wrap = image, wraplength
        self._mode = 1 if ctk.get_appearance_mode() == "Dark" else 0
        self._scale = ctk.ScalingTracker.get_widget_scaling(master)
        ctk.AppearanceModeTracker.add(self._on_mode, self)
        ctk.ScalingTracker.add_widget(self._on_scale, self)
        self._apply()

    def _pick(self, c):
        return c[self._mode] if isinstance(c, (tuple, list)) else c

    def _apply(self):
        opts = {"fg": self._pick(self._fg), "bg": self._pick(self._bg)}
        if self._font is not None:
            f = self._font
            opts["font"] = (f.cget("family"), -abs(round(f.cget("size") * self._scale)),
                            f.cget("weight"))
        if self._wrap:
            opts["wraplength"] = int(self._wrap * self._scale)
        if self._img is not None:
            opts["image"] = self._img.create_scaled_photo_image(
                self._scale, "dark" if self._mode else "light")
        super().configure(**opts)

    def style(self, fg=None, wraplength=None, text=None):
        """Change colour / wrap width / text, touching Tk only when something changed."""
        opts = {}
        if fg is not None and fg != self._fg:
            self._fg = fg
            opts["fg"] = self._pick(fg)
        if wraplength is not None and wraplength != self._wrap:
            self._wrap = wraplength
            opts["wraplength"] = int(wraplength * self._scale)
        if text is not None and text != self.cget("text"):
            opts["text"] = text
        if opts:
            super().configure(**opts)

    def _on_mode(self, mode):
        m = 1 if mode == "Dark" else 0
        if m != self._mode:
            self._mode = m
            self._apply()

    def _on_scale(self, widget_scaling, _window_scaling):
        if widget_scaling != self._scale:
            self._scale = widget_scaling
            self._apply()

    def destroy(self):
        ctk.AppearanceModeTracker.remove(self._on_mode)
        ctk.ScalingTracker.remove_widget(self._on_scale, self)
        super().destroy()


class LiteFrame(tk.Frame):
    """Plain Tk frame for pure layout containers that behaves like a transparent
    CTkFrame: it follows CTk's light / dark mode and scales its grid padding.

    A transparent CTkFrame is a frame plus a full-size canvas that CTk redraws on
    every resize; for the page area and the containers inside each parameter row
    (row, controls, formula) that was the largest single cost of resizing a page."""

    def __init__(self, master, bg=CARD, **kw):
        super().__init__(master, bd=0, highlightthickness=0, **kw)
        self._bg = bg
        self._mode = 1 if ctk.get_appearance_mode() == "Dark" else 0
        self._scale = ctk.ScalingTracker.get_widget_scaling(master)
        self._grid_kw = {}
        ctk.AppearanceModeTracker.add(self._on_mode, self)
        ctk.ScalingTracker.add_widget(self._on_scale, self)
        self._apply()

    def cget(self, key):
        # CTk widgets read the colour of a plain Tk parent with cget("bg"); return the
        # (light, dark) pair so they follow mode switches as with a CTk parent
        if key in ("bg", "background"):
            return self._bg
        return super().cget(key)

    __getitem__ = cget

    def _apply(self):
        bg = self._bg[self._mode] if isinstance(self._bg, (tuple, list)) else self._bg
        super().configure(bg=bg)

    def _scaled(self, kw):
        def px(v):
            return tuple(round(x * self._scale) for x in v) if isinstance(v, tuple) \
                else round(v * self._scale)
        return {k: px(v) if k in ("padx", "pady", "ipadx", "ipady") else v
                for k, v in kw.items()}

    def grid(self, cnf=None, **kw):
        """Like CTk widgets: padding in logical px, scaled with the UI / DPI scale."""
        kw.update(cnf or {})
        if kw:
            self._grid_kw = kw
            super().grid_configure(**self._scaled(kw))
        else:                                       # re-show after grid_remove()
            super().grid_configure()

    grid_configure = grid

    def _on_mode(self, mode):
        m = 1 if mode == "Dark" else 0
        if m != self._mode:
            self._mode = m
            self._apply()

    def _on_scale(self, widget_scaling, _window_scaling):
        if widget_scaling != self._scale:
            self._scale = widget_scaling
            if self._grid_kw and self.winfo_manager() == "grid":
                super().grid_configure(**self._scaled(self._grid_kw))

    def destroy(self):
        ctk.AppearanceModeTracker.remove(self._on_mode)
        ctk.ScalingTracker.remove_widget(self._on_scale, self)
        super().destroy()


dump_config = A.dump_config


# =============================================================================
# One parameter row
# =============================================================================
class ParamRow:
    FLOAT_RE = r"-?\d*\.?\d*(?:[eE][-+]?\d*)?"
    INT_RE = r"-?\d*"

    def __init__(self, app, parent, sec, name, grid_row):
        self.app, self.sec, self.name = app, sec, name
        self.spec = app.raw[sec][name]
        self.title = pretty(name)
        unit = self.spec.get("unit", "-")
        self.unit = UNIT_TEXT.get(unit, unit)
        self.issue_text, self.level, self.enabled = "", "ok", True
        self._quiet = False
        self._bad = False                  # entry currently shows the red "invalid" border
        default = getattr(A.SECTIONS[sec](), name)
        if "choices" in self.spec:
            self.kind = "choice"
        elif isinstance(default, str):
            self.kind = "file" if name == "airfoil_file" else "text"
        else:
            self.kind = "number"
            self.is_int = isinstance(default, int)
            self.lo, self.hi = float(self.spec["min"]), float(self.spec["max"])
            span = self.hi - self.lo
            self.step = 1 if self.is_int else (10 ** (math.floor(math.log10(span)) - 2)
                                               if span > 0 else 0.01)

        f = self.frame = LiteFrame(parent)
        f.grid(row=grid_row, column=0, sticky="ew", padx=12, pady=6)
        f.grid_columnconfigure(1, weight=1)
        self.dot = LiteLabel(f, text="●", font=app.f_small, fg=OK, width=2)
        self.dot.grid(row=0, column=0, sticky="nw", pady=(4, 0))
        self.lbl = LiteLabel(f, text=self.title, font=app.f_label, fg=text_fg(), anchor="w")
        self.lbl.grid(row=0, column=1, sticky="w", pady=(4, 0))
        self.desc = LiteLabel(f, text=self.spec.get("description", ""), font=app.f_small,
                              anchor="w", justify="left", wraplength=app.desc_wrap)
        self.desc.grid(row=1, column=1, sticky="w", pady=(4, 0))
        formula = FORMULAS.get((sec, name))
        if formula:
            tex, legend = formula
            box = LiteFrame(f)
            box.grid(row=2, column=1, sticky="w", pady=(5, 0))
            tex_label(box, f"${tex}$", 14).pack(side="left")
            if legend:
                tex_label(box, legend_text(legend), 9.5, ("#4b5563", "#9ca3af")).pack(
                    side="left", padx=(18, 0))
        self.ctrl = ctrl = LiteFrame(f)
        ctrl.grid(row=0, column=2, rowspan=2, sticky="e", padx=(8, 0))
        self.iter_btn = None
        if self.kind != "file":
            self.iter_btn = ctk.CTkButton(f, text="Iterate", width=62, height=24,
                                          corner_radius=12, border_width=1, bg_color=CARD,
                                          font=app.f_tiny, command=self.toggle_iter)
            self.iter_btn.grid(row=0, column=3, rowspan=2, padx=(10, 0))
            Tooltip(self.iter_btn, lambda: (
                "Stop iterating this parameter" if self.sweep_var() else
                "Iterate this parameter in the design sweep: vary it between a min and "
                "max value with a number of iterations while the others stay fixed."))
        self.reset_btn = LiteLabel(f, text="↺", font=app.f_head, cursor="hand2", width=2)
        self.reset_btn.grid(row=0, column=4, rowspan=2, padx=(6, 0))
        self.reset_btn.bind("<Button-1>", lambda _e: self.reset())
        self.reset_btn.bind("<Enter>", lambda _e: self.reset_btn.style(fg=ACCENT), add="+")
        self.reset_btn.bind("<Leave>", lambda _e: self.reset_btn.style(fg=MUTED), add="+")
        Tooltip(self.reset_btn, lambda: "Reset to the value in the loaded file")
        for w in (self.dot, self.lbl):
            Tooltip(w, self.tooltip)
        getattr(self, f"_build_{self.kind}")(ctrl)
        self.show_value(self.value())
        self.iter_panel = None
        self.refresh_iter()

    # ---- widgets ---------------------------------------------------------------
    def _build_number(self, ctrl):
        app = self.app
        self.slider = ctk.CTkSlider(ctrl, from_=self.lo, to=self.hi, width=160,
                                    command=self.on_slide, bg_color=CARD,
                                    number_of_steps=(int(self.hi - self.lo)
                                                     if self.is_int and self.hi - self.lo <= 400
                                                     else None))
        self.slider.grid(row=0, column=0, padx=(0, 8))
        self.slider.bind("<ButtonPress-1>", lambda _e: app.set_dragging(True), add="+")
        self.slider.bind("<ButtonRelease-1>", lambda _e: app.set_dragging(False), add="+")
        self.entry = ctk.CTkEntry(ctrl, width=86, justify="right", font=app.f_mono,
                                  bg_color=CARD)
        self.entry.grid(row=0, column=1)
        self.entry.configure(validate="key",
                             validatecommand=(self.entry.register(self._vcmd_number), "%P"))
        self.entry.bind("<Return>", self.commit)
        self.entry.bind("<FocusOut>", self.commit)
        self.entry.bind("<Escape>", lambda _: self.show_value(self.value()))
        LiteLabel(ctrl, text=self.unit, width=4, anchor="w", font=app.f_small).grid(
            row=0, column=2, padx=(4, 0))
        self.range_lbl = LiteLabel(ctrl, text=f"min {fmt_lim(self._typed(self.lo))}   ·   "
                                              f"max {fmt_lim(self._typed(self.hi))}",
                                   font=app.f_tiny)
        self.range_lbl.grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))
        self.default_border = ctk.ThemeManager.theme["CTkEntry"]["border_color"]

    def _build_choice(self, ctrl):
        choices = self.spec["choices"]
        labels = [TAIL_CHOICES.get(c, c) for c in choices] if self.name == "configuration" \
            else list(choices)
        self.to_value = dict(zip(labels, choices))
        self.to_label = dict(zip(choices, labels))
        self.menu = ctk.CTkOptionMenu(ctrl, values=labels, width=230, dynamic_resizing=False,
                                      bg_color=CARD,
                                      command=lambda lab: self.app.set_value(
                                          self.sec, self.name, self.to_value[lab]))
        self.menu.grid(row=0, column=0)
        LiteLabel(ctrl, text=f"{len(choices)} options", font=self.app.f_tiny).grid(
            row=1, column=0, sticky="w", pady=(3, 0))

    def _build_text(self, ctrl):
        self.entry = ctk.CTkEntry(ctrl, width=86, justify="center", font=self.app.f_mono,
                                  bg_color=CARD)
        self.entry.grid(row=0, column=0)
        self.entry.configure(validate="key", validatecommand=(
            self.entry.register(lambda P: self._quiet or re.fullmatch(r"\d{0,4}", P) is not None),
            "%P"))
        self.entry.bind("<Return>", self.commit)
        self.entry.bind("<FocusOut>", self.commit)
        hint = "symmetric: 00xx" if self.spec.get("pattern", "").startswith("^00") \
            else "4 digits, e.g. 2412"
        LiteLabel(ctrl, text=hint, font=self.app.f_tiny).grid(row=1, column=0, sticky="w",
                                                               pady=(3, 0))
        self.default_border = ctk.ThemeManager.theme["CTkEntry"]["border_color"]

    def _build_file(self, ctrl):
        self.entry = ctk.CTkEntry(ctrl, width=200, placeholder_text="(use NACA section)",
                                  font=self.app.f_small, bg_color=CARD)
        self.entry.grid(row=0, column=0)
        self.entry.bind("<Return>", self.commit)
        self.entry.bind("<FocusOut>", self.commit)
        self.browse = ctk.CTkButton(ctrl, text="Browse…", width=74, command=self._browse,
                                    bg_color=CARD)
        self.browse.grid(row=0, column=1, padx=(6, 0))
        LiteLabel(ctrl, text=".dat (Selig / Lednicer) - empty = NACA",
                  font=self.app.f_tiny).grid(row=1, column=0, columnspan=2, sticky="w",
                                             pady=(3, 0))
        self.default_border = ctk.ThemeManager.theme["CTkEntry"]["border_color"]

    # ---- values ------------------------------------------------------------------
    def value(self):
        return self.app.raw[self.sec][self.name]["value"]

    def _typed(self, v):
        return int(round(v)) if self.kind == "number" and self.is_int else float(v)

    def _set_entry(self, text):
        if self.entry.get() == text:
            return
        self._quiet = True
        try:
            st = self.entry.cget("state")
            if st != "normal":
                self.entry.configure(state="normal")
            self.entry.delete(0, "end")
            if text != "":
                self.entry.insert(0, text)
            if st != "normal":
                self.entry.configure(state=st)
        finally:
            self._quiet = False

    def show_value(self, v):
        if self.kind == "number":
            self._set_entry(fmt(v))
            self.slider.set(min(max(float(v), self.lo), self.hi))
            self._border(False)
        elif self.kind == "choice":
            self.menu.set(self.to_label.get(v, v))
        else:
            self._set_entry(v)
            self._border(False)

    def _border(self, bad):
        if self._bad == bad:                            # avoid redrawing the entry
            return
        self._bad = bad
        if hasattr(self, "entry"):
            self.entry.configure(border_color=ERR if bad else self.default_border)
        if self.kind == "number":
            self.range_lbl.style(fg=ERR if bad else MUTED)

    def reset(self):
        v = self.app.file_values[(self.sec, self.name)]
        self.app.set_value(self.sec, self.name, v)
        self.show_value(v)

    # ---- numeric input -----------------------------------------------------------
    def _vcmd_number(self, P):
        """Key filter: only number syntax, never beyond the parameter's limits."""
        if self._quiet:
            return True
        if not re.fullmatch(self.INT_RE if self.is_int else self.FLOAT_RE, P):
            return False
        if P.startswith("-") and self.lo >= 0:
            self.app.flash(f"{self.title}: negative values are not allowed "
                           f"(min {fmt(self._typed(self.lo))}).", "warn")
            return False
        try:
            v = float(P)
        except ValueError:                       # partial input like "", "-", "1e"
            self._border(P not in ("", "-", ".", "-."))
            return True
        lim = max(abs(self.lo), abs(self.hi))
        if abs(v) > lim and "e" not in P.lower():
            self.app.flash(f"{self.title}: the limit is {fmt(self._typed(self.lo))} … "
                           f"{fmt(self._typed(self.hi))} {self.unit}", "warn")
            return False
        inside = self.lo <= v <= self.hi
        self._border(not inside)
        if inside:                               # live update while typing
            self.entry.after_idle(lambda: self._apply_typed(v))
        return True

    def _apply_typed(self, v):
        v = self._typed(v)
        self.slider.set(v)
        self.app.set_value(self.sec, self.name, v)

    def snap(self, v):
        if self.is_int:
            return int(round(v))
        v = self.lo + round((v - self.lo) / self.step) * self.step
        return float(f"{min(max(v, self.lo), self.hi):.10g}")

    def on_slide(self, v):
        v = self.snap(v)
        self._set_entry(fmt(v))
        self._border(False)
        self.app.set_value(self.sec, self.name, v)

    def commit(self, _=None):
        if not self.enabled:
            return
        text = self.entry.get().strip()
        if self.kind == "number":
            try:
                v = float(text)
                if not math.isfinite(v):
                    raise ValueError
            except ValueError:
                if text:
                    self.app.flash(f"{self.title}: '{text}' is not a number - restored.", "warn")
                self.show_value(self.value())
                return
            if not self.lo <= v <= self.hi:
                c = min(max(v, self.lo), self.hi)
                self.app.flash(f"{self.title}: {v:g} is outside "
                               f"[{fmt(self._typed(self.lo))}, {fmt(self._typed(self.hi))}] "
                               f"- set to {fmt(self._typed(c))}.", "warn")
                v = c
            v = self._typed(v)
            self.app.set_value(self.sec, self.name, v)
            self.show_value(v)
            return
        pattern = self.spec.get("pattern", "")
        if self.kind == "file" and text and not os.path.isfile(text):
            self.app.flash(f"{self.title}: file not found - restored.", "warn")
            self.show_value(self.value())
            return
        if pattern and not re.search(pattern, text):
            self.app.flash(f"{self.title}: '{text}' is not valid ({self.desc.cget('text')}) "
                           "- restored.", "warn")
            self.show_value(self.value())
            return
        self.app.set_value(self.sec, self.name, text)
        self._border(False)

    def _browse(self):
        path = filedialog.askopenfilename(title="Select airfoil", initialdir=HERE,
                                          filetypes=[("Airfoil coordinates", "*.dat"),
                                                     ("All files", "*.*")])
        if path:
            path = os.path.normpath(path)
            self._set_entry(path)
            self.commit()

    # ---- iteration (design sweep) -------------------------------------------------
    def sweep_var(self):
        return self.app.find_sweep_var(self.sec, self.name)

    def toggle_iter(self):
        if self.sweep_var() is not None:
            self.app.remove_sweep_var(self.sec, self.name)
        else:
            self.app.add_sweep_var(self.default_iter())

    def default_iter(self) -> dict:
        param = f"{self.sec}.{self.name}"
        if self.kind == "number":
            v = float(self.value())
            a, b = (sorted((v * 0.8, v * 1.2)) if v else (self.lo, self.hi))
            a, b = max(a, self.lo), min(b, self.hi)
            if self.is_int:
                a, b = math.floor(a), math.ceil(b)
            if not a < b:
                a, b = self.lo, self.hi
            a, b = self._typed(float(f"{a:.4g}")), self._typed(float(f"{b:.4g}"))
            a, b = max(a, self._typed(self.lo)), min(b, self._typed(self.hi))
            n = 5 if not self.is_int else max(2, min(5, int(b - a) + 1))
            return {"param": param, "min": a, "max": b, "count": n}
        if self.kind == "choice":
            return {"param": param, "values": list(self.spec["choices"])}
        return {"param": param, "values": [self.value()]}

    def refresh_iter(self):
        """Show / hide the iteration panel to match the sweep definition."""
        if self.iter_btn is None:
            return
        var = self.sweep_var()
        if var is None:
            self.iter_btn.configure(text="Iterate", fg_color="transparent",
                                    text_color=MUTED, border_color=DISABLED,
                                    hover_color=NAV_SEL)
            if self.iter_panel is not None:
                self.iter_panel.destroy()
                self.iter_panel = None
            return
        self.iter_btn.configure(text="Iterating", fg_color=ACCENT, text_color="white",
                                border_color=ACCENT,
                                hover_color=ctk.ThemeManager.theme["CTkButton"]["hover_color"])
        if self.iter_panel is None:
            self._build_iter_panel()
        self._show_iter_values(var)

    def _build_iter_panel(self):
        app = self.app
        p = self.iter_panel = ctk.CTkFrame(self.frame, fg_color=ITER_BG, corner_radius=8,
                                           bg_color=CARD)
        p.grid(row=3, column=1, columnspan=4, sticky="ew", pady=(6, 2))
        p.grid_columnconfigure(8, weight=1)
        ctk.CTkLabel(p, text="ITERATE", font=app.f_tiny, text_color=ACCENT).grid(
            row=0, column=0, padx=(12, 6), pady=6, sticky="w")
        self.it_entries = {}
        if self.kind == "number":
            unit = f" {self.unit}" if self.unit else ""
            for i, (key, text) in enumerate((("min", f"Min{unit}"), ("max", f"Max{unit}"),
                                             ("count", "Iterations"))):
                ctk.CTkLabel(p, text=text, font=app.f_small).grid(
                    row=0, column=1 + 2 * i, padx=(10, 4))
                e = ctk.CTkEntry(p, width=52 if key == "count" else 84, height=26,
                                 justify="right", font=app.f_mono)
                e.grid(row=0, column=2 + 2 * i)
                e.configure(validate="key", validatecommand=(
                    e.register(lambda P, k=key: self._vcmd_iter(P, k)), "%P"))
                e.bind("<Return>", lambda _e, k=key: self._commit_iter(k))
                e.bind("<FocusOut>", lambda _e, k=key: self._commit_iter(k))
                self.it_entries[key] = e
        elif self.kind == "choice":
            box = ctk.CTkFrame(p, fg_color="transparent")
            box.grid(row=0, column=1, columnspan=7, sticky="w", pady=4)
            self.it_checks = {}
            for i, c in enumerate(self.spec["choices"]):
                var = tk.BooleanVar()
                cb = ctk.CTkCheckBox(box, text=self.to_label.get(c, c), variable=var,
                                     font=app.f_small, checkbox_width=18, checkbox_height=18,
                                     command=self._commit_iter_choice)
                cb.grid(row=i // 3, column=i % 3, sticky="w", padx=(8, 6), pady=2)
                self.it_checks[c] = var
        else:
            ctk.CTkLabel(p, text="Values", font=app.f_small).grid(row=0, column=1, padx=(10, 4))
            e = ctk.CTkEntry(p, width=230, height=26, font=app.f_mono,
                             placeholder_text="e.g. 0009, 0010, 0012")
            e.grid(row=0, column=2, columnspan=5, sticky="w")
            e.configure(validate="key", validatecommand=(
                e.register(lambda P: self._quiet or re.fullmatch(r"[0-9,; ]*", P) is not None),
                "%P"))
            e.bind("<Return>", lambda _e: self._commit_iter_text())
            e.bind("<FocusOut>", lambda _e: self._commit_iter_text())
            self.it_entries["values"] = e
        x = ctk.CTkButton(p, text="✕", width=24, height=24, fg_color="transparent",
                          hover_color=NAV_SEL, text_color=MUTED, command=self.toggle_iter)
        x.grid(row=0, column=9, padx=(4, 8))
        Tooltip(x, lambda: "Stop iterating - the fixed value above is used again")
        self.it_info = ctk.CTkLabel(p, text="", font=app.f_tiny, text_color=MUTED,
                                    anchor="w", justify="left", wraplength=520)
        self.it_info.grid(row=1, column=0, columnspan=10, sticky="w", padx=12, pady=(0, 6))

    def _put(self, e, text):
        self._quiet = True
        try:
            e.delete(0, "end")
            e.insert(0, text)
        finally:
            self._quiet = False

    def _sweepvar_obj(self, var):
        if self.kind == "number":
            return S.SweepVar(self.sec, self.name, "number", lo=float(var["min"]),
                              hi=float(var["max"]), count=int(var["count"]),
                              is_int=self.is_int, lim_lo=self.lo, lim_hi=self.hi)
        return S.SweepVar(self.sec, self.name, "list", values=list(var["values"]))

    def _show_iter_values(self, var):
        if self.kind == "number":
            for k in ("min", "max", "count"):
                self._put(self.it_entries[k], fmt(var[k]))
                self.it_entries[k].configure(border_color=self.default_border)
        elif self.kind == "choice":
            for c, bv in self.it_checks.items():
                bv.set(c in var["values"])
        else:
            self._put(self.it_entries["values"], ", ".join(var["values"]))
        lv = self._sweepvar_obj(var).levels()
        shown = [fmt(x) if self.kind == "number" else
                 (self.to_label.get(x, x) if self.kind == "choice" else x) for x in lv]
        if len(shown) > 9:
            shown = shown[:5] + ["…"] + shown[-3:]
        txt = f"{len(lv)} value{'s' * (len(lv) != 1)}:  " + "   ·   ".join(shown)
        if self.kind == "number" and self.is_int and len(lv) < var["count"]:
            txt += f"   (only {len(lv)} different whole numbers in this range)"
        if len(lv) < 2:
            txt += "   - select at least two values to vary this parameter"
        self.it_info.configure(text=txt + "\nThe fixed value above is replaced by these "
                                          "values in the design sweep.")

    def _vcmd_iter(self, P, key):
        if self._quiet:
            return True
        if key == "count":
            return re.fullmatch(r"\d{0,3}", P) is not None
        if not re.fullmatch(self.INT_RE if self.is_int else self.FLOAT_RE, P):
            return False
        if P.startswith("-") and self.lo >= 0:
            self.app.flash(f"{self.title}: negative values are not allowed.", "warn")
            return False
        try:
            v = float(P)
        except ValueError:
            return True
        if abs(v) > max(abs(self.lo), abs(self.hi)) and "e" not in P.lower():
            self.app.flash(f"{self.title}: iteration range must stay within "
                           f"{fmt_lim(self._typed(self.lo))} … "
                           f"{fmt_lim(self._typed(self.hi))} {self.unit}", "warn")
            return False
        self.it_entries[key].configure(
            border_color=self.default_border if self.lo <= v <= self.hi else ERR)
        return True

    def _commit_iter(self, key):
        var = self.sweep_var()
        if var is None or self.iter_panel is None:
            return
        old = dict(var)
        text = self.it_entries[key].get().strip()
        try:
            v = float(text)
            if not math.isfinite(v):
                raise ValueError
        except ValueError:
            if text:
                self.app.flash(f"{self.title}: '{text}' is not a number - restored.", "warn")
            self._show_iter_values(var)
            return
        if key == "count":
            n = min(max(int(round(v)), 2), S.MAX_COUNT)
            if n != v:
                self.app.flash(f"{self.title}: iterations must be 2 … {S.MAX_COUNT} "
                               f"- set to {n}.", "warn")
            var["count"] = n
        else:
            c = min(max(v, self.lo), self.hi)
            if c != v:
                self.app.flash(f"{self.title}: {v:g} is outside the parameter limits "
                               f"[{fmt_lim(self._typed(self.lo))}, "
                               f"{fmt_lim(self._typed(self.hi))}] - set to "
                               f"{fmt(self._typed(c))}.", "warn")
            var[key] = self._typed(c)
            if var["min"] > var["max"]:
                var["min"], var["max"] = var["max"], var["min"]
                self.app.flash(f"{self.title}: min was larger than max - swapped.", "info")
            if var["min"] == var["max"]:
                var.update(old)
                self.app.flash(f"{self.title}: min and max must be different - restored.",
                               "warn")
        self._show_iter_values(var)
        if var != old:
            self.app.sweep_changed()

    def _commit_iter_choice(self):
        var = self.sweep_var()
        chosen = [c for c, bv in self.it_checks.items() if bv.get()]
        if not chosen:
            self.app.flash(f"{self.title}: keep at least one option selected.", "warn")
            self._show_iter_values(var)
            return
        var["values"] = chosen
        self._show_iter_values(var)
        self.app.sweep_changed()

    def _commit_iter_text(self):
        var = self.sweep_var()
        if var is None:
            return
        parts = [s for s in re.split(r"[,; ]+", self.it_entries["values"].get()) if s]
        pattern = self.spec.get("pattern", "")
        good = []
        for s in parts:
            if pattern and not re.search(pattern, s):
                self.app.flash(f"{self.title}: '{s}' is not valid "
                               f"({self.spec.get('description', '')}) - removed.", "warn")
            elif s not in good:
                good.append(s)
        if not good:
            self._show_iter_values(var)
            return
        changed = good != var["values"]
        var["values"] = good
        self._show_iter_values(var)
        if changed:
            self.app.sweep_changed()

    # ---- state ---------------------------------------------------------------------
    def set_enabled(self, on: bool):
        if on == self.enabled:
            return
        self.enabled = on
        st = "normal" if on else "disabled"
        for w in ("slider", "entry", "menu", "browse"):
            if hasattr(self, w):
                getattr(self, w).configure(state=st)
        if hasattr(self, "slider"):
            th = ctk.ThemeManager.theme["CTkSlider"]
            self.slider.configure(button_color=th["button_color"] if on else DISABLED,
                                  progress_color=th["progress_color"] if on else DISABLED)
        if hasattr(self, "entry"):
            self.entry.configure(text_color=ctk.ThemeManager.theme["CTkEntry"]["text_color"]
                                 if on else DISABLED)
        self.lbl.style(fg=text_fg() if on else DISABLED)
        self.set_issue(self.level, self.issue_text, force=True)

    def set_issue(self, level, text="", force=False):
        changed = force or level != self.level
        self.level, self.issue_text = level, text
        if changed:                                   # recolour only on a real change
            color = {"ok": OK, "warn": WARN, "error": ERR}[level]
            self.dot.style(fg=color if self.enabled else DISABLED)

    def tooltip(self):
        t = f"{self.title}\n{self.spec.get('description', '')}"
        if self.kind == "number":
            t += (f"\nAllowed: {fmt_lim(self._typed(self.lo))} … "
                  f"{fmt_lim(self._typed(self.hi))} {self.unit}")
        if not self.enabled:
            t += "\n\nNot used by the current tail layout / settings."
        if self.issue_text:
            t += "\n\n" + self.issue_text
        return t

    def matches(self, q):
        return (q in self.title.lower() or q in self.name.lower()
                or q in self.spec.get("description", "").lower())


# =============================================================================
# Application
# =============================================================================
class AircraftDesigner(ctk.CTk):
    def __init__(self, config_path: str):
        super().__init__()
        # CTk binds <Configure> on the root window. Tk runs a root binding for the
        # <Configure> of every child widget too, so each resize step called CTk's
        # handler (two window-size queries) once per widget - thousands of times.
        # The class tag of the root window is only on the root itself.
        self.unbind("<Configure>")
        self.bind_class(self.winfo_class(), "<Configure>", self._update_dimensions_event,
                        add="+")
        ctk.set_default_color_theme("blue")
        self.f_title = ctk.CTkFont(size=22, weight="bold")
        self.f_head = ctk.CTkFont(size=14, weight="bold")
        self.f_label = ctk.CTkFont(size=13, weight="bold")
        self.f_body = ctk.CTkFont(size=13)
        self.f_small = ctk.CTkFont(size=11)
        self.f_tiny = ctk.CTkFont(size=10)
        self.f_mono = ctk.CTkFont(family="Consolas", size=12)
        self.f_big = ctk.CTkFont(size=17, weight="bold")

        self._fit_window_to_screen(1480, 900, 1240, 760)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.path, self.raw = None, None
        self.dirty = False
        self.pages, self.rows, self.current = {}, {}, None
        self.analysis_job, self.flash_job = None, None
        self.last = None                 # (cfg, ac, results) of the last valid analysis
        self.issues, self.results, self.notes, self.current_ac = [], [], [], None
        self.proc, self.log_queue, self.run_target = None, queue.Queue(), None
        self.sweep_designs, self.sweep_base, self.sweep_used = [], None, []
        self.sweep_base_key, self.sweep_thread, self.sweep_q = None, None, queue.Queue()
        self.sweep_stop = threading.Event()
        self.preview_design = None        # (index, ac) of a sweep design shown in the preview
        # rendering state: only redraw what is visible and has changed
        self.dragging = False
        self.desc_wrap = 300              # description wrap width (logical px)
        self._preview_key = None          # what the preview currently shows
        self._stale = {"Preview": True, "Checks": True, "Data": True}
        self._checks_sig = None
        self._data_text = None
        self._badge_state = {}
        self._title = None
        self._wrap_job = None
        self._pending = {}                # page key -> generator still building it
        self._prebuild = []               # page keys still to build while idle
        self._prebuild_job = None
        self._page_wrap = {}              # page key -> description wrap it was laid out with
        self._host_size = (0, 0)          # latest size of the page area
        self._repark_job = None           # idle re-layout of hidden pages after a resize
        self._preview_size = None         # canvas size the preview was last rendered for
        self._preview_pending = None      # canvas size waiting to be rendered
        self._preview_resize_job = None

        self._build_layout()
        ctk.AppearanceModeTracker.add(self._on_appearance, self)
        self.load(config_path)
        self.bind_all("<Control-s>", lambda e: self.save())
        self.bind_all("<Control-o>", lambda e: self.open_dialog())

    # ------------------------------------------------------------------ layout
    # The three columns have fixed sizes: the window is split once, and nothing a
    # parameter change puts into a label (long messages, wrapped text) can resize
    # the parameter panel or the preview - which would also force a full re-render.
    RIGHT_W = 490                       # right panel width (logical px)
    STATUS_WRAP = 330                   # status text width beside the Build button

    def _fit_window_to_screen(self, w, h, min_w, min_h):
        """Default size, but never larger than the screen's work area (so the
        bottom of the preview cannot end up behind the taskbar or off screen)."""
        s = ctk.ScalingTracker.get_window_dpi_scaling(self) or 1.0
        work_w, work_h = self.winfo_screenwidth(), self.winfo_screenheight() - 48
        if sys.platform == "win32":
            try:
                import ctypes
                import ctypes.wintypes as wt
                r = wt.RECT()
                ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)
                work_w, work_h = r.right - r.left, r.bottom - r.top
            except Exception:
                pass
        avail_w, avail_h = int(work_w / s), int(work_h / s) - 40     # minus title bar
        self.minsize(min(min_w, avail_w), min(min_h, avail_h))
        if w <= avail_w and h <= avail_h:
            self.geometry(f"{w}x{h}")
        else:
            self.geometry(f"{min(w, avail_w)}x{min(h, avail_h)}+0+0")
            if sys.platform == "win32":
                self.after(0, lambda: self.state("zoomed"))

    def _build_layout(self):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(2, weight=0)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)

        # sidebar --------------------------------------------------------------
        sb = ctk.CTkFrame(self, width=210, corner_radius=0)
        sb.grid(row=0, column=0, rowspan=2, sticky="nsw")
        sb.grid_propagate(False)
        sb.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(sb, text="✈  " + APP_NAME, font=self.f_big).grid(
            row=0, column=0, padx=18, pady=(22, 0), sticky="w")
        ctk.CTkLabel(sb, text="Parametric SolidWorks aircraft", font=self.f_small,
                     text_color=MUTED).grid(row=1, column=0, padx=18, pady=(0, 18), sticky="w")
        self.nav, self.badges = {}, {}
        for i, (key, title) in enumerate(PAGES):
            if key == "constraints":
                ctk.CTkFrame(sb, height=1, fg_color=NAV_SEL).grid(
                    row=10 + i * 2 - 1, column=0, sticky="ew", padx=16, pady=8)
            b = ctk.CTkButton(sb, text=title, anchor="w", height=38, corner_radius=8,
                              font=self.f_body, fg_color="transparent",
                              text_color=("gray10", "gray90"), hover_color=NAV_SEL,
                              command=lambda k=key: self.show_page(k))
            b.grid(row=10 + i * 2, column=0, sticky="ew", padx=10, pady=2)
            badge = ctk.CTkLabel(b, text="", width=22, height=18, corner_radius=9,
                                 fg_color=ERR, text_color="white", font=self.f_tiny)
            self.nav[key], self.badges[key] = b, badge

        sb.grid_rowconfigure(30, weight=1)
        files = ctk.CTkFrame(sb, fg_color="transparent")
        files.grid(row=31, column=0, sticky="ew", padx=10)
        files.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(files, text="Open…", height=30, command=self.open_dialog).grid(
            row=0, column=0, sticky="ew", padx=(0, 3), pady=3)
        ctk.CTkButton(files, text="Save", height=30, command=self.save).grid(
            row=0, column=1, sticky="ew", padx=(3, 0), pady=3)
        ctk.CTkButton(files, text="Save as…", height=30, fg_color="transparent",
                      border_width=1, text_color=("gray10", "gray90"),
                      command=self.save_as).grid(row=1, column=0, sticky="ew", padx=(0, 3), pady=3)
        ctk.CTkButton(files, text="Reset all", height=30, fg_color="transparent",
                      border_width=1, text_color=("gray10", "gray90"),
                      command=self.reset_all).grid(row=1, column=1, sticky="ew", padx=(3, 0), pady=3)

        ctk.CTkLabel(sb, text="Appearance", font=self.f_small, text_color=MUTED).grid(
            row=32, column=0, padx=18, pady=(14, 0), sticky="w")
        self.mode_menu = ctk.CTkSegmentedButton(sb, values=["Light", "Dark", "System"],
                                                command=self.set_mode)
        self.mode_menu.set("System")
        self.mode_menu.grid(row=33, column=0, padx=14, pady=(2, 6), sticky="ew")
        ctk.CTkLabel(sb, text="UI scale", font=self.f_small, text_color=MUTED).grid(
            row=34, column=0, padx=18, sticky="w")
        scale = ctk.CTkOptionMenu(sb, values=["90%", "100%", "110%", "125%"], height=28,
                                  command=lambda s: ctk.set_widget_scaling(int(s[:-1]) / 100))
        scale.set("100%")
        scale.grid(row=35, column=0, padx=14, pady=(2, 18), sticky="ew")

        # main area ------------------------------------------------------------
        main = ctk.CTkFrame(self, fg_color="transparent", width=600, height=400)
        main.grid(row=0, column=1, sticky="nsew", padx=(16, 8), pady=(14, 4))
        main.grid_propagate(False)          # takes the space left over, never its content's
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)
        head = ctk.CTkFrame(main, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        head.grid_columnconfigure(0, weight=1)
        self.page_title = ctk.CTkLabel(head, text="", font=self.f_title, anchor="w")
        self.page_title.grid(row=0, column=0, sticky="w")
        self.page_sub = ctk.CTkLabel(head, text="", font=self.f_small, text_color=MUTED,
                                     anchor="w")
        self.page_sub.grid(row=1, column=0, sticky="w")
        self.search = ctk.CTkEntry(head, width=210, placeholder_text="Filter parameters…")
        self.search.bind("<KeyRelease>", lambda _: self.apply_filter())
        self.search.bind("<Escape>", lambda _: self.clear_filter())
        self.search.grid(row=0, column=1, rowspan=2, padx=(8, 6))
        self.reset_page_btn = ctk.CTkButton(head, text="Reset page", width=96,
                                            fg_color="transparent", border_width=1,
                                            text_color=("gray10", "gray90"),
                                            command=self.reset_page)
        self.reset_page_btn.grid(row=0, column=2, rowspan=2)
        # plain frame: a transparent CTkFrame would redraw a canvas the size of the
        # whole page area behind the page on every resize step
        self.page_host = LiteFrame(main, bg=self._page_bg())
        self.page_host.grid(row=1, column=0, sticky="nsew")
        self.page_host.grid_columnconfigure(0, weight=1)
        self.page_host.grid_rowconfigure(0, weight=1)
        self.page_host.bind("<Configure>", self._on_host_resize, add="+")

        # right panel ----------------------------------------------------------
        right = ctk.CTkFrame(self, fg_color="transparent", width=self.RIGHT_W, height=400)
        right.grid(row=0, column=2, sticky="nsew", padx=(8, 16), pady=(14, 4))
        right.grid_propagate(False)         # fixed width, whatever the content
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        card = ctk.CTkFrame(right, fg_color=CARD, corner_radius=12)
        card.grid(row=0, column=0, sticky="ew")
        card.grid_columnconfigure(0, weight=1)
        self.status_big = ctk.CTkLabel(card, text="", font=self.f_big, anchor="w", height=30)
        self.status_big.grid(row=0, column=0, sticky="w", padx=16, pady=(12, 0))
        # always exactly two lines high, so the card (and the preview below it)
        # keeps its height; the full message is in the Checks tab and the tooltip
        self.status_small = ctk.CTkLabel(card, text="", font=self.f_small, text_color=MUTED,
                                         anchor="nw", justify="left", height=32,
                                         wraplength=self.STATUS_WRAP + 40)
        self.status_small.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))
        Tooltip(self.status_small, lambda: self._status_full)
        self._status_full = ""
        self.quick_build = ctk.CTkButton(card, text="Build ▸", width=90, height=34,
                                         font=self.f_label, command=self.build)
        self.quick_build.grid(row=0, column=1, rowspan=2, padx=14)

        self.tabs = ctk.CTkTabview(right, corner_radius=12, command=self._on_tab_change)
        self.tabs.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        for name in ("Preview", "Checks", "Data"):
            self.tabs.add(name)
        self._build_preview(self.tabs.tab("Preview"))
        self.checks_frame = ctk.CTkScrollableFrame(self.tabs.tab("Checks"), fg_color="transparent")
        self.checks_frame.pack(fill="both", expand=True)
        self.checks_frame.grid_columnconfigure(0, weight=1)
        self._build_data(self.tabs.tab("Data"))

        # status bar -------------------------------------------------------------
        # placed (not gridded) inside a fixed-height strip, so a long message can
        # never widen the columns above it
        bar = ctk.CTkFrame(self, fg_color="transparent", height=24)
        bar.grid(row=1, column=1, columnspan=2, sticky="ew", padx=18, pady=(0, 6))
        self.statusbar = ctk.CTkLabel(bar, text="", anchor="w", font=self.f_small,
                                      text_color=MUTED, height=22)
        self.statusbar.place(x=0, rely=0.5, anchor="w")

    def _build_preview(self, tab):
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x")
        self.preview_caption = ctk.CTkLabel(bar, text="Current configuration",
                                            font=self.f_small, text_color=MUTED, anchor="w")
        self.preview_caption.pack(side="left", padx=4)
        self.preview_back = ctk.CTkButton(bar, text="Show current", width=96, height=24,
                                          font=self.f_tiny, command=self.clear_design_preview)
        self.fig = Figure(figsize=(4.6, 5.6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=tab)
        tkc = self.canvas.get_tk_widget()
        tkc.pack(fill="both", expand=True)
        # matplotlib re-renders the whole figure (~80 ms) on every <Configure> of its
        # canvas, i.e. many times while the window is resized, maximised or restored.
        # Replace its handler: render once, when the size has stopped changing.
        tkc.bind("<Configure>", self._on_preview_configure)
        # axes are created once; each update only swaps the aircraft outlines.
        # Their positions are computed from the aircraft's proportions (see
        # _layout_views) so every view always shows the complete aircraft.
        self.axes = [self.fig.add_axes((0.1, 0.1, 0.8, 0.2)) for _ in range(3)]
        for ax, title in zip(self.axes, ("Top view", "Side view", "Front view")):
            ax.set_aspect("equal", adjustable="datalim")
            # fixed title and (empty) axis-label positions: otherwise matplotlib
            # measures every tick label on each draw to place them (~40 % of a draw)
            ax.set_title(title, fontsize=9, loc="left", y=1.0, pad=4)
            ax.xaxis.set_label_coords(0.5, -0.1)
            ax.yaxis.set_label_coords(-0.1, 0.5)
            for s in ax.spines.values():
                s.set_visible(False)
            ax.set_axisbelow(True)
            ax.xaxis.set_major_locator(MaxNLocator(nbins="auto", steps=[1, 2, 2.5, 5, 10]))
            ax.yaxis.set_major_locator(MaxNLocator(nbins="auto", steps=[1, 2, 2.5, 5, 10]))
        self._view_aspects = np.array([0.8, 0.3, 0.25])    # height / width of each view
        self._preview_artists, self._preview_dark = [], None
        self._caption_state = None
        self._layout_views()
        self.canvas.mpl_connect("resize_event", lambda _e: self._layout_views())

    def _on_preview_configure(self, e):
        size = (e.width, e.height)
        if e.width < 2 or e.height < 2:
            return
        if self._preview_resize_job is not None:
            self.after_cancel(self._preview_resize_job)
            self._preview_resize_job = None
        if size == self._preview_size:
            return
        self._preview_pending = size
        self._preview_resize_job = self.after(150, self._resize_preview)

    def _resize_preview(self):
        """Resize the figure to the canvas and render it once, straight away."""
        if self._mouse_held():                        # still dragging the window edge
            self._preview_resize_job = self.after(50, self._resize_preview)
            return
        self._preview_resize_job = None
        size = self._preview_size = self._preview_pending
        tkc = self.canvas.get_tk_widget()
        self.canvas.resize(types.SimpleNamespace(width=size[0], height=size[1]))
        # resize() queued an idle draw after blanking the image; draw now instead,
        # so the blank image is never shown and the figure is rendered only once
        job = getattr(self.canvas, "_idle_draw_id", None)
        if job:
            tkc.after_cancel(job)
            self.canvas._idle_draw_id = None
        self.canvas.draw()

    def _build_data(self, tab):
        frame = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        frame.pack(fill="both", expand=True)
        frame.grid_columnconfigure((0, 1), weight=1)
        self.metric_labels = {}
        metrics = ["Wing area", "Wing MAC", "Root / tip chord", "Aircraft length",
                   "Aircraft height", "Tail arm", "Stabiliser area S_h", "Fin area S_v",
                   "Vh / Vv achieved", "Wing root room", "Tail root room", "Wing-tail gap"]
        for i, m in enumerate(metrics):
            c = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=10)
            c.grid(row=i // 2, column=i % 2, sticky="ew", padx=4, pady=4)
            ctk.CTkLabel(c, text=m, font=self.f_tiny, text_color=MUTED).pack(
                anchor="w", padx=10, pady=(6, 0))
            v = ctk.CTkLabel(c, text="–", font=self.f_head)
            v.pack(anchor="w", padx=10, pady=(0, 6))
            self.metric_labels[m] = v
        ctk.CTkLabel(frame, text="All derived values", font=self.f_label).grid(
            row=10, column=0, columnspan=2, sticky="w", padx=6, pady=(12, 2))
        self.derived_box = ctk.CTkTextbox(frame, height=520, font=self.f_mono, wrap="none")
        self.derived_box.grid(row=11, column=0, columnspan=2, sticky="ew", padx=4)

    # ------------------------------------------------------------------ pages
    # Only the visible page fills the page area; hidden pages are "parked": placed
    # far outside it at a fixed size. On Windows every Tk widget is a native window,
    # so the way hidden pages are kept decides what resizing and switching cost:
    #   * stacked under the visible page (same grid cell) they were resized, laid
    #     out and repainted with it on every resize, maximise and restore step;
    #   * unmapped, every switch re-shows and repaints hundreds of native windows.
    # Parked pages stay mapped (switching is a single move) but lie outside the
    # window, so they are never painted, and their fixed size means a window resize
    # does not touch them. A page that was parked at another size is laid out once,
    # when it is shown.
    # Pages are filled by generators that yield after every row / card, so the pages
    # the user has not opened yet can be built in small steps while the GUI is idle.
    PARK_X = -30000

    @staticmethod
    def _outer(page):
        return getattr(page, "_parent_frame", page)

    # Tk's own place(): CTk's override refuses width / height, and these are real
    # pixel sizes of the page area that must not be scaled again.
    def _park(self, outer):
        w = max(self.page_host.winfo_width(), 200)
        h = max(self.page_host.winfo_height(), 200)
        tk.Place.place_configure(outer, x=self.PARK_X, y=0, width=w, height=h,
                                 relwidth=0, relheight=0)

    def _unpark(self, outer):
        """Show a page at the current size of the page area. The size is absolute
        (not relwidth=1): while the window is being resized the page keeps its
        layout, and _fit_page re-lays it out once the size has settled."""
        w = max(self.page_host.winfo_width(), 200)
        h = max(self.page_host.winfo_height(), 200)
        tk.Place.place_configure(outer, x=0, y=0, width=w, height=h, relwidth=0, relheight=0)

    def show_page(self, key):
        if self.current == key:
            return
        self._finish_page(key)
        page = self.pages[key]
        prev, self.current = self.current, key
        self._unpark(self._outer(page))
        if prev in self.pages:
            self._park(self._outer(self.pages[prev]))
        self._apply_wrap_to(key)
        for k in (prev, key):
            if k in self.nav:
                self.nav[k].configure(fg_color=NAV_SEL if k == key else "transparent")
        self.page_title.configure(text=dict(PAGES)[key])
        self.page_sub.configure(text=PAGE_SUBTITLES[key])
        is_param = key in PARAM_PAGES
        if is_param != (prev in PARAM_PAGES) or prev is None:
            for w in (self.search, self.reset_page_btn):
                (w.grid if is_param else w.grid_remove)()
        if is_param and self.search.get():
            self.clear_filter()
        if key == "constraints":
            self._update_constraints_page()
        elif key == "sweep":
            self.refresh_sweep_page()

    def _create_page(self, key):
        # bg_color: the page area is a plain Tk frame (LiteFrame), from which CTk
        # cannot read the light / dark colour pair itself
        if key == "sweep":                 # holds its own scrolling table
            page = ctk.CTkFrame(self.page_host, fg_color="transparent",
                                bg_color=self._page_bg())
            page.grid_columnconfigure(0, weight=1)
            gen = self._run_once(self._fill_sweep_page, page)
        else:
            page = ctk.CTkScrollableFrame(self.page_host, fg_color="transparent",
                                          bg_color=self._page_bg())
            page.grid_columnconfigure(0, weight=1)
            if key in PARAM_PAGES:
                gen = self._fill_param_page(page, key)
            elif key == "constraints":
                gen = self._fill_constraints_page(page)
            else:
                gen = self._run_once(self._fill_build_page, page)
        self._park(self._outer(page))
        self.pages[key] = page
        self._pending[key] = gen

    @staticmethod
    def _run_once(fn, page):
        fn(page)
        yield

    def _finish_page(self, key):
        """Make sure the page exists and is completely built."""
        if key not in self.pages:
            self._create_page(key)
        gen = self._pending.pop(key, None)
        if gen is not None:
            for _ in gen:
                pass
            self._page_built(key)

    def _page_built(self, key):
        if key in PARAM_PAGES:
            self._page_wrap.pop(key, None)
            self._apply_wrap_to(key)
            for (sec, _), row in self.rows.items():
                if sec == key:
                    row.set_enabled(self.relevant(sec, row.name))
            self._apply_row_flags()
        elif key == "build":
            self._update_build_buttons(force=True)

    def _repark(self, key):
        """After a page was pre-built in the background: lay it out at the current
        page-area size, so showing it later needs no layout pass."""
        page = self.pages.get(key)
        if page is not None and key != self.current:
            self._park(self._outer(page))

    def _start_prebuild(self):
        self._prebuild = [k for k, _ in PAGES if k not in self.pages or k in self._pending]
        if self._prebuild_job is None:
            self._prebuild_job = self.after(400, self._prebuild_idle)

    def _prebuild_step(self):
        """Build one row / card of a page the user has not opened yet."""
        self._prebuild_job = None
        while self._prebuild:
            key = self._prebuild[0]
            if key not in self.pages:
                self._create_page(key)
            gen = self._pending.get(key)
            if gen is None:
                self._prebuild.pop(0)
                continue
            try:
                next(gen)
            except StopIteration:
                del self._pending[key]
                self._prebuild.pop(0)
                self._page_built(key)
                self._repark(key)
            break
        if self._prebuild:                 # idle-only: user input is always handled first
            self._prebuild_job = self.after(25, self._prebuild_idle)

    def _prebuild_idle(self):
        self._prebuild_job = self.after_idle(self._prebuild_step)

    @staticmethod
    def _mouse_held():
        """True while the primary mouse button is down, e.g. during a window-edge
        drag. Resize work is postponed until it is released: a pure timer is not
        enough, because one slow step would let it fire in the middle of the drag."""
        if sys.platform != "win32":
            return False
        try:
            u32 = ctypes.windll.user32
            vk = 0x02 if u32.GetSystemMetrics(23) else 0x01     # SM_SWAPBUTTON
            return bool(u32.GetAsyncKeyState(vk) & 0x8000)
        except Exception:
            return False

    def _on_host_resize(self, e):
        # Laying out a page (hundreds of native widgets) on every intermediate size
        # of a window drag is what made resizing stutter; only the final size matters.
        self._host_size = (e.width, e.height)
        if self._wrap_job is not None:
            self.after_cancel(self._wrap_job)
        self._wrap_job = self.after(60, self._fit_page)

    def _fit_page(self):
        """Resize the visible page to the page area, once the size has settled."""
        if self._mouse_held():
            self._wrap_job = self.after(50, self._fit_page)
            return
        self._wrap_job = None
        width_px, height_px = self._host_size
        page = self.pages.get(self.current)
        if page is not None:
            tk.Place.place_configure(self._outer(page), width=max(width_px, 200),
                                     height=max(height_px, 200))
        self._apply_wrap(width_px)
        if self._repark_job is not None:
            self.after_cancel(self._repark_job)
        self._repark_job = self.after(300, self._repark_next)

    def _repark_next(self):
        """After a window resize: bring one hidden page to the new page-area size
        (off screen, so it is laid out but not painted), then the next one a little
        later. Switching to a page then needs no layout pass, and the user is never
        blocked for more than one page at a time."""
        self._repark_job = None
        if self._mouse_held():
            self._repark_job = self.after(300, self._repark_next)
            return
        w = max(self.page_host.winfo_width(), 200)
        h = max(self.page_host.winfo_height(), 200)
        for key, page in self.pages.items():
            if key == self.current or key in self._pending:
                continue
            outer = self._outer(page)
            info = outer.place_info()
            if int(info.get("width") or 0) != w or int(info.get("height") or 0) != h:
                self._park(outer)
                self._apply_wrap_to(key)
                self._repark_job = self.after(120, self._repark_next)
                return

    def _apply_wrap(self, width_px):
        """Let every description use the free width beside its controls."""
        s = ctk.ScalingTracker.get_widget_scaling(self)
        # row width minus dot, slider + entry + unit, Iterate and reset (~460 px)
        wrap = max(200, int(width_px / s) - 460)
        if abs(wrap - self.desc_wrap) > 8:
            self.desc_wrap = wrap
        self._apply_wrap_to(self.current)

    def _apply_wrap_to(self, key):
        """Re-wrap one page's descriptions - only the visible page; hidden pages
        catch up when they are shown, instead of every page on every resize."""
        if key not in PARAM_PAGES or key in self._pending \
                or self._page_wrap.get(key) == self.desc_wrap:
            return
        self._page_wrap[key] = self.desc_wrap
        for (sec, _), row in self.rows.items():
            if sec == key:
                row.desc.style(wraplength=self.desc_wrap)

    def _fill_param_page(self, page, sec):
        block = self.raw[sec]
        listed = [n for _, names in GROUPS[sec] for n in names]
        groups = [(g, [n for n in names if n in block]) for g, names in GROUPS[sec]]
        extra = [n for n in block if n not in listed]
        if extra:
            groups.append(("Other", extra))
        page.cards = []
        for gi, (title, names) in enumerate(groups):
            if not names:
                continue
            card = ctk.CTkFrame(page, fg_color=CARD, corner_radius=12)
            card.grid(row=gi, column=0, sticky="ew", pady=(0, 12), padx=(0, 6))
            card.grid_columnconfigure(0, weight=1)
            LiteLabel(card, text=title.upper(), font=self.f_tiny, fg=ACCENT,
                      anchor="w").grid(row=0, column=0, sticky="w", padx=16, pady=(12, 2))
            rows = []
            page.cards.append((card, rows))
            for ri, name in enumerate(names):
                row = ParamRow(self, card, sec, name, ri + 1)
                self.rows[(sec, name)] = row
                rows.append(row)
                yield
            ctk.CTkFrame(card, height=6, fg_color="transparent").grid(row=len(names) + 1, column=0)

    def _fill_constraints_page(self, page):
        self.cons_widgets = []
        for i, c in enumerate(self.raw.get("constraints", [])):
            if i:
                yield
            card = ctk.CTkFrame(page, fg_color=CARD, corner_radius=10)
            card.grid(row=i, column=0, sticky="ew", pady=(0, 8), padx=(0, 6))
            card.grid_columnconfigure(1, weight=1)
            pill = ctk.CTkLabel(card, text="…", width=52, height=24, corner_radius=6,
                                fg_color=MUTED, text_color="white", font=self.f_tiny)
            pill.grid(row=0, column=0, rowspan=3, padx=12, pady=10)
            LiteLabel(card, text=c["name"].replace("_", " ").capitalize(),
                      font=self.f_label, fg=text_fg(), anchor="w").grid(
                row=0, column=1, sticky="w", pady=(10, 0))
            LiteLabel(card, text=c.get("description", ""), font=self.f_small, anchor="w",
                      justify="left", wraplength=420).grid(row=1, column=1, sticky="w",
                                                           pady=(3, 0))
            try:
                formula = tex_label(card, f"${expr_to_tex(c['expr'])}$", 12.5)
            except Exception:                       # unusual expression: show it as text
                formula = LiteLabel(card, text=c["expr"], font=self.f_mono, anchor="w",
                                    justify="left", wraplength=420)
            formula.grid(row=2, column=1, sticky="w", pady=(5, 10))
            Tooltip(formula, lambda e=c["expr"]: f"Expression in the configuration file:\n{e}")
            val = LiteLabel(card, text="", font=self.f_mono, fg=text_fg(), width=10,
                            anchor="e")
            val.grid(row=0, column=2, rowspan=3, padx=(8, 4))
            LiteLabel(card, text=f"[{c['min']:g} … {c['max']:g}]", font=self.f_small,
                      width=14).grid(row=0, column=3, rowspan=3, padx=(0, 12))
            self.cons_widgets.append((c, pill, val))
        hint = LiteLabel(page, text="Limits of the rules are defined in the 'constraints' "
                                    "list of the configuration file.",
                         font=self.f_small, bg=self._page_bg())
        hint.grid(row=999, column=0, sticky="w", pady=(4, 8))

    # ---------------------------------------------------------------- sweep page
    def _ghost_button(self, parent, text, cmd, width=None):
        kw = {"width": width} if width else {}
        return ctk.CTkButton(parent, text=text, height=32, fg_color="transparent",
                             border_width=1, text_color=("gray10", "gray90"),
                             command=cmd, **kw)

    def _fill_sweep_page(self, page):
        page.grid_rowconfigure(2, weight=1)

        # 1. parameters to iterate --------------------------------------------
        c1 = ctk.CTkFrame(page, fg_color=CARD, corner_radius=12)
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 10), padx=(0, 6))
        c1.grid_columnconfigure(0, weight=1)
        self.sw_vars_title = ctk.CTkLabel(c1, text="PARAMETERS TO ITERATE", font=self.f_tiny,
                                          text_color=ACCENT)
        self.sw_vars_title.grid(row=0, column=0, sticky="w", padx=16, pady=(10, 2))
        self._ghost_button(c1, "Clear all", self.clear_sweep_vars, 84).grid(
            row=0, column=1, padx=12, pady=(8, 0))
        self.sw_vars_box = ctk.CTkScrollableFrame(c1, height=64, fg_color="transparent")
        self.sw_vars_box._scrollbar.configure(height=0)   # its default 200 px sets the height
        self.sw_vars_box.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        self.sw_vars_box.grid_columnconfigure(2, weight=1)

        # 2. sampling ------------------------------------------------------------
        c2 = ctk.CTkFrame(page, fg_color=CARD, corner_radius=12)
        c2.grid(row=1, column=0, sticky="ew", pady=(0, 10), padx=(0, 6))
        c2.grid_columnconfigure(5, weight=1)
        ctk.CTkLabel(c2, text="SAMPLING", font=self.f_tiny, text_color=ACCENT).grid(
            row=0, column=0, columnspan=6, sticky="w", padx=16, pady=(10, 4))
        self.sw_mode = ctk.CTkSegmentedButton(c2, values=list(S.MODES.values()),
                                              command=self._sweep_mode_changed)
        self.sw_mode.grid(row=1, column=0, columnspan=3, sticky="w", padx=16)
        self.sw_mode_hint = ctk.CTkLabel(c2, text="", font=self.f_small, text_color=MUTED,
                                         anchor="w", justify="left", wraplength=330)
        self.sw_mode_hint.grid(row=1, column=3, columnspan=3, sticky="w", padx=(12, 12))
        rnd = self.sw_random_box = ctk.CTkFrame(c2, fg_color="transparent")
        rnd.grid(row=2, column=0, columnspan=6, sticky="w", padx=12, pady=(8, 0))
        ctk.CTkLabel(rnd, text="Designs", font=self.f_small).pack(side="left", padx=(4, 4))
        self.sw_samples = ctk.CTkEntry(rnd, width=60, height=28, justify="right",
                                       font=self.f_mono)
        self.sw_samples.pack(side="left")
        ctk.CTkLabel(rnd, text="Seed", font=self.f_small).pack(side="left", padx=(14, 4))
        self.sw_seed = ctk.CTkEntry(rnd, width=60, height=28, justify="right", font=self.f_mono)
        self.sw_seed.pack(side="left")
        for e, lim in ((self.sw_samples, 4), (self.sw_seed, 9)):
            e.configure(validate="key", validatecommand=(
                e.register(lambda P, n=lim: re.fullmatch(rf"\d{{0,{n}}}", P) is not None), "%P"))
            e.bind("<Return>", lambda _e: self._commit_sweep_settings())
            e.bind("<FocusOut>", lambda _e: self._commit_sweep_settings())
        self.sw_fill = tk.BooleanVar(value=True)
        ctk.CTkSwitch(rnd, text="Keep sampling until this many valid designs are found",
                      variable=self.sw_fill, font=self.f_small,
                      command=self._commit_sweep_settings).pack(side="left", padx=(16, 0))
        act = ctk.CTkFrame(c2, fg_color="transparent")
        act.grid(row=3, column=0, columnspan=6, sticky="ew", padx=12, pady=(10, 12))
        act.grid_columnconfigure(2, weight=1)
        self.sw_gen_btn = ctk.CTkButton(act, text="Generate designs", height=34, width=150,
                                        font=self.f_label, command=self.generate_designs)
        self.sw_gen_btn.grid(row=0, column=0, padx=4)
        self.sw_stop_btn = self._ghost_button(act, "Stop", self.sweep_stop.set, 64)
        self.sw_stop_btn.grid(row=0, column=1, padx=4)
        self.sw_count_lbl = ctk.CTkLabel(act, text="", font=self.f_small, anchor="w",
                                         justify="left", wraplength=420)
        self.sw_count_lbl.grid(row=0, column=2, sticky="w", padx=10)
        self.sw_progress = ctk.CTkProgressBar(act, height=6)
        self.sw_progress.grid(row=1, column=0, columnspan=3, sticky="ew", padx=4, pady=(8, 0))
        self.sw_progress.set(0)

        # 3. results -------------------------------------------------------------
        c3 = ctk.CTkFrame(page, fg_color=CARD, corner_radius=12)
        c3.grid(row=2, column=0, sticky="nsew", padx=(0, 6))
        c3.grid_columnconfigure(0, weight=1)
        c3.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(c3, text="GENERATED DESIGNS", font=self.f_tiny, text_color=ACCENT).grid(
            row=0, column=0, sticky="w", padx=16, pady=(10, 0))
        self.sw_view = ctk.CTkSegmentedButton(c3, values=["Designs", "Build log"],
                                              command=self._sweep_view)
        self.sw_view.set("Designs")
        self.sw_view.grid(row=0, column=1, padx=12, pady=(8, 0))
        self.sw_summary = ctk.CTkLabel(c3, text="Nothing generated yet.", font=self.f_small,
                                       text_color=MUTED, anchor="w")
        self.sw_summary.grid(row=1, column=0, columnspan=2, sticky="w", padx=16)

        self.sw_table = ctk.CTkFrame(c3, fg_color="transparent")
        self.sw_table.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=12, pady=(4, 0))
        self.sw_table.grid_columnconfigure(0, weight=1)
        self.sw_table.grid_rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(self.sw_table, style="Sweep.Treeview", show="headings",
                                 selectmode="browse", height=8)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys = ctk.CTkScrollbar(self.sw_table, command=self.tree.yview)
        ys.grid(row=0, column=1, sticky="ns")
        xs = ctk.CTkScrollbar(self.sw_table, orientation="horizontal",
                              command=self.tree.xview)
        xs.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_design_select)
        self.tree.bind("<Double-1>", lambda _e: self.load_selected_design())
        self._style_tree()
        self.sw_log = ctk.CTkTextbox(c3, font=self.f_mono, wrap="none")

        self.sw_detail = ctk.CTkLabel(c3, text="Select a design to preview it on the right.",
                                      font=self.f_small, text_color=MUTED, anchor="w",
                                      justify="left", wraplength=640)
        self.sw_detail.grid(row=3, column=0, columnspan=2, sticky="w", padx=16, pady=(6, 0))
        btns = ctk.CTkFrame(c3, fg_color="transparent")
        btns.grid(row=4, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 12))
        self.sw_load_btn = self._ghost_button(btns, "Load into editor",
                                              self.load_selected_design)
        self.sw_load_btn.pack(side="left", padx=4)
        self.sw_export_btn = self._ghost_button(btns, "Export valid designs…",
                                                self.export_designs)
        self.sw_export_btn.pack(side="left", padx=4)
        self.sw_build_btn = ctk.CTkButton(btns, text="Build valid designs in SolidWorks…",
                                          height=32, command=self.build_designs)
        self.sw_build_btn.pack(side="left", padx=4)
        self.sw_png = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(btns, text="Preview images", variable=self.sw_png, font=self.f_small,
                        checkbox_width=18, checkbox_height=18).pack(side="left", padx=10)
        self.sw_stale = ctk.CTkLabel(c3, text="", font=self.f_small, text_color=WARN,
                                     anchor="w", justify="left", wraplength=640)
        self.refresh_sweep_page()

    def _style_tree(self):
        dark = ctk.get_appearance_mode() == "Dark"
        st = ttk.Style(self)
        if st.theme_use() != "clam":
            st.theme_use("clam")
        bg, fg = ("#242424", "#e5e7eb") if dark else ("#ffffff", "#1f2937")
        head = "#333333" if dark else "#e5e7eb"
        sel = "#1d4ed8" if dark else "#bfdbfe"
        rh = int(24 * ctk.ScalingTracker.get_widget_scaling(self))
        st.configure("Sweep.Treeview", background=bg, fieldbackground=bg, foreground=fg,
                     rowheight=rh, borderwidth=0, font=("Segoe UI", 10))
        st.configure("Sweep.Treeview.Heading", background=head, foreground=fg,
                     relief="flat", font=("Segoe UI", 10, "bold"), padding=(4, 3))
        st.map("Sweep.Treeview", background=[("selected", sel)],
               foreground=[("selected", "#ffffff" if dark else "#111827")])
        st.map("Sweep.Treeview.Heading", background=[("active", head)])
        if hasattr(self, "tree"):
            self.tree.tag_configure("bad", foreground="#f87171" if dark else "#b91c1c")
            self.tree.tag_configure("ok", foreground=fg)

    def _sweep_view(self, which):
        if which == "Designs":
            self.sw_log.grid_remove()
            self.sw_table.grid()
        else:
            self.sw_table.grid_remove()
            self.sw_log.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=12, pady=(4, 0))

    # ---- sweep definition ---------------------------------------------------------
    def sweep_block(self) -> dict:
        sw = self.raw.setdefault("sweep", {})
        sw.setdefault("mode", "grid")
        sw.setdefault("samples", 20)
        sw.setdefault("seed", 1)
        sw.setdefault("fill_valid", True)
        sw.setdefault("variables", [])
        return sw

    def find_sweep_var(self, sec, name):
        return next((v for v in self.sweep_block()["variables"]
                     if v.get("param") == f"{sec}.{name}"), None)

    def add_sweep_var(self, var):
        self.sweep_block()["variables"].append(var)
        self._after_sweep_var(var["param"])

    def remove_sweep_var(self, sec, name):
        sw = self.sweep_block()
        sw["variables"] = [v for v in sw["variables"] if v.get("param") != f"{sec}.{name}"]
        self._after_sweep_var(f"{sec}.{name}")

    def clear_sweep_vars(self):
        params = [v.get("param", "") for v in self.sweep_block()["variables"]]
        self.sweep_block()["variables"] = []
        for p in params:
            self._after_sweep_var(p, quiet=True)
        self.sweep_changed()

    def _after_sweep_var(self, param, quiet=False):
        sec, _, name = param.partition(".")
        row = self.rows.get((sec, name))
        if row is not None:
            row.refresh_iter()
        if not quiet:
            self.sweep_changed()

    def sweep_changed(self):
        self.set_dirty(True)
        self.refresh_sweep_page()
        self.update_badges()

    def _sweep_mode_changed(self, label):
        self.sweep_block()["mode"] = {v: k for k, v in S.MODES.items()}[label]
        self.sweep_changed()

    def _commit_sweep_settings(self):
        sw = self.sweep_block()
        old = (sw["samples"], sw["seed"], sw["fill_valid"])
        for key, e, lo, hi in (("samples", self.sw_samples, 1, S.MAX_DESIGNS),
                               ("seed", self.sw_seed, 0, 10 ** 9)):
            try:
                v = int(e.get())
            except ValueError:
                v = sw[key]
            c = min(max(v, lo), hi)
            if c != v:
                self.flash(f"Random {key} must be {lo} … {hi:,} - set to {c}.", "warn")
            sw[key] = c
        sw["fill_valid"] = bool(self.sw_fill.get())
        if (sw["samples"], sw["seed"], sw["fill_valid"]) != old:
            self.sweep_changed()
        else:
            self.refresh_sweep_page()

    def refresh_sweep_page(self):
        if not hasattr(self, "sw_vars_box") or not self.sw_vars_box.winfo_exists():
            return
        sw = self.sweep_block()
        variables, settings, errors = S.parse_sweep(self.raw)
        items = sw["variables"]
        running = self.sweep_thread is not None and self.sweep_thread.is_alive()
        # Rebuild only when something shown here changed - not on every visit.
        # The signature is stored on the list widget, so a reloaded page rebuilds.
        relevance = tuple(self.relevant(*p.partition(".")[::2])
                          if p.partition(".")[0] in self.raw
                          and p.partition(".")[2] in self.raw[p.partition(".")[0]] else None
                          for p in (it.get("param", "?") for it in items))
        sig = (json.dumps(sw, sort_keys=True, default=str), tuple(errors), running, relevance)
        if getattr(self.sw_vars_box, "_sig", None) == sig:
            self._update_sweep_buttons()
            return
        self.sw_vars_box._sig = sig
        # selected parameters list
        for w in self.sw_vars_box.winfo_children():
            w.destroy()
        self.sw_vars_title.configure(text=f"PARAMETERS TO ITERATE  ({len(items)})")
        if not items:
            ctk.CTkLabel(self.sw_vars_box, text="No parameters selected yet. Press 'Iterate' "
                         "next to any parameter on the Wing, Fuselage, Tail or Placement page "
                         "and set its min, max and number of iterations. Every other parameter "
                         "stays fixed at its current value.", font=self.f_small,
                         text_color=MUTED, wraplength=620, justify="left").grid(
                row=0, column=0, columnspan=4, sticky="w", padx=6, pady=8)
        by_key = {v.label: v for v in variables}
        sweeps_cfg = any(v.get("param") == "tail.configuration" for v in items)
        for i, item in enumerate(items):
            param = item.get("param", "?")
            sec, _, name = param.partition(".")
            ctk.CTkLabel(self.sw_vars_box, text=dict(PAGES).get(sec, sec), width=74,
                         height=22, corner_radius=6, fg_color=NAV_SEL,
                         font=self.f_tiny).grid(row=i, column=0, padx=(4, 8), pady=2)
            v = by_key.get(param)
            if v is None:
                desc, col = "invalid - see the message below", ERR
            elif v.kind == "number":
                lv = v.levels()
                unit = UNIT_TEXT.get(self.raw[sec][name].get("unit", "-"), "")
                desc = (f"{fmt(item['min'])} … {fmt(item['max'])} {unit}   ·   "
                        f"{len(lv)} values")
                col = ("gray10", "gray90")
            else:
                shown = [TAIL_CHOICES.get(x, x) if name == "configuration" else x
                         for x in v.values]
                desc, col = ", ".join(shown), ("gray10", "gray90")
            if (sec in self.raw and name in self.raw[sec] and not self.relevant(sec, name)
                    and not (sec == "tail" and sweeps_cfg)):
                desc += "   (not used by the current settings - has no effect)"
                col = WARN
            ctk.CTkLabel(self.sw_vars_box, text=f"{pretty(name)}", font=self.f_label,
                         anchor="w").grid(row=i, column=1, sticky="w")
            ctk.CTkLabel(self.sw_vars_box, text=desc, font=self.f_small, text_color=col,
                         anchor="w").grid(row=i, column=2, sticky="w", padx=8)
            ctk.CTkButton(self.sw_vars_box, text="Edit", width=46, height=22,
                          font=self.f_tiny, fg_color="transparent", border_width=1,
                          text_color=("gray10", "gray90"),
                          command=lambda s=sec, n=name: self.focus_row(s, n)).grid(
                row=i, column=3, padx=2)
            ctk.CTkButton(self.sw_vars_box, text="✕", width=24, height=22,
                          fg_color="transparent", hover_color=NAV_SEL, text_color=MUTED,
                          command=lambda s=sec, n=name: self.remove_sweep_var(s, n)).grid(
                row=i, column=4, padx=(2, 4))
        # sampling settings
        self.sw_mode.set(S.MODES.get(sw["mode"], S.MODES["grid"]))
        for e, key in ((self.sw_samples, "samples"), (self.sw_seed, "seed")):
            if self.focus_get() is not e._entry:
                e.delete(0, "end")
                e.insert(0, str(sw[key]))
        self.sw_fill.set(bool(sw["fill_valid"]))
        hints = {"grid": "Every combination of the iterated values "
                         "(design count = product of the iterations).",
                 "linked": "All iterated parameters step together from min to max; "
                           "design k uses the k-th value of each.",
                 "random": "Latin hypercube: values spread evenly over each range in "
                           "random combinations (iterations are not used)."}
        self.sw_mode_hint.configure(text=hints.get(sw["mode"], ""))
        if sw["mode"] == "random":
            self.sw_random_box.grid()
        else:
            self.sw_random_box.grid_remove()
        # design count / problems
        if errors:
            self.sw_count_lbl.configure(text="✗  " + "\n✗  ".join(errors), text_color=ERR)
        elif not variables:
            self.sw_count_lbl.configure(text="Select at least one parameter to iterate.",
                                        text_color=MUTED)
        else:
            n = S.design_count(variables, settings["mode"], settings["samples"])
            if settings["mode"] == "grid" and len(variables) > 1:
                formula = " × ".join(str(len(v.levels())) for v in variables) + f" = {n:,}"
            else:
                formula = f"{n:,}"
            extra = (" valid" if settings["mode"] == "random" and settings["fill_valid"]
                     else "")
            self.sw_count_lbl.configure(
                text=f"{formula}{extra} designs will be generated and each one checked "
                     "against every limit and constraint.",
                text_color=("gray10", "gray90"))
        self.sw_gen_btn.configure(state="disabled" if (errors or not variables or running)
                                  else "normal")
        self.sw_stop_btn.configure(state="normal" if running else "disabled")
        self._update_sweep_buttons()

    def focus_row(self, sec, name):
        """Open the parameter's page and scroll its row into view."""
        self.show_page(sec)
        row = self.rows.get((sec, name))
        page = self.pages.get(sec)
        if row is None or page is None:
            return
        if self.search.get():
            self.clear_filter()
        self.update_idletasks()
        inner = row.frame.winfo_rooty() - page.winfo_rooty()
        total = max(page.winfo_height(), 1)
        page._parent_canvas.yview_moveto(max(0.0, (inner - 40) / total))
        if row.iter_panel is not None:
            row.iter_panel.configure(fg_color=ACCENT)
            self.after(450, lambda: row.iter_panel is not None
                       and row.iter_panel.configure(fg_color=ITER_BG))

    # ---- generation ---------------------------------------------------------------
    def generate_designs(self):
        self._commit_sweep_settings()
        variables, settings, errors = S.parse_sweep(self.raw)
        if errors or not variables:
            self.flash("Fix the sweep definition first.", "warn")
            return
        if self.sweep_thread is not None and self.sweep_thread.is_alive():
            return
        self.clear_design_preview()
        self.sweep_base = S.base_of(self.raw)
        self.sweep_base_key = json.dumps(self.sweep_base, sort_keys=True)
        self.sweep_used, self.sweep_settings = variables, settings
        self.sweep_designs = []
        self.sweep_stop.clear()
        cols = (["#", "status"] + [v.label for v in variables]
                + [m for m, _, _ in S.METRICS] + ["reason"])
        heads = {"#": "#", "status": "Status", "reason": "Reason"}
        for v in variables:
            unit = UNIT_TEXT.get(self.raw[v.sec][v.name].get("unit", "-"), "")
            heads[v.label] = pretty(v.name) + (f" [{unit}]" if unit else "")
        heads.update({"wing_area_mm2": "Wing area [m²]", "wing_mac_mm": "MAC [mm]",
                      "tail_arm_mm": "Tail arm [mm]", "tail_s_h_mm2": "Stab. area [m²]",
                      "tail_s_v_mm2": "Fin area [m²]", "aircraft_length_mm": "Length [mm]"})
        self.tree.delete(*self.tree.get_children())
        self.tree.configure(columns=cols)
        for c in cols:
            w = {"#": 46, "status": 78, "reason": 520}.get(
                c, max(96, 8 * len(heads.get(c, c)) + 16))
            self.tree.heading(c, text=heads.get(c, c), anchor="w" if c == "reason" else "e")
            self.tree.column(c, width=w, minwidth=40, stretch=c == "reason",
                             anchor="w" if c in ("reason", "status") else "e")
        self.sw_view.set("Designs")
        self._sweep_view("Designs")
        q, base, stop = self.sweep_q, self.sweep_base, self.sweep_stop

        def work():
            try:
                S.run(base, variables, settings,
                      callback=lambda d, done, total: q.put((d, done, total)),
                      stop=stop.is_set)
            except Exception as exc:                       # pragma: no cover
                q.put(exc)
            q.put(None)

        self.sweep_thread = threading.Thread(target=work, daemon=True)
        self.sweep_thread.start()
        self.sw_summary.configure(text="Generating…", text_color=MUTED)
        self.refresh_sweep_page()
        self.after(40, self._poll_sweep)

    def _design_row(self, d):
        vals = [d.index, "valid" if d.ok else "rejected"]
        for v in self.sweep_used:
            x = d.values[v.key]
            vals.append(TAIL_CHOICES.get(x, x) if v.name == "configuration"
                        else fmt(x) if not isinstance(x, str) else x)
        for m, _, f in S.METRICS:
            x = d.metrics.get(m)
            vals.append("" if x is None else f"{x * f:.4g}" if f != 1 else f"{x:.0f}")
        vals.append("" if d.ok else d.issues[0])
        return vals

    def _poll_sweep(self):
        done_flag = False
        for _ in range(400):
            try:
                item = self.sweep_q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                done_flag = True
                break
            if isinstance(item, Exception):
                self.flash(f"Sweep failed: {item}", "warn")
                continue
            d, done, total = item
            self.sweep_designs.append(d)
            self.tree.insert("", "end", iid=str(d.index), values=self._design_row(d),
                             tags=("ok" if d.ok else "bad",))
            self.sw_progress.set(min(done / max(total, 1), 1))
        n_ok = sum(d.ok for d in self.sweep_designs)
        n = len(self.sweep_designs)
        if not done_flag:
            self.sw_summary.configure(text=f"Generating…  {n} checked, {n_ok} valid")
            self.after(40, self._poll_sweep)
            return
        self.sw_progress.set(1)
        stopped = self.sweep_stop.is_set()
        s = self.sweep_settings
        msg = (f"{n_ok} of {n} designs satisfy every parameter limit and cross-component "
               f"constraint" + (f" · {n - n_ok} rejected" if n - n_ok else ""))
        if stopped:
            msg += " · stopped early"
        elif s["mode"] == "random" and s["fill_valid"] and n_ok < s["samples"]:
            msg += (f" · only {n_ok} of the {s['samples']} requested valid designs were found "
                    f"in {n} attempts - narrow the ranges")
        self.sw_summary.configure(text=msg, text_color=OK if n_ok else ERR)
        self.flash(f"Design sweep finished: {n_ok} valid design{'s' * (n_ok != 1)}.",
                   "ok" if n_ok else "warn")
        self.refresh_sweep_page()

    def _update_sweep_buttons(self):
        if not hasattr(self, "sw_build_btn"):
            return
        running = self.sweep_thread is not None and self.sweep_thread.is_alive()
        busy = self.proc is not None and self.proc.poll() is None
        has_valid = any(d.ok for d in self.sweep_designs) and not running
        self.sw_export_btn.configure(state="normal" if has_valid else "disabled")
        btn_fg = ctk.ThemeManager.theme["CTkButton"]["fg_color"]
        ok = has_valid and not busy
        self.sw_build_btn.configure(state="normal" if ok else "disabled",
                                    fg_color=btn_fg if ok else DISABLED)
        self.sw_load_btn.configure(state="normal" if self.tree.selection() and not running
                                   else "disabled")
        stale = (self.sweep_base_key is not None and self.raw is not None
                 and json.dumps(S.base_of(self.raw), sort_keys=True) != self.sweep_base_key)
        if stale and self.sweep_designs:
            self.sw_stale.configure(text="⚠  Parameters have changed since these designs were "
                                         "generated. Export and build use the parameters of "
                                         "the generation - press 'Generate designs' to update.")
            self.sw_stale.grid(row=5, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 10))
        else:
            self.sw_stale.grid_remove()

    # ---- using the results ----------------------------------------------------------
    def selected_design(self):
        sel = self.tree.selection() if hasattr(self, "tree") else ()
        if not sel:
            return None
        idx = int(sel[0])
        return next((d for d in self.sweep_designs if d.index == idx), None)

    def _on_design_select(self, _=None):
        d = self.selected_design()
        self._update_sweep_buttons()
        if d is None:
            return
        vals = ",  ".join(f"{pretty(n)} = {TAIL_CHOICES.get(x, x) if n == 'configuration' else fmt(x) if not isinstance(x, str) else x}"
                          for (_, n), x in d.values.items())
        if d.ok:
            self.sw_detail.configure(text=f"Design {d.index}: valid  -  {vals}",
                                     text_color=OK)
        else:
            self.sw_detail.configure(text=f"Design {d.index}: rejected  -  {vals}\n• "
                                          + "\n• ".join(d.issues[:3]), text_color=ERR)
        _, ac, _ = S.analyse(S.design_raw(self.sweep_base, d.values))
        if ac is not None:
            self.preview_design = (d.index, ac, d.ok)
            if self.tabs.get() == "Preview":
                self._draw_current_preview()
            else:
                self.show_tab("Preview")

    def clear_design_preview(self):
        if self.preview_design is None:
            return
        self.preview_design = None
        self._draw_current_preview()

    def _draw_current_preview(self):
        """Draw the preview only if it is visible and shows something new."""
        if self.preview_design is not None:
            idx, ac, ok = self.preview_design
            caption = (f"Sweep design {idx}  ({'valid' if ok else 'rejected'})",
                       OK if ok else ERR, True)
        else:
            ac = self.current_ac
            caption = ("Current configuration", MUTED, False)
            if ac is None and self._preview_key is not None:
                caption = ("Previous design - the current settings cannot be drawn",
                           WARN, False)
        if caption != self._caption_state:
            self._caption_state = caption
            self.preview_caption.configure(text=caption[0], text_color=caption[1])
            if caption[2]:
                self.preview_back.pack(side="right", padx=4)
            else:
                self.preview_back.pack_forget()
        if ac is None or self.dragging or self.tabs.get() != "Preview":
            return
        key = (ac, ctk.get_appearance_mode())
        if self._preview_key is not None and self._preview_key[0] is ac \
                and self._preview_key[1:] == key[1:]:
            return
        self._preview_key = key
        self.draw_preview(ac)

    def load_selected_design(self):
        d = self.selected_design()
        if d is None:
            return
        raw = S.design_raw(self.sweep_base, d.values)
        for sec in A.SECTIONS:
            for name, spec in raw[sec].items():
                self.raw[sec][name]["value"] = copy.deepcopy(spec["value"])
                row = self.rows.get((sec, name))
                if row is not None:
                    row.show_value(spec["value"])
        self.preview_design = None
        self.set_dirty(True)
        self.refresh_relevance()
        self.run_analysis()
        self.flash(f"Design {d.index} loaded into the editor (not saved yet).", "ok")

    def _pick_folder(self, title):
        folder = filedialog.askdirectory(title=title, initialdir=os.path.join(HERE),
                                         mustexist=False)
        if not folder:
            return None
        folder = os.path.normpath(folder)
        old = glob.glob(os.path.join(folder, "design_*.json"))
        if old and not messagebox.askyesno(
                APP_NAME, f"{folder} already contains {len(old)} design files.\n"
                          "They will be replaced. Continue?"):
            return None
        return folder

    def _write_designs(self, folder):
        valid = [d for d in self.sweep_designs if d.ok]
        self.sw_progress.set(0)
        prog = (lambda k, n: (self.sw_progress.set(k / n), self.update_idletasks())) \
            if self.sw_png.get() else None
        try:
            files, csv_path = S.write_designs(self.sweep_base, self.sweep_designs, folder,
                                              previews=self.sw_png.get(), progress=prog)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not write the designs:\n{exc}")
            return None
        self.sw_progress.set(1)
        return files, csv_path

    def export_designs(self):
        if not any(d.ok for d in self.sweep_designs):
            return
        folder = self._pick_folder("Folder for the design files")
        if not folder:
            return
        out = self._write_designs(folder)
        if out:
            files, csv_path = out
            self.flash(f"{len(files)} valid designs + {os.path.basename(csv_path)} written to "
                       f"{folder}", "ok")

    def build_designs(self):
        valid = [d for d in self.sweep_designs if d.ok]
        if not valid or (self.proc is not None and self.proc.poll() is None):
            return
        folder = self._pick_folder("Folder for the design files and SolidWorks parts")
        if not folder:
            return
        if not messagebox.askyesno(
                APP_NAME, f"Build {len(valid)} aircraft in SolidWorks?\n\nEach design is built "
                          f"in a new part, saved as design_###.SLDPRT in\n{folder}\nand closed "
                          "again. Only designs that pass every rule are built."):
            return
        if not self._write_designs(folder):
            return
        cmd = [sys.executable, "-u", os.path.join(HERE, "aircraft_sweep.py"), "build", folder]
        self.sw_view.set("Build log")
        self._sweep_view("Build log")
        self._start_process(cmd, self.sw_log, self.sw_progress, "sweep",
                            f"Building {len(valid)} designs in SolidWorks…")

    def _fill_build_page(self, page):
        c1 = ctk.CTkFrame(page, fg_color=CARD, corner_radius=12)
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 12), padx=(0, 6))
        c1.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(c1, text="SOLIDWORKS", font=self.f_tiny, text_color=ACCENT).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(10, 4))
        self.new_part_var = tk.BooleanVar(value=False)
        ctk.CTkSwitch(c1, text="Always create a new part (otherwise the aircraft in the "
                               "active part is rebuilt)", variable=self.new_part_var,
                      font=self.f_body).grid(row=1, column=0, columnspan=3, sticky="w",
                                             padx=16, pady=6)
        ctk.CTkLabel(c1, text="Save part as", font=self.f_body).grid(row=2, column=0,
                                                                     sticky="w", padx=16)
        self.save_part_var = tk.StringVar()
        ctk.CTkEntry(c1, textvariable=self.save_part_var,
                     placeholder_text="optional .SLDPRT path").grid(row=2, column=1, sticky="ew",
                                                                    padx=8, pady=6)
        ctk.CTkButton(c1, text="Browse…", width=80, command=self._browse_part).grid(
            row=2, column=2, padx=(0, 16))
        btns = ctk.CTkFrame(c1, fg_color="transparent")
        btns.grid(row=3, column=0, columnspan=3, sticky="ew", padx=12, pady=(10, 14))
        self.build_btn = ctk.CTkButton(btns, text="Build aircraft in SolidWorks", height=40,
                                       font=self.f_label, command=self.build)
        self.build_btn.pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Export preview image…", height=40, fg_color="transparent",
                      border_width=1, text_color=("gray10", "gray90"),
                      command=self.export_image).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Export report…", height=40, fg_color="transparent",
                      border_width=1, text_color=("gray10", "gray90"),
                      command=self.export_report).pack(side="left", padx=4)

        c2 = ctk.CTkFrame(page, fg_color=CARD, corner_radius=12)
        c2.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        c2.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(c2, text="BUILD LOG", font=self.f_tiny, text_color=ACCENT).grid(
            row=0, column=0, sticky="w", padx=16, pady=(10, 4))
        self.progress = ctk.CTkProgressBar(c2, mode="determinate", height=6)
        self.progress.grid(row=1, column=0, sticky="ew", padx=16)
        self.progress.set(0)
        self.log = ctk.CTkTextbox(c2, height=380, font=self.f_mono, wrap="none")
        self.log.grid(row=2, column=0, sticky="ew", padx=12, pady=(8, 14))
        self.log.insert("end", "Press 'Build' to generate the aircraft. Output appears here.\n")

    # ---------------------------------------------------------------- model I/O
    def load(self, path):
        try:
            with open(path) as fh:
                raw = json.load(fh)
            for sec in A.SECTIONS:
                if not isinstance(raw.get(sec), dict):
                    raise ValueError(f"section '{sec}' missing")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open {path}:\n{exc}")
            if self.raw is None:
                raise SystemExit(1)
            return
        if self.sweep_thread is not None and self.sweep_thread.is_alive():
            self.sweep_stop.set()
            self.sweep_thread.join(timeout=5)
        self.raw, self.path = raw, os.path.abspath(path)
        self.sweep_block()
        warm_formulas(all_formula_texts(raw))
        self.file_values = {(s, n): copy.deepcopy(spec["value"])
                            for s in A.SECTIONS for n, spec in raw[s].items()}
        if self._prebuild_job is not None:
            self.after_cancel(self._prebuild_job)
            self._prebuild_job = None
        for page in self.pages.values():
            self._outer(page).destroy()
        self.pages, self.rows, self.current = {}, {}, None
        self._pending, self._prebuild, self._page_wrap = {}, [], {}
        self.cons_widgets, self.build_btn = [], None
        self.__dict__.pop("_cons_state", None)
        self.sweep_designs, self.sweep_base, self.sweep_used = [], None, []
        self.sweep_base_key, self.preview_design = None, None
        for name in [n for n in vars(self) if n.startswith("sw_")] + ["tree"]:
            self.__dict__.pop(name, None)
        self.set_dirty(False)
        self.show_page("wing")
        self.run_analysis()
        self._start_prebuild()
        self.flash(f"Loaded {self.path}", "info")

    def open_dialog(self):
        if not self.confirm_discard():
            return
        path = filedialog.askopenfilename(title="Open aircraft configuration",
                                          initialdir=os.path.dirname(self.path or HERE),
                                          filetypes=[("Aircraft configuration", "*.json")])
        if path:
            self.load(path)

    def save(self, path=None):
        path = path or self.path
        try:
            with open(path, "w") as fh:
                fh.write(dump_config(self.raw))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save:\n{exc}")
            return False
        self.path = os.path.abspath(path)
        self.file_values = {(s, n): copy.deepcopy(spec["value"])
                            for s in A.SECTIONS for n, spec in self.raw[s].items()}
        self.set_dirty(False)
        self.flash(f"Saved {self.path}", "ok")
        return True

    def save_as(self):
        path = filedialog.asksaveasfilename(title="Save configuration as",
                                            initialdir=os.path.dirname(self.path),
                                            defaultextension=".json",
                                            filetypes=[("Aircraft configuration", "*.json")])
        if path:
            self.save(path)

    def set_dirty(self, d):
        self.dirty = d
        title = f"{APP_NAME}{'  •  unsaved changes' if d else ''}"
        if title != self._title:                       # not on every slider event
            self._title = title
            self.title(title)

    def confirm_discard(self):
        if not self.dirty:
            return True
        ans = messagebox.askyesnocancel(APP_NAME, "Save changes to the configuration first?")
        if ans is None:
            return False
        return self.save() if ans else True

    def on_close(self):
        if self.proc is not None and self.proc.poll() is None:
            if not messagebox.askyesno(APP_NAME, "A SolidWorks build is still running. Quit?"):
                return
        if self.confirm_discard():
            self.destroy()

    # ---------------------------------------------------------------- values
    def get_value(self, sec, name):
        return self.raw[sec][name]["value"]

    def set_value(self, sec, name, v):
        spec = self.raw[sec][name]
        if isinstance(v, float):
            v = float(f"{v:.10g}")
        if spec["value"] == v and type(spec["value"]) is type(v):
            return
        spec["value"] = v
        self.preview_design = None
        self.set_dirty(True)
        self.refresh_relevance()
        self.schedule_analysis()

    def _reset(self, keys):
        for key in keys:
            sec, name = key
            self.raw[sec][name]["value"] = copy.deepcopy(self.file_values[key])
            if key in self.rows:
                self.rows[key].show_value(self.raw[sec][name]["value"])
        self.set_dirty(any(self.raw[s][n]["value"] != v for (s, n), v in self.file_values.items()))
        self.refresh_relevance()
        self.schedule_analysis()

    def reset_page(self):
        if self.current in PARAM_PAGES:
            self._reset([k for k in self.file_values if k[0] == self.current])
            self.flash(f"{dict(PAGES)[self.current]} reset to the file values.", "info")

    def reset_all(self):
        if messagebox.askyesno(APP_NAME, "Reset every parameter to the values in the file?"):
            self._reset(list(self.file_values))
            self.flash("All parameters reset to the file values.", "info")

    def relevant(self, sec, name):
        v = lambda s, n: self.raw[s][n]["value"]
        if sec == "tail":
            cfg = v("tail", "configuration")
            rules = {
                "v_tail_angle_deg": cfg in V_TYPES,
                "stab_height_frac": cfg == "cruciform",
                "fin_below_frac": cfg == "h_tail",
                "ventral_fin_frac": cfg == "y_tail",
                "h_dihedral_deg": cfg not in V_TYPES,
                "v_aspect_ratio": cfg not in ("v_tail", "inverted_v"),
                "v_taper_ratio": cfg not in ("v_tail", "inverted_v"),
                "v_sweep_deg": cfg not in ("v_tail", "inverted_v"),
                "fin_x_offset_mm": cfg not in ("v_tail", "inverted_v"),
                "vh": v("tail", "h_area_mm2") == 0,
                "vv": v("tail", "v_area_mm2") == 0,
                "naca": v("tail", "airfoil_file") == "",
            }
            return rules.get(name, True)
        if sec == "wing" and name == "naca":
            return v("wing", "airfoil_file") == ""
        if sec == "fuselage" and name in ("canopy_start_frac", "canopy_length_frac",
                                          "canopy_peak_pos"):
            return v("fuselage", "canopy_height_mm") > 0
        return True

    def refresh_relevance(self):
        for (sec, name), row in self.rows.items():
            row.set_enabled(self.relevant(sec, name))

    def clear_filter(self):
        self.search.delete(0, "end")
        self.apply_filter()
        self.focus_set()

    def apply_filter(self):
        page = self.pages.get(self.current)
        if page is None or not hasattr(page, "cards"):
            return
        q = self.search.get().strip().lower()
        for card, rows in page.cards:
            shown = 0
            for r in rows:
                if not q or r.matches(q):
                    r.frame.grid()
                    shown += 1
                else:
                    r.frame.grid_remove()
            (card.grid if shown else card.grid_remove)()

    # ---------------------------------------------------------------- analysis
    def schedule_analysis(self):
        if self.analysis_job is not None:
            self.after_cancel(self.analysis_job)
        self.analysis_job = self.after(220, self.run_analysis)

    def run_analysis(self):
        self.analysis_job = None
        issues, results, ac, notes, cfg = [], [], None, [], None
        ref = re.compile(r"\b(wing|fuselage|tail|integration)\.(\w+)")

        def add(text, level, params=(), sections=()):
            ps = set(params)
            ss = set(sections) | {p[0] for p in ps}
            issues.append(dict(text=text, level=level, params=ps, sections=ss))

        try:
            cfg = A.config_from_dict(self.raw)
            p = cfg.params
            ac = A.integrate(p["wing"], p["fuselage"], p["tail"], p["integration"])
            results = A.evaluate_constraints(cfg, ac)
            notes = ac.notes
            for msg in A.hard_checks(ac):
                low = msg.lower()
                secs = set()
                if "wing" in low:
                    secs |= {"wing", "integration", "fuselage"}
                if "tail" in low:
                    secs |= {"tail", "integration", "fuselage"}
                add(msg, "error", ref.findall(msg), secs)
            for c, val, ok, msg in results:
                if ok:
                    continue
                params = ref.findall(c["expr"])
                secs = set()
                for d in re.findall(r"\bderived\.(\w+)", c["expr"]):
                    secs |= section_of_derived(d)
                add(msg, "constraint", params, secs)
        except A.ConfigError as exc:
            for e in exc.errors:
                m = re.match(r"(wing|fuselage|tail|integration)[.:]", e)
                add(e, "error", ref.findall(e), {m.group(1)} if m else set())
        except Exception as exc:
            add(f"{type(exc).__name__}: {exc}", "error", (), set(PARAM_PAGES))

        self.issues, self.notes, self.results = issues, notes, results
        if ac is not None:
            self.last = (cfg, ac, results)
        self.current_ac = ac
        self.update_views()

    def update_views(self):
        """Refresh everything that depends on the analysis - but only widgets that
        are visible and whose content actually changed."""
        self._apply_row_flags()
        self.update_badges()
        issues = self.issues
        n_c = len(self.results)
        if not issues:
            state = ("✓  Ready to build", OK,
                     f"All {n_c} constraints satisfied - wing, fuselage and tail fit together.")
        else:
            state = (f"✗  {len(issues)} issue{'s' * (len(issues) > 1)}", ERR,
                     issues[0]["text"])
        if state != getattr(self, "_status_state", None):
            self._status_state = state
            self._status_full = state[2]
            self.status_big.configure(text=state[0], text_color=state[1])
            self.status_small.configure(text=fit_lines(state[2], self.f_small,
                                                       self.STATUS_WRAP, 2))
        self._update_build_buttons()
        self._update_checks(issues)
        if self.current == "constraints":
            self._update_constraints_page()
        if self.current == "sweep":
            self._update_sweep_buttons()
        if self.current_ac is not None:
            self._update_data(self.current_ac)
        self._draw_current_preview()

    def _apply_row_flags(self):
        flagged = {}
        for it in self.issues:
            for p in it["params"]:
                prev = flagged.get(p)
                lvl = "error" if it["level"] == "error" else "warn"
                if prev is None or (prev[0] == "warn" and lvl == "error"):
                    flagged[p] = (lvl, [])
                flagged[p][1].append(it["text"])
        for key, row in self.rows.items():
            if key in flagged:
                lvl, texts = flagged[key]
                row.set_issue(lvl, "Problem:\n• " + "\n• ".join(texts))
            else:
                row.set_issue("ok")

    def _update_build_buttons(self, force=False):
        can_build = not self.issues and (self.proc is None or self.proc.poll() is not None)
        btn = getattr(self, "build_btn", None)
        state = (can_build, id(btn))
        if state == getattr(self, "_build_state", None) and not force:
            return
        self._build_state = state
        btn_fg = ctk.ThemeManager.theme["CTkButton"]["fg_color"]
        for b in (self.quick_build, btn):
            if b is not None and b.winfo_exists():
                b.configure(state="normal" if can_build else "disabled",
                            fg_color=btn_fg if can_build else DISABLED)

    def update_badges(self):
        issues = self.issues
        n_vars = len(self.sweep_block()["variables"]) if self.raw is not None else 0
        sweep_bad = bool(S.parse_sweep(self.raw)[2]) if n_vars else False
        for key, _ in PAGES:
            color = ERR
            if key == "sweep":
                n, color = n_vars, (ERR if sweep_bad else ACCENT)
            else:
                n = (sum(1 for it in issues if key in it["sections"]) if key in PARAM_PAGES
                     else sum(1 for it in issues if it["level"] == "constraint")
                     if key == "constraints" else len(issues) if key == "build" else 0)
            if self._badge_state.get(key) == (n, color):
                continue
            self._badge_state[key] = (n, color)
            badge = self.badges[key]
            if n:
                badge.configure(text=str(n), fg_color=color)
                badge.place(relx=1.0, x=-14, rely=0.5, anchor="e")
            else:
                badge.place_forget()

    # ---- right-hand tabs: only the visible one is drawn ---------------------------
    def _on_tab_change(self):
        name = self.tabs.get()
        if name == "Preview":
            self._draw_current_preview()
        elif name == "Checks":
            self._update_checks(self.issues)
        elif name == "Data" and self.current_ac is not None:
            self._update_data(self.current_ac)

    def show_tab(self, name):
        if self.tabs.get() != name:
            self.tabs.set(name)
            self._on_tab_change()

    def set_dragging(self, on):
        self.dragging = on
        if not on:
            self.after_idle(self._draw_current_preview)

    def _update_checks(self, issues):
        sig = (tuple((i["text"], i["level"], tuple(sorted(i["sections"]))) for i in issues),
               tuple(self.notes), len(self.results))
        if sig == self._checks_sig or self.tabs.get() != "Checks":
            return
        self._checks_sig = sig
        for w in self.checks_frame.winfo_children():
            w.destroy()
        r = 0
        if not issues:
            ctk.CTkLabel(self.checks_frame, text="✓  No problems found", font=self.f_head,
                         text_color=OK).grid(row=r, column=0, sticky="w", pady=(6, 2))
            r += 1
            ctk.CTkLabel(self.checks_frame, text=f"Every parameter is within its limits and all "
                                                 f"{len(self.results)} cross-component rules hold.",
                         font=self.f_small, text_color=MUTED, wraplength=400,
                         justify="left").grid(row=r, column=0, sticky="w")
            r += 1
        for it in issues:
            color = ERR if it["level"] == "error" else WARN
            f = ctk.CTkFrame(self.checks_frame, fg_color=CARD, corner_radius=8)
            f.grid(row=r, column=0, sticky="ew", pady=3)
            f.grid_columnconfigure(1, weight=1)
            ctk.CTkFrame(f, width=4, fg_color=color, corner_radius=2).grid(
                row=0, column=0, sticky="ns", padx=(6, 8), pady=6)
            where = ", ".join(sorted(dict(PAGES)[s] for s in it["sections"] if s in dict(PAGES)))
            ctk.CTkLabel(f, text=it["text"], font=self.f_small, wraplength=380,
                         justify="left", anchor="w").grid(row=0, column=1, sticky="w", pady=(6, 0))
            ctk.CTkLabel(f, text=("Range error" if it["level"] == "error" else "Constraint")
                         + (f"  ·  {where}" if where else ""), font=self.f_tiny,
                         text_color=MUTED, anchor="w").grid(row=1, column=1, sticky="w",
                                                            pady=(0, 6))
            r += 1
        for n in self.notes:
            ctk.CTkLabel(self.checks_frame, text=f"ⓘ  {n}", font=self.f_small,
                         text_color=WARN, wraplength=400, justify="left").grid(
                row=r, column=0, sticky="w", pady=2)
            r += 1

    def _update_constraints_page(self):
        values = {c["name"]: (v, ok) for c, v, ok, _ in self.results}
        cache = self.__dict__.setdefault("_cons_state", {})
        for c, pill, val in getattr(self, "cons_widgets", []):
            v, ok = values.get(c["name"], (None, None))
            if ok is None:
                state = ("n/a", MUTED, "–", text_fg())
            else:
                state = ("OK" if ok else "FAIL", OK if ok else ERR,
                         "n/a" if v is None else f"{v:.4g}", text_fg() if ok else ERR)
            if cache.get(id(pill)) == state or not pill.winfo_exists():
                continue
            cache[id(pill)] = state
            pill.configure(text=state[0], fg_color=state[1])
            val.style(text=state[2], fg=state[3])

    def _update_data(self, ac):
        if self.tabs.get() != "Data" or getattr(self, "_data_ac", None) is ac:
            return
        self._data_ac = ac
        d = ac.derived
        m = self.metric_labels
        m["Wing area"].configure(text=f"{d['wing_area_mm2'] / 1e6:.4f} m²")
        m["Wing MAC"].configure(text=f"{d['wing_mac_mm']:.1f} mm")
        m["Root / tip chord"].configure(text=f"{d['wing_root_chord_mm']:.0f} / "
                                             f"{d['wing_tip_chord_mm']:.0f} mm")
        m["Aircraft length"].configure(text=f"{d['aircraft_length_mm']:.0f} mm")
        m["Aircraft height"].configure(text=f"{d['aircraft_height_mm']:.0f} mm")
        m["Tail arm"].configure(text=f"{d['tail_arm_mm']:.0f} mm  "
                                     f"({d['tail_arm_mm'] / d['wing_mac_mm']:.2f} MAC)")
        m["Stabiliser area S_h"].configure(text=f"{d['tail_s_h_mm2'] / 1e6:.4f} m²")
        m["Fin area S_v"].configure(text=f"{d['tail_s_v_mm2'] / 1e6:.4f} m²")
        m["Vh / Vv achieved"].configure(text=f"{d['tail_vh_effective']:.3f} / "
                                             f"{d['tail_vv_effective']:.4f}")
        for key, label in (("wing_root_fit_mm", "Wing root room"),
                           ("tail_root_fit_mm", "Tail root room"),
                           ("wing_tail_gap_mm", "Wing-tail gap")):
            m[label].configure(text=f"{d[key]:.1f} mm", text_color=OK if d[key] >= 0 else ERR)
        text = "\n".join(f"{k:30s}{v:14.3f}" for k, v in d.items())
        if text != self._data_text:
            self._data_text = text
            self.derived_box.configure(state="normal")
            self.derived_box.delete("1.0", "end")
            self.derived_box.insert("end", text)
            self.derived_box.configure(state="disabled")

    # ---------------------------------------------------------------- preview
    @staticmethod
    def _preview_colors(dark):
        return {"bg": "#2b2b2b" if dark else "#dbdbdb", "fg": "#d1d5db" if dark else "#374151",
                "grid": "#3f3f46" if dark else "#c4c4c4"}

    def _style_preview(self, dark):
        c = self._preview_colors(dark)
        self.fig.set_facecolor(c["bg"])
        # canvas background = figure colour, so no white shows around the image
        # while a resize waits for its render
        self.canvas.get_tk_widget().configure(background=c["bg"])
        for ax in self.axes:
            ax.set_facecolor(c["bg"])
            ax.title.set_color(c["fg"])
            ax.tick_params(colors=c["fg"], labelsize=7)
            ax.grid(True, color=c["grid"], lw=0.5)
        self._preview_dark = dark

    PART_COLORS = {"fuselage": "#94a3b8", "wing": "#3b82f6", "tail": "#f59e0b"}

    def draw_preview(self, ac):
        dark = ctk.get_appearance_mode() == "Dark"
        if dark != self._preview_dark:
            self._style_preview(dark)
        self._draw_views(ac, self._preview_colors(dark))
        self.canvas.draw_idle()

    # ---- three orthographic views ---------------------------------------------------
    def _layout_views(self):
        """Place the three views so each gets a height matching its proportions.

        Margins are in points (they hold the titles and tick labels at any size);
        the remaining height is shared in proportion to each view's height/width
        ratio, so the complete aircraft fills every view."""
        fig = self.fig
        W, H = fig.get_size_inches() * fig.dpi
        if W < 50 or H < 50:
            return
        k = fig.dpi / 72.0                              # points -> pixels
        left, right, top, bottom = 38 * k, 16 * k, 6 * k, 4 * k
        title, ticks, gap = 16 * k, 16 * k, 8 * k
        box_w = max(W - left - right, 10)
        avail = H - top - bottom - 3 * (title + ticks) - 2 * gap
        a = np.clip(self._view_aspects, 0.04, 3.0)
        need = box_w * a                              # heights at full width
        if need.sum() <= avail:                       # fits: full width, spare -> gaps
            heights, extra = need, (avail - need.sum()) / 3
        else:
            # Too tall: shrink the views alike, but every view keeps at least
            # min(its need, 20 % of the height) so a slim side / front view of a
            # very large-span aircraft stays readable; the others share the rest.
            floor = 0.2 * avail
            heights, fixed = need.copy(), np.zeros(len(a), bool)
            for _ in range(len(a)):
                free = ~fixed
                t = (avail - heights[fixed].sum()) / need[free].sum()
                heights[free] = need[free] * t
                low = free & (heights < np.minimum(need, floor))
                if not low.any():
                    break
                heights[low] = np.minimum(need[low], floor)
                fixed |= low
            extra = 0.0
        heights = np.maximum(heights, 10)
        y = H - top - extra / 2
        for ax, h in zip(self.axes, heights):
            y -= title + h
            ax.set_position((left / W, y / H, box_w / W, h / H))
            y -= ticks + gap + extra

    def _draw_views(self, ac, c):
        bg, fg = c["bg"], c["fg"]
        fus_c, wing_c, tail_c = (self.PART_COLORS[p] for p in ("fuselage", "wing", "tail"))
        for a in self._preview_artists:
            a.remove()
        arts = []
        polys = ([], [], [])                       # per view: (zorder, verts, rgba)

        def fill(view, xs, ys, color, z, alpha=1.0):
            polys[view].append((z, np.column_stack([xs, ys]), to_rgba(color, alpha)))

        x = np.linspace(0, ac.fus_lay.L, 300)
        hw, top, bot, _, _ = F.body_lines(ac.fp, ac.fus_lay, x)
        fill(0, np.r_[x, x[::-1]], np.r_[hw, -hw[::-1]], fus_c, 2)
        fill(1, np.r_[x, x[::-1]], np.r_[top, bot[::-1]], fus_c, 2)
        big = max(ac.stations, key=lambda s: s.half_w * (s.top - s.bot))
        fill(2, big.pts[:, 2], big.pts[:, 1], fus_c, 2)

        le = np.array([s.lower[0] for s in ac.wing_sections])
        te = np.array([s.upper[0] for s in ac.wing_sections])
        poly = np.vstack([le, te[::-1]])
        loops = np.vstack([np.vstack([s.upper, s.lower]) for s in ac.wing_sections])
        for sgn in (1, -1):
            fill(0, poly[:, 0], sgn * poly[:, 2], wing_c, 3, 0.9)
            h = fast_hull(np.column_stack([sgn * loops[:, 2], loops[:, 1]]))
            fill(2, h[:, 0], h[:, 1], wing_c, 1, 0.9)
        root = ac.wing_sections[0]
        rl = np.vstack([root.upper, root.lower[1:]])
        fill(1, rl[:, 0], rl[:, 1], wing_c, 3)

        for _, _, secs in ac.tail_sections:
            pts = np.vstack([np.vstack([s.upper, s.lower]) for s in secs])
            for view, i, j, z in ((0, 0, 2, 4), (1, 0, 1, 1), (2, 2, 1, 3)):
                h = fast_hull(pts[:, [i, j]])
                fill(view, h[:, 0], h[:, 1], tail_c, z, 0.92)

        d = ac.derived
        y_dim = d["fuselage_bottom_min_mm"] - 0.25 * ac.fp.max_height_mm
        extra = {1: np.array([[d["wing_ac_x_mm"], y_dim],
                              [0.0, y_dim - 0.2 * ac.fp.max_height_mm]])}
        # One collection per view (drawn in z order); limits computed with numpy -
        # much faster than one patch per outline with matplotlib's own autoscaling.
        aspects = []
        for view, ax in enumerate(self.axes):
            items = sorted(polys[view], key=lambda t: t[0])
            pc = PolyCollection([v for _, v, _ in items], facecolors=[col for *_, col in items],
                                edgecolors=fg, linewidths=0.8, zorder=2)
            ax.add_collection(pc, autolim=False)
            arts.append(pc)
            allv = np.vstack([v for _, v, _ in items] + ([extra[view]] if view in extra else []))
            span = np.ptp(allv, axis=0)
            aspects.append(span[1] / span[0] if span[0] > 0 else 1.0)
            ax.ignore_existing_data_limits = True
            ax.update_datalim(allv)                     # one vectorised bounding box
            ax.autoscale_view()
            if view == 0 and not ax.yaxis_inverted():   # top view: +Z downwards
                ax.invert_yaxis()
        ax_side = self.axes[1]
        arts.append(ax_side.annotate("", xy=(d["wing_ac_x_mm"], y_dim),
                                     xytext=(d["wing_ac_x_mm"] + d["tail_arm_mm"], y_dim),
                                     arrowprops=dict(arrowstyle="<->", color=fg, lw=0.8)))
        arts.append(ax_side.text(d["wing_ac_x_mm"] + d["tail_arm_mm"] / 2, y_dim,
                                 f" tail arm {d['tail_arm_mm']:.0f} mm ", ha="center",
                                 va="center", fontsize=7, color=fg,
                                 bbox=dict(fc=bg, ec="none", pad=1)))
        self._preview_artists = arts
        self._view_aspects = np.array(aspects)
        self._layout_views()

    def set_mode(self, mode):
        ctk.set_appearance_mode(mode.lower())

    def _on_appearance(self, _mode):
        """Also runs when Windows switches theme while in 'System' mode."""
        self._style_tree()
        self.after(60, self._draw_current_preview)

    def _page_bg(self):
        return tuple(ctk.ThemeManager.theme["CTk"]["fg_color"])

    # ---------------------------------------------------------------- export / build
    def _browse_part(self):
        path = filedialog.asksaveasfilename(title="Save SolidWorks part as",
                                            defaultextension=".SLDPRT",
                                            filetypes=[("SolidWorks part", "*.SLDPRT")])
        if path:
            self.save_part_var.set(os.path.normpath(path))

    def export_image(self):
        if self.last is None:
            return
        path = filedialog.asksaveasfilename(title="Export preview", defaultextension=".png",
                                            initialfile="aircraft_preview.png",
                                            filetypes=[("PNG image", "*.png")])
        if path:
            with contextlib.redirect_stdout(io.StringIO()):
                A.plot_aircraft(self.last[1], path)
            self.flash(f"Preview saved to {path}", "ok")

    def export_report(self):
        if self.last is None:
            return
        path = filedialog.asksaveasfilename(title="Export report", defaultextension=".txt",
                                            initialfile="aircraft_report.txt",
                                            filetypes=[("Text", "*.txt")])
        if path:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                A.print_report(*self.last)
            with open(path, "w") as fh:
                fh.write(buf.getvalue())
                if self.issues:
                    fh.write("\nIssues:\n" + "\n".join(f"  - {i['text']}" for i in self.issues))
            self.flash(f"Report saved to {path}", "ok")

    def build(self):
        if self.issues:
            self.flash("Resolve the issues first - the model can only be built when every "
                       "rule is satisfied.", "warn")
            return
        if self.proc is not None and self.proc.poll() is None:
            return
        self.show_page("build")
        tmp = os.path.join(tempfile.gettempdir(), "aircraft_designer_config.json")
        with open(tmp, "w") as fh:
            fh.write(dump_config(self.raw))
        cmd = [sys.executable, "-u", os.path.join(HERE, "aircraft_sw.py"), "--config", tmp]
        if self.new_part_var.get():
            cmd.append("--new-part")
        if self.save_part_var.get().strip():
            cmd += ["--save", self.save_part_var.get().strip()]
        self._start_process(cmd, self.log, self.progress, "single", "Building in SolidWorks…")

    def _start_process(self, cmd, log, progress, kind, message):
        """Run a SolidWorks build script and stream its output into `log`."""
        log.delete("1.0", "end")
        log.insert("end", "> " + " ".join(os.path.basename(c) if i < 3 else c
                                         for i, c in enumerate(cmd)) + "\n")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     creationflags=flags)
        self.run_target = (log, progress, kind)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        if kind == "sweep":
            progress.configure(mode="determinate")
            progress.set(0)
        else:
            progress.configure(mode="indeterminate")
            progress.start()
        self.update_views()
        self.flash(message, "info")
        self.after(100, self._poll_log)

    def _reader(self, proc):
        for line in proc.stdout:
            self.log_queue.put(line)
        proc.wait()
        self.log_queue.put(None)

    def _poll_log(self):
        log, progress, kind = self.run_target
        done = False
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if line is None:
                done = True
                break
            if log.winfo_exists():
                log.insert("end", line)
                log.see("end")
            m = re.match(r"\[(\d+)/(\d+)\]", line)
            if kind == "sweep" and m and progress.winfo_exists():
                progress.set((int(m.group(1)) - 1) / int(m.group(2)))
        if not done:
            self.after(100, self._poll_log)
            return
        ok = self.proc.returncode == 0
        if progress.winfo_exists():
            progress.stop()
            progress.configure(mode="determinate")
            progress.set(1 if ok else 0)
        if kind == "sweep":
            self.flash("All designs built in SolidWorks." if ok else
                       "Some designs could not be built - see the build log.",
                       "ok" if ok else "warn")
        else:
            self.flash("Aircraft built in SolidWorks." if ok
                       else f"Build failed (exit code {self.proc.returncode}) - see the log.",
                       "ok" if ok else "warn")
        self.update_views()

    # ---------------------------------------------------------------- status bar
    def flash(self, text, kind="info"):
        color = {"ok": OK, "warn": WARN, "info": MUTED}.get(kind, MUTED)
        self.statusbar.configure(text=text, text_color=color)
        if self.flash_job is not None:
            self.after_cancel(self.flash_job)
        self.flash_job = self.after(7000, lambda: self.statusbar.configure(text=""))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else A.DEFAULT_CONFIG
    ctk.set_appearance_mode("system")
    app = AircraftDesigner(path)
    app.mainloop()


if __name__ == "__main__":
    main()
