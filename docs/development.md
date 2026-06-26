# Development

This page is for anyone modifying or contributing to Phidler — for using
the app, see the [User Guide](guide.md).

## Environment notes

On Linux, if your system already has a Qt6 install (from whatever
package manager your distro uses), the dynamic linker can resolve it
*before* the newer Qt6 bundled inside the PySide6 wheel, causing an
`undefined symbol` crash on import. `run.sh` and `run_tests.sh` work
around this by prepending PySide6's own Qt lib directory to
`LD_LIBRARY_PATH` before launching, so the linker finds the matching
version first. If you ever run the app a different way (not via those
scripts) and hit this crash, set the same environment variable yourself —
`run.sh` derives the path the same way:

```
export LD_LIBRARY_PATH="$(find .venv/lib -maxdepth 1 -name 'python3.*')/site-packages/PySide6/Qt/lib:$LD_LIBRARY_PATH"
```

## Code layout

```
src/phidler/
  app.py                  # QApplication bootstrap + PDK activation
  main_window.py          # menus/toolbar/docks, action wiring
  pdk_catalog.py           # introspects gf.components into a placeable, categorized, PDK-validated catalog; name/category prettification
  custom_components.py     # loads user Python files, registers valid factories with the active PDK
  drc.py                   # width/spacing checks against user-supplied thresholds
  project_io.py            # save/load: replays the document's own recipe, not raw objects
  export_script.py          # writes a standalone .py that recreates the design via direct gdsfactory calls
  import_script.py          # AST-parses a Phidler-generated .py back into document/scene state
  waveguide_calc.py          # effective-index-method single-mode width estimate + platform presets
  fdtd_sim.py                # pure-compute FDTD wiring (photonfdtd) — no Qt/threading, fully unit-testable
  model/
    document.py            # LayoutDocument — owns the gdsfactory top cell; ProjectSettings metadata
    placed_instance.py     # PlacedInstance / PlacedRoute records
    layers.py               # layer list, populated on demand as layers are actually used (not pre-seeded)
    commands.py             # QUndoCommand subclasses (Add/Delete/Move/EditParams/Route)
  canvas/
    scene.py                # QGraphicsScene wrapper around LayoutDocument; fixed large sceneRect for panning
    view.py                 # pan/zoom/grid/snap/zoom-to-fit, Y-flip, drag->undo wiring, placement/routing/context-menu
    polygon_item.py         # per-instance QGraphicsItem rendering (hull+holes, ports)
    transform_handles.py      # on-canvas drag handles for rotate/scale (the standard 2D-editor convention)
  panels/
    component_palette.py    # curated category tree (core photonics first, niche under "Other"), click-to-place, hover preview wiring
    component_preview.py     # renders a component's actual geometry to a small pixmap; floating popup widget
    properties_panel.py     # dynamic parameter form from factory signatures
    layers_panel.py          # layer visibility/color dock widget
    drc_panel.py             # DRC threshold inputs + violation list
    console_panel.py          # interactive Python REPL (code.InteractiveInterpreter) against the live session
    project_settings_dialog.py # material/thickness/wavelength picker shown on startup and File > New
    fdtd_window.py            # FDTD top-level window: mode-solve tab, propagation tab, source table, movie playback; FdtdWorker/ModeWorker (QThread wrappers around fdtd_sim.py)
tests/                       # all run under QT_QPA_PLATFORM=offscreen
```

## Key design notes

