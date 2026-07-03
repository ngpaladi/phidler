# User Guide

Phidler is a graphical layout tool for photonic integrated circuits, built on
[gdsfactory](https://gdsfactory.github.io/gdsfactory/) and its generic PDK. You
place real photonic components, connect them with real routed waveguides, verify
the result, and export a foundry-ready GDS — without writing code, though you can
drop into Python at any point.

This page is the **reference** for every panel and tool. If you would rather
learn by building something concrete, the [MZI tutorial](tutorial.md) walks the
whole flow end to end on a real device; here we explain each piece in depth.

## The design process, end to end

A photonic layout comes together in roughly this order. Each phase feeds the
next, so it helps to see the whole arc before diving into the details:

1. **Pick a platform** ([Project Settings](#project-settings)). The material
   system — silicon, silicon nitride, lithium niobate/tantalate — sets the
   core/cladding indices and thickness that drive the suggested waveguide width,
   the propagation-time readouts, and the FDTD stack. Choose this first: it is
   the physical context everything else is interpreted in (and it is reopenable
   anytime).
2. **Place components** ([Placing components](#placing-components)). Drop the
   building blocks of your circuit — splitters, bends, rings, grating couplers —
   from the palette. Phidler exposes ~300 components from the generic PDK,
   grouped by function. This is choosing *what* sits on the chip.
3. **Set each component's parameters** ([Editing parameters](#editing-parameters)).
   Dial in lengths, widths, radii, gaps, and the **cross-section** each part
   uses. The cross-section is the most consequential choice — it decides the
   waveguide's width and which [layers](#layers) the geometry lands on, and it is
   what lets a component connect cleanly to the rest of your circuit.
4. **Connect them with routes** ([Routing](#routing)). Click port-to-port and
   Phidler lays down a real waveguide with low-loss Euler bends, optionally
   length-matched to a target delay. This is where a set of parts becomes a
   circuit.
5. **Verify**. Two independent checks: [DRC](#design-rule-checking-drc) for
   geometric rules (minimum width/spacing), and [FDTD simulation](#fdtd-simulation)
   for the physics — first a sub-second
   [vertical mode solve](#checking-your-cladding-is-thick-enough) to confirm the
   waveguide guides a single, well-confined mode, then full 3D propagation to
   watch light move through the device. The mode solve's effective index feeds
   back into the propagation-time readouts, so verifying and designing inform
   each other.
6. **Export** ([Saving, loading, and exporting](#saving-loading-and-exporting)).
   A `.phidler` project to keep editing, a `.gds` for the foundry, or a
   gdsfactory `.py` script for code review and version control.

In practice you will not go straight down the list — you will route, hit a
spacing violation, widen a gap, re-route, re-simulate. The point is that the
tools are arranged around that loop. The rest of this guide covers each phase in
turn.

## Project Settings

On startup (and via File > New), Phidler shows a Project Settings dialog:

<img src="screenshots/project_settings_dialog.png" width="420" alt="Project Settings dialog">

- **Platform**: pick Silicon (SOI), Silicon Nitride, Lithium Niobate (LN),
  Lithium Tantalate (LT), or Custom to set the core/cladding refractive
  indices and core thickness. LN/LT use real published thin-film-on-
  insulator index values (cross-checked against literature; see
  [Development](development.md#key-design-notes) for sourcing), not a
  full multi-layer stack-up model — there's no LN/LT-specific
  cross_section in the generic PDK either, so these presets affect the
  suggested-width estimate and project metadata, same as the other
  platforms, not the actual GDS layers.
- **Design wavelength**: used together with the platform to estimate a
  suggested single-mode waveguide width.
- **Cladding thickness**: a generic default (2µm), not tied to any
  specific foundry process or platform — switching platforms doesn't
  change this field, since it's a wafer/process choice rather than a
  material property. Doesn't affect the suggested-width estimate (which
  assumes a semi-infinite cladding) but is the real vertical extent used
  by FDTD Simulation's mode solver and propagation runs — see
  [FDTD simulation](#fdtd-simulation) for why this number actually
  matters there.
- **Default cross-section**: which gdsfactory cross-section new routes use
  by default.
- **Etch / slab layers**: for rib waveguides, list the partial-etch layers
  (e.g. SLAB150 on layer 2) and the **slab thickness** each leaves behind — the
  core height *remaining* after the etch, less than the full core thickness
  above. FDTD Simulation then models a ridge-over-slab rib instead of a fully
  etched strip (see [FDTD simulation](#fdtd-simulation)). Leave it empty for a
  plain strip waveguide. Add a row with **Add etch layer**; a row left at 0 µm
  is ignored.

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
- Click a component (or select it and press Enter) to arm placement, then
  click on the canvas to place it.

<img src="screenshots/palette_hover_preview.png" width="260" alt="Component palette"> <img src="screenshots/hover_preview_popup.png" width="200" alt="Hover preview">

### The component catalog

Phidler exposes the placeable parts of the generic PDK — roughly 300 components
that build cleanly on their own. (Parts that need required arguments, or that
are not real standalone cells, are filtered out so everything in the palette
actually places.) They are grouped by function; the core photonic categories are
expanded by default, and each one answers a recurring need in a circuit:

<img src="screenshots/palette_catalog.png" width="320" alt="The component palette with its top categories expanded, showing the grouped PDK catalog">


- **Waveguides** — the straight sections (and crossings) light travels along.
  `straight` is the workhorse; `crossing` lets two waveguides cross with low
  crosstalk; `straight_heater_metal_simple` carries a heater on top for
  thermo-optic tuning, and `straight_pin` embeds a PIN junction for modulation.
- **Bends** — turn a waveguide. `bend_euler` is the default low-loss *adiabatic*
  bend (curvature ramps up gradually rather than snapping to a fixed radius);
  `bend_circular` is a constant-radius turn; `bend_s` and `bezier` offset a
  waveguide sideways.
- **Tapers** — smoothly change a waveguide's width or cross-section. `taper` is
  linear; `taper_adiabatic` and `taper_parabolic` are lower-loss profiles;
  `taper_sc_nc` / `taper_nc_sc` convert between silicon and nitride waveguides.
- **Couplers** — split or combine light by *evanescent* coupling between two
  closely-spaced waveguides. `coupler` is a directional coupler; `coupler_ring`
  couples a bus into a ring; `coupler_adiabatic` and `coupler_broadband` split
  flatly across wavelength.
- **Edge couplers** — get light on and off the chip at a cleaved facet:
  `edge_coupler_silicon`, or `edge_coupler_array` for a whole fiber array.
- **MMIs** — multimode-interference splitters, the compact, broadband,
  fabrication-tolerant workhorse for splitting and combining: `mmi1x2` (1→2),
  `mmi2x2` (2→2), plus `_with_sbend` variants that fan the ports apart.
- **Rings** — resonators for filtering, modulation, and sensing: `ring_single`,
  `ring_double`, `ring_single_pn` (a modulator), `ring_double_heater` (tunable),
  and `disk` (a whispering-gallery disk resonator).
- **MZIs** — ready-made Mach–Zehnder interferometers (the [tutorial](tutorial.md)
  builds one by hand from MMIs and routes): `mzi`, and `mzi_lattice` for a
  cascade with a sharper filter response.
- **Grating couplers** — couple light *vertically* to a fiber above the chip via
  a diffraction grating: `grating_coupler_elliptical_te`,
  `grating_coupler_rectangular`, `grating_coupler_array`.
- **Filters** — wavelength- and polarization-selective parts: `dbr` (a Bragg
  reflector), `awg` (an arrayed-waveguide-grating (de)multiplexer),
  `polarization_splitter_rotator`, and `terminator` (dumps unwanted light
  without reflecting it).
- **Spirals** — coil a long waveguide into a small footprint, for delay lines or
  on-chip propagation-loss measurement: `spiral`, `delay_snake`,
  `spiral_racetrack_heater_metal`.
- **Detectors** — `ge_detector_straight_si_contacts`, a germanium photodetector
  that converts guided light into photocurrent.

Everything else — MEMS, quantum/superconducting parts, microfluidics, analog RF,
pads, vias, dies, text, and process-control test structures — lives under
**Other**. Use the filter box to search across all of them by name (it matches
both the raw gdsfactory name and the prettified display label).

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
  snaps to the grid. Snapping is applied live as you drag, not just on drop.
- **Rotate / Flip**: `R` rotates 90°; `H` and `V` flip the selection
  horizontally / vertically (about either screen axis). You can also use the
  on-canvas transform overlay (see below).
- **Delete / Copy / Paste**: `Delete`, `Ctrl+C`, `Ctrl+V`.
- **Cancel with `Esc`**: backs out of whatever you're in the middle of —
  an armed placement, the measure or source tool, or a route. `Esc` works no
  matter which panel has focus (you don't have to click the canvas first).
  Routing is two-stage: the first `Esc` drops a half-finished route (the start
  port you picked), and a second `Esc` exits routing mode.
- **Undo / Redo**: `Ctrl+Z` / `Ctrl+Shift+Z`.
- **Pan**: middle-mouse-drag. **Zoom**: scroll wheel, or View > Zoom to
  Fit / Zoom to Selection (`Ctrl+0` / `Ctrl+Shift+0`).
- **Right-click** the canvas for a context menu of common actions.
- Grid pitch and snap-to-grid are adjustable from the toolbar.

### Aligning and distributing multiple instances

Select 2 or more instances, then use **Edit > Align** (or the same submenu
in the right-click context menu):

- Align Left/Right/Top/Bottom Edges, or Align Horizontal/Vertical Centers.
- Distribute Horizontally/Vertically — needs 3+ instances; spaces their
  centers evenly along that axis, keeping the two extreme instances fixed.

Each is a single undo step, even when it moves several instances.

![Three instances aligned to a common top edge](screenshots/align_result.png)

![Three instances spaced evenly after Distribute Horizontally](screenshots/distribute_result.png)

### Transform handles

Selecting a single instance shows a **rotate handle** above it on the canvas:

<img src="screenshots/transform_overlay.png" width="520" alt="On-canvas rotate handle above the selected shape">

- **Drag the handle above the shape** to rotate freely around the instance's
  position. It previews live and commits to the undo stack on release.
- For a quick 90° rotation, a flip, or resetting rotation/mirror/scale back to
  defaults, use `R` / `H` / `V` or the right-click context menu.
- **Scale is not a drag gesture** — set it numerically in the Properties
  panel's **Scale** field (below), so a stray drag can't accidentally resize a
  component. Scale is a real geometric magnification of the shape and ports,
  not a component parameter like length.

## Editing parameters

Select an instance and use the **Properties** panel (right side) to edit
its parameters — length, width, radius, cross-section, etc. Click **Apply**
to regenerate its geometry. `cross_section` is a dropdown of the active
platform's valid names, so you can't type something invalid.

<img src="screenshots/properties_panel_example.png" width="300" alt="Properties panel showing editable parameters for a selected ring resonator">

### What the parameters are, and how they edit

Phidler reads the component's actual function signature, so the fields you see
are exactly the parameters that part accepts — they differ from component to
component. Every parameter that has a default is shown, and how it edits depends
on its type:

- **Numbers** (lengths, widths, counts, radii) are numeric fields.
- **Booleans** are checkboxes.
- **`cross_section`** is a **dropdown** constrained to the active PDK's valid
  names, so you cannot type one that will not build.
- Other **text** parameters are free-text fields. A few take the *name* of a
  sub-component (e.g. `bend="bend_euler"`); these are editable, but most designs
  leave them at the default.
- A parameter whose default is a complex object (a function, a list) is shown
  greyed-out with its value and marked *"not editable from the panel yet"* —
  adjust those by exporting to a script or from the console instead.

Click **Apply** to rebuild the instance's geometry from the new values (undoable
like any edit). The parameters you will meet most often, across many components:

| Parameter | What it controls |
|---|---|
| `cross_section` | The waveguide profile — width and layers — the part is drawn with. The most consequential parameter; see below. |
| `length` | Length of a straight section, taper, or heater (µm). |
| `width` | Waveguide or feature width (µm). Widening raises confinement and shifts the effective index. |
| `radius` | Bend radius (µm). Smaller is more compact but lossier, down to a minimum the cross-section enforces. |
| `gap` | Edge-to-edge spacing (µm) between two coupled waveguides (couplers, rings) — the single biggest lever on how strongly they exchange light. |
| `length_x` / `length_y` | The footprint of a two-dimensional part — an MZI's arm length, a ring's straight sections. |
| `npoints` | How many points discretize a curve: higher is smoother but heavier geometry. |
| `taper_length` | The length a width/cross-section transition happens over; longer is more adiabatic (lower loss). |
| `spacing` / `pitch` | Center-to-center spacing in arrayed parts (grating-coupler arrays, edge-coupler arrays). |

### Cross-sections

A **cross-section** is the recipe for a waveguide's transverse profile: its
width, and which [layers](#layers) it occupies. It is both a component parameter
(`cross_section`) and the choice in the routing toolbar, so a route and the
components it joins can share one and line up. The generic PDK's main
cross-sections:

| Cross-section | What it is |
|---|---|
| `strip` | The default silicon wire — full-etch `WG` core, ~500 nm wide. Highest confinement; the standard for dense routing. |
| `rib` / `rib2` | A ridge over a partial-etch slab (`WG` + `SLAB`). Lower loss and the basis for doped/active devices, at the cost of weaker lateral confinement. Tell the simulator about the slab so it models the rib correctly — see [Rib waveguides](#rib-waveguides-etch-slab-layers). |
| `nitride` | A silicon-nitride (`WGN`) waveguide — lower index contrast means lower loss and broader bandwidth; used for visible light and low-loss interconnect. |
| `slot` | Two rails separated by a narrow gap that concentrates the field *in* the slot — for sensing and electro-optic/nonlinear fills. |
| `pn` / `pin` | A waveguide with a PN or PIN junction across it (adds doping layers) — the basis of carrier-depletion / injection modulators. |
| `strip_heater_metal` / `rib_heater_doped` | A waveguide with a heater on top (metal) or doped into the slab, for thermo-optic phase tuning. |
| `metal1` / `metal2` / `metal3` / `metal_routing` | Not optical — electrical routing on the metal layers, for the contacts and pads active devices need. |

Because the cross-section decides which layers geometry lands on, your choice
here is what populates the [Layers](#layers) panel: picking `rib`, a `pn`, or a
heater cross-section is exactly what makes the slab, doping, and metal layers
appear.

### Precision transform entry

The same panel has a **Transform** section above the parameter form —
X, Y, Rotation, Mirror, and Scale as typed numeric fields, for exact
placement instead of dragging by eye (matching a known coordinate from a
foundry PDK, for instance). Edit the values and click **Apply Transform**
to commit — it's undoable the same way a canvas drag is. The fields track
the selected instance's live transform automatically; editing one pauses
that tracking until you click elsewhere, so your typing isn't overwritten.

## Layers

Every shape in a GDS layout lives on a **layer** — a `(layer number, datatype)`
pair that tells the foundry which fabrication step draws it: which etch, which
implant, which metal. A photonic component is rarely a single layer; a modulator,
for instance, carries a waveguide core, doping implants, contacts, and metal all
at once.

The **Layers** panel (right side) lists every layer your design *actually* uses.
It starts empty and grows as you place, route, and import — a layer appears the
moment some component puts geometry on it, named from the active PDK (`WG`,
`SLAB150`, `M1`, …). Toggle a layer's visibility or change its color with the
checkbox and swatch; hover a layer for a one-line note on what it is for (the
same descriptions collected below).

<img src="screenshots/layers_panel_example.png" width="320" alt="Layers panel showing active layers with visibility toggles and color swatches">

Layers appear because **components carry them**, not because you draw on them
directly. Phidler routes optical waveguides; it has no electrical-net router, so
the doping, metal, and heater layers below show up when you place a part that
already includes them (a heater-tuned ring, a PN modulator, a detector) or pick a
[cross-section](#cross-sections) that uses them — not from wiring them yourself.

### Layer types in the generic PDK

The generic PDK models a fairly complete silicon-photonics process. Grouped by
what they do, these are the layers you are likely to encounter:

**Silicon waveguide and etch** — the layers that define where light is guided.

| Layer | What it is |
|---|---|
| `WG` | Silicon waveguide core — full etch (~220 nm on standard SOI). Defines single-mode wire waveguides and grating teeth. |
| `SLAB150` | Half-etch silicon slab (~150 nm etch, leaving ~70 nm slab). Used under grating couplers and rib waveguides to improve coupling efficiency and confinement. |
| `SLAB90` | Shallow-etch slab (~90 nm). For very-low-loss rib waveguides, where a thicker slab reduces scattering. |
| `SHALLOW_ETCH` / `DEEP_ETCH` | Process masks for the shallow (~90 nm) and full-depth (~220 nm) silicon etch steps. |
| `DEEPTRENCH` | Deep isolation trench etched through the whole device layer to isolate regions optically and electrically. |
| `UNDERCUT` | Removes buried oxide beneath the silicon to form suspended membranes — MEMS, or ultra-low-loss waveguides. |
| `WGCLAD` | Waveguide cladding (typically SiO₂) surrounding the core. |

The half-etch slab layers are exactly what [FDTD's rib-waveguide
support](#rib-waveguides-etch-slab-layers) needs to know about to simulate a rib
correctly rather than as a fully-etched strip.

**Doping implants** — define PN/PIN junctions for modulators and the
low-resistance contacts that feed them.

| Layer | What it is |
|---|---|
| `N` / `P` | Moderate n-type / p-type silicon doping — the junction of a PN-junction phase shifter or PIN modulator. |
| `NP` / `PP` | N+ / P+ implants — higher doping next to the waveguide to reduce series resistance. |
| `NPP` / `PPP` | N++ / P++ implants — heavy doping for low-resistance ohmic contact to metal vias. |

**Germanium** — for detectors and electro-absorption modulators.

| Layer | What it is |
|---|---|
| `GE` | Germanium grown selectively on silicon; absorbs near-IR (1.3–1.6 µm) for photodetectors and EA modulators. |
| `GEN` / `GEP` | N-doped / P-doped germanium — the cathode and anode contact regions of a Ge photodetector. |

**Silicon nitride** — a lower-contrast, lower-loss guiding layer.

| Layer | What it is |
|---|---|
| `WGN` | Silicon-nitride (Si₃N₄) waveguide core — lower index contrast than silicon gives lower loss and broader bandwidth; used for visible-light and low-loss interconnect PICs. |
| `WGN_CLAD` | Silicon-nitride waveguide cladding. |

**Metals, vias, and heaters** — electrical contact, routing, and thermo-optic
tuning.

| Layer | What it is |
|---|---|
| `VIAC` | Contact via — metal plug from M1 down to silicon or germanium. |
| `VIA1` / `VIA2` | Vias connecting M1→M2 and M2→M3. |
| `M1` / `M2` / `M3` | Metal routing layers 1–3; M3 is typically bond pads and RF lines. |
| `HEATER` | Resistive metal heater over a waveguide for thermo-optic phase shifting (silicon's index drifts ~1.8×10⁻⁴ /K). |
| `PADOPEN` | Pad opening — removes passivation over a bond pad for wire bonding or probing. |

**Process and utility** — non-optical layers the foundry and tooling need.

| Layer | What it is |
|---|---|
| `FLOORPLAN` | Chip floorplan boundary — the die extent. |
| `DICING` | Dicing lane, kept clear of devices so the wafer saw can cleave safely. |
| `DEVREC` | Device-recognition bounding box — the exclusion zone around a cell for placement and DRC. |
| `NO_TILE_SI` | Inhibits dummy-silicon fill in a region. |
| `PADDING` | Extra clearance around a device. |
| `TEXT` / `LABEL_INSTANCE` / `LABEL_SETTINGS` | Human-readable labels and stored metadata — not physical structures. |

**Ports and simulation markers** — annotations, not fabricated geometry.

| Layer | What it is |
|---|---|
| `PORT` / `PORTE` / `PORTH` | Optical / electrical / horizontal port markers — where a cell connects to its neighbors or the outside world. |
| `TE` / `TM` | TE- and TM-polarization port markers (TE — in-plane E-field — is the standard for SOI strip waveguides). |
| `SOURCE` / `MONITOR` | FDTD source and monitor markers — where a simulation injects power or records fields. |
| `DRC_MARKER` | Highlights a design-rule violation; not a physical layer. |

You will rarely see all of these at once — only the layers your particular
components and cross-sections use ever appear in the panel.

## Routing

1. Click **Route** in the toolbar (or press the shortcut shown in the
   tooltip).
2. Pick a cross-section from the toolbar dropdown.
3. Click a port on one component, then a port on another. A route is
   drawn between them — straight sections joined by euler bends
   (continuously-varying curvature, the standard low-loss "adiabatic"
   turn in photonics), not constant-radius circular bends.
4. Routes are selectable and deletable like any other item, and fully
   undoable.

**Diagonal** routing (on by default in the toolbar) sends a route along the
short diagonal path with all-angle euler bends, instead of a manhattan
L/U-turn. Untick it for manhattan-only routes. (Diagonal is ignored when a
length goal is set — those use the manhattan meander.)

**Component avoidance**: when a component sits on the straight path between the
two ports, the route automatically detours around it rather than crossing
through it. This is best-effort — a single detour around the obstacles in the
way; if it can't cleanly clear everything (several components boxing the route
in, or one right at a port), it falls back to a direct route rather than
weaving through. gdsfactory has no general obstacle router, so think of it as
"gets out of the way of the obvious blocker", not guaranteed avoidance on a
dense layout.

<img src="screenshots/routing_example.png" width="700" alt="Routing: a straight waveguide connected to a bend via an auto-routed euler path, with the Route button active in the toolbar">

## Measuring distances

1. Click **Measure** in the toolbar.
2. Click a first point, then a second. A dashed line and label appear
   showing the distance, dx, and dy between them (also shown in the
   status bar) — clicking near a port snaps to its exact center, the
   same way routing's port clicks do.
3. Click again to start a new measurement (clears the old one), or press
   `Esc` to cancel a pending first point and exit Measure mode.

<img src="screenshots/measure_tool_example.png" width="700" alt="Measure tool: a dashed cyan line with a distance/dx/dy label across two components">

Turning on Measure mode turns off Route mode and cancels any armed
placement, and vice versa — only one click-driven mode is active at a
time.

## Reference GDS backdrop

**File > Import Reference GDS…** loads an existing layout (e.g. a foundry
floorplan) to design against. It's shown dimmed and is **not** included
in your own GDS export — it's purely a visual aid. **File > Clear
Reference** removes it. The reference path is remembered with the saved
project.

![A new waveguide drawn over a dimmed reference-GDS backdrop](screenshots/reference_overlay.png)

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

## FDTD simulation

The **Simulate** button in the toolbar opens a separate window that runs a
real local FDTD solve against your actual placed layout, using `photonfdtd` —
a separate, optional dependency not published on PyPI. If it's not
installed, you'll see a message explaining what to install instead of a
crash. The window has two tabs: a fast **Vertical Mode Profile** solver,
and full **Propagation (FDTD)** with movie playback.

### Checking your cladding is thick enough

Before running a full simulation, the **Vertical Mode Profile** tab is
worth a quick check — it solves the guided mode for your waveguide's
cross-section in well under a second:

1. Enter the **Core width**, **Wavelength**, and (optionally) more than
   one mode to solve for.
2. Click **Solve**. The plot shows the mode's intensity confined within
   the core (outlined in cyan over the field), decaying into the cladding
   above and below.
3. The status line reports the effective index and whether the mode is
   **well confined** or whether the **cladding may be too thin** — if
   your cladding (set in **File > Project Settings…**) isn't thick
   enough, the mode gets visibly squashed against the edge of the plot
   instead of decaying naturally. That's your answer to "is my cladding
   thickness enough" without needing a full propagation run.

![Vertical mode profile: a confined mode shown within the waveguide core outline, with n_eff and a confinement status reported above](screenshots/fdtd_mode_profile.png)

### Placing sources and running a simulation

The **Propagation (FDTD)** tab runs a true 3D time-domain simulation and
plays the result back as a movie:

1. Click **Place Source on Canvas**, then click anywhere on the main
   canvas (clicking near a port snaps to its exact position, the same
   as the measure tool). Each click adds a row to the source table and a
   marker on the canvas.
2. Set the source's color either as **Wavelength (µm)** or **Energy
   (eV)** — editing either column updates the other automatically, so
   you can specify "a photon at 0.8 eV" directly instead of converting
   it to a wavelength by hand.
3. In the table, pick each source's **Kind**:
   - **dipole** — a plain oscillating point source. Always available,
     simplest option, not mode-matched to anything.
   - **single_photon** — launches a wavepacket built from the real
     guided mode at that position (needs **Core width** filled in),
     normalized to carry approximately one photon's worth of energy.
     **Photon count** scales the energy up from there.
   - **scripted** — type a Python expression of `t` (time, in seconds)
     into the **Script** column, e.g.
     `np.sin(2*np.pi*1.93e14*t) * np.exp(-((t-3e-15)/1e-15)**2)`
     (`np` is available). Evaluated with the same trust model as the
     scripting console elsewhere in this app — full Python, not a
     restricted sandbox, since this is already a single-user desktop
     tool. Wavelength/Energy/Photon count/Core width are ignored for
     this kind.
   - **cherenkov** — models a charged particle punching *up through the
     chip* (perpendicular to the layout plane, out of the top-down view)
     faster than light's local phase velocity. It is laid down as a track
     of point dipoles along the z axis, each fired with a delay equal to
     the particle's transit time to that point (distance / βc), whose
     superposition forms the Cherenkov shock cone — seen top-down as a
     ring spreading from the impact point. Set the particle speed
     **β = v/c** and the **tilt from vertical** in the source row
     (Cherenkov radiation requires β·n > 1, i.e. faster than the medium's
     phase velocity).
4. Set **Cell size** and **Run time**, then click **Run Simulation**. If
   the estimated run time is more than a few seconds, you'll be asked to
   confirm first — true 3D propagation is genuinely more expensive than
   a quick preview, and this estimate is calibrated against real
   measured runs, not guessed. Run time goes up to 100 ps on the slider
   (1 ns if you type it into the box) for watching light propagate over a
   long distance; however long the run, the movie is kept to a few hundred
   frames so it stays playable.
5. Once it finishes, use the **Play** button and the slider underneath
   to scrub through the field evolving over time, overlaid on a cyan
   outline of your actual chip layout — looping back to the start
   automatically. **Speed** sets the playback rate (0.25×–4×), and
   **Save GIF…** writes the whole animation — field plus chip outline, exactly
   as shown — to an animated GIF at the current speed.

While a run is in flight, a progress bar under the **Compute** button tracks
it: a busy indicator during start-up (the kernel compile locally, or
connect-and-upload for a remote run), then a 0–100 % fill as the solve steps
through time. The same bar drives local, GPU, and remote runs.

![FDTD propagation tab mid-run: the progress bar partway to complete, below the run controls and the "Run on remote server" row](screenshots/fdtd_run_progress.png)

![Propagation result: red/blue field pattern radiating from a point source and coupling into a waveguide, overlaid on its outline, with the time slider and Play button below the source table](screenshots/fdtd_propagation.png)

#### Speed

Propagation runs use a few accelerators so they don't crawl:

- **Numba** (the **Acceleration** row) is on by default when installed. It
  JIT-compiles photonfdtd's field-update kernel — roughly 5× faster than the
  plain NumPy engine — and runs in the background, so the window stays
  responsive. The very first run compiles the kernel and is slower; that's
  cached to disk, so every run after is fast.
- **GPU** (CuPy) is far faster still and is left off by default mainly because
  of its ~1 s startup, which only pays off on larger runs. It runs in a
  separate process now (its own GPU context), so — unlike before — it no longer
  freezes the UI or risks crashing the app; the worker just waits on the child.
  Both GPU vendors work through the same code path: install CuPy's CUDA build
  for an **NVIDIA** GPU (`pip install cupy-cuda12x`) or its ROCm build for an
  **AMD** GPU (`pip install cupy-rocm-5-0`) — photonfdtd uses only generic CuPy
  array ops, so either drives the solve. The status line reports which backend
  actually ran (**"Done on GPU (CUDA)…"** / **"…GPU (ROCm)…"** vs
  **"…on Numba…"**), so a GPU request that quietly fell back to CPU is visible.
- The propagation domain keeps only as much **cladding** as the mode's
  evanescent field actually needs (a few decay lengths, scaled by your
  platform's index contrast), rather than the full cladding the mode solver
  uses — that's a thinner z-stack and a much smaller, faster run, with no change
  to the top-down field you see. (Runs are also single-precision by default.)
- The recorded **field movie** keeps only the one component and the one
  horizontal plane (mid-core, z=0) the playback actually shows, not the whole 3D
  volume — typically ~90× less memory. The plane shown is mid-core height, where
  the guided mode is strongest.

#### Running out of memory on a big layout

The *solve* itself (the field arrays it steps in time) scales with the number of
grid cells — about 100 bytes per cell — and on one machine that's the only thing
that determines peak memory, so the whole-chip grid is what runs you out of
memory. Two things help:

- The **"Large simulation" dialog** now estimates the run before it starts: the
  grid's predicted **memory** (the number that OOMs) and the run **time** on each
  backend (Numba / GPU / plain NumPy). Read it — it tells you if a run won't fit.
- **Simulate selected components only** (the *Region* checkbox): select the
  device(s) you care about on the canvas, and the FDTD domain shrinks to just
  their bounding box (plus your sources). On a sprawling layout that's the
  difference between tens of GB and a couple — gigabytes → fits. It grids whole
  components, so it never cuts a device in half, but light leaving the region is
  absorbed at the edge, so use it for a **local** look at a device, not a
  through-circuit measurement. The memory/time estimate reflects the smaller
  region too.

If even a region is too big, a coarser **cell size** is the other lever (fewer
cells in every dimension), at some cost in accuracy.

#### Running on a remote server

If this machine is slow, low on memory, or has no GPU, you can offload a run to
another machine over SSH — a workstation or a GPU box — and get the field movie
back, while the UI here stays responsive. Tick **Run on remote server** and click
**Configure…** to set it up:

- **SSH host alias** — a `Host` entry from your `~/.ssh/config`. Phidler shells
  out to `ssh`/`scp`/`rsync` and lets your SSH config and agent/keys handle
  authentication; it stores no passwords (key-based, non-interactive auth is
  required).
- **Remote directory** and **Remote Python** — where to install into, and the
  (venv) interpreter to use on the remote.
- **Use GPU on the remote host** — request the remote's GPU regardless of
  whether *this* machine has one. The result reports the backend that actually
  ran, so a fallback to CPU is visible.

**Test connection** checks that the remote Python can import phidler and
photonfdtd. The first time, **Set up remote** does a one-time install: it uploads
the phidler and photonfdtd sources and `pip install`s them into the remote
Python, streaming the output into the log so you can watch for any build error.
After that, ticking **Run on remote server** sends each run to that host and
brings the result back automatically — the progress bar fills from the remote
solve just as it does locally.

<img src="screenshots/remote_config_dialog.png" width="560" alt="Remote server setup dialog: SSH host alias, remote directory and Python, a use-GPU-on-remote toggle, and Test connection / Set up remote actions over a log pane">

The remote must be a POSIX host (Linux/macOS) reachable by key-based SSH.
Projects that place **custom components** (local `.py` files) can't be offloaded
— those files don't exist on the remote — and that case is reported as a clear
error rather than failing mid-run.

**Read the disclaimer in the window.** Both tabs run a real solve against
your geometry, not a mockup — but treat the results as a qualitative
look at how light spreads through your structure, not a calibrated
transmission measurement or an actual quantum simulation, the same
spirit as the waveguide-width estimate in Project Settings. The mode
solver is scalar (no TE/TM distinction), and "photon count" scales
energy correctly *relative* to itself, but the absolute one-photon
baseline isn't exactly h·f — see the window's own disclaimer text for
specifics.

The material stack (core/cladding index, core thickness, **and now
cladding thickness**) comes from your current Project Settings platform
— switching between Silicon, SiN, LN, or LT there changes what gets
simulated here too.

#### Rib waveguides (etch / slab layers)

By default the simulation treats the waveguide layer (1, 0) as a full-height
strip and everything else as cladding. If your design uses a **partial-etch
slab** — a rib waveguide, where a layer like SLAB150 (2, 0) leaves a thinner
slab of core material beside the full-height ridge — list that layer under
**Etch / slab layers** in Project Settings with its slab thickness. Both the
propagation run and the vertical mode solver then model the ridge-over-slab
cross-section: core material up to the slab height in the slab region, full
core height under the ridge. Without it, a rib is simulated as if it were fully
etched (no slab), which changes the mode and the confinement. A configured
layer that isn't actually drawn in the layout is skipped (it won't silently
map the wrong geometry).
