# Feature Tour

A visual walk through what Phidler can do. If you'd rather build something
hands-on, jump to the [MZI Tutorial](tutorial.md); for the full reference, see
the [User Guide](guide.md).

## Open or start a project

Launching Phidler opens a recent-projects list — pick up where you left off,
browse for a file, or start fresh (which opens
[Project Settings](guide.md#project-settings) to choose a material platform).

<img src="screenshots/startup_dialog.png" width="440" alt="The startup dialog listing recent projects with Open Selected, Open Other, and New Project buttons">

## The workspace

One window holds the whole flow: a searchable **component palette** on the left,
the **canvas** in the middle, and docked **Properties**, **Layers**, **DRC**, and
**Console** panels on the right and bottom. Toolbars across the top carry the
place / route / measure tools and the grid, snap, units, export, and simulate
controls.

![The Phidler main window with a Mach–Zehnder interferometer laid out](screenshots/main_overview.png)

## Place components

Every gdsfactory PDK component is in the palette, grouped by kind. Type in the
filter box to narrow hundreds of factories down to what you want, and hover any
entry for a live rendered preview before you place it.

<img src="screenshots/palette_search.png" width="330" alt="Component palette filtered to 'ring', showing matching couplers, rings, and dies"> <img src="screenshots/palette_hover_preview.png" width="360" alt="Palette with a hover preview popup showing a rendered thumbnail of the component under the cursor">

The catalog is the whole gdsfactory generic PDK, grouped by kind — waveguides,
bends, couplers, splitters, rings, gratings, edge couplers, and more.

<img src="screenshots/palette_catalog.png" width="320" alt="The component palette with its top categories expanded, showing the grouped PDK catalog">

Selecting a placed component opens its full parameter set in the **Properties**
panel — position and orientation, an **Array** section for tiling, and every
gdsfactory factory argument (radius, gap, length, …), edited live.

<img src="screenshots/properties_panel_example.png" width="300" alt="Properties panel for a ring resonator showing transform, array, and component parameter fields">

## Edit on the canvas

Drag to move; grab the on-canvas handles to rotate and scale; or type exact
values into Properties. A right-click gives you rotate, flip, align, copy,
delete, and zoom — all also on the **Edit** menu with keyboard shortcuts.

<img src="screenshots/transform_overlay.png" width="440" alt="A selected bend with on-canvas transform handles for rotation and scaling"> <img src="screenshots/context_menu.png" width="250" alt="The right-click canvas context menu listing rotate, flip, align, copy, delete, and zoom actions">

Select several components at once to move them as a group — or line them up and
space them evenly with the **Align** and **Distribute** tools.

![Two components selected together on the canvas](screenshots/multi_select.png)

**Align** snaps a selection to a shared edge or centre:

![Three splitters aligned to a common top edge](screenshots/align_result.png)

…and **Distribute** spaces three or more evenly:

![Three ring resonators spaced evenly after Distribute Horizontally](screenshots/distribute_result.png)

A background grid keeps things tidy — with **Snap** on, placement, dragging, and
routing all round to the grid pitch you set.

<img src="screenshots/grid_snap_closeup.png" width="470" alt="A close-up of the canvas grid with a waveguide port snapped to a grid intersection">

## Array a component

Need a fiber array, a bank of rings, or a splitter tree? Set columns, rows, and
pitch in the Array section and Phidler tiles the component as a single unit.

![A vertical grating-coupler array placed as one arrayed instance](screenshots/array_layout.png)

## Route between ports

Click a port, then another, and Phidler draws a waveguide between them. Pick the
**cross-section** the route is drawn with, route with all-angle diagonal bends or
Manhattan, and — the good part — set a **goal length** and let Phidler insert an
adiabatic meander to hit it, so matched delays come for free.

![Routing mode: hovering a port with the rubber-band preview tracking to the target](screenshots/tutorial_routing_feedback.png)

<img src="screenshots/cross_section_dropdown.png" width="230" alt="The cross-section dropdown open, listing strip, rib, nitride, and other PDK cross-sections">

Select a route to read back its exact length and propagation time, including how
far it lands from your goal.

![An MZI with a meandered, length-matched lower arm and the route length readout](screenshots/tutorial_mzi_delay_readout.png)

## Measure

The measure tool reports the distance between any two points — snapping to
nearby ports — with a dimension annotation right on the canvas.

![The measure tool showing the distance between two ports with an on-canvas dimension line](screenshots/measure_tool_example.png)

## Distance or delay

Switch the **Units** control from microns to propagation time and the rulers and
readouts re-express length as time-of-flight (fs / ns), using the effective
index from your last mode solve — the view that matters when you're budgeting
delays.

![The canvas rulers labelled in femtoseconds of propagation time instead of microns](screenshots/units_time_view.png)

## Layers

The **Layers** panel lists every layer actually used by your design, with a
visibility toggle and an editable colour per layer.

<img src="screenshots/layers_panel_example.png" width="300" alt="The Layers panel listing waveguide, slab, metal, and via layers with visibility toggles and colour swatches">

## Trace over a reference

Import an existing GDS as a dimmed, non-editable backdrop and lay your design
over it — handy for matching an existing chip or a supplied floorplan.

![A new waveguide being drawn over a dimmed reference-GDS ring resonator](screenshots/reference_overlay.png)

## Check design rules

A quick DRC checks minimum width and spacing on a chosen layer and lists the
violations; click one to zoom straight to it on the canvas.

<img src="screenshots/drc_panel_results.png" width="320" alt="The DRC panel with min-width and min-spacing controls and a flagged violation"> <img src="screenshots/drc_violation.png" width="470" alt="A DRC width violation highlighted in red on the canvas">

## Simulate with FDTD

Phidler ships a real [FDTD engine](guide.md#fdtd-simulation). Start with the
**vertical mode profile** — solve the guided mode of your waveguide cross-section
and see its field and effective index.

![The FDTD mode-profile tab showing the solved guided mode field](screenshots/fdtd_mode_profile.png)

Then place **sources** on the canvas — a plain dipole, a mode-matched single
photon, a scripted waveform, or a Cherenkov track — pick the cladding material,
choose CPU/Numba/GPU, and run.

<img src="screenshots/fdtd_source_setup.png" width="420" alt="The FDTD propagation controls with two sources placed in the source table"> <img src="screenshots/fdtd_source_kind_dropdown.png" width="150" alt="The source-kind dropdown listing dipole, single_photon, scripted, and cherenkov"> <img src="screenshots/fdtd_cladding_material_dropdown.png" width="300" alt="The cladding-material dropdown listing SiO2, air, Si3N4, and other presets">

Watch the pulse propagate through your actual layout, scrub the field movie
frame by frame, and export it as an animated GIF.

![An FDTD propagation snapshot: a pulse mid-flight along a waveguide, with playback controls](screenshots/fdtd_field_midframe.png)

Runs too big for your laptop? Offload to a **GPU** (NVIDIA CUDA or AMD ROCm) or
to a **remote server** over SSH — the progress bar and results come back the same
either way.

<img src="screenshots/remote_config_dialog.png" width="460" alt="The remote-server configuration dialog with host alias, remote directory, Python path, and a GPU toggle">

## Script it when you want to

For anything the UI doesn't cover, a **scripting console** runs Python against
the live session with `gf`, the document, and `place()`/`route()` helpers — mix
clicking and code freely.

![The scripting console placing and routing components against the live session](screenshots/console_session.png)

## What you can build

From a single waveguide to a full circuit — splitters and combiners, ring
resonators and add–drop filters, fiber-coupler arrays, delay lines, and
interferometers — laid out visually and exported as GDS.

![A clean add–drop ring resonator laid out on the canvas](screenshots/showcase_ring.png)

Active devices too — here a thermo-optic phase shifter, its metal heater and via
stack sitting over the optical waveguide (note the metal layers in the Layers
panel).

![A straight waveguide with a metal heater and via routing — a thermo-optic phase shifter](screenshots/heater_showcase.png)

## Save & export

Projects save to an editable `.phidler` file that remembers your whole
design and simulation set-up. When you're done, export a foundry-ready
**GDSII**, or a Python script that rebuilds the layout in gdsfactory.

<img src="screenshots/menu_file.png" width="300" alt="The File menu showing New, Open, Save, Import Reference GDS, Import Custom Components, Export GDS, and Export Python Script">

---

Ready to build one yourself? Start the [MZI Tutorial »](tutorial.md)
