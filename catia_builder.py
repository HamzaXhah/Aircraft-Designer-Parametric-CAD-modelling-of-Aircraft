#!/usr/bin/env python3
"""
CATIA V5 backend for the parametric aircraft (pywin32 / COM automation)
=======================================================================

The CATIA counterpart of the SolidWorks builders in wing_solidworks.py,
fuselage_sw.py, tail_sw.py and aircraft_sw.py. It takes exactly the same
geometry (wing sections, fuselage stations, tail sections - all computed in
pure Python by those modules) and builds the same model in a CATPart:

    SolidWorks                              CATIA V5
    ------------------------------------    ---------------------------------------
    part document (default template)        Documents.Add("Part")
    reference plane per station + sketch    3-D section curves in a geometrical set
    sketch spline (CreateSpline2)           HybridShapeSpline through coordinate points
    TE line (CreateLine)                    HybridShapeLinePtPt
    closed sketch profile                   HybridShapeAssemble (Join) of the curves
    loft (InsertProtrusionBlend2, merge)    Multi-sections Solid in PartBody (adds)
    nose / tail tip sketch point            tiny cap section of the same body law
                                            (CATIA automation rejects point sections)
    mirror body about the Front Plane       Mirror of PartBody about the XY plane
    global variables (Equation manager)     real parameters in a parameter set
    hide reference planes                   hide the construction geometrical sets
    mass properties                         SPAWorkbench measurable (volume, area)
    SaveAs3 (.SLDPRT)                       Document.SaveAs (.CATPart)

Coordinates are the same as in the SolidWorks model and need no conversion:
X aft from the nose tip, Y up, Z spanwise (right wing +Z), millimetres
(the CATIA automation API works in mm).

The aircraft is built in the active CATPart only if that part is completely
empty; otherwise a new part opens in its own window, so a part with any of
your own work in it is never changed. Naming follows the SolidWorks feature
names, so the model trees look alike (the component scripts use the names to
replace a previous component when they build into the active part):

    PartBody/   Wing_Loft, Wing_Mirror, Fuselage_Loft, Tail_Loft_<panel>
    Wing_Sections, Fus_Sections, Tail_Sections      geometrical sets (hidden)
    Aircraft_Parameters                             parameter set (AC_* values)

Requires: Windows, CATIA V5 (tested on V5-6R2022), pywin32, numpy.
"""

from __future__ import annotations

import contextlib
import math
import os
import time

import numpy as np

CATIA_PROGID = "CATIA.Application"
CATIA_EXT = ".CATPart"
HIDE = 1                                  # CatVisPropertyShow: 0 = show, 1 = no show
COUPLING_RATIO = 1                        # multi-section coupling: ratio
RELIMIT_ON_SECTIONS = 1                   # loft limited to the first / last section

# What each component owns in the part: (prefixes of its features, geometrical set)
COMPONENTS = {
    "wing": (("Wing_",), "Wing_Sections"),
    "fuselage": (("Fus_", "Fuselage_"), "Fus_Sections"),
    "tail": (("Tail_",), "Tail_Sections"),
}
AIRCRAFT_PREFIXES = ("Wing_", "Fus_", "Fuselage_", "Tail_")
AIRCRAFT_PARAM_SET = "Aircraft_Parameters"


class CatiaError(RuntimeError):
    pass


def catia_path(path: str) -> str:
    """Absolute path with the .CATPart extension CATIA needs."""
    path = os.path.abspath(path)
    root, ext = os.path.splitext(path)
    return path if ext.lower() == CATIA_EXT.lower() else root + CATIA_EXT


