# Phidler

A graphical CAD application for photonic integrated circuit (PIC) layout,
built on [gdsfactory](https://gdsfactory.github.io/gdsfactory/) (phidl's
actively-maintained successor) with a PySide6 desktop UI, and exportable GDS.

## Running it

```
./run.sh
```

This activates the project's venv and launches the app. (See "Environment
gotcha" below for why a plain `python -m phidler` from a different shell
might crash on this specific machine.)

![Phidler main window: a ring resonator and an MMI splitter on the canvas, with the component palette, layers, and DRC panels visible](docs/screenshots/main_overview.png)

The screenshots in this README are real renders — `QWidget.grab()` against
the actual running app under `QT_QPA_PLATFORM=offscreen`, not mockups. That
confirms every feature paints correctly; it does not confirm how any of it
*feels* to use (drag responsiveness, click precision, whether a layout
feels cramped) — see "Verification status" below for what that distinction
means in practice. The script that generates them,
[`docs/capture_screenshots.py`](docs/capture_screenshots.py), is checked in
so they can be regenerated after future UI changes.

## Features

- **Canvas**: pan (middle-mouse-drag — works reliably regardless of how
  little content is placed, see "Bugs found from actual use" below), zoom
  (scroll wheel), grid with snap-to-grid (pitch and snap toggle adjustable
  from the toolbar), zoom to fit / zoom to selection (View menu, `Ctrl+0` /
  `Ctrl+Shift+0`), live cursor coordinate readout in the status bar,
  right-click context menu. Single global Y-flip so GDS coordinates
  display correctly.
- **Port-to-port snapping**: when dragging an instance, if one of its
  ports ends up within 2µm of another instance's port, it snaps so they
  align exactly — instead of falling back to plain grid-rounding, which
  is what still happens when nothing's close enough. Dragging several
  selected instances together shifts the whole group by the same offset
  if any one of them finds a match, so their arrangement relative to each
  other doesn't shift. Proximity-only — it doesn't check whether the two
  ports actually face each other, a deliberate v1 simplification.
- **Editing**: click to select, drag to move (including multiple selected
  items at once), rotate (`R`) / mirror (`M`), multi-select via rubber-band
  drag or Select All (`Ctrl+A`), delete, copy (`Ctrl+C`) / paste (`Ctrl+V`).
  Full undo/redo (`Ctrl+Z` / `Ctrl+Shift+Z`) for every operation.
- **On-canvas transform controls**: selecting a single instance shows a
  floating panel of buttons/sliders right over it — rotate ±90° buttons, a
  free-rotation slider, a mirror toggle, a scale slider (10%-400%), and a
  reset button. Scale is a genuine geometric magnification of the placed
  instance (klayout `DCplxTrans`'s `mag`, the same mechanism GDS itself
  uses for scaled structure references) — it resizes the actual shape and
  its ports, not a component parameter like `length`. Dragging a slider
  previews live and only commits an undoable change on release, same
  pattern as dragging an instance on the canvas.

  ![Transform overlay shown over a selected bend_euler instance, with rotate buttons, a rotation slider, a mirror toggle, and a scale slider](docs/screenshots/transform_overlay.png)
- **Component palette** (left dock): photonics-core categories (waveguides,
  bends, tapers, couplers, edge couplers, MMIs, rings, MZIs, grating
  couplers, filters, spirals, detectors) shown expanded at the top; the
  generic PDK's other domains (MEMS, quantum/superconducting electronics,
  microfluidics, analog RF, process-control-monitor test structures, dies,
  vias, pads, shapes, text, containers — ~300 components total across all
  categories) are tucked under one collapsed "Other" node instead of
  competing for attention. Names are prettified ("MMI 1x2" instead of
  "mmi1x2"; raw name always available as a tooltip) and filterable by
  either form. Hovering a component shows a floating preview of its actual
  rendered geometry (not an icon) near the cursor. Double-click (or Enter)
  arms placement, then click the canvas to place it.

  <img src="docs/screenshots/palette_hover_preview.png" width="280" alt="Component palette with curated categories and prettified names"> <img src="docs/screenshots/hover_preview_popup.png" width="220" alt="Hover preview popup showing an S-bend's actual rendered geometry">
