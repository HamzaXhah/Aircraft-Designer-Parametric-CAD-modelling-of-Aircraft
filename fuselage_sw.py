"""
Parametric UAV fuselage generator for SOLIDWORKS (pywin32 / COM).

Inputs  : fuselage length L and maximum diameter D (mm)
Derived : R, fineness ratio, Ln, Lm, Lt, re, max boattail angle, volumes
Nose    : "ellipse" or "vonkarman" (LD-Haack), chosen with NOSE_TYPE / --nose

Profile (half-section, drawn on the Front plane, x = fuselage axis, y = radius):
    nose curve -> cylinder line -> cubic tail cone -> end cap -> axis line
then revolved 360 deg about the axis line.

Usage:
    python fuselage_sw.py                      # uses defaults below
    python fuselage_sw.py -L 4000 -D 500 --nose vonkarman
    python fuselage_sw.py -L 4000 -D 500 --save C:\\work\\fuselage.SLDPRT

Requires: Windows, SOLIDWORKS, pywin32 (pip install pywin32)
"""

import argparse
import math

# ======================= USER INPUTS ========================================
LENGTH_MM = 3500.0       # fuselage length L
DIAMETER_MM = 500.0      # max diameter D
NOSE_TYPE = "ellipse"    # "ellipse" or "vonkarman"

# ======================= DESIGN RATIOS (derived geometry) ===================
KN = 1.5                 # nose length    Ln = KN * D
KT = 1.5                # tail length    Lt = KT * D
KE = 0.55                 # tail end radius re = KE * R  (0 -> pointed tail)
N_NOSE_PTS = 31          # spline fit points for the nose
N_TAIL_PTS = 25          # spline fit points for the tail cone
ADD_TANGENCY = True      # add tangent relations at the nose/tail junctions
MAX_BOATTAIL_DEG = 15.0  # warning threshold for the tail closing angle

FEATURE_NAME = "Fuselage"
SKETCH_NAME = "Fuselage_Profile"

# SOLIDWORKS constants
SW_DOC_PART = 1
SW_DEFAULT_TEMPLATE_PART = 8
SW_DELETE_ABSORBED_AND_CHILDREN = 3
SW_SAVE_AS_CURRENT_VERSION = 0
SW_SAVE_AS_OPTIONS_SILENT = 1
MARK_PROFILE = 0
MARK_REVOLVE_AXIS = 16


# ============================================================================
# 1. Derived parameters (pure Python, no CAD needed)
# ============================================================================
def derive_parameters(L, D, kn=KN, kt=KT, ke=KE):
    """Return a dict of all derived fuselage parameters (same units as L, D)."""
    if L <= 0 or D <= 0:
        raise ValueError("Length and diameter must be positive.")

    R = D / 2.0
    fineness = L / D
    notes = []

    # Nose + tail must fit inside L; otherwise shrink them proportionally
    if kn + kt > fineness:
        scale = fineness / (kn + kt)
        notes.append(
            f"L/D = {fineness:.2f} < kn + kt = {kn + kt:.2f}: nose and tail "
            f"scaled by {scale:.3f}, no cylindrical mid-body."
        )
        kn, kt = kn * scale, kt * scale

    Ln = kn * D
    Lt = kt * D
    Lm = max(0.0, L - Ln - Lt)
    re = ke * R

    # Max tail slope of the cubic blend occurs at s = 0.5
    beta_max = math.degrees(math.atan(1.5 * (R - re) / Lt))
    if beta_max > MAX_BOATTAIL_DEG:
        notes.append(
            f"Max boattail angle {beta_max:.1f} deg exceeds "
            f"{MAX_BOATTAIL_DEG:.0f} deg: flow separation likely, increase KT."
        )

    # Volumes (analytic)
    V_mid = math.pi * R**2 * Lm
    V_tail = math.pi * Lt * (re**2 + re * (R - re) + (13.0 / 35.0) * (R - re) ** 2)
    V_nose_ellipse = (2.0 / 3.0) * math.pi * R**2 * Ln
    V_nose_vk = 0.5 * math.pi * R**2 * Ln          # Von Karman: integral of r^2 dx = R^2 Ln / 2

    return dict(
        L=L, D=D, R=R, fineness=fineness,
        kn=kn, kt=kt, ke=ke,
        Ln=Ln, Lm=Lm, Lt=Lt, re=re,
        x_nose_end=Ln, x_tail_start=Ln + Lm,
        beta_max_deg=beta_max,
        V_mid=V_mid, V_tail=V_tail,
        V_nose={"ellipse": V_nose_ellipse, "vonkarman": V_nose_vk},
        notes=notes,
    )


