# SolidWorks API calls used by the aircraft generator

These are all the SolidWorks API calls (through pywin32 COM) that the program
makes to build the aircraft. They are listed in the order they run during a
complete aircraft build: `SolidWorksAircraftBuilder.build()` in
[`aircraft_sw.py`](aircraft_sw.py), which uses the wing, fuselage and tail
builders in [`wing_solidworks.py`](wing_solidworks.py),
[`fuselage_sw.py`](fuselage_sw.py) and [`tail_sw.py`](tail_sw.py). The GUI,
the design sweep and `aircraft_sw.py --cad solidworks` all go through this
path. The differences in the standalone component scripts are listed at the end.

- **Units:** the SolidWorks API works in **metres**. Section points are
  computed in mm and multiplied by `MM = 1e-3` before they are passed in.
- **Interfaces:** `ISldWorks` is the application, `IModelDoc2` is the part
  document and `IFeature` is a feature in the FeatureManager tree.

| Constant | Value | Meaning |
|---|---|---|
| `SW_DOC_PART` | 1 | `swDocPART`, the document type of a part |
| `SW_DEFAULT_TEMPLATE_PART` | 8 | `swDefaultTemplatePart`, the user preference that holds the default part template |
| `SW_REFPLANE_DISTANCE` | 8 | `swRefPlaneReferenceConstraint_Distance`, an offset plane |
| `SW_REFPLANE_FLIP` | 256 | `swRefPlaneReferenceConstraint_OptionFlip`, offset to the other side |
| `SW_DELETE_ABSORBED_AND_CHILDREN` | 3 | delete a feature with its absorbed features and children |
| `SW_SOLID_BODY` | 0 | `swSolidBody`, the body type for `GetBodies2` |
| `SW_SAVEAS_SILENT` | 1 | `swSaveAsOptions_Silent`, save without dialogs |

---

## 0. Check before the build (GUI)

| # | Call | Code | Explanation |
|---|---|---|---|
| 0.1 | `winreg.OpenKey(HKEY_CLASSES_ROOT, "SldWorks.Application")` | `aircraft_sw.cad_installed` | Checks that SolidWorks is registered for COM automation; this is a Windows registry call, not a SolidWorks one. |

## 1. Connect to SolidWorks

| # | Call | Code | Explanation |
|---|---|---|---|
| 1.1 | `win32com.client.Dispatch("SldWorks.Application")` | `SolidWorksWingBuilder.__init__` | Connects to the running SolidWorks session, or starts one, and returns the `ISldWorks` application object. |
| 1.2 | `ISldWorks.Visible = True` | `SolidWorksWingBuilder.__init__` | Shows the SolidWorks window so the build can be watched. |
| 1.3 | `win32com.client.VARIANT(VT_DISPATCH, None)` | all three builders | Creates the null `Callout` argument that `SelectByID2` needs. |
| 1.4 | `win32com.client.GetActiveObject("SldWorks.Application")` | `SolidWorksFuselageBuilder.__init__`, `SolidWorksTailBuilder.__init__` | Attaches the fuselage and tail builders to the same running session (they fall back to `Dispatch` if none is running). |
| 1.5 | `CDispatch._FlagAsMethod(name)` | `_call_com_method` / `_call` | pywin32 helper that makes zero-argument SolidWorks methods (`FirstFeature`, `GetTitle`, ...) be called as methods instead of read as properties. |

## 2. Open or create the part

