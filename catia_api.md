# CATIA V5 API calls used by the aircraft generator

These are all the CATIA V5 automation calls (through pywin32 COM) that the
program makes to build the aircraft. They are listed in the order they run
during a complete aircraft build: `CatiaAircraftBuilder.build()` in
[`catia_builder.py`](catia_builder.py). The GUI, the design sweep and
`aircraft_sw.py --cad catia` all go through this path. The standalone
component scripts (`--cad catia`) use the same calls through
`build_component()`; the differences are listed at the end.

- **Units:** the CATIA automation API works in **millimetres**, so the section
  points are passed in as computed.
- **References:** most CATIA methods take a `Reference`, not the object itself;
  `Part.CreateReferenceFromObject` makes one.
- **Same geometry, different form:** SolidWorks builds the aircraft from
  sketches on planes. CATIA builds it from 3-D wireframe curves in
  geometrical sets, plus multi-sections solids in `PartBody`.

| Constant | Value | Meaning |
|---|---|---|
| `HIDE` | 1 | `catVisPropertyNoShow`, used with `VisProperties.SetShow` |
| `COUPLING_RATIO` | 1 | multi-sections coupling: ratio |
| `RELIMIT_ON_SECTIONS` | 1 | the loft starts and ends at the first and last section |

---

## 0. Check before the build (GUI)

| # | Call | Code | Explanation |
|---|---|---|---|
| 0.1 | `winreg.OpenKey(HKEY_CLASSES_ROOT, "CATIA.Application")` | `aircraft_sw.cad_installed` | Checks that CATIA is registered for COM automation; this is a Windows registry call, not a CATIA one. |

## 1. Connect to CATIA (`CatiaPart.__init__`)

| # | Call | Explanation |
|---|---|---|
| 1.1 | `win32com.client.GetActiveObject("CATIA.Application")` | Attaches to the running CATIA session. |
| 1.2 | `win32com.client.Dispatch("CATIA.Application")` | Starts CATIA if none is running (this needs `cnext /regserver` to have been run once). |
| 1.3 | `Application.Visible = True` | Shows the CATIA window. |

## 2. Choose the part: use an empty active part or open a new one (`CatiaPart.open_part`)

### 2a. Look at the active document (`_active_part_doc`)

| # | Call | Explanation |
|---|---|---|
| 2.1 | `Application.ActiveDocument` | Gets the document in the active window. |
| 2.2 | `PartDocument.Part` | Gets its `Part`; this raises for products, drawings and other non-part documents, which are then never used. |

### 2b. Check whether it is empty (`part_contents`)

