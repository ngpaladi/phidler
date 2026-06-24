# User Guide

## Project Settings

On startup (and via File > New), Phidler shows a Project Settings dialog:

<img src="screenshots/project_settings_dialog.png" width="420" alt="Project Settings dialog">

- **Platform**: pick Silicon (SOI), Silicon Nitride, or Custom to set the
  core/cladding refractive indices and core thickness.
- **Design wavelength**: used together with the platform to estimate a
  suggested single-mode waveguide width.
- **Default cross-section**: which gdsfactory cross-section new routes use
  by default.

The suggested width is an estimate from the effective-index method, not a
full 2D mode solve — read the note in the dialog before treating it as
exact. It's a starting point, not a substitute for your foundry's PDK
documentation.

You can reopen this dialog anytime via **File > Project Settings…**
without clearing your current design.

## Placing components

Use the **Components** panel on the left:

- Type in the filter box to search by name.
- Core photonics categories (waveguides, bends, couplers, MMIs, rings,
  MZIs, grating couplers, filters, spirals, tapers, edge couplers,
  detectors) are expanded by default. Everything else (MEMS, quantum,
  microfluidics, analog, test structures, etc.) is under **Other**.
- Hover over a component to preview its actual geometry before placing it.
- Double-click (or select and press Enter) to arm placement, then click
  on the canvas to place it.

<img src="screenshots/palette_hover_preview.png" width="260" alt="Component palette"> <img src="screenshots/hover_preview_popup.png" width="200" alt="Hover preview">

### Custom components

**File > Import Custom Components…** loads a Python file and adds any
`@gf.cell`-decorated functions it defines (with no required arguments) to
the palette under **Custom**. The file is remembered with the project, so
reopening a saved project that uses a custom part re-imports it
automatically.

## Editing on the canvas

- **Select**: click an item. Rubber-band drag to select multiple, or
  **Edit > Select All** (`Ctrl+A`).
- **Move**: drag a selected item. Dragging a port close to another
  instance's port snaps them into exact alignment; otherwise the move
  snaps to the grid.
- **Rotate / Mirror**: `R` / `M`, or use the on-canvas transform overlay
  (see below).
- **Delete / Copy / Paste**: `Delete`, `Ctrl+C`, `Ctrl+V`.
- **Undo / Redo**: `Ctrl+Z` / `Ctrl+Shift+Z`.
- **Pan**: middle-mouse-drag. **Zoom**: scroll wheel, or View > Zoom to
  Fit / Zoom to Selection (`Ctrl+0` / `Ctrl+Shift+0`).
- **Right-click** the canvas for a context menu of common actions.
- Grid pitch and snap-to-grid are adjustable from the toolbar.

### Transform overlay

Selecting a single instance shows a floating panel directly over it:

<img src="screenshots/transform_overlay.png" width="520" alt="Transform overlay">

- Rotate ±90° buttons, or drag the rotation slider for free rotation.
- Mirror toggle.
- Scale slider (10%–400%) — this is a real geometric magnification (it
  resizes the instance's shape and ports), not a component parameter.
- Reset clears rotation, mirror, and scale back to defaults.

Dragging a slider previews live; the change commits to the undo stack
when you release it.

## Editing parameters

Select an instance and use the **Properties** panel (right side) to edit
its parameters — length, width, radius, cross-section, etc. Click **Apply**
to regenerate its geometry. `cross_section` is a dropdown of the active
platform's valid names, so you can't type something invalid.

## Layers

The **Layers** panel (right side) lists every layer your design actually
uses — it starts empty and grows as you place/route/import. Toggle
visibility or change a layer's color with the checkbox and color swatch.

## Routing

1. Click **Route** in the toolbar (or press the shortcut shown in the
   tooltip).
2. Pick a cross-section from the toolbar dropdown.
3. Click a port on one component, then a port on another. A route is
   drawn between them.
4. Routes are selectable and deletable like any other item, and fully
   undoable.

## Reference GDS backdrop

**File > Import Reference GDS…** loads an existing layout (e.g. a foundry
floorplan) to design against. It's shown dimmed and is **not** included
in your own GDS export — it's purely a visual aid. **File > Clear
Reference** removes it. The reference path is remembered with the saved
project.

## Design rule checking (DRC)

The **DRC** panel (right side) runs a width/spacing check against
thresholds you enter yourself:

<img src="screenshots/drc_violation.png" width="600" alt="DRC panel showing a violation">

1. Pick a layer.
2. Enter a minimum width and/or minimum spacing in microns.
3. Click **Run Check**.
4. Double-click a violation in the results list to jump the canvas to it.

This checks against the numbers you typed in, not against any official
foundry rule deck — the generic PDK this app uses doesn't ship one.

## Saving, loading, and exporting

| Action | Menu | Produces |
|---|---|---|
| Save your editable project | File > Save / Save As | `.phidler` (JSON) |
| Reopen a project | File > Open | accepts `.phidler` or `.py` |
| Export final GDS | File > Export GDS | `.gds` |
| Export as Python code | File > Export Python Script… | `.py` |

**`.phidler`** is the full-fidelity project format — instances, routes,
layer colors, the reference backdrop path, and project settings all
round-trip exactly.

**Exported `.py` scripts** recreate your design with direct gdsfactory
calls (`gf.get_component(...)`, `add_ref`, `route_single`) — useful for
keeping a design as reviewable, version-controlled code. Running the
script directly (`python my_design.py`) writes a `.gds` named after the
script next to it.

**Opening a `.py` script** (File > Open) reads the actual code back into
Phidler — including hand-edits. If you change `length=10.0` to
`length=25.0` directly in the script and reopen it, Phidler picks up your
edit. This is additive to `.phidler`, not a replacement: layer colors and
the reference backdrop don't have a representation in the script format
and reset to defaults when you open one. Restructuring the generated code
(loops, helper functions, heavily renamed variables) isn't supported and
will raise a clear error rather than silently guessing wrong.

## Scripting console

The **Console** dock (bottom, toggle from the View menu) is a Python REPL
running against your live session:

<img src="screenshots/console_session.png" width="700" alt="Scripting console">

Available names:

| Name | What it is |
|---|---|
| `gf` | the `gdsfactory` module |
| `doc` | the current `LayoutDocument` |
| `scene` | the current `LayoutScene` |
| `view` | the current `LayoutView` |
| `win` | the main window |
| `place(spec, x=, y=, rotation=, mirror=, **kwargs)` | places a component immediately |
| `route(inst_a, port_a, inst_b, port_b, cross_section=)` | routes between two ports immediately |

Multi-line blocks (`for`, `if`, `def`, ...) work like a normal REPL — keep
typing until you enter a blank line. Up/Down arrows recall history.

Everything the console does is real and immediate, but **bypasses the
undo stack** — it's a power-user tool for quick scripted edits, not a
replacement for the normal undo-tracked UI actions.