# ============================================================================
# 2. Profile point generation
# ============================================================================
def nose_points(p, nose_type, n=N_NOSE_PTS):
    """(x, r) points from tip (0,0) to (Ln, R). Clustered near the tip."""
    R, Ln = p["R"], p["Ln"]
    pts = []
    if nose_type == "ellipse":
        # Exact parameterisation: x = Ln(1 - cos phi), r = R sin phi
        for i in range(n):
            phi = (math.pi / 2.0) * i / (n - 1)
            pts.append((Ln * (1.0 - math.cos(phi)), R * math.sin(phi)))
    elif nose_type == "vonkarman":
        # LD-Haack: theta in [0, pi], x = Ln(1 - cos theta)/2
        for i in range(n):
            th = math.pi * i / (n - 1)
            x = Ln * (1.0 - math.cos(th)) / 2.0
            r = R / math.sqrt(math.pi) * math.sqrt(max(0.0, th - math.sin(2 * th) / 2.0))
            pts.append((x, r))
    else:
        raise ValueError("nose_type must be 'ellipse' or 'vonkarman'")
    pts[0] = (0.0, 0.0)        # exact tip on axis
    pts[-1] = (Ln, R)          # exact junction
    return pts


def tail_points(p, n=N_TAIL_PTS):
    """(x, r) points from (Ln+Lm, R) to (L, re) using the cubic smoothstep."""
    R, re, Lt, x0 = p["R"], p["re"], p["Lt"], p["x_tail_start"]
    pts = []
    for i in range(n):
        s = i / (n - 1)
        h = 1.0 - 3.0 * s**2 + 2.0 * s**3
        pts.append((x0 + s * Lt, re + (R - re) * h))
    pts[0] = (x0, R)
    pts[-1] = (p["L"], re)
    return pts


def numeric_volume(p, nose_type):
    """Trapezoidal check of the analytic volume (uses dense sampling)."""
    def integrate(pts):
        return sum(
            math.pi * 0.5 * (r1**2 + r2**2) * (x2 - x1)
            for (x1, r1), (x2, r2) in zip(pts, pts[1:])
        )
    return (integrate(nose_points(p, nose_type, 4001))
            + p["V_mid"] + integrate(tail_points(p, 4001)))


def print_summary(p, nose_type):
    V_total = p["V_nose"][nose_type] + p["V_mid"] + p["V_tail"]
    print("=" * 56)
    print(f" UAV fuselage  |  nose: {nose_type}")
    print("=" * 56)
    print(f" Length L            : {p['L']:10.1f} mm")
    print(f" Diameter D          : {p['D']:10.1f} mm   (R = {p['R']:.1f} mm)")
    print(f" Fineness ratio L/D  : {p['fineness']:10.2f}")
    print(f" kn / kt / ke        : {p['kn']:.3f} / {p['kt']:.3f} / {p['ke']:.3f}")
    print(f" Nose length Ln      : {p['Ln']:10.1f} mm")
    print(f" Mid-body length Lm  : {p['Lm']:10.1f} mm")
    print(f" Tail length Lt      : {p['Lt']:10.1f} mm")
    print(f" Tail end radius re  : {p['re']:10.1f} mm")
    print(f" Max boattail angle  : {p['beta_max_deg']:10.2f} deg")
    print(f" Volume (analytic)   : {V_total * 1e-9:10.4f} m^3")
    for n in p["notes"]:
        print(f" NOTE: {n}")
    print("=" * 56)


# ============================================================================
# 3. SOLIDWORKS COM helpers
# ============================================================================
def connect_solidworks():
    import win32com.client
    try:
        sw = win32com.client.GetActiveObject("SldWorks.Application")
        print("Connected to running SOLIDWORKS session.")
    except Exception:
        print("Starting SOLIDWORKS ...")
        sw = win32com.client.Dispatch("SldWorks.Application")
    sw.Visible = True
    return sw


def get_or_create_part(sw):
    """Use the active part; if no part is active, create a new one."""
    model = sw.ActiveDoc
    if model is not None and call_com_method(model, "GetType") == SW_DOC_PART:
        print(f"Using active part: {call_com_method(model, 'GetTitle')}")
        return model

    template = sw.GetUserPreferenceStringValue(SW_DEFAULT_TEMPLATE_PART)
    if not template:
        raise RuntimeError(
            "No default part template set. Set it in Tools > Options > "
            "System Options > Default Templates."
        )
    model = sw.NewDocument(template, 0, 0.0, 0.0)
    if model is None:
        raise RuntimeError(f"Could not create a new part from template:\n{template}")
    print("Created new part document.")
    return model


def null_dispatch():
    import pythoncom
    import win32com.client
    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


def double_array(values):
    import pythoncom
    import win32com.client
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(values))