| # | Call | Code | Explanation |
|---|---|---|---|
| 2.1 | `ISldWorks.ActiveDoc` | `SolidWorksAircraftBuilder.open_part` | Gets the active document, so an aircraft already in it can be rebuilt (skipped with `--new-part`). |
| 2.2 | `IModelDoc2.GetType()` | `open_part` | Checks that the active document is a part (`swDocPART = 1`). |
| 2.3 | `IModelDoc2.FirstFeature()` | `_features` | Gets the first feature in the FeatureManager tree, to start walking it. |
| 2.4 | `IFeature.GetNextFeature()` | `_features` | Moves to the next feature in the tree. |
| 2.5 | `IFeature.Name` | `_ours` | Reads a feature's name to find the aircraft features (`Wing_`, `Fus_`, `Fuselage_`, `Tail_`). |
| 2.6 | `IModelDoc2.GetTitle()` | `open_part` | Gets the document title for the "Rebuilding aircraft in active part" message. |
| 2.7 | `ISldWorks.GetUserPreferenceStringValue(8)` | `open_part` | Reads the path of the default part template (Tools > Options > Default Templates). |
| 2.8 | `ISldWorks.NewDocument(template, 0, 0, 0)` | `open_part` | Creates a new part document from that template. |
| 2.9 | `IModelDoc2.Extension` | `attach` | Gets the `IModelDocExtension` object (selection, delete, mass properties). |
| 2.10 | `IModelDoc2.FeatureManager` | `attach` | Gets the `IFeatureManager` object that creates planes, lofts and mirrors. |
| 2.11 | `IModelDoc2.SketchManager` | `attach` | Gets the `ISketchManager` object that creates sketches and sketch entities. |
| 2.12 | `IFeature.GetTypeName2()` | `_default_planes` | Finds the first three `"RefPlane"` features: the Front, Top and Right planes. |

## 3. Delete the previous aircraft (rebuild in the active part)

Repeated for each aircraft feature, last one first.

| # | Call | Code | Explanation |
|---|---|---|---|
| 3.1 | `IModelDoc2.ClearSelection2(True)` | `delete_existing` | Clears the current selection. |
| 3.2 | `IFeature.Select2(False, 0)` | `delete_existing` | Selects the old feature. |
| 3.3 | `IModelDocExtension.DeleteSelection2(3)` | `delete_existing` | Deletes the selected feature with its absorbed features and children. |

## 4. Start the build

| # | Call | Code | Explanation |
|---|---|---|---|
| 4.1 | `ISldWorks.CommandInProgress = True` | `build` | Tells SolidWorks a long API operation is running, which suspends screen updates and makes the API calls much faster. |

## 5. Wing

Done for each wing station `i` (root to tip), then the loft and the mirror.

### 5a. Station plane (`SolidWorksWingBuilder.offset_plane`, stations 1..n; station 0 uses the Front Plane)

| # | Call | Explanation |
|---|---|---|
| 5.1 | `IModelDoc2.ClearSelection2(True)` | Clears the selection. |
| 5.2 | `IFeature.Select2(False, 0)` (Front Plane) | Selects the Front Plane as the plane's reference. |
| 5.3 | `IFeatureManager.InsertRefPlane(8, z, 0, 0, 0, 0)` | Creates a reference plane offset from the Front Plane by the station's spanwise distance z. |
| 5.4 | `IFeature.GetSpecificFeature2()` | Gets the `IRefPlane` object behind the plane feature. |
| 5.5 | `IRefPlane.Transform.ArrayData[11]` | Reads the plane's Z translation to check that it went to +Z. |
| 5.6 | `IModelDoc2.EditDelete()` | Deletes the plane if it went to -Z ... |
| 5.7 | `IFeatureManager.InsertRefPlane(8 \| 256, z, 0, 0, 0, 0)` | ... and recreates it with the flip option. |
| 5.8 | `IFeature.Name = "Wing_Station_i"` | Names the plane. |

### 5b. Airfoil section sketch (`SolidWorksWingBuilder.sketch_section`)

