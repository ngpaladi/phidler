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
  model/
    document.py            # LayoutDocument — owns the gdsfactory top cell; ProjectSettings metadata
    placed_instance.py     # PlacedInstance / PlacedRoute records
    layers.py               # layer list, populated on demand as layers are actually used (not pre-seeded)
    commands.py             # QUndoCommand subclasses (Add/Delete/Move/EditParams/Route)
  canvas/
    scene.py                # QGraphicsScene wrapper around LayoutDocument; fixed large sceneRect for panning
    view.py                 # pan/zoom/grid/snap/zoom-to-fit, Y-flip, drag->undo wiring, placement/routing/context-menu
    polygon_item.py         # per-instance QGraphicsItem rendering (hull+holes, ports)
    transform_overlay.py     # on-canvas rotate/mirror/scale buttons+sliders
  panels/
    component_palette.py    # curated category tree (core photonics first, niche under "Other"), click-to-place, hover preview wiring
    component_preview.py     # renders a component's actual geometry to a small pixmap; floating popup widget
    properties_panel.py     # dynamic parameter form from factory signatures
    layers_panel.py          # layer visibility/color dock widget
    drc_panel.py             # DRC threshold inputs + violation list
    console_panel.py          # interactive Python REPL (code.InteractiveInterpreter) against the live session
    project_settings_dialog.py # material/thickness/wavelength picker shown on startup and File > New
tests/                       # all run under QT_QPA_PLATFORM=offscreen
```

## Key design notes

- Scale's transform math (`mag` composing with rotation/mirror) was
  verified against `klayout.db.DCplxTrans` directly before being wired up,
  same discipline as the original rotate/mirror math: mirror and uniform
  scale commute (so `QTransform.scale(mag, -mag if mirror else mag)` in
  one call reproduces klayout's mirror-then-scale exactly), but rotation
  must still be the outermost (last-applied) operation.
- The transform overlay repositions via a 120ms polling timer rather than
  hooking into every view-mutating interaction (pan/zoom/resize/drag)
  individually — simpler and harder to leave a gap in than enumerating
  every path that could move the selected item on screen. It skips
  re-syncing its own slider values while the user is mid-drag on one
  (`QSlider.isSliderDown()`), or the periodic sync would fight the drag.
- `QWidget.isVisible()` reflects the *entire* ancestor chain, not just the
  widget's own shown/hidden state — a test that called `view.show()` but
  never `win.show()` saw the transform overlay (parented through
  `view.viewport()`) report `isVisible() == False` even after calling
  `.show()` on it directly, since `MainWindow` itself was never shown.
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

## Testing

Run the suite with `./run_tests.sh`. It runs headlessly under
`QT_QPA_PLATFORM=offscreen` and currently covers 178 tests.

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
- The on-canvas transform overlay (buttons/sliders for rotate/mirror/scale)
  and the Project Settings dialog are both new and both fall in this
  bucket too. Every piece of their *logic* is tested directly (slider
  live-preview vs. commit-on-release, value sync, the width calculation
  itself) but `QDialog.exec()` is a blocking modal call — same as every
  other dialog in this app — so nobody has actually seen the Project
  Settings dialog rendered, and the transform overlay's on-canvas
  position/sizing/readability hasn't been judged by a human either.

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