def call_com_method(obj, name):
    """Call a zero-argument COM method that may look like a property to pywin32."""
    from win32com.client.dynamic import CDispatch

    # SolidWorks sometimes exposes no-argument methods through IDispatch as
    # properties. Flag them as methods before invocation so the dynamic wrapper
    # does not try to call the returned COM object's default member instead.
    if isinstance(obj, CDispatch):
        obj._FlagAsMethod(name)
    try:
        return getattr(obj, name)()
    except Exception as exc:
        raise RuntimeError(f"SOLIDWORKS COM method '{name}' failed: {exc}") from exc


def find_feature(model, name):
    feat = call_com_method(model, "FirstFeature")
    while feat is not None:
        if feat.Name == name:
            return feat
        feat = call_com_method(feat, "GetNextFeature")
    return None


def first_reference_plane(model):
    """Front plane = first RefPlane in the tree (locale-independent)."""
    feat = call_com_method(model, "FirstFeature")
    while feat is not None:
        if call_com_method(feat, "GetTypeName2") == "RefPlane":
            return feat
        feat = call_com_method(feat, "GetNextFeature")
    raise RuntimeError("No reference plane found in the part.")


def delete_existing(model):
    """Remove a previous fuselage (feature + absorbed sketch) for a clean rebuild."""
    for name in (FEATURE_NAME, SKETCH_NAME):
        feat = find_feature(model, name)
        if feat is not None:
            model.ClearSelection2(True)
            feat.Select2(False, 0)
            model.Extension.DeleteSelection2(SW_DELETE_ABSORBED_AND_CHILDREN)
            print(f"Deleted existing '{name}'.")
    model.ClearSelection2(True)


def add_tangent(model, seg_a, seg_b):
    stage = "clearing the current selection"
    try:
        model.ClearSelection2(True)
        stage = "creating selection data"
        sd = call_com_method(model.SelectionManager, "CreateSelectData")
        if sd is None:
            raise RuntimeError("SOLIDWORKS returned no selection data")
        stage = "selecting the first sketch segment"
        if not seg_a.Select4(False, sd):
            raise RuntimeError("the first sketch segment could not be selected")
        stage = "selecting the second sketch segment"
        if not seg_b.Select4(True, sd):
            raise RuntimeError("the second sketch segment could not be selected")
        stage = "adding the tangent constraint"
        model.SketchAddConstraints("sgTANGENT")
    except Exception as exc:
        print(f"  [warn] tangent relation skipped while {stage}: {exc}")
    finally:
        try:
            model.ClearSelection2(True)
        except Exception as exc:
            print(f"  [warn] could not clear selection after tangent operation: {exc}")


def write_global_variables(model, p, nose_type):
    """Record inputs and derived values in the Equation Manager (reference only)."""
    stage = "getting the Equation Manager"
    try:
        eqm = call_com_method(model, "GetEquationMgr")
        names = {
            "Fus_L": f"{p['L']:.4f}mm", "Fus_D": f"{p['D']:.4f}mm",
            "Fus_Ln": f"{p['Ln']:.4f}mm", "Fus_Lm": f"{p['Lm']:.4f}mm",
            "Fus_Lt": f"{p['Lt']:.4f}mm", "Fus_re": f"{p['re']:.4f}mm",
            "Fus_LD": f"{p['fineness']:.4f}",
            "Fus_Nose": "1" if nose_type == "ellipse" else "2",
        }
        # remove old entries with the same names
        stage = "counting existing equations"
        equation_count = call_com_method(eqm, "GetCount")
        for i in range(equation_count - 1, -1, -1):
            stage = f"checking existing equation {i}"
            eq = eqm.Equation(i)
            if any(eq.startswith(f'"{n}"') for n in names):
                stage = f"deleting existing equation {i}"
                eqm.Delete(i)
        for n, v in names.items():
            stage = f"writing global variable {n}"
            added_index = eqm.Add2(-1, f'"{n}" = {v}', False)
            if added_index == -1:
                raise RuntimeError("Equation Manager rejected the equation")
        stage = "evaluating equations"
        call_com_method(eqm, "EvaluateAll")
        if eqm.Status == -1:
            raise RuntimeError("Equation Manager reported an equation evaluation error")
    except Exception as exc:
        print(f"  [warn] global variables not written while {stage}: {exc}")