- Mirror and uniform scale commute: `QTransform.scale(mag, -mag if mirror else mag)` reproduces klayout's mirror-then-scale in one call. Rotation must be the outermost (last-applied) operation. Math verified against `klayout.db.DCplxTrans` in `tests/test_gds_roundtrip.py`.
- Transform handles reposition on a 120ms polling timer rather than hooking every view-mutating interaction (pan/zoom/resize/drag). The timer skips re-sync while any handle has `is_dragging` set to avoid fighting an active drag.
- Handles are `QGraphicsItem`s in the scene, not floating `QWidget`s. `ItemIgnoresTransformations` keeps each handle's on-screen pixel size constant across zoom, and pan/zoom propagate to scene items for free.
- Corner-drag scale keeps the **diagonally opposite corner** fixed in absolute scene coordinates. The `mag` and `(x, y)` satisfying "opposite corner unchanged, dragged corner tracks cursor" are solved once at drag-start. Anchoring at the instance's local origin (where the transform math pivots) does not have this property for components like `straight` whose origin sits near the bounding box edge.
- Rotation is a **delta angle** (mouse sweep since drag start, added to the angle at press-time), not an absolute target. The scene-frame `atan2` angle and `DCplxTrans`/`QTransform.rotate()` move in the same direction, so no sign correction is needed for the Y-flip.
- `LayoutScene` uses a fixed 100mm×100mm `sceneRect`. `QGraphicsView` auto-sizing from content bounds collapses to `(0, 0)` when content fits the viewport, breaking panning entirely — see [Bugs found from actual use](#bugs-found-from-actual-use).
- Geometry is pulled from gdsfactory once per place/edit/import — never during a drag — so dragging only touches Qt item transforms.
- Polygon holes render via odd-even fill (`QPainterPath`), not `QGraphicsPolygonItem` which drops holes.
- Qt-side transform math is verified numerically against `klayout.db.DCplxTrans` in `tests/test_gds_roundtrip.py` and `tests/test_holes.py`. `fitInView`'s interaction with the Y-flip and `QComboBox.findData()`'s unreliable tuple matching were both verified empirically before use.
- The component catalog only includes names registered in the active PDK and excludes `ComponentAllAngle` factories, which require `add_ref_off_grid()`.
- Routes use `gdsfactory.functions.get_polygons(ref)`, returning already-absolute top-cell coordinates — no additional Qt-side transform needed, unlike instances.
- The reference GDS backdrop is a standalone `gf.Component`, never added to the document's top cell, so it can't appear in GDS export.
- `QUndoStack.push()` inserts a command even if its `redo()` raises. `AddRouteCommand`/`EditParamsCommand` guard this with an internal `.error` flag. The delete macro pushes routes before instances so undo restores instances before routes need them back.
- `QApplication.sendEvent` with a `QContextMenuEvent` segfaults under the offscreen platform. Tests call event-handler overrides directly as plain methods; hover-preview tests emit `itemEntered` directly instead of injecting a synthetic mouse-move.
- There is a pre-existing ~1-in-9 native-crash flake under the offscreen platform, unrelated to any particular feature area. Root cause not pinned down. Rerun if a run fails.
- The hover preview always uses default layer colors, ignoring Layers-panel customizations. Its pixmap cache is keyed by name, so a second custom import redefining a name shows stale geometry. Both are cosmetic, preview-only.
- The scripting console uses `code.InteractiveInterpreter` for multi-line support. Each call needs the full accumulated buffer — the interpreter has no memory between calls. `quit()`/`exit()` raise `SystemExit` that propagates through `runsource()` uncaught; all calls wrap it in `except SystemExit`.
- Console mutations render on the canvas immediately but don't refresh the Layers/DRC panels — those only update on `undo_stack.indexChanged`, which the console bypasses by design.
- Project Settings is triggered via `QTimer.singleShot(0, window._new_project)` from `app.main()`, not from `MainWindow.__init__`. `_new_project()` opens a blocking modal; triggering it during construction would hang every test that instantiates `MainWindow`. `_new_project()` is split into a dialog-showing wrapper and a testable `_reset_to_new_project(settings)` core.
- The suggested single-mode waveguide width uses a two-step effective-index method (bisection for vertical confinement, then lateral cutoff). EIM runs narrow for high-index-contrast platforms: ~319nm for 220nm SOI at 1550nm, versus ~450-500nm commonly used. The dialog's disclaimer says this explicitly.
- LN `core_index` (2.211 at 1550nm) is from the Zelmon, Small & Jundt (1997) Sellmeier fit. LT index (2.14) is a standard literature value with a single source.
- `import_script.py` inspects only direct top-level statements (`tree.body`), not a recursive `ast.walk()`. A loop body statically matches the same assignment shape as a single instance; the iteration count is unknowable at parse time, so unrecognized top-level forms raise `ScriptParseError` instead of silently reconstructing the wrong number of instances.
- Port-to-port snapping uses `kdb.DCplxTrans` for absolute port positions, the same primitive as instance geometry. During drag, positions are computed against the instance's live Qt transform via `LayoutDocument.get_absolute_ports_for_transform`, not the still-stale document transform.
- `hasFocus()` never becomes true under `QT_QPA_PLATFORM=offscreen`. The regression test for `PropertiesPanel._is_editing_transform` monkeypatches the guard method directly.
- The measure label uses `QGraphicsItem.ItemIgnoresTransformations` for constant on-screen text size. The flag must be accessed on the base class — `label.ItemIgnoresTransformations` on a `QGraphicsSimpleTextItem` instance raises `AttributeError`.
- Measure-click port snapping reuses `InstanceItem.nearest_port` with the same hit-radius as routing, to keep thresholds consistent.
- `QRectF.top()`/`bottom()` return whatever min/max order the rect was constructed with — not geometric top/bottom. `_selected_scene_bboxes` calls `.normalized()` explicitly. With the canvas Y-flip, larger scene-y renders higher on screen, so `Align Top` reads `box.bottom()` internally — `test_align_top_uses_the_visual_screen_direction_not_qrectf_naming` guards this.
- Align/Distribute moves each instance by a single scalar shift along one axis (`target − current_edge_or_center`). `mapRectToScene` gives the correct axis-aligned bbox for any rotation/scale, so no per-instance transform decomposition is needed.
- FDTD logic is split into a pure-compute core (`fdtd_sim.py`, no Qt or threading) and thin Qt wrappers (`FdtdWorker`/`ModeWorker` in `fdtd_window.py`). `fdtd_sim.py` is fully unit-testable without a display.
- The simulation runs true 3D — `z_size=0.0` (quasi-2D collapse) was removed because it made cladding thickness inert: the single z-slice resolved core/cladding contrast by XY footprint alone, not vertical position. Cost: ~6×10⁻⁸s/cell-step.
- Background slabs covering only above/below the core's z-range left the core's own z-range stamped as vacuum (eps_r=1) outside the waveguide polygon. Fixed by adding a third slab spanning the core z-range, stamped first so the waveguide polygon wins on top.
- `photonfdtd.ModeSolver` (2D scalar-Helmholtz cross-section eigenmode solver) powers the Vertical Mode Profile tab. `mode_confinement()` converts the edge/peak amplitude ratio to "well confined" / "cladding may be too thin". Eigensolve cost is superlinear in grid size; the UI defaults (`cell_size_um=0.02`) stay well under a second.
- `photonfdtd.sources.SinglePhotonSource` amplitude is scaled by `sqrt(photon_count)`, not by stacking N copies, so energy scales linearly with photon count. Stacking N coherent copies at the same position would scale energy as N².
- `from_gdsfactory` returns `sources=[]`/`monitors=[]`. `build_simulation` adds the configured sources and a `FieldMonitor`.
- Constructing `FdtdWindow` raises `ImportError` if the optional `fdtd` extras aren't installed. `MainWindow._open_fdtd_window` catches this and shows a message box.
- `FdtdWorker`/`ModeWorker` tests call `.run()` directly (not a real `QThread`). Compute correctness is covered by `fdtd_sim.py`'s tests; the worker tests verify call-through and signal emission.
- `kind="scripted"` sources use a `ScriptedWaveform` in `fdtd_sim.py` that `eval()`s a Python expression of `t` (time in seconds), with the same no-sandbox trust model as the scripting console.

## Testing

Run the suite with `./run_tests.sh`. It runs headlessly under
`QT_QPA_PLATFORM=offscreen` and currently covers 284 tests.

### Bugs found from actual use

Two bugs surfaced the first time the app ran on a real display:

- **Panning** — `QGraphicsView` collapsed the scrollable range to `(0, 0)` when content fit inside the viewport. Fixed with a fixed 100mm×100mm `sceneRect` independent of content.
- **Layers panel** — pre-populated with all 47 PDK layers regardless of what the design used. Fixed by populating layers on demand, the first time each is actually placed or routed.

Both are covered by regression tests.

### Verification splits into two tiers

#### Fully verified, headless

- Every placed/edited/routed/exported shape's coordinates checked
  numerically against `klayout.db.DCplxTrans`, including rotation,
  mirroring, and polygon holes.
- The entire 310-component catalog is exhaustively placed and exported in
  `tests/test_scale.py`. This caught 25 unregistered catalog entries and
  4 `ComponentAllAngle` factories needing a different placement API.
- Transactional correctness for all four mutating operations (place, edit,
  route, delete) — each has a regression test covering the "raises
  partway through" case that previously left corrupted state.
- Save/load round-trips by replaying the document's recipe (component spec
  + kwargs + transform, port pairs for routes), tolerating missing
  reference or custom-component files. Re-imports custom-component files
  before replaying instances, since those only exist in the PDK registry
  for the process that imported them.