| # | Call | Explanation |
|---|---|---|
| 5.9 | `IModelDoc2.ClearSelection2(True)` | Clears the selection. |
| 5.10 | `IFeature.Select2(False, 0)` (station plane) | Selects the plane to sketch on. |
| 5.11 | `ISketchManager.InsertSketch(True)` | Opens a new sketch on the selected plane. |
| 5.12 | `ISketchManager.ActiveSketch` | Gets the `ISketch` that is now open. |
| 5.13 | `ISketch.ModelToSketchTransform.ArrayData` | Gets the 3-D model to 2-D sketch transform, used to convert the section points into sketch coordinates. |
| 5.14 | `ISketchManager.AddToDB = True` | Adds entities straight to the database, with no snapping or automatic relations. |
| 5.15 | `ISketchManager.DisplayWhenAdded = False` | Skips redrawing each entity as it is added (faster). |
| 5.16 | `ISketchManager.CreateSpline2(points, False)` | Creates the upper-surface spline (trailing edge to leading edge) through the points (a `VT_ARRAY \| VT_R8` VARIANT). |
| 5.17 | `ISketchManager.CreateSpline2(points, False)` | Creates the lower-surface spline (leading edge to trailing edge). |
| 5.18 | `ISketchManager.CreateLine(x1, y1, 0, x2, y2, 0)` | Closes a blunt trailing edge with a straight line (only if the trailing edge is not sharp). |
| 5.19 | `ISketchManager.AddToDB = False` | Restores normal sketching. |
| 5.20 | `ISketchManager.DisplayWhenAdded = True` | Restores normal display. |
| 5.21 | `ISketchManager.InsertSketch(True)` | Closes the sketch. |
| 5.22 | `IModelDoc2.FeatureByPositionReverse(0)` | Gets the feature that was just created (the sketch). |
| 5.23 | `IFeature.Name = "Wing_Section_i"` | Names the sketch. |

### 5c. Wing loft (`SolidWorksWingBuilder.loft`)

| # | Call | Explanation |
|---|---|---|
| 5.24 | `IModelDoc2.ClearSelection2(True)` | Clears the selection. |
| 5.25 | `IModelDocExtension.SelectByID2(name, "SKETCH", x, y, z, True, 1, None, 0)` | Adds each section sketch to the selection with mark 1 (a loft profile), picked near the trailing edge so the loft connectors line up. |
| 5.26 | `IFeatureManager.InsertProtrusionBlend2(...)` | Creates the boss loft through the selected profiles, with "merge result" on. |
| 5.27 | `IFeature.Name = "Wing_Loft"` | Names the loft. |

### 5d. Mirror the half wing (`SolidWorksWingBuilder.mirror_body`)

| # | Call | Explanation |
|---|---|---|
| 5.28 | `IModelDoc2.GetBodies2(0, False)` | Gets the solid bodies in the part (the half wing). |
| 5.29 | `IModelDoc2.ClearSelection2(True)` | Clears the selection. |
| 5.30 | `IFeature.Select2(False, 2)` (Front Plane) | Selects the Front Plane with mark 2: the mirror plane. |
| 5.31 | `IModelDoc2.SelectionManager` | Gets the `ISelectionMgr` object. |
| 5.32 | `ISelectionMgr.CreateSelectData()` | Creates an `ISelectData` object for a marked selection. |
| 5.33 | `ISelectData.Mark = 256` | Sets mark 256: bodies to mirror. |
| 5.34 | `IBody2.Select2(True, selectData)` | Adds the wing body to the selection with that mark. |
| 5.35 | `IFeatureManager.InsertMirrorFeature2(True, False, True, False, 0)` | Mirrors the body about the plane and merges the two halves into one solid. |
| 5.36 | `IFeature.Name = "Wing_Mirror"` | Names the mirror. |

## 6. Fuselage

### 6a. Nose tip point (`SolidWorksFuselageBuilder.sketch_point`, on the Right Plane)