# =============================================================================
# 1. SESSION AND PART HELPERS
# =============================================================================
class CatiaPart:
    """Connection to CATIA plus the geometry operations the builders need."""

    def __init__(self, visible: bool = True):
        try:
            import win32com.client
        except ImportError as exc:
            raise SystemExit("pywin32 not found: pip install pywin32") from exc
        self.win32 = win32com.client
        try:
            self.app = win32com.client.GetActiveObject(CATIA_PROGID)
            print("Connected to running CATIA session.")
        except Exception:
            print("Starting CATIA ...")
            try:
                self.app = win32com.client.Dispatch(CATIA_PROGID)
            except Exception as exc:
                raise CatiaError(
                    "Could not connect to CATIA V5. Check that CATIA is installed and "
                    "registered for automation (run 'cnext /regserver' once as "
                    "administrator) and that it runs under the same Windows user and "
                    "privilege level as this script.") from exc
        self.app.Visible = visible
        self.doc = self.part = None

    # ---- document -----------------------------------------------------------
    def _active_part_doc(self):
        """The active document if it is a CATPart, else None."""
        try:
            doc = self.app.ActiveDocument
            doc.Part                                        # raises for products etc.
            return doc
        except Exception:
            return None

    @staticmethod
    def part_contents(doc, prefixes=AIRCRAFT_PREFIXES):
        """What a CATPart holds, as (project items, other items) name lists.

        Project items are the solids, geometrical sets and parameter sets this
        program makes (names starting with `prefixes`, *_Parameters sets).
        Everything else - sketches, other features, extra bodies, geometrical
        sets, relations, user parameters - counts as the user's own work. A
        collection that cannot be read is reported as other content, so an
        unreadable part is never treated as empty."""
        ours, other = [], []

        def items(get, what):
            try:
                coll = get()
                return [coll.Item(i).Name for i in range(1, coll.Count + 1)]
            except Exception:
                other.append(f"<unreadable {what}>")
                return []

        part = doc.Part
        body = part.MainBody
        for n in items(lambda: part.Bodies, "bodies"):
            if n != body.Name:
                other.append(n)
        for n in items(lambda: body.Shapes, "features"):
            (ours if n.startswith(prefixes) else other).append(n)
        for n in items(lambda: part.HybridBodies, "geometrical sets"):
            (ours if n.startswith(prefixes) else other).append(n)
        root = part.Parameters.RootParameterSet
        for n in items(lambda: root.ParameterSets, "parameter sets"):
            (ours if n.startswith(prefixes) or n == AIRCRAFT_PARAM_SET else other).append(n)
        other += items(lambda: body.Sketches, "sketches")
        # a multi-sections solid lists its loft surface here under the same name
        for n in items(lambda: body.HybridShapes, "wireframe"):
            if not n.startswith(prefixes):
                other.append(n)
        other += items(lambda: part.OrderedGeometricalSets, "ordered geometrical sets")
        other += items(lambda: part.Relations, "relations")
        other += items(lambda: root.DirectParameters, "parameters")
        return ours, other

    def open_part(self, reuse: str = "empty") -> bool:
        """Attach to the active CATPart or create a new part in its own window.

        reuse = "never"  : always a new part
                "empty"  : use the active part only if it is completely empty
                "project": use it only if it is empty or holds nothing but this
                           program's geometry (lets a component script add the
                           tail to a fuselage part); a part with any work of the
                           user's is never changed
        Returns True when the active part is used."""
        doc = None if reuse == "never" else self._active_part_doc()
        if doc is not None:
            ours, other = self.part_contents(doc)       # all of this program's prefixes
            if not other and (reuse == "project" or not ours):
                self.attach(doc)
                print(f"Building in the active part: {doc.Name} "
                      f"({'project geometry only' if ours else 'empty'})")
                return True
            print(f"Active part {doc.Name} is not empty - left unchanged.")
        doc = self.app.Documents.Add("Part")
        if doc is None:
            raise CatiaError("CATIA failed to create a new part.")
        self.attach(doc)
        print("Created new part document.")
        return False

    def attach(self, doc):
        self.doc = doc
        self.part = doc.Part
        self.hsf = self.part.HybridShapeFactory
        self.sf = self.part.ShapeFactory
        self.body = self.part.MainBody

    def ref(self, obj):
        return self.part.CreateReferenceFromObject(obj)

    @contextlib.contextmanager
    def batch(self):
        """No screen refresh and no undo recording while the model is generated."""
        app = self.app
        with contextlib.suppress(Exception):
            app.RefreshDisplay = False
        with contextlib.suppress(Exception):
            app.DisableNewUndoRedoTransaction()
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                app.EnableNewUndoRedoTransaction()
            with contextlib.suppress(Exception):
                app.RefreshDisplay = True

    # ---- finding and deleting earlier geometry ------------------------------
    def _items(self, collection):
        return [collection.Item(i) for i in range(1, collection.Count + 1)]

    def find_ours(self, prefixes, param_sets=()):
        """Solid features in PartBody, geometrical sets and parameter sets that
        belong to the aircraft (by name)."""
        shapes = [s for s in self._items(self.body.Shapes) if s.Name.startswith(prefixes)]
        sets = [h for h in self._items(self.part.HybridBodies) if h.Name.startswith(prefixes)]
        psets = []
        if param_sets:
            with contextlib.suppress(Exception):
                root = self.part.Parameters.RootParameterSet.ParameterSets
                psets = [p for p in self._items(root) if p.Name in param_sets]
        return shapes, sets, psets

    def _delete(self, objs):
        if not objs:
            return 0
        sel = self.doc.Selection
        sel.Clear()
        for o in objs:
            sel.Add(o)
        sel.Delete()
        sel.Clear()
        return len(objs)

    def delete_existing(self, prefixes, param_sets=(), label="aircraft"):
        """Delete solids first (last one first), then their wireframe, then
        the parameter sets - the same order as the SolidWorks builders."""
        shapes, sets, psets = self.find_ours(prefixes, param_sets)
        removed = 0
        for s in reversed(shapes):                      # mirror before its loft
            removed += self._delete([s])
        removed += self._delete(sets)
        with contextlib.suppress(Exception):            # not critical
            removed += self._delete(psets)
        if removed:
            print(f"Deleted {removed} items of the previous {label}.")
        return removed

    # ---- wireframe ----------------------------------------------------------
    def geometry_set(self, name):
        hb = self.part.HybridBodies.Add()
        hb.Name = name
        return hb

    def _point(self, xyz):
        x, y, z = (float(v) for v in xyz)
        return self.hsf.AddNewPointCoord(x, y, z)

    def spline(self, hb, pts, name, closed=False):
        """Spline through `pts` (n, 3) mm. The coordinate points are aggregated
        under the spline instead of filling the tree. Returns (spline, points)."""
        sp = self.hsf.AddNewSpline()
        sp.SetSplineType(0)                             # cubic, through the points
        sp.SetClosing(1 if closed else 0)
        objs = []
        for q in pts:
            pt = self._point(q)
            sp.AddPoint(self.ref(pt))
            objs.append(pt)
        hb.AppendHybridShape(sp)
        sp.Name = name
        return sp, objs

    def line(self, hb, p0, p1, name):
        ln = self.hsf.AddNewLinePtPt(self.ref(p0), self.ref(p1))
        hb.AppendHybridShape(ln)
        ln.Name = name
        return ln

    def join(self, hb, curves, name):
        jn = self.hsf.AddNewJoin(self.ref(curves[0]), self.ref(curves[1]))
        for c in curves[2:]:
            jn.AddElement(self.ref(c))
        hb.AppendHybridShape(jn)
        jn.Name = name
        try:
            self.part.UpdateObject(jn)
        except Exception as exc:
            raise CatiaError(f"{name}: the section curves do not form one closed "
                             "profile.") from exc
        return jn

    def airfoil_profile(self, hb, upper, lower, sharp_te, name):
        """Closed airfoil section: upper spline (TE -> LE) + lower spline
        (LE -> TE) + a TE line when the trailing edge is blunt - the same
        curves the SolidWorks sketch holds. Returns (profile, TE closing point)."""
        up, up_pts = self.spline(hb, upper, f"{name}_Upper")
        lo, _ = self.spline(hb, lower, f"{name}_Lower")
        curves = [up, lo]
        if not sharp_te:                                # close a blunt trailing edge
            curves.append(self.line(hb, self._point(lower[-1]), self._point(upper[0]),
                                    f"{name}_TE"))
        return self.join(hb, curves, name), up_pts[0]

    def hide(self, objs):
        try:
            sel = self.doc.Selection
            sel.Clear()
            for o in objs:
                sel.Add(o)
            sel.VisProperties.SetShow(HIDE)
            sel.Clear()
        except Exception as exc:
            print(f"  [warn] could not hide construction geometry: {exc}")

    # ---- solids -------------------------------------------------------------
    def _loft_sections(self, loft, sections):
        loft.SectionCoupling = COUPLING_RATIO
        loft.Relimitation = RELIMIT_ON_SECTIONS
        for obj, closing in sections:
            # no closing point: pass None (a VT_DISPATCH null VARIANT is rejected)
            loft.AddSectionToLoft(self.ref(obj), 1,
                                  self.ref(closing) if closing is not None else None)

    def solid_loft(self, sections, name, hb=None):
        """Multi-sections solid through [(closed profile, closing point)],
        added to PartBody (the SolidWorks loft with 'merge result').

        If CATIA cannot build the solid loft directly it falls back to a
        multi-sections surface in `hb` closed into a solid (Close Surface)."""
        self.part.InWorkObject = self.body
        feat = self.sf.AddNewLoft()
        self._loft_sections(feat.HybridShape, sections)
        try:
            self.part.UpdateObject(feat)
        except Exception as first:
            self._delete([feat])
            if hb is None:
                raise CatiaError(f"Loft {name} failed: {first}") from first
            srf = self.hsf.AddNewLoft()
            self._loft_sections(srf, sections)
            hb.AppendHybridShape(srf)
            srf.Name = f"{name}_Surface"
            try:
                self.part.UpdateObject(srf)
                self.part.InWorkObject = self.body
                feat = self.sf.AddNewCloseSurface(self.ref(srf))
                self.part.UpdateObject(feat)
            except Exception as exc:
                self._delete([srf])
                raise CatiaError(f"Loft {name} failed. Check its section curves in "
                                 f"the geometrical set '{hb.Name}'.") from exc
            print(f"  [note] {name}: built as a closed multi-sections surface.")
        feat.Name = name
        return feat

    def mirror_body(self, name):
        """Mirror of everything in PartBody so far about the XY plane (z = 0)."""
        self.part.InWorkObject = self.body
        plane = self.part.OriginElements.PlaneXY
        feat = self.sf.AddNewMirror(self.ref(plane))
        try:
            self.part.UpdateObject(feat)
        except Exception:
            self._delete([feat])
            print("  [warn] mirror failed; half wing left in place.")
            return None
        feat.Name = name
        return feat

    # ---- parameters, view, report, save -------------------------------------
    def write_parameters(self, set_name, values: dict):
        """Record values as real parameters in their own parameter set - the
        CATIA counterpart of the SolidWorks global variables (reference only)."""
        try:
            sets = self.part.Parameters.RootParameterSet.ParameterSets
            with contextlib.suppress(Exception):
                self._delete([p for p in self._items(sets) if p.Name == set_name])
            params = sets.CreateSet(set_name).DirectParameters
            for n, v in values.items():
                params.CreateReal(n, float(v))
        except Exception as exc:
            print(f"  [warn] parameters not written: {exc}")

    def update(self):
        try:
            self.part.Update()
        except Exception as exc:
            raise CatiaError(f"CATIA could not update the part: {exc}") from exc

    def show_isometric(self):
        """Isometric view with +Y up (the SolidWorks orientation), zoom to fit."""
        try:
            viewer = self.app.ActiveWindow.ActiveViewer
            vp = viewer.Viewpoint3D
            s = -np.ones(3) / math.sqrt(3.0)
            up = np.array([0.0, 1.0, 0.0]) - s[1] * s
            up /= np.linalg.norm(up)
            vp.PutSightDirection(tuple(float(v) for v in s))
            vp.PutUpDirection(tuple(float(v) for v in up))
            viewer.Reframe()
        except Exception as exc:
            print(f"  [warn] could not set the view: {exc}")

    def report(self):
        try:
            spa = self.doc.GetWorkbench("SPAWorkbench")
            m = spa.GetMeasurable(self.ref(self.body))
            print(f"  CATIA volume         : {m.Volume:.5f} m^3")
            print(f"  CATIA area           : {m.Area:.4f} m^2")
        except Exception as exc:
            print(f"  [warn] mass properties unavailable: {exc}")

    def save_as(self, path, tries=3) -> bool:
        """Save as .CATPart (extension added if missing), overwriting a closed
        file silently. CATIA cannot hold two documents with the same file name,
        so if that name is already open (e.g. the window of an earlier build),
        the part is saved under the next free name: name_2, name_3, ..."""
        path = catia_path(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open_names = set()
        for k in range(1, self.app.Documents.Count + 1):
            with contextlib.suppress(Exception):
                other = self.app.Documents.Item(k)
                if other.FullName != self.doc.FullName:
                    open_names.add(other.Name.lower())
        if os.path.basename(path).lower() in open_names:
            root, ext = os.path.splitext(path)
            n = 2
            while (os.path.basename(f"{root}_{n}{ext}").lower() in open_names
                   or os.path.exists(f"{root}_{n}{ext}")):
                n += 1
            print(f"  [note] {os.path.basename(path)} is already open in CATIA; "
                  f"saving as {os.path.basename(root)}_{n}{ext} instead.")
            path = f"{root}_{n}{ext}"
        err = None
        alerts = None
        with contextlib.suppress(Exception):
            alerts = self.app.DisplayFileAlerts
            self.app.DisplayFileAlerts = False
        try:
            for k in range(tries):
                try:
                    self.doc.SaveAs(path)
                    if os.path.exists(path) and os.path.getsize(path) > 0:
                        print(f"  saved to {path}")
                        return True
                except Exception as exc:
                    err = exc
                time.sleep(1.0 + k)
        finally:
            if alerts is not None:
                with contextlib.suppress(Exception):
                    self.app.DisplayFileAlerts = alerts
        print(f"  [error] could not save {path} ({err or 'file not written'}; "
              "is the file open in CATIA or locked by another program?)")
        return False

    def close(self):
        """Close the part document that was just built."""
        if self.doc is not None:
            self.doc.Close()
        self.doc = self.part = None


# =============================================================================
# 2. COMPONENT GEOMETRY (same inputs as the SolidWorks builders)
# =============================================================================
def build_wing(cp: CatiaPart, sections, mirror=True, prefix="Wing"):
    """Wing sections (wing_solidworks.Section, model mm) -> loft (+ mirror)."""
    hb = cp.geometry_set(COMPONENTS["wing"][1])
    profiles = [cp.airfoil_profile(hb, s.upper, s.lower, s.sharp_te, f"{prefix}_Section_{i}")
                for i, s in enumerate(sections)]
    cp.solid_loft(profiles, f"{prefix}_Loft", hb)
    if mirror:
        cp.mirror_body(f"{prefix}_Mirror")
    return hb


CAP_WIDTH_FRAC = 0.01                     # cap section width / max width
CAP_MIN_GAP_MM = 0.05                     # closest a cap may get to the nose / tail end


def _cap_station(F, p, lay, lo, hi, growing):
    """A tiny section of the true body law between x = lo and hi, where the
    half-width is CAP_WIDTH_FRAC of the maximum (bisection; the half-width
    grows from the nose tip and shrinks towards a pointed tail end)."""
    target = CAP_WIDTH_FRAC * lay.W / 2

    def hw(x):
        return float(F.body_lines(p, lay, np.array([x]))[0][0])

    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if (hw(mid) < target) == growing:
            lo = mid
        else:
            hi = mid
    x = 0.5 * (lo + hi)
    x = max(x, CAP_MIN_GAP_MM) if growing else min(x, lay.L - CAP_MIN_GAP_MM)
    a, top, bot, nt, nb = (float(v[0]) for v in F.body_lines(p, lay, np.array([x])))
    return F.superellipse_section(x, a, top, bot, nt, nb, p.n_section_points)


def build_fuselage(cp: CatiaPart, p, lay, stations):
    """Fuselage stations (fuselage_sw.Station) -> loft through every closed
    superellipse section.

    SolidWorks lofts from a point at the nose tip (and to one at a pointed tail
    end). CATIA's automation rejects point sections, so the loft starts / ends
    at a tiny cap section of the same body law instead (1 % of the max width,
    see CAP_WIDTH_FRAC), which closes the body practically to a point."""
    import fuselage_sw as F
    hb = cp.geometry_set(COMPONENTS["fuselage"][1])
    loops = [(f"{F.SKETCH_PREFIX}{i}", st.pts) for i, st in enumerate(stations)]
    caps = [(F.TIP_SKETCH, _cap_station(F, p, lay, 0.0, stations[0].x, True))]
    if stations[0].x <= caps[0][1][0, 0] + CAP_MIN_GAP_MM:
        caps = []                                       # first station is already tiny
    loops = caps + loops
    if lay.pointed_tail:
        x_last = stations[-1].x
        end = _cap_station(F, p, lay, x_last, lay.L, False)
        if end[0, 0] > x_last + CAP_MIN_GAP_MM:
            loops.append((F.TAIL_SKETCH, end))
    profiles = []
    for name, pts in loops:
        sp, objs = cp.spline(hb, pts[:-1], name, closed=True)   # first == last: periodic
        profiles.append((sp, objs[0]))                  # couple at the +Z side point
    try:
        cp.solid_loft(profiles, F.LOFT_NAME)
    except CatiaError:
        # as in SolidWorks: retry without the tip sections - the nose then
        # starts at the first regular station
        trimmed = [pr for pr, (name, _) in zip(profiles, loops)
                   if name not in (F.TIP_SKETCH, F.TAIL_SKETCH)]
        if len(trimmed) == len(profiles):
            raise
        print("  [warn] loft through the tip sections failed; retrying without them.")
        cp.solid_loft(trimmed, F.LOFT_NAME, hb)
    return hb


def build_tail(cp: CatiaPart, tail_sections):
    """Tail sections [(tag, panel index, [root, tip])] -> one loft per panel side."""
    import tail_sw as T
    hb = cp.geometry_set(COMPONENTS["tail"][1])
    for tag, _, secs in tail_sections:
        profiles = [cp.airfoil_profile(hb, s.upper, s.lower, s.sharp_te,
                                       f"{T.SKETCH_PREFIX}{tag}_{k}")
                    for k, s in enumerate(secs)]
        cp.solid_loft(profiles, f"{T.LOFT_PREFIX}{tag}", hb)
    return hb


def numeric_fields(obj, prefix, skip=()) -> dict:
    """Numeric fields of a parameter dataclass as {prefix + name: value}."""
    from dataclasses import asdict
    return {f"{prefix}{k}": v for k, v in asdict(obj).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in skip}


def build_component(kind: str, build_fn, params=None, save_path=None, reuse="project",
                    label=None) -> bool:
    """Standalone build of one component (used by the component scripts with
    --cad catia): open / reuse a part, delete that component's previous
    geometry, build, record the parameters, update, view, save."""
    prefixes, _ = COMPONENTS[kind]
    label = label or kind
    pset = f"{prefixes[0]}Parameters"
    cp = CatiaPart(visible=True)
    cp.open_part(reuse=reuse)
    cp.delete_existing(prefixes, (pset,), label)
    with cp.batch():
        hb = build_fn(cp)
        if params is not None:
            cp.write_parameters(pset, numeric_fields(params, prefixes[0]))
        cp.update()
        cp.hide([hb])
    cp.show_isometric()
    cp.report()
    saved = cp.save_as(save_path) if save_path else True
    print("Done.")
    return saved


# =============================================================================
# 3. COMPLETE AIRCRAFT (same interface as aircraft_sw.SolidWorksAircraftBuilder)
# =============================================================================
class CatiaAircraftBuilder:
    label = "CATIA V5"
    extension = CATIA_EXT

    def __init__(self, new_part: bool = False):
        # The aircraft goes into the active part only if that part is
        # completely empty; otherwise (or with new_part) a new part opens in its
        # own window. A part with anything in it is never changed.
        self.cp = CatiaPart(visible=True)
        self.app = self.cp.app
        self.force_new = new_part
        self.model = None

    def open_part(self):
        self.cp.open_part(reuse="never" if self.force_new else "empty")
        self.model = self.cp.doc

    def delete_existing(self):
        self.cp.delete_existing(AIRCRAFT_PREFIXES, (AIRCRAFT_PARAM_SET,))

    def build(self, cfg, ac, save_path: str | None = None) -> bool:
        from aircraft_sw import equation_values
        cp = self.cp
        self.open_part()
        self.delete_existing()
        with cp.batch():
            print("  wing ...")
            sets = [build_wing(cp, ac.wing_sections, mirror=True)]
            print("  fuselage ...")
            sets.append(build_fuselage(cp, ac.fp, ac.fus_lay, ac.stations))
            print("  tail ...")
            sets.append(build_tail(cp, ac.tail_sections))
            cp.write_parameters(AIRCRAFT_PARAM_SET, equation_values(cfg, ac))
            cp.update()
            cp.hide(sets)
        cp.show_isometric()
        n = cp.body.Shapes.Count
        print(f"  solid features       : {n} (all merged in {cp.body.Name})")
        cp.report()
        saved = True
        if save_path:
            saved = self.save_as(save_path)
        print("Done.")
        return saved

    def save_as(self, path, tries=3) -> bool:
        return self.cp.save_as(path, tries)

    def close(self):
        self.cp.close()
        self.model = None
