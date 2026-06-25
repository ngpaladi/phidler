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

- Scale's transform math (`mag` composing with rotation/mirror) was
  verified against `klayout.db.DCplxTrans` directly before being wired up,
  same discipline as the original rotate/mirror math: mirror and uniform
  scale commute (so `QTransform.scale(mag, -mag if mirror else mag)` in
  one call reproduces klayout's mirror-then-scale exactly), but rotation
  must still be the outermost (last-applied) operation.
- The transform handles reposition via a 120ms polling timer rather than
  hooking into every view-mutating interaction (pan/zoom/resize/drag)
  individually — simpler and harder to leave a gap in than enumerating
  every path that could move the selected item on screen. It skips
  re-syncing handle positions while any handle reports `is_dragging`, or
  the periodic sync would fight the drag.
- The handles are real `QGraphicsItem`s added directly to the scene (not
  a `QWidget` floating over the viewport, which the first version of this
  feature used) — confirmed empirically that `ItemIgnoresTransformations`
  keeps a handle's on-screen pixel size constant across zoom levels, the
  standard look for resize handles, and that being plain scene items
  means they pan/zoom with the view for free, no manual position-mapping
  needed the way the QWidget version required.
- A corner-drag scale gesture keeps the **diagonally opposite corner**
  fixed in absolute scene coordinates, the standard 2D-editor resize
  behavior — solved once at drag-start for the `mag` and `(x, y)` that
  satisfy both "opposite corner unchanged" and "dragged corner tracks the
  cursor," not re-derived per mouse-move frame. This was a deliberate
  fix during development: anchoring the scale at the instance's local
  origin instead (where `mag` actually mathematically pivots, per
  klayout's `DCplxTrans`) does NOT have this property — for `straight`,
  whose local origin sits almost on the bounding box edge, that would
  make one corner barely move and the opposite corner swing wildly for a
  small mouse movement. The rotate handle, by contrast, pivots around the
  instance's local origin directly (same pivot the `R` key already uses),
  which needs no such compensation.
- The rotate handle computes rotation as a **delta angle** (how far the
  mouse has swept around the pivot since the press, added to the
  rotation at press-time), not an absolute target angle — verified
  empirically that the scene-frame `atan2` angle and the `rotation`
  parameter consumed by `DCplxTrans`/`QTransform.rotate()` move in the
  same direction by the same amount, so the delta is correct without any
  sign correction for the canvas's global Y-flip.
- `LayoutScene` sets a fixed, large `sceneRect` (100mm × 100mm) rather than
  letting `QGraphicsView` auto-size it from placed content — see
  [Bugs found from actual use](#bugs-found-from-actual-use) below for why
  that auto-sizing breaks panning entirely once content fits inside the
  viewport.
- Geometry is pulled from gdsfactory once per place/edit/import — never
  during a drag — so dragging only touches Qt item transforms and stays
  responsive regardless of layout size.
- The canvas renders polygon holes correctly (odd-even fill `QPainterPath`,
  not a hole-dropping `QGraphicsPolygonItem`) so what you see always
  matches what gets exported. Verified against a deliberately-holed
  in-memory component, not just assumed.
- The Qt-side rotate/mirror/translate math was verified numerically against
  `klayout.db.DCplxTrans` directly (`tests/test_gds_roundtrip.py`,
  `tests/test_holes.py`) rather than assumed — gdsfactory 9.x's API (PDK
  activation requirement, `route_single` replacing `get_route`, polygon
  extraction needing a dbu conversion) postdates a lot of training data, so
  every non-trivial API call here was checked against the actually-installed
  version first. The same discipline applied to Qt itself: `fitInView`'s
  interaction with the global Y-flip and `QComboBox.findData()`'s
  unreliable matching on Python tuples were both verified empirically
  rather than assumed, in either direction (confirming one fear was
  unfounded, the other very real).
- The component catalog only includes names actually registered in the
  active PDK's cell registry and excludes `ComponentAllAngle` factories
  (which need `add_ref_off_grid()`, a different placement primitive this
  app doesn't support) — both exclusions were added after an exhaustive
  placement test caught them, not designed in upfront.
- Routes are rendered via `gdsfactory.functions.get_polygons(ref)`, which
  returns already-absolute (top-cell) coordinates for an arbitrary ref —
  so route geometry needs no additional Qt-side transform, unlike instances.
- A GDS reference backdrop is kept as a standalone `gf.Component`, never
  added into the document's top cell, so it can't leak into your own
  GDS export.
- `QUndoStack.push()` still inserts a command even if its `redo()` raises,
  and undoes a macro's children in reverse push order — both confirmed
  empirically, not assumed. `AddRouteCommand`/`EditParamsCommand` guard the
  first with an internal `.error` flag; the instance/route delete macro is
  ordered specifically to satisfy the second (routes pushed before
  instances, so undo restores instances before routes need them back).
- Synthetic native event injection (`QApplication.sendEvent` with a
  constructed `QContextMenuEvent`) segfaulted the interpreter under the
  offscreen platform during test-writing. Tests for event-handler overrides
  call them directly as plain methods instead — this isn't a production
  code path concern (real events never go through `sendEvent`), but it's
  why some tests in this suite look structurally different from the
  `QTest`-based mouse-drag tests elsewhere. The hover-preview tests follow
  the same approach: they emit `itemEntered` directly (a plain Qt signal,
  not a native event) rather than injecting a synthetic mouse-move.
- The full test suite has a low (~1-in-9 in one measured batch), pre-
  existing native-crash flake under the offscreen platform — present
  before any of the FDTD/matplotlib work, confirmed directly: it still
  reproduces with `--ignore`-ing every new FDTD test file, so the
  `PySide6.QtSvg` module visible in the crash's loaded-extension-modules
  dump is incidental (loaded by something already in the dependency
  tree), not evidence the FDTD integration caused it. Root cause not
  pinned down; treated the same as the `sendEvent` segfault above — a
  known, narrow, environment-specific instability documented here rather
  than silently retried until green. If a run fails, rerun it.
- The hover preview always colors layers with the same deterministic
  default scheme as a brand-new layer would get, not whatever you've
  customized in the open document's Layers panel — and its by-name pixmap
  cache would show stale geometry if you import a second custom file that
  redefines a name from the first. Both are cosmetic, preview-only
  limitations, not correctness issues for the actual design/export.
- The scripting console uses `code.InteractiveInterpreter` for proper
  multi-line handling rather than reimplementing it — but each call needs
  the *full accumulated* buffer, not just the latest line (confirmed
  empirically: passing one line at a time inside a `for` block raised
  `IndentationError`, since the interpreter has no memory of its own
  between calls). Separately, `quit()`/`exit()` raise `SystemExit`, which
  propagates straight through `runsource()` uncaught — confirmed this
  would silently kill the whole desktop app, not just the console, hence
  the explicit `except SystemExit` around every call.
- Console mutations render on the canvas immediately but don't refresh the
  Layers/DRC panels — those only refresh on `undo_stack.indexChanged`,
  which console activity never touches (by design: it bypasses the undo
  stack entirely). `place('straight')` from the console won't make WG show
  up in the Layers panel until some other action touches the undo stack.
  Cosmetic, not a correctness issue, and consistent with the console being
  a deliberate bypass rather than a GUI-equivalent path.
- The Project Settings dialog shows automatically on app startup via
  `QTimer.singleShot(0, window._new_project)` called from `app.main()` —
  deliberately *not* from `MainWindow.__init__` itself. `_new_project()`
  opens a blocking modal dialog; triggering that during construction would
  hang every single test that builds a `MainWindow()` (there's no event
  loop running yet for a synthetic dismissal). The singleShot fires after
  the event loop starts and the window is already shown, which is also
  just better UX (you see the app, then the dialog) than the reverse.
  `_new_project()` itself is split into the dialog-showing wrapper and a
  testable `_reset_to_new_project(settings)` core, the same pattern used
  for every other dialog-gated action in this app (context menu, custom
  component import).
- The suggested single-mode waveguide width uses a real two-step
  effective-index method (slab solve for vertical confinement via
  bisection, then a second slab cutoff for the lateral direction) — not a
  made-up number, but verified during development to run meaningfully
  narrower than real-world practice for silicon specifically (high index
  contrast is a known weak point of EIM): ~319nm cutoff for 220nm SOI at
  1550nm, against the ~450-500nm commonly used and the generic PDK's own
  500nm "strip" default. The dialog's disclaimer says this explicitly
  rather than letting the number imply more precision than it has.
- The Lithium Niobate preset's `core_index` (2.211 at 1550nm) was
  cross-checked against two independent sources rather than taken from
  one: the standard Zelmon, Small & Jundt (1997) Sellmeier fit for
  congruent LiNbO3, and a second data point — the photonfdtd project's
  own LNOI mode-solver example uses n=2.30 at 600nm, which the same
  Sellmeier equation reproduces to 3 significant figures (2.296),
  confirming the coefficients are correct rather than mis-remembered. The
  Lithium Tantalate preset's index (2.14) is a standard literature
  reference value but didn't have a second source available to
  cross-check against during development — noted as such in the code
  rather than presented with the same confidence as the LN figure.
- `import_script.py` only inspects a script's *direct top-level
  statements* (`tree.body`), never a recursive `ast.walk()`. Confirmed
  empirically why this matters: a hand-written `for i in range(3): inst =
  top.add_ref(...)` matches the exact same static assignment shape as one
  real Phidler-generated instance — a recursive walk would silently
  reconstruct 1 instance from code that creates 3 at runtime, since
  static analysis can't know how many times a loop body actually
  executes. Restricting to top-level statements turns that into "this
  loop isn't a recognized top-level shape" → a clear `ScriptParseError`,
  not a silent wrong answer. The same restriction is why a renamed
  `inst_N` variable still loads (it's still a flat, top-level statement,
  just without a recoverable original id) while a loop, conditional, or
  helper function around the same code does not.
- Port-to-port snapping reuses `kdb.DCplxTrans` (the same transform
  primitive used for instance geometry) to compute absolute port
  positions, rather than re-deriving the mag/rotation/mirror composition
  for ports separately — verified directly against a known transform
  (a 90°-rotated, translated waveguide's ports land exactly where the
  rotation+translation predicts). While dragging, it has to compute a
  dragged instance's port positions against its *live, uncommitted* Qt
  item position, not the document's (still-stale) stored transform — see
  `LayoutDocument.get_absolute_ports_for_transform`, which takes an
  explicit override transform for exactly this.
- The Properties panel's precision-entry transform fields use the exact
  same is-interacting-style guard the on-canvas handles use for their own
  periodic resync (`PropertiesPanel._is_editing_transform`, checked via
  `QWidget.hasFocus()`) — but `hasFocus()` never becomes true under
  `QT_QPA_PLATFORM=offscreen`, confirmed empirically across several
  `setFocus()`/`activateWindow()` combinations, since there's no real
  window manager to grant input focus headlessly. The regression test for
  this guard's logic monkeypatches the guard method directly rather than
  trying to exercise real Qt focus, which this environment can't produce.
- The measure tool's label uses `QGraphicsItem.ItemIgnoresTransformations`
  for constant on-screen text size across zoom — the same flag the
  on-canvas transform handles use, and a real, easy-to-hit mistake during
  development: setting the flag via `label.ItemIgnoresTransformations`
  (an instance attribute lookup on a `QGraphicsSimpleTextItem`) raises
  `AttributeError`, since the flag is only defined on the `QGraphicsItem`
  base class, not inherited into the instance namespace that way — caught
  immediately by actually running a simulated click through the real
  widget rather than only unit-testing the math in isolation.
- Port-snapping for a measurement click reuses `InstanceItem.nearest_port`
  unchanged — the same hit-radius logic and constant routing mode's port
  clicks already use — rather than introducing a second, possibly
  inconsistent distance threshold for "close enough to snap."
- Align/Distribute's "top"/"bottom" had a real, easy-to-get-backwards
  trap: a plain `QRectF`'s `top()`/`bottom()`/`left()`/`right()` only
  return whatever min/max order the rect happened to be *constructed*
  with — confirmed empirically with `QRectF().setCoords(0, 5, 10, 0)`,
  where `top()` returned 5.0 and `bottom()` returned 0.0, the opposite of
  the conventional smaller/larger-y meaning. `QGraphicsItem.mapRectToScene`
  happened to already return a normalized rect in the cases tested, but
  `_selected_scene_bboxes` calls `.normalized()` explicitly anyway rather
  than depending on that being guaranteed. Separately, even a correctly
  *normalized* rect's `top()` (the smaller scene-y) is the **visual
  bottom** on screen, not the visual top — confirmed empirically that the
  canvas's global Y-flip makes a larger scene-y coordinate render higher
  up — so "Align Top" deliberately reads `box.bottom()` internally, with
  a regression test (`test_align_top_uses_the_visual_screen_direction_
  not_qrectf_naming`) that exists specifically to catch a future edit
  that gets this backwards.
- Align/Distribute computes each instance's move as a single scalar
  shift along one axis (`target − current_edge_or_center`, added to the
  stored `x` or `y`), rather than decomposing each instance's rotation/
  mirror/scale to reposition it — this works uniformly regardless of an
  instance's own rotation or scale, since `mapRectToScene` already gives
  the correct axis-aligned bounding box for *any* transform, and only the
  position needs to change for a pure align/distribute (rotation, mirror,
  and scale are deliberately left untouched).
- The FDTD integration is split into a pure-compute core (`fdtd_sim.py`,
  no Qt or threading at all) and a thin Qt layer on top (`fdtd_window.py`'s
  `FdtdWorker`/`ModeWorker`, `QThread` wrappers) — built and tested in
  that order, on advice given before writing any of it: entangling
  compute with threading is what makes a feature like this hard to test,
  so they're kept apart deliberately, not as an afterthought refactor.
- The original v1 of this feature forced `z_size=0.0` on
  `from_gdsfactory`'s 3D `Simulation`, collapsing the vertical dimension
  to one cell for speed — this was **replaced**, not kept: a user asked
  "shouldn't I be able to set cladding thickness?", and investigation
  showed the quasi-2D collapse made that setting inert regardless of its
  value, since the single z-slice resolved core/cladding contrast purely
  by XY polygon footprint, not vertical position. True 3D is genuinely
  more expensive (calibrated empirically: 141k cells/394 steps → 2.5s,
  525k cells/1312 steps → 40.8s, ~6×10⁻⁸s/cell-step), but is what makes
  cladding thickness — and the "money shot" top-down field movie — mean
  what they claim to mean.
- Removing the z-collapse surfaced a real, separate bug, caught by
  directly inspecting `sim.eps_r` rather than trusting the adapter's
  docs: background slabs only covering *above* and *below* the core's
  own z-range left that z-range itself unstamped outside the waveguide
  polygon, silently defaulting to vacuum (`eps_r=1`) instead of lateral
  cladding. Fixed by adding a third background slab spanning the same
  z-range as the core layer, stamped first so the polygon still wins
  inside the waveguide footprint (per `from_gdsfactory`'s own documented
  stamping order).
- `photonfdtd.ModeSolver` (a 2D scalar-Helmholtz cross-section eigenmode
  solver, already in the package, previously unused) is the tool that
  makes "cladding thickness" answerable: at a deliberately-too-thin
  0.05µm cladding the mode is visibly truncated against the solver's
  zero-amplitude domain boundary (wrong n_eff 2.14 vs converged 2.60,
  29% edge/peak amplitude ratio); at ≥1.0µm it converges cleanly (~0%).
  `mode_confinement()` turns this into a direct "well confined" /
  "cladding may be too thin" status message instead of leaving the user
  to interpret a raw ratio. The solver's own eigensolve cost is
  superlinear in grid size (6.5s at 414k points, didn't converge in 30s
  at 1.65M during calibration) — the UI defaults (`cell_size_um=0.02`,
  modest lateral padding) were chosen to stay well under a second, not
  guessed.
- `photonfdtd.sources.SinglePhotonSource` (also already in the package)
  is the literal mechanism behind "inject a photon at a given energy" —
  a `ModeSource` built from a solved mode profile, amplitude-normalized
  to carry approximately `h·freq0` of energy; its own docstring flags
  this normalization as approximate and suggests verifying with a
  `FluxMonitor`. Done: the absolute one-photon baseline measured ~20×
  off from the theoretical value (consistent with that disclosed
  approximation), but the *relative* N-photon scaling was confirmed
  exact — `photon_count=4` gave exactly 4× the integrated flux energy of
  `photon_count=1`, and `photon_count=9` gave exactly 9×. This matters
  because the naive approach (stacking N coherent copies of the source
  at the same place/phase) would have scaled energy by N² instead of N
  — amplitude adds linearly for coherent sources, and energy is
  proportional to amplitude squared. `build_source` instead scales one
  source's amplitude by `sqrt(photon_count)`, confirmed by the test above
  to give the correct linear-in-N energy scaling.
- `from_gdsfactory` itself returns `sources=[]`/`monitors=[]` (per its
  own docstring) — `build_simulation` adds the configured sources and a
  `FieldMonitor` itself; skipping that step would silently produce an
  all-zero field with nothing exciting it, not an error.
- `FdtdWindow`'s matplotlib canvases need an explicit `setMinimumHeight`
  — the same squeeze-to-~10px issue found (and fixed the same way) for
  the original docked panel applies to any matplotlib canvas sharing
  layout space with other widgets; confirmed again by actually grabbing
  a screenshot of the assembled window, not just unit-testing it.
- A real layout-rendering bug was caught the same way: drawing the chip
  outline by setting the plot's axis limits to the layout's own bounding
  box *before* adding the field image clipped almost the entire field
  out of view, since the simulated domain (padding + PML) is wider than
  the bbox. Fixed by drawing the field image first and setting axis
  limits to its own extent, with the chip outline drawn as a reference
  on top — not found by reasoning about the code, found by looking at a
  rendered screenshot and noticing the field was almost entirely cropped
  away.
- Constructing `FdtdWindow()` (via `from phidler.panels.fdtd_window
  import FdtdWindow`, inside `MainWindow._open_fdtd_window`) raises
  `ImportError` if matplotlib isn't installed (it's part of the optional
  `fdtd` extras, not a core dependency) — caught there and shown as a
  message box instead of a crash, so a user without the extras installed
  still gets a fully working app, just without this one menu action's
  real functionality.
- `FdtdWorker`/`ModeWorker`'s own tests call `.run()` directly rather
  than driving a real `QThread` start/stop cycle in most cases, on advice
  given before writing the tests: the compute-correctness confidence
  already comes from `fdtd_sim.py`'s own tests, so the worker only needs
  to show it calls through and emits correctly. A handful of real
  end-to-end tests do exist in `test_fdtd_window.py`, each driven via a
  bounded `QCoreApplication.processEvents()` polling loop rather than a
  blind wait — kept deliberately few, for the same reason.

## Testing

Run the suite with `./run_tests.sh`. It runs headlessly under
`QT_QPA_PLATFORM=offscreen` and currently covers 276 tests.

### Bugs found from actual use

Most of this app was built and tested in a headless environment. The
first time it was actually run on a real display, two real bugs surfaced
immediately that no amount of headless testing could have caught:

- **Panning didn't work at all.** `QGraphicsView` auto-computes its
  scrollable range from the placed content's own tight bounding box. A
  single small waveguide easily fits inside any normal window, so that
  range collapsed to exactly `(0, 0)` — there was nowhere to scroll to,
  even though the middle-drag press/move/release handling itself was
  correct. Fixed by giving the canvas a fixed 100mm×100mm virtual working
  area, independent of however much or little is actually placed.
- **The Layers panel was overwhelming** — pre-populated with the active
  PDK's entire ~47-layer map (every doping layer, via, metal layer, label
  layer, etc.) regardless of whether the current design touched any of
  them. Fixed by having the document start with an empty layer set and
  only add an entry the first time something placed/routed/imported
  actually uses that layer.

Both are now fixed and covered by regression tests (including one driven
through real simulated `QTest` mouse press/move/release sequences for the
panning fix) — but they're a concrete reminder that the "implemented but
unverified" framing below was not hypothetical caution.

### Verification splits into two tiers

It's worth being explicit about which is which rather than letting a
passing test count imply more than it proves:

#### Fully verified, headless, with confidence

Geometry, transforms, and data integrity, which don't depend on how
anything looks:

- Every placed/edited/routed/exported shape's coordinates checked
  numerically against `klayout.db.DCplxTrans` directly, including
  rotation, mirroring, and polygon holes (a real bug here — holes were
  silently dropped from rendering — was caught and fixed).
- The entire 310-component catalog is exhaustively placed and exported in
  `tests/test_scale.py` (not just a hand-picked sample) — this caught two
  real bugs: 25 catalog entries that weren't actually registered in the
  PDK (would have failed on placement) and 4 `ComponentAllAngle` factories
  needing a different placement API than the rest of the catalog.
- Four separate "a mutating operation can raise partway through and leave
  corrupted state" bugs were found and fixed transactionally, each with a
  regression test: (1) an invalid property edit used to delete an
  instance's geometry before validating the replacement, leaving it
  visible but silently dropped from GDS export; (2) `QUndoStack.push()`
  inserts a command even when its `redo()` raises, which could poison the
  undo stack on a failed route or a failed placement; (3) deleting an
  instance and its route together relied on `QUndoStack` undoing a macro's
  children in reverse push order — the original push order made undo fail
  with a `KeyError` partway through, leaving only half the deletion undone;
  (4) the same `redo()`-raises hazard applied to placing a component at
  all, which matters much more now that custom (unvetted, user-supplied)
  components can fail in ways the exhaustively-tested built-in catalog
  never does.
- Save/load round-trips by replaying the document's own recipe
  (component spec + kwargs + transform, port pairs for routes) rather than
  serializing gdsfactory objects, and tolerates a missing/moved reference
  GDS file or custom-component file without failing the rest of the load.
  Re-imports any custom-component files a project used *before* replaying
  instances, since those only exist in the active PDK's registry for the
  process that imported them — verified by simulating a fresh session
  with `pdk.remove_cell()` rather than just reusing the same in-process
  registration the save came from.
- Multi-item drag (dragging one of several selected items moves all of
  them) and middle-drag panning — both confirmed via real simulated
  `QTest` mouse press/move/release sequences, not just unit-level model
  calls (the panning test specifically guards against the no-scrollable-
  range regression described above).
- Pure transform/math helpers (`view.snap()`, cursor-position
  `mapToScene`, zoom-to-fit/selection's scale and containment) checked
  directly rather than via injected mouse events.
- Custom component loading distinguishes functions actually *defined* in a
  user's file from ones merely imported into it (e.g. `from
  gdsfactory.components import straight`) via `__module__`, verified
  empirically that `@gf.cell` preserves that correctly rather than
  assumed; a function that raises or returns the wrong type is skipped,
  not allowed to crash the whole import.

#### Implemented, but genuinely needs your eyes

These features' entire point is how they look or feel, which cannot be
assessed without a real display:

- Right-click context menu, status bar cursor readout, grid pitch/snap
  controls, zoom to fit/selection, component hover preview. The underlying
  logic for each is tested (e.g. "does the menu contain the right
  actions," "does the reported coordinate match the transform," "is the
  rendered preview pixmap non-blank and cached correctly") but **nothing
  tests that the menu visually appears under the cursor, that the
  coordinate label is legible, that zooming feels smooth, or that the
  hover preview's popup position/size/legibility is actually good** —
  those need a human.
- One honest caveat from getting here: a synthetic `QContextMenuEvent`
  injected through Qt's real event system (`QApplication.sendEvent`)
  **segfaulted the interpreter** under this offscreen platform. That code
  path doesn't exist in the actual app (production goes through a real
  platform event, never `sendEvent`), so it isn't a correctness concern,
  but it's why these tests call the event-handler overrides directly as
  plain methods instead of injecting native events — a deliberate,
  narrower verification style than the mouse-drag tests elsewhere in this
  suite.
- Drag responsiveness, pan/zoom comfort, color legibility, whether the
  routing click-to-pick-a-port interaction feels natural — none of this
  changed since the original headless build, and none of it can be judged
  without running it.
- The on-canvas transform handles and the Project Settings dialog are both
  new and both fall in this bucket too. Every piece of their *logic* is
  tested directly — including, for the handles, the specific property
  that actually matters (a corner drag keeps the diagonally opposite
  corner exactly fixed, confirmed via a real simulated `QTest` mouse
  drag through the actual view, not just a direct method call) — but
  `QDialog.exec()` is a blocking modal call, same as every other dialog
  in this app, so nobody has actually seen the Project Settings dialog
  rendered, and whether the handles feel natural to grab/drag at actual
  mouse speed hasn't been judged by a human either.

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