| # | Call | Explanation |
|---|---|---|
| 6.1 | `IModelDoc2.ClearSelection2(True)` | Clears the selection. |
| 6.2 | `IFeature.Select2(False, 0)` (Right Plane) | Selects the Right Plane (x = 0, the nose tip). |
| 6.3 | `ISketchManager.InsertSketch(True)` | Opens a sketch on it. |
| 6.4 | `ISketchManager.ActiveSketch` | Gets the open sketch. |
| 6.5 | `ISketch.ModelToSketchTransform.ArrayData` | Gets the model-to-sketch transform for the point. |
| 6.6 | `ISketchManager.AddToDB = True` | Turns off snapping and automatic relations. |
| 6.7 | `ISketchManager.CreatePoint(x, y, 0)` | Creates the nose tip point, which the loft starts from. |
| 6.8 | `ISketchManager.AddToDB = False` | Restores normal sketching. |
| 6.9 | `ISketchManager.InsertSketch(True)` | Closes the sketch. |
| 6.10 | `IModelDoc2.FeatureByPositionReverse(0)` | Gets the new sketch feature. |
| 6.11 | `IFeature.Name = "Fus_NoseTip"` | Names it. |

### 6b. Station planes and cross-sections (for each station `i`)

| # | Call | Explanation |
|---|---|---|
| 6.12 | `IModelDoc2.ClearSelection2(True)` + `IFeature.Select2(False, 0)` (Right Plane) | Selects the Right Plane as the plane's reference. |
| 6.13 | `IFeatureManager.InsertRefPlane(8, x, 0, 0, 0, 0)` | Creates a plane at the station's distance x aft of the nose. |
| 6.14 | `IFeature.GetSpecificFeature2().Transform.ArrayData[9]` | Reads the plane's X translation to check that it went aft (+X). |
| 6.15 | `IModelDoc2.EditDelete()` + `InsertRefPlane(8 \| 256, ...)` | Deletes the plane and recreates it flipped if it went forward. |
| 6.16 | `IFeature.Name = "Fus_Station_i"` | Names the plane. |
| 6.17 | `IFeature.Select2` + `ISketchManager.InsertSketch(True)` | Opens a sketch on the station plane. |
| 6.18 | `ISketchManager.ActiveSketch.ModelToSketchTransform.ArrayData` | Gets the transform for the section points. |
| 6.19 | `ISketchManager.AddToDB = True`, `DisplayWhenAdded = False` | Fast entity creation with no automatic relations. |
| 6.20 | `ISketchManager.CreateSpline2(points, False)` | Creates the closed superellipse cross-section as one spline (the first and last points are the same). |
| 6.21 | `ISketchManager.CreateSpline2(...)` x2 | Fallback: two half splines that meet at the side points, if the closed spline fails. |
| 6.22 | `ISketchManager.AddToDB = False`, `DisplayWhenAdded = True` | Restores normal sketching. |
| 6.23 | `ISketchManager.InsertSketch(True)` | Closes the sketch. |
| 6.24 | `IModelDoc2.FeatureByPositionReverse(0)` + `IFeature.Name = "Fus_Section_i"` | Gets and names the section sketch. |

### 6c. Pointed tail end (only when the tail end has no width or height)

| # | Call | Explanation |
|---|---|---|
| 6.25 | `IFeatureManager.InsertRefPlane(8, L, 0, 0, 0, 0)` (+ the check in 6.14-6.15) | Creates the plane `Fus_Station_end` at the fuselage length L. |
| 6.26 | `ISketchManager.InsertSketch` / `CreatePoint` / `InsertSketch` | Sketches the tail end point `Fus_TailTip`, which the loft ends at. |

### 6d. Fuselage loft (`SolidWorksFuselageBuilder.loft`)

| # | Call | Explanation |
|---|---|---|
| 6.27 | `IModelDoc2.ClearSelection2(True)` | Clears the selection. |
| 6.28 | `IModelDocExtension.SelectByID2(name, "SKETCH", x, y, z, True, 1, None, 0)` | Selects the nose point, each section and the tail point in order as loft profiles (mark 1); sections are picked at their +Z side point. |
| 6.29 | `IFeatureManager.InsertProtrusionBlend2(...)` | Creates the fuselage loft and merges it with the wing. |
| 6.30 | `SelectByID2` + `InsertProtrusionBlend2` again | Retries without the tip point sketches if the first loft fails. |
| 6.31 | `IFeature.Name = "Fuselage_Loft"` | Names the loft. |