- **Custom components**: File > Import Custom Components… loads a Python
  file, finds every function *defined* in it (not merely imported) that
  takes no required arguments and returns a `gf.Component`, registers each
  with the active PDK, and adds them to the palette under "Custom" —
  prettified names and all. A broken or unsupported function in the file
  is skipped with a status-bar note rather than failing the whole import.
  The imported file path is remembered and re-imported automatically when
  you reopen a project that uses one of its parts (custom cells only live
  in the PDK's registry for the process that imported them, so without
  this a saved layout using a custom part would fail to reload at all —
  caught in review, fixed the same way as the reference GDS path, with a
  regression test that simulates a fresh process via `pdk.remove_cell()`).
- **Project settings** (shown on startup and File > New, editable anytime
  via File > Project Settings…): pick a material platform (Silicon SOI,
  Silicon Nitride, or Custom indices), core thickness, and design
  wavelength. Sets the default routing cross-section and shows a
  suggested single-mode waveguide width — **read the disclaimer in the
  dialog before trusting that number**: it's a real effective-index-method
  approximation (verified against a known silicon platform during
  development), not a substitute for actual mode-solving, and it's known
  to run narrower than real-world practice for high-contrast platforms
  like silicon (it suggests ~290nm for 220nm SOI at 1550nm; the PDK's own
  "strip" cross-section — and most real designs — default to 500nm). The
  chosen platform/thickness/wavelength are recorded as project metadata
  in both the `.phidler` file and the exported Python script's header —
  they don't change the active PDK or any geometry on their own.

  <img src="docs/screenshots/project_settings_dialog.png" width="380" alt="Project Settings dialog with platform picker and the suggested-width disclaimer">
- **Properties panel** (right dock): editing a selected instance's
  parameters (length, width, radius, cross-section, etc.) regenerates its
  geometry live, undoable. `cross_section` is a dropdown of the active
  PDK's actual valid names, not freeform text, so a typo can't reach
  gdsfactory and fail.
- **Layers panel** (right dock): visibility toggle + color picker per
  layer. Starts empty and only ever lists a layer the first time something
  you actually place/route/import uses it — not the active PDK's entire
  ~47-layer map regardless of relevance (see "Bugs found from actual use").
- **Routing**: toggle "Route" in the toolbar, pick a cross-section from the
  toolbar dropdown, click a port, click a second port — routes between them
  via `gdsfactory.routing.route_single`, rendered on the canvas and fully
  undoable/deletable (including the case where a route and one of its
  endpoint instances are deleted together in the same action).
- **GDS import as a reference backdrop**: File > Import Reference GDS loads
  an existing layout (e.g. a foundry floorplan) to design against. It's
  rendered dimmed and is *not* included in your own GDS export — it's a
  visual backdrop, not part of the design. The reference path round-trips
  through project save/load too.
- **DRC**: a width/spacing check against thresholds *you enter* in the DRC
  panel (right dock) — **not validated against any official foundry rule
  deck**, since the active generic PDK doesn't expose one. Violations are
  listed and double-click-to-zoom on the canvas.

  ![DRC panel showing a width violation found against a 0.2um threshold](docs/screenshots/drc_violation.png)
- **Project save/load**: File > New/Open/Save/Save As round-trips a
  `.phidler` JSON file capturing the editable design (instances, routes,
  layer colors, reference backdrop path) — distinct from File > Export
  GDS, which is the flattened output artifact.
- **GDS export**: File > Export GDS (or the toolbar button).
- **Python script export**: File > Export Python Script… writes a
  standalone `.py` file that recreates the design via direct gdsfactory
  calls (`gf.get_component(...)`, `add_ref`, `route_single`) — for keeping
  the layout as reviewable, version-controllable code, alongside (not
  instead of) the `.phidler` recipe and the flattened GDS. Reuses the same
  instance/route data `save_project` does, just emitted as Python source.
  Running the exported script directly (`python my_design.py`) writes a
  `.gds` named after the script itself (`my_design.gds`) next to it —
  verified by actually invoking it as a subprocess, not just importing it
  as a module (`__name__` is only `"__main__"` when run that way, which
  matters since that's the gate on the GDS-writing code). A route whose
  endpoint instance no longer exists (no cascade-delete is a known
  limitation) becomes a comment in the script rather than a crash when the
  script is run.
- **Python script import**: File > Open now also accepts the `.py` files
  this app exports — it parses the script's actual AST (not by executing
  it and introspecting the result, which would lose the original
  component name + kwargs behind gdsfactory's mangled internal cell
  names; not via a separately-embedded data blob either, which could
  silently drift from hand-edits) to recover instances, transforms,
  routes, custom-component paths, and project settings directly from the
  real code. **This means editing a value directly in the script — e.g.
  changing `length=10.0` to `length=25.0` — and reopening it picks up the
  edit**, which is the actual point: the script is read as the source of
  truth, not a frozen snapshot. Additive to `.phidler`, not a replacement
  for it: layer color/visibility overrides and the reference GDS backdrop
  path have no representation in the generated script and reset to
  defaults on a `.py` open. Opening a `.py` also deliberately does not
  set the "current project path" — Save afterward goes through Save As
  instead of silently overwriting your script with JSON. The parser
  understands simple literal-value edits to Phidler's own generated
  shape; restructuring the code (loops, renamed-beyond-`inst_N`
  variables creating ambiguity, helper functions) raises a clear error
  rather than silently reconstructing something wrong — confirmed
  empirically that a naive recursive parse would otherwise silently
  collapse a `for` loop creating 3 instances down to 1.
