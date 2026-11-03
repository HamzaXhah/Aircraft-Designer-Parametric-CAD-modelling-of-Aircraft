"""Create a 100 x 60 x 20 mm block through the SOLIDWORKS COM API."""
from pathlib import Path
import sys

import pythoncom
import pywintypes
import win32com.client

from connect import connect

LENGTH_MM = 100.0
WIDTH_MM = 60.0
HEIGHT_MM = 20.0


def com_value(obj, name):
    """Handle no-argument methods exposed as properties by late binding."""
    value = getattr(obj, name)
    return value() if callable(value) and not hasattr(value, "_oleobj_") else value


def create_block():
    sw_app = connect()
    # swDefaultTemplatePart = 8. Create a fresh document for this block.
    template = sw_app.GetUserPreferenceStringValue(8)
    if not template or not Path(template).is_file():
        installed = sorted(Path("C:/ProgramData/SOLIDWORKS").glob("*/templates/Part.PRTDOT"))
        template = str(installed[-1]) if installed else ""
    if not template or not Path(template).is_file():
        raise RuntimeError(
            "Set a valid Part template in SOLIDWORKS: "
            "Tools > Options > System Options > Default Templates."
        )
    part = sw_app.NewDocument(template, 0, 0.0, 0.0)
    if part is None:
        raise RuntimeError("SOLIDWORKS could not create a new part from the template.")

    # Find a reference plane by type so localized plane names also work.
    plane = com_value(part, "FirstFeature")
    while plane is not None:
        if com_value(plane, "GetTypeName2") == "RefPlane":
            break
        plane = com_value(plane, "GetNextFeature")
    if plane is None or not plane.Select2(False, 0):
        raise RuntimeError("Could not select a reference plane for the sketch.")

    sketch_manager = part.SketchManager
    sketch_manager.InsertSketch(True)
    # SOLIDWORKS API lengths are in meters.
    segments = sketch_manager.CreateCornerRectangle(
        0.0, 0.0, 0.0, LENGTH_MM / 1000.0, WIDTH_MM / 1000.0, 0.0
    )
    if segments is None:
        raise RuntimeError("Rectangle creation failed.")
    # Extrude the active sketch; SOLIDWORKS exits sketch mode automatically.

    extrusion = part.FeatureManager.FeatureExtrusion2(
        True, False, False, 0, 0, HEIGHT_MM / 1000.0, 0.0,
        False, False, False, False, 0.0, 0.0,
        False, False, False, False, True, True, True, 0, 0.0, False
    )
    if extrusion is None:
        raise RuntimeError("Block extrusion failed; the new part remains open for inspection.")
    extrusion.Name = "SimpleBlock"
    part.ClearSelection2(True)
    part.ShowNamedView2("", 7)  # swIsometricView
    com_value(part, "ViewZoomtofit2")

    bodies = part.GetBodies2(0, False)  # swSolidBody
    if not bodies or len(bodies) != 1:
        raise RuntimeError("Expected exactly one solid body after extrusion.")

    output = Path(__file__).resolve().with_name("simple_block.SLDPRT")
    index = 1
    while output.exists():
        output = output.with_name(f"simple_block_{index}.SLDPRT")
        index += 1
    errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    null_dispatch = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
    saved = part.Extension.SaveAs(str(output), 0, 1, null_dispatch, errors, warnings)
    if not saved or errors.value or not output.is_file():
        raise RuntimeError(f"Could not save the block: COM save error {errors.value}.")
    print(f"Created one solid block: {LENGTH_MM:g} x {WIDTH_MM:g} x {HEIGHT_MM:g} mm")
    print(f"Saved: {output}")
    if warnings.value:
        print(f"SOLIDWORKS save warnings: {warnings.value}")
    return part


if __name__ == "__main__":
    try:
        create_block()
    except (pywintypes.com_error, RuntimeError) as error:
        print(f"Geometry creation failed: {error}", file=sys.stderr)
        sys.exit(1)