- Multi-item drag and middle-drag panning via real `QTest`
  press/move/release sequences.
- Transform/math helpers (`view.snap()`, `mapToScene`, zoom-to-fit scale
  and containment) checked directly.
- Custom component loading distinguishes functions *defined* in a user's
  file from ones merely imported into it via `__module__`; a function that
  raises or returns the wrong type is skipped cleanly.

#### Needs a real display

The logic for each of these is tested, but correctness of appearance and
feel can't be assessed headlessly:

- Right-click context menu, status bar cursor readout, grid controls, zoom
  to fit/selection, hover preview — whether they're visually correct and
  feel right.
- Drag responsiveness, pan/zoom comfort, color legibility, routing
  click-to-pick-a-port feel.
- On-canvas transform handles and Project Settings dialog — logic is
  tested (including corner-drag via real `QTest` drag), but on-screen
  appearance and grab comfort need a human.

### Manual test checklist

Launch `./run.sh` and try:

1. Place a few different components from the palette (try a ring, an MMI,
   a grating coupler — not just a straight waveguide), drag one or several
   selected at once, rotate/mirror, undo/redo.
2. Edit a selected instance's parameters (including the cross_section
   dropdown) in the Properties panel and watch it regenerate.
3. Toggle "Route", pick a cross-section, click a port on one component then
   a port on another, confirm a route appears and is selectable/deletable.
4. Right-click the canvas — confirm the context menu appears under the
   cursor and its actions work.
5. Watch the status bar while moving the mouse over the canvas; adjust the
   grid pitch/snap controls in the toolbar.
6. Zoom to fit / zoom to selection from the View menu.
7. Toggle a layer's visibility and change its color in the Layers panel.
8. Import a GDS as a reference, confirm it renders dimmed and stays out of
   your own export; save the project, reopen it, confirm the reference
   comes back too.
9. Run a DRC check, double-click a violation to confirm the view jumps
   there.
10. Import a custom component (File > Import Custom Components… on a
    Python file with a `@gf.cell`-decorated function) and confirm it shows
    up under "Custom" in the palette, prettified name and all.
11. Export GDS and open it in KLayout (or anything else you trust) to
    confirm it looks right.

If anything feels off (drag lag, grid too dense/sparse, colors hard to
read, zoom too sensitive, routing clicks feeling imprecise, the context
menu appearing in the wrong place), that's exactly the kind of feedback
this checklist exists to surface.