| # | Call | Explanation |
|---|---|---|
| 2.3 | `Part.MainBody` | Gets `PartBody`, the main body. |
| 2.4 | `Part.Bodies` | Lists all bodies; any body other than `PartBody` counts as the user's content. |
| 2.5 | `Body.Name` | Name of the main body, to skip it in that list. |
| 2.6 | `Body.Shapes` | Solid features in `PartBody` (pads, lofts, mirrors, ...). |
| 2.7 | `Part.HybridBodies` | Geometrical sets. |
| 2.8 | `Part.Parameters.RootParameterSet` | The root parameter set of the part. |
| 2.9 | `ParameterSet.ParameterSets` | Parameter sets under the root (the program's own set is `Aircraft_Parameters`). |
| 2.10 | `Body.Sketches` | Sketches in `PartBody`; any sketch counts as the user's content. |
| 2.11 | `Body.HybridShapes` | Wireframe and surfaces in `PartBody` (a multi-sections solid lists its loft surface here under the same name). |
| 2.12 | `Part.OrderedGeometricalSets` | Ordered geometrical sets. |
| 2.13 | `Part.Relations` | Formulas, rules and design tables. |
| 2.14 | `ParameterSet.DirectParameters` | Parameters directly under the root (user parameters). |
| 2.15 | `Collection.Count` / `Collection.Item(i)` / `AnyObject.Name` | Read the names in each of the collections above. |
| 2.16 | `Document.Name` | File name of the active part, for the log message. |

### 2c. Open a new part if the active part is not empty

| # | Call | Explanation |
|---|---|---|
| 2.17 | `Application.Documents.Add("Part")` | Creates a new CATPart in its own window. |

### 2d. Get the factories (`attach`)

| # | Call | Explanation |
|---|---|---|
| 2.18 | `PartDocument.Part` | Gets the `Part` of the chosen document. |
| 2.19 | `Part.HybridShapeFactory` | Gets the factory for points, splines, lines, joins and surfaces. |
| 2.20 | `Part.ShapeFactory` | Gets the factory for solid features: multi-sections solid, mirror, close surface. |
| 2.21 | `Part.MainBody` | Gets `PartBody`, where the solids go. |

## 3. Delete earlier aircraft geometry (`CatiaPart.delete_existing`)

This finds nothing in an empty or new part. It does work when a component
script rebuilds its own component.

| # | Call | Explanation |
|---|---|---|
| 3.1 | `Body.Shapes` / `Part.HybridBodies` / `ParameterSets` + `Item(i).Name` | Find the solids, geometrical sets and parameter sets with the aircraft prefixes. |
| 3.2 | `PartDocument.Selection` | Gets the document's `Selection` object. |
| 3.3 | `Selection.Clear()` | Empties the selection. |
| 3.4 | `Selection.Add(object)` | Adds an old feature to the selection (the solids are deleted last one first, so the mirror goes before its loft). |
| 3.5 | `Selection.Delete()` | Deletes everything selected. |

## 4. Start the build (`CatiaPart.batch`)

| # | Call | Explanation |
|---|---|---|
| 4.1 | `Application.RefreshDisplay = False` | Stops redrawing the screen while the geometry is created (much faster). |
| 4.2 | `Application.DisableNewUndoRedoTransaction()` | Stops recording undo steps during the build. |

## 5. Wing (`build_wing`)

### 5a. Geometrical set (`geometry_set`)

| # | Call | Explanation |
|---|---|---|
| 5.1 | `Part.HybridBodies.Add()` | Creates a new geometrical set for the construction curves. |
| 5.2 | `HybridBody.Name = "Wing_Sections"` | Names it. |

### 5b. Airfoil section, for each station `i` (`airfoil_profile`)

The upper spline goes from the trailing edge to the leading edge, and the
lower spline from the leading edge to the trailing edge. These are the same
curves the SolidWorks sketch holds, but in 3-D.

| # | Call | Explanation |
|---|---|---|
| 5.3 | `HybridShapeFactory.AddNewSpline()` | Creates an empty spline. |
| 5.4 | `HybridShapeSpline.SetSplineType(0)` | Makes it a cubic spline that passes through its points. |
| 5.5 | `HybridShapeSpline.SetClosing(0)` | Leaves it open (an airfoil surface curve). |
| 5.6 | `HybridShapeFactory.AddNewPointCoord(x, y, z)` | Creates a coordinate point for each airfoil point. These points are not added to a set, so they are aggregated under the spline. |
| 5.7 | `Part.CreateReferenceFromObject(point)` | Makes a reference to the point. |
| 5.8 | `HybridShapeSpline.AddPoint(reference)` | Adds the point to the spline. |
| 5.9 | `HybridBody.AppendHybridShape(spline)` | Puts the spline in the geometrical set. |
| 5.10 | `HybridShapeSpline.Name = "Wing_Section_i_Upper"` | Names it; 5.3-5.10 repeat for `..._Lower`. |
| 5.11 | `HybridShapeFactory.AddNewPointCoord(...)` x2 | Creates the two trailing-edge end points (blunt trailing edge only). |
| 5.12 | `HybridShapeFactory.AddNewLinePtPt(ref1, ref2)` | Creates the line that closes a blunt trailing edge. |
| 5.13 | `HybridBody.AppendHybridShape(line)` + `.Name = "..._TE"` | Puts the line in the set and names it. |
| 5.14 | `HybridShapeFactory.AddNewJoin(ref_upper, ref_lower)` | Joins the upper and lower splines into one curve. |
| 5.15 | `HybridShapeAssemble.AddElement(ref_te)` | Adds the trailing-edge line to the join. |
| 5.16 | `HybridBody.AppendHybridShape(join)` + `.Name = "Wing_Section_i"` | Puts the closed profile in the set and names it. |
| 5.17 | `Part.UpdateObject(join)` | Builds the join now, so a profile that is not closed fails right away with a clear message. |

### 5c. Wing loft (`solid_loft`)

| # | Call | Explanation |
|---|---|---|
| 5.18 | `Part.InWorkObject = MainBody` | Makes `PartBody` the current body, so the solid is added to it. |
| 5.19 | `ShapeFactory.AddNewLoft()` | Creates an empty multi-sections solid. |
| 5.20 | `Loft.HybridShape` | Gets its `HybridShapeLoft` definition, which holds the sections. |
| 5.21 | `HybridShapeLoft.SectionCoupling = 1` | Couples the sections by ratio. |
| 5.22 | `HybridShapeLoft.Relimitation = 1` | Limits the loft to the first and last section. |
| 5.23 | `HybridShapeLoft.AddSectionToLoft(ref_profile, 1, ref_closing_point)` | Adds each section profile in order; the closing point at the trailing edge keeps the loft from twisting. |
| 5.24 | `Part.UpdateObject(loft)` | Builds the solid. |
| 5.25 | `Loft.Name = "Wing_Loft"` | Names it. |

If the solid fails at 5.24, the program uses a surface instead:

| # | Call | Explanation |
|---|---|---|
| 5.26 | `Selection.Add` + `Selection.Delete` | Deletes the failed solid. |
| 5.27 | `HybridShapeFactory.AddNewLoft()` | Creates a multi-sections surface instead. |
| 5.28 | `SectionCoupling` / `Relimitation` / `AddSectionToLoft` | Adds the same sections to the surface. |
| 5.29 | `HybridBody.AppendHybridShape(surface)` + `.Name = "Wing_Loft_Surface"` | Puts the surface in the geometrical set. |
| 5.30 | `Part.UpdateObject(surface)` | Builds the surface. |
| 5.31 | `ShapeFactory.AddNewCloseSurface(ref_surface)` | Fills the closed surface into a solid in `PartBody`. |
| 5.32 | `Part.UpdateObject(closeSurface)` | Builds that solid. |

### 5d. Mirror the half wing (`mirror_body`)

| # | Call | Explanation |
|---|---|---|
| 5.33 | `Part.InWorkObject = MainBody` | Makes `PartBody` current. |
| 5.34 | `Part.OriginElements.PlaneXY` | Gets the XY plane (z = 0), the aircraft's plane of symmetry. |
| 5.35 | `ShapeFactory.AddNewMirror(ref_plane)` | Mirrors everything in `PartBody` about that plane. |
| 5.36 | `Part.UpdateObject(mirror)` | Builds the mirror; if it fails, the mirror is deleted and the half wing is kept. |
| 5.37 | `Mirror.Name = "Wing_Mirror"` | Names it. |

## 6. Fuselage (`build_fuselage`)

| # | Call | Explanation |
|---|---|---|
| 6.1 | `Part.HybridBodies.Add()` + `.Name = "Fus_Sections"` | Creates the fuselage geometrical set. |
| 6.2 | `HybridShapeFactory.AddNewSpline()` | Creates a spline for each cross-section: first a tiny nose cap section, then every station, then a tiny tail cap section if the tail end is pointed. |
| 6.3 | `HybridShapeSpline.SetSplineType(0)` | Makes it a cubic spline through its points. |
| 6.4 | `HybridShapeSpline.SetClosing(1)` | Closes it into a periodic loop (the superellipse cross-section). |
| 6.5 | `AddNewPointCoord` + `CreateReferenceFromObject` + `AddPoint` | Adds the section points. |
| 6.6 | `HybridBody.AppendHybridShape(spline)` + `.Name` | Puts each section in the set and names it (`Fus_NoseTip`, `Fus_Section_i`, `Fus_TailTip`). |
| 6.7 | `Part.InWorkObject` / `ShapeFactory.AddNewLoft()` / `Loft.HybridShape` | Starts the fuselage multi-sections solid in `PartBody`. |
| 6.8 | `SectionCoupling = 1`, `Relimitation = 1` | Couples by ratio and limits the loft to the end sections. |
| 6.9 | `AddSectionToLoft(ref_section, 1, ref_first_point)` | Adds each closed section, coupled at its +Z side point. |
| 6.10 | `Part.UpdateObject(loft)` + `.Name = "Fuselage_Loft"` | Builds and names the fuselage solid. |
| 6.11 | `Selection.Delete` + a new loft from 6.7-6.10 | Retries without the two cap sections if the first loft fails (with the surface fallback 5.27-5.32 if that fails too). |

CATIA automation does not accept a point as a loft section, so the nose and a
pointed tail end are closed with a cap section 1% of the maximum width
instead of the point that SolidWorks uses.

## 7. Tail (`build_tail`)

| # | Call | Explanation |
|---|---|---|
| 7.1 | `Part.HybridBodies.Add()` + `.Name = "Tail_Sections"` | Creates the tail geometrical set. |
| 7.2 | Calls 5.3-5.17 for each root and tip section | Builds the closed airfoil profiles `Tail_Sk_<panel>_k`, using the same calls as the wing. |
| 7.3 | Calls 5.18-5.25 for each panel side | Lofts each panel into `PartBody` as `Tail_Loft_<panel>` (surface fallback 5.26-5.32 if needed). |

## 8. Parameters (`write_parameters`)

The CATIA counterpart of the SolidWorks global variables.

| # | Call | Explanation |
|---|---|---|
| 8.1 | `Part.Parameters.RootParameterSet.ParameterSets` | Gets the parameter sets under the root. |
| 8.2 | `Selection.Add` + `Selection.Delete` | Deletes an earlier `Aircraft_Parameters` set if there is one. |
| 8.3 | `ParameterSets.CreateSet("Aircraft_Parameters")` | Creates the parameter set. |
| 8.4 | `ParameterSet.DirectParameters` | Gets the parameter list of the new set. |
| 8.5 | `Parameters.CreateReal("AC_<group>_<name>", value)` | Adds each parameter and derived value as a real parameter. |

## 9. Update

| # | Call | Explanation |
|---|---|---|
| 9.1 | `Part.Update()` | Updates the whole part (all features and parameters). |

## 10. Hide the construction geometry (`hide`)

| # | Call | Explanation |
|---|---|---|
| 10.1 | `Selection.Clear()` | Empties the selection. |
| 10.2 | `Selection.Add(geometricalSet)` | Selects `Wing_Sections`, `Fus_Sections` and `Tail_Sections`. |
| 10.3 | `Selection.VisProperties.SetShow(1)` | Hides them (no show), leaving only the solid visible. |
| 10.4 | `Selection.Clear()` | Empties the selection. |

## 11. End the build (`batch` exit)

| # | Call | Explanation |
|---|---|---|
| 11.1 | `Application.EnableNewUndoRedoTransaction()` | Turns undo recording back on. |
| 11.2 | `Application.RefreshDisplay = True` | Turns screen redraw back on. |

## 12. View (`show_isometric`)

| # | Call | Explanation |
|---|---|---|
| 12.1 | `Application.ActiveWindow.ActiveViewer` | Gets the 3-D viewer of the part window. |
| 12.2 | `Viewer3D.Viewpoint3D` | Gets the viewer's camera position. |
| 12.3 | `Viewpoint3D.PutSightDirection((-1, -1, -1)/sqrt(3))` | Looks along the isometric diagonal. |
| 12.4 | `Viewpoint3D.PutUpDirection(up)` | Sets +Y as up on screen, matching the SolidWorks isometric view. |
| 12.5 | `Viewer3D.Reframe()` | Zooms to fit the whole aircraft. |

## 13. Check and report

| # | Call | Code | Explanation |
|---|---|---|---|
| 13.1 | `Body.Shapes.Count` | `CatiaAircraftBuilder.build` | Number of solid features merged in `PartBody`, printed in the log. |
| 13.2 | `PartDocument.GetWorkbench("SPAWorkbench")` | `report` | Gets the Space Analysis workbench, which measures geometry. |
| 13.3 | `SPAWorkbench.GetMeasurable(ref_PartBody)` | `report` | Gets a `Measurable` object for `PartBody`. |
| 13.4 | `Measurable.Volume` | `report` | Solid volume in m³, printed in the report. |
| 13.5 | `Measurable.Area` | `report` | Surface area in m², printed in the report. |

## 14. Save (`CatiaPart.save_as`)

| # | Call | Explanation |
|---|---|---|
| 14.1 | `Application.Documents.Count` / `Documents.Item(k)` | Lists the open documents. |
| 14.2 | `Document.FullName` / `Document.Name` | Finds the file names already open, because CATIA cannot hold two documents with the same name (then the part is saved as `name_2`, `name_3`, ...). |
| 14.3 | `Application.DisplayFileAlerts` (read, then `= False`) | Remembers the setting and turns off the overwrite and other file dialogs. |
| 14.4 | `PartDocument.SaveAs(path)` | Saves the part as `.CATPart`; retried up to 3 times. |
| 14.5 | `Application.DisplayFileAlerts = <previous value>` | Restores the user's setting. |

## 15. Close (sweep only, when parts are not kept open)

| # | Call | Explanation |
|---|---|---|
| 15.1 | `Document.Close()` | Closes the part document after it is saved. |

---

## Differences in the standalone component scripts

`wing_solidworks.py`, `fuselage_sw.py` and `tail_sw.py` with `--cad catia` call
`build_component()`, which runs the same calls in this order: connect (1),
choose the part (2), delete that component's earlier geometry (3), start the
build (4), the component's geometry (5, 6 or 7), parameters (8), update (9),
hide (10), end the build (11), view (12), report (13) and save (14).

| Script | Part used | Parameter set |
|---|---|---|
| [`wing_solidworks.py`](wing_solidworks.py) | the active part only if it is empty, otherwise a new part | `Wing_Parameters` |
| [`fuselage_sw.py`](fuselage_sw.py) | the active part if it is empty or holds only this program's geometry, otherwise a new part (`--new-part` always opens a new one) | `Fus_Parameters` |
| [`tail_sw.py`](tail_sw.py) | as for the fuselage, so a tail can be added to a fuselage part | `Tail_Parameters` |