# ============================================================================
# 4. Build geometry
# ============================================================================
def build_fuselage(model, p, nose_type):
    mm = 1e-3  # SOLIDWORKS API works in metres
    skm = model.SketchManager
    ext = model.Extension

    delete_existing(model)

    # --- sketch on Front plane ----------------------------------------------
    plane = first_reference_plane(model)
    model.ClearSelection2(True)
    plane.Select2(False, 0)
    skm.InsertSketch(True)

    skm.AddToDB = True              # no snapping / inference while drawing
    skm.DisplayWhenAdded = False

    def spline(pts):
        data = []
        for x, r in pts:
            data += [x * mm, r * mm, 0.0]
        seg = skm.CreateSpline2(double_array(data), True)
        if seg is None:
            raise RuntimeError("Spline creation failed.")
        return seg

    def line(x1, y1, x2, y2):
        seg = skm.CreateLine(x1 * mm, y1 * mm, 0.0, x2 * mm, y2 * mm, 0.0)
        if seg is None:
            raise RuntimeError("Line creation failed.")
        return seg

    L, R, re = p["L"], p["R"], p["re"]
    x_ne, x_ts = p["x_nose_end"], p["x_tail_start"]

    nose = spline(nose_points(p, nose_type))
    mid = line(x_ne, R, x_ts, R) if p["Lm"] > 1e-6 else None
    tail = spline(tail_points(p))
    cap = line(L, re, L, 0.0) if re > 1e-6 else None
    axis = line(L, 0.0, 0.0, 0.0)   # closing edge on the axis = revolve axis

    skm.AddToDB = False
    skm.DisplayWhenAdded = True

    if ADD_TANGENCY:
        if mid is not None:
            add_tangent(model, nose, mid)
            add_tangent(model, mid, tail)
        else:
            add_tangent(model, nose, tail)

    # IDispatch exposes GetName as a string property for sketch segments.
    axis_name = axis.GetName
    skm.InsertSketch(True)           # exit sketch

    sketch_feat = model.FeatureByPositionReverse(0)
    sketch_feat.Name = SKETCH_NAME

    # --- revolve -------------------------------------------------------------
    model.ClearSelection2(True)
    ok1 = ext.SelectByID2(SKETCH_NAME, "SKETCH", 0, 0, 0, False,
                          MARK_PROFILE, null_dispatch(), 0)
    ok2 = ext.SelectByID2(f"{axis_name}@{SKETCH_NAME}", "EXTSKETCHSEGMENT",
                          0, 0, 0, True, MARK_REVOLVE_AXIS, null_dispatch(), 0)
    if not (ok1 and ok2):
        raise RuntimeError("Could not select sketch / revolve axis.")

    feat = model.FeatureManager.FeatureRevolve2(
        True, True, False, False, False, False,   # single dir, solid, not thin/cut
        0, 0, 2.0 * math.pi, 0.0,                  # blind, 360 deg
        False, False, 0.0, 0.0,
        0, 0.0, 0.0,
        True, True, True,                          # merge, feat scope, auto-select
    )
    if feat is None:
        raise RuntimeError("Revolve failed - check the sketch for open contours.")
    feat.Name = FEATURE_NAME

    write_global_variables(model, p, nose_type)

    model.ClearSelection2(True)
    model.ForceRebuild3(False)
    model.ShowNamedView2("*Isometric", 7)
    call_com_method(model, "ViewZoomtofit2")
    print(f"Created feature '{FEATURE_NAME}'.")
    return feat


def report_mass_props(model):
    """Volume / wetted area from SOLIDWORKS for comparison with the analytic value."""
    try:
        mp = call_com_method(model.Extension, "CreateMassProperty")
        if mp is None:
            raise RuntimeError("SOLIDWORKS did not return a mass-property object")
        print(f" SOLIDWORKS volume   : {mp.Volume:10.4f} m^3")
        print(f" Wetted surface area : {mp.SurfaceArea:10.4f} m^2")
    except Exception as exc:
        print(f"  [warn] mass properties unavailable: {exc}")


# ============================================================================
# 5. Main
# ============================================================================
def main():
    ap = argparse.ArgumentParser(description="Parametric UAV fuselage for SOLIDWORKS")
    ap.add_argument("-L", "--length", type=float, default=LENGTH_MM, help="length [mm]")
    ap.add_argument("-D", "--diameter", type=float, default=DIAMETER_MM, help="diameter [mm]")
    ap.add_argument("--nose", choices=["ellipse", "vonkarman"], default=NOSE_TYPE)
    ap.add_argument("--save", default=None, help="optional .SLDPRT path to save to")
    ap.add_argument("--dry-run", action="store_true", help="print parameters only")
    args = ap.parse_args()

    p = derive_parameters(args.length, args.diameter)
    print_summary(p, args.nose)
    if args.dry_run:
        print(f" Volume (numeric)    : {numeric_volume(p, args.nose) * 1e-9:10.4f} m^3")
        return

    sw = connect_solidworks()
    model = get_or_create_part(sw)
    build_fuselage(model, p, args.nose)
    report_mass_props(model)

    if args.save:
        errs = model.SaveAs3(args.save, SW_SAVE_AS_CURRENT_VERSION, SW_SAVE_AS_OPTIONS_SILENT)
        print(f"Saved to {args.save}" if errs == 0 else f"Save returned error code {errs}")


if __name__ == "__main__":
    main()