## 7. Tail

For each tail panel side (for example `HStab_R`, `HStab_L`, `VStab`, or the V-tail
and twin-fin panels), with a root and a tip section.

### 7a. Section plane (`SolidWorksTailBuilder.plane`)

| # | Call | Explanation |
|---|---|---|
| 7.1 | `IModelDoc2.ClearSelection2(True)` + `IFeature.Select2(False, 0)` | Selects the Front Plane (horizontal panels, offset in z) or the Top Plane (vertical fins, offset in y). |
| 7.2 | `IFeatureManager.InsertRefPlane(8, d, 0, 0, 0, 0)` | Creates the plane offset by the section's distance d; planes at the same position are reused. |
| 7.3 | `IFeature.GetSpecificFeature2().Transform.ArrayData[11 or 10]` | Reads the Z (Front) or Y (Top) translation to check the side. |
| 7.4 | `IModelDoc2.EditDelete()` + `InsertRefPlane(8 \| 256, ...)` | Recreates the plane flipped if it went to the wrong side. |
| 7.5 | `IFeature.Name = "Tail_Plane_n"` | Names the plane. |

### 7b. Section sketch (`SolidWorksTailBuilder.sketch_section`)

| # | Call | Explanation |
|---|---|---|
| 7.6 | `ClearSelection2` + `IFeature.Select2` + `ISketchManager.InsertSketch(True)` | Opens a sketch on the section plane. |
| 7.7 | `ISketchManager.ActiveSketch.ModelToSketchTransform.ArrayData` | Gets the transform for the airfoil points. |
| 7.8 | `ISketchManager.AddToDB = True`, `DisplayWhenAdded = False` | Fast entity creation with no automatic relations. |
| 7.9 | `ISketchManager.CreateSpline2(points, False)` x2 | Creates the upper and lower airfoil splines. |
| 7.10 | `ISketchManager.CreateLine(...)` | Closes a blunt trailing edge. |
| 7.11 | `ISketchManager.AddToDB = False`, `DisplayWhenAdded = True` | Restores normal sketching. |
| 7.12 | `ISketchManager.InsertSketch(True)` | Closes the sketch. |
| 7.13 | `IModelDoc2.FeatureByPositionReverse(0)` + `IFeature.Name = "Tail_Sk_<panel>_k"` | Gets and names the section sketch. |

### 7c. Panel loft (`SolidWorksTailBuilder.loft`)

| # | Call | Explanation |
|---|---|---|
| 7.14 | `IModelDoc2.ClearSelection2(True)` | Clears the selection. |
| 7.15 | `IModelDocExtension.SelectByID2(name, "SKETCH", x, y, z, True, 1, None, 0)` | Selects the root and tip sketches as loft profiles, picked near the trailing edge. |
| 7.16 | `IFeatureManager.InsertProtrusionBlend2(...)` | Lofts the panel and merges it with the fuselage. |
| 7.17 | `IFeature.Name = "Tail_Loft_<panel>"` | Names the loft. |

## 8. Hide the construction planes (`SolidWorksAircraftBuilder.hide_planes`)

| # | Call | Explanation |
|---|---|---|
| 8.1 | `IModelDoc2.ClearSelection2(True)` | Clears the selection. |
| 8.2 | `IFeature.GetTypeName2()` | Finds the aircraft features of type `"RefPlane"`. |
| 8.3 | `IFeature.Select2(True, 0)` | Adds each station plane to the selection. |
| 8.4 | `IModelDoc2.BlankRefGeom()` | Hides the selected reference planes. |
| 8.5 | `IModelDoc2.ClearSelection2(True)` | Clears the selection. |

## 9. Global variables (`SolidWorksAircraftBuilder.write_equations`)