- **Scripting console** (bottom dock, View menu to toggle): an interactive
  Python REPL running against the live session — `gf`, `doc`
  (`LayoutDocument`), `scene` (`LayoutScene`), `view` (`LayoutView`), `win`
  (this window), plus `place(spec, x=, y=, rotation=, mirror=, **kwargs)`
  and `route(inst_a, port_a, inst_b, port_b, cross_section=)` convenience
  helpers that both update the model *and* render immediately. Supports
  multi-line blocks (`for`/`if`/`def`, waits for a blank line like a normal
  REPL) and Up/Down history. Everything the console does — `doc`/`scene`
  calls directly, or through `place()`/`route()` — is real and immediate
  but **bypasses the undo stack**; only the actual UI actions (palette,
  toolbar, menus) push undoable commands. Power-user tool, not a
  GUI-equivalent shortcut.

  ![Scripting console session placing two components, routing them, and printing a summary](docs/screenshots/console_session.png)

## Bugs found from actual use

Everything above this point in development had only ever been checked
headlessly. Once actually run on a real display, two real bugs surfaced
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

## Verification status

This was built and iterated on primarily in a **headless environment**
(`QT_QPA_PLATFORM=offscreen`). 178 automated tests cover what's checkable
that way; run them with:

```
./run_tests.sh
```

That verification splits cleanly into two tiers, and it's worth being
explicit about which is which rather than letting a passing test count
imply more than it proves:

**Fully verified, headless, with confidence** — geometry, transforms, and
data integrity, which don't depend on how anything looks:
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

**Implemented, but genuinely needs your eyes** — these features' entire
point is how they look or feel, which cannot be assessed without a real
display:
- Right-click context menu, status bar cursor readout, grid pitch/snap
  controls, zoom to fit/selection, component hover preview. The underlying
  logic for each is tested (e.g. "does the menu contain the right
  actions," "does the reported coordinate match the transform," "is the
  rendered preview pixmap non-blank and cached correctly") but **nothing
  tests that the menu visually appears under the cursor, that the
  coordinate label is legible, that zooming feels smooth, or that the
  hover preview's popup position/size/legibility is actually good** —
  those need a human.
- One honest caveat from getting here: I tried to test the context menu by
  injecting a synthetic `QContextMenuEvent` through Qt's real event
  system (`QApplication.sendEvent`) — it **segfaulted the interpreter**
  under this offscreen platform. That code path doesn't exist in the
  actual app (production goes through a real platform event, never
  `sendEvent`), so it isn't a correctness concern, but it's why these
  tests call the event-handler overrides directly as plain methods instead
  of injecting native events — a deliberate, narrower verification style
  than the mouse-drag tests elsewhere in this suite.
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

Please launch `./run.sh` and try:

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
menu appearing in the wrong place), tell me and I'll tune it.

## Environment gotcha (this machine specifically)

This machine has an apt-installed Qt6 6.4.2 under
`/usr/lib/x86_64-linux-gnu/`, which conflicts with the newer Qt6 bundled
inside the PySide6 wheel (`undefined symbol` crash on import) because the
dynamic linker finds the system one first. `run.sh` and `run_tests.sh` work
around this by prepending PySide6's own Qt lib directory to
`LD_LIBRARY_PATH` before launching. If you ever run the app a different way
(not via those scripts), you may need to do the same manually.

## Architecture

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

Key design notes:
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
  letting `QGraphicsView` auto-size it from placed content — see "Bugs
  found from actual use" above for why that auto-sizing breaks panning
  entirely once content fits inside the viewport.
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