| # | Call | Explanation |
|---|---|---|
| 9.1 | `IModelDoc2.GetEquationMgr()` | Gets the `IEquationMgr` object (Tools > Equations). |
| 9.2 | `IEquationMgr.GetCount()` | Number of equations already in the part. |
| 9.3 | `IEquationMgr.Equation(i)` | Reads equation i, to find old `"AC_..."` variables. |
| 9.4 | `IEquationMgr.Delete(i)` | Deletes an old `AC_` variable. |
| 9.5 | `IEquationMgr.Add2(-1, '"AC_<group>_<name>" = value', False)` | Adds each parameter and derived value as a global variable at the end of the list, without solving yet. |
| 9.6 | `IEquationMgr.EvaluateAll()` | Solves all equations once. |

## 10. Rebuild and finish

| # | Call | Code | Explanation |
|---|---|---|---|
| 10.1 | `IModelDoc2.ForceRebuild3(False)` | `build` | Rebuilds every feature in the part, not only the changed ones. |
| 10.2 | `ISldWorks.CommandInProgress = False` | `build` | Ends the long operation and turns screen updates back on. |
| 10.3 | `IModelDoc2.ClearSelection2(True)` | `build` | Leaves nothing selected. |
| 10.4 | `IModelDoc2.ShowNamedView2("*Isometric", 7)` | `build` | Switches to the standard isometric view (view ID 7). |
| 10.5 | `IModelDoc2.ViewZoomtofit2()` | `build` | Zooms to fit the whole aircraft. |

## 11. Check and report

| # | Call | Code | Explanation |
|---|---|---|---|
| 11.1 | `IModelDoc2.GetBodies2(0, False)` | `build` | Counts the solid bodies; 1 means wing, fuselage and tail merged into one solid. |
| 11.2 | `IModelDocExtension.CreateMassProperty()` | `build` | Creates the `IMassProperty` object for the part. |
| 11.3 | `IMassProperty.Volume` | `build` | Solid volume in m³, printed in the report. |
| 11.4 | `IMassProperty.SurfaceArea` | `build` | Surface area in m², printed in the report. |

## 12. Save and close

| # | Call | Code | Explanation |
|---|---|---|---|
| 12.1 | `IModelDoc2.SaveAs3(path, 0, 1)` | `SolidWorksAircraftBuilder.save_as` | Saves the part as `.SLDPRT` silently; returns 0 on success and is retried up to 3 times. |
| 12.2 | `IModelDoc2.GetTitle()` | `close` | Gets the document title (sweep only, when the parts are not kept open). |
| 12.3 | `ISldWorks.CloseDoc(title)` | `close` | Closes the part document after it is saved. |

---

## Differences in the standalone component scripts

The component scripts use the same calls for their geometry (sections 5, 6 or
7). Around the geometry they work differently:

| Script | Difference |
|---|---|
| [`wing_solidworks.py`](wing_solidworks.py) | Connects with `Dispatch` only. It always creates a new part (`GetUserPreferenceStringValue` + `NewDocument`) and never reuses the active one. It writes wing equations with `IEquationMgr.Add2(-1, equation, True)` (solved straight away; some are formulas such as `"Croot" = 2 * "Span" / ...`) before the sketches, and saves with `SaveAs3` once, without retrying. It does not hide planes or report mass properties. |
| [`fuselage_sw.py`](fuselage_sw.py) | Uses the active part when it is a part (`ActiveDoc` + `GetType`), otherwise a new one. It deletes the previous fuselage by feature name (`Select2` + `DeleteSelection2`). It writes `Fus_*` variables first with `GetEquationMgr` / `Delete` / `Add2` / `EvaluateAll`, hides the `Fus_Station_` planes with `BlankRefGeom`, and reports `CreateMassProperty`. |
| [`tail_sw.py`](tail_sw.py) | Same pattern as the fuselage script, with the `Tail_` prefix and `Tail_*` variables. It also reports the body count with `GetBodies2`. |
