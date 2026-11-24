# Tutorial — Simulating a whole optoelectronic chip

FDTD solves a few tens of micrometres of layout. A chip is millimetres across,
with modulators, routing, pads and detectors, so the **Circuit (whole chip)**
tab of the simulation window treats it as a circuit instead. Every placed
component gets a physics model from photonfdtd, the layout's connections join
them, and the metal is followed from each bond pad to the electrodes, heaters
and detectors it reaches. A whole chip then solves in a fraction of a second
per point, for:

- an **operating point** at the voltages you set;
- a **voltage sweep** of one pad;
- a **wavelength sweep**;
- a **time-domain** run, with square waves, sines, pulses or a PRBS on the
  pads, through each modulator's electrode bandwidth and each detector's
  bandwidth.

It needs photonfdtd 0.16 or newer (with `photonfdtd.circuit` and
`photonfdtd.lnoi`); with an older photonfdtd the tab says so and stays
disabled.

## Where the models come from

| Component | Model |
|---|---|
| **Lithium Niobate (photonfdtd)** palette: phase shifter, MZI switch, delta-beta coupler switch | photonfdtd solves the electrode field and the modes on the device's own cross-section, then carries them along the device. The first time a design is used this takes about a minute; after that it comes from a cache. |
| Waveguide photodetector (same palette) | responsivity, dark current and bandwidth, from its parameters |
| Straights, bends, tapers, optical routes | a waveguide of their length, with the n_eff, n_g and loss set in the tab |
| `straight_heater_metal` | a heater: a resistor between its two contacts plus a thermo-optic phase |
| Pads, metal routes, vias | one piece of metal each |
| `mmi1x2`, `mmi2x2` | ideal splitters |

Any other optical component stops the build with a message naming it.

## Building a small chip

1. **Place** an *LNOI MZI Switch* from **Lithium Niobate (photonfdtd)**, two
   *Waveguide Photodetectors* to its right and a *Pad* above it.
2. **Route** the switch's outputs `o3` and `o4` to the detectors' `o1` with
   the `lnoi_rib` cross-section, and the pad to the switch's `sig_w` port
   with `metal_routing`.
3. Click **Simulate** on the toolbar and choose the **Circuit (whole chip)** tab.
4. Click **Build circuit from layout**. The tab lists:
    - **Components**: every instance and the kind of model it got;
    - **Optical inputs**: the ports left open in the layout, here the
      switch's two inputs. Give one of them a laser power (1 mW by default
      on the first).
    - **Electrical drives**: one row per net. A net is metal that is
      connected together: the pad with its route and the signal electrode,
      each ground electrode, each detector contact. *Connects* lists what
      is on each net.
5. Set the **drives**. *DC* holds a net at *Low / DC*, and *Float* leaves it
   undriven. *Square*, *Sine*, *Pulse* and *PRBS* use *Low*, *High* and
   *Freq / rate*. Set the switch's grounds and the detectors' anodes to DC
   0 V, and the cathodes to DC 2 V for reverse bias.
   A detector needs a circuit between its contacts: left floating, it reads
   0 mA, and the status line under the results says so.

## Running it

- **Operating point**: the results table lists each detector's photocurrent,
  each open port's output power, every net's voltage and every heater's power.
- **Voltage sweep**: pick the pad's net, a range and a number of points. The
  photocurrents swap between the two detectors every V_pi (about 4.4 V for
  5 mm arms).
- **Wavelength sweep**: uses the *Wavelength from / to* range.
- **Time domain**: set a duration and a number of steps, and put a *PRBS*
  (0 V to V_pi, 10e9 for 10 Gb/s) on the pad. The photocurrents follow the
  bits, rounded by the modulator bandwidth set in the tab and by the
  detectors' own bandwidth.

## Component by component: FDTD without the whole layout in memory

An FDTD grid's memory grows with the area it covers (about 100 bytes per
cell), so one run of a whole layout soon needs more than the machine has. The
Circuit tab can instead measure each passive component with its own small FDTD
run and join the results:

1. In the Circuit tab, tick **Measure components by FDTD, one component at a
   time**.
2. Click **Build circuit from layout**. The status line names the component
   and port being simulated; **Stop** ends the build after the run in
   progress.
3. Run any analysis as before.

What happens during the build:

- Every *distinct* passive component (a bend, a taper, a splitter, a crossing,
  a ring, …) is simulated alone. Each of its ports is extended with a straight
  stub of the port's own cross-section, the stub's guided mode is launched
  into the component from one port at a time, and the light leaving every port
  is measured. That gives the component's S-matrix: amplitude and phase from
  each port to each port, over a band of wavelengths.
- The circuit solver joins the S-matrices along the layout's connections, so
  interference, loss and reflections between components are all in the result.
- A component used many times is simulated once: the euler bend of every
  route is one run per port, however many routes there are. Results are also
  kept on disk (`~/.cache/phidler/component_sparams`), so a later build only
  simulates components that are new or changed. Untick *Reuse cached results*
  to measure everything again.
- Straights keep their analytic model (the tab's n_eff, n_g and loss), and
  detectors and metal keep the models listed above. So do modulators and
  heaters, unless you give voltages to run them at (next section).

The memory needed is that of the **largest single component**, not of the
layout. After the build the status line reports both, for example *"the
largest grid is 65,408 cells, against 309,632 cells for the whole layout in
one run"*. The *Model* column of the component list shows which parts were
measured (*S-matrix, FDTD*, or *FDTD (cached)*).

| Setting | Meaning |
|---|---|
| **Domain** | *2.5-D (effective index)* collapses the vertical stack and runs in the chip plane: fast, and usually enough for routing parts. *Full 3-D* resolves the stack. |
| **Simulate** | *Every passive component*, or *only components with no built-in model* (bends, tapers and MMIs then keep their ideal models, and only parts such as crossings and rings are simulated). |
| **Cell size** | *Auto* is wavelength / 30 in 2.5-D and wavelength / 20 in 3-D. Results at the default are good to a few percent; halve it for a closer answer. |
| **Band width**, **Wavelengths in band** | The band the S-matrices are recorded over, centred on the tab's *Wavelength*. One pulse covers the whole band, so more wavelengths cost no extra runs. A wavelength sweep outside the band holds the values at the band's edge. |
| **Largest component grid** | The most cells one component may use, about 100 MB per million. A larger bend, taper or driven part keeps its built-in model, with a note; any other component stops the build with a message naming it. |
| **Driven parts at (V)** | Voltages to simulate the modulators and heaters at, for example `0, 5`. Empty: they keep photonfdtd's models. See below. |

### Modulators and heaters at a set voltage

Type the voltages you want in **Driven parts at (V)**, separated by commas:
`0, 5` for off and on, or more points for a sweep. Each modulator and heater
is then simulated by FDTD once per voltage (times its ports), like any other
component:

- **A modulator** gets each voltage on its signal electrode, with its grounds
  at 0 V. A part with two signal electrodes is run at every combination, up to
  9 states.
- **A heater** gets each voltage across it; the power is V² / R.
- **The index change** each volt (or watt) causes comes from photonfdtd's own
  model of the part: the cross-section solve for the LN cells, P_pi for a
  heater. It is applied to the waveguide under the electrodes: both arms of an
  MZI switch with opposite signs, each waveguide of a delta-beta coupler
  section by section, the whole length of a phase shifter or heater. FDTD then
  carries the light through the real geometry (the splitter, the bends, the
  couplers) with that index in place.
- **The grid** covers the waveguide layers only. Electrodes and pads are not
  in the optical run, so they add no memory.

The component stays a modulator or heater in the circuit, on the same nets and
with the same bandwidth, so the drives table and every analysis work as
before. At the listed voltages the result is the FDTD one. Between two listed
voltages the amplitude and phase of each S-matrix entry are interpolated, and
outside them the nearest is held, so list every state you want to read off. A
status note names the voltages each driven part was measured at.

Each state is cached on its own: adding a voltage later runs only the new one.

Limits: the full-size LN cells (millimetres long) are far over the grid limit
and keep their models; this is for parts short enough for FDTD. Component
files that carry their own model, and detectors, are not simulated. A driven
part with two optical ports must have them in line. Absorption that changes
with the drive is not included, only the index.

What it assumes:

- **One mode per port.** Only the fundamental mode of each port is followed.
  Light a component scatters into higher modes or radiation counts as its
  loss.
- **Coupling only through ports.** Two parts close enough to couple through
  their evanescent fields (a ring beside its bus, drawn as two components)
  must be one component to be simulated together.
- **Ports facing along x or y** in the component's own frame. The component
  may still be placed at any rotation.
- **Resonant parts need time.** A run ends when the light has left the
  component. A ring that is still ringing at the time limit is reported in a
  note, and its S-matrix is less accurate.

From Python or the console:

```python
from phidler import component_sim as cs, chip_circuit as cc

cfg = cs.ComponentSimConfig(wavelength_um=1.55, eim=True)
sp = cs.simulate_component(cell, doc.project_settings, cfg)   # one component
sp.s[("o1", "o2")]                                             # complex, per wavelength
circ = cc.build_circuit(doc, cc.CircuitConfig(), fdtd=cfg)     # the whole layout
print(circ.fdtd.summary())
```

## Component files

Parts can travel as **component files** (`*.pcomp.json`, plain-text JSON).
Like a KiCad library part, one file holds:
- the layout;
- the ports, and the pins (which electrical ports are one piece of metal);
- the circuit model: the solved physics, an optional S-parameter table, and
  the recipe the physics came from;
- optionally, a generator to rebuild the part with other parameters.

- **File → Import Component…** adds a file to the palette under
  **Component Files**. The part places like any other and simulates in the
  Circuit tab with its own model, with no re-solve. If its generator is a
  photonfdtd or gdsfactory function, the part's parameters are editable in
  the Properties panel. If the file carries its own Python generator code,
  Phidler asks before allowing it to run; without that permission the part
  keeps its fixed geometry.
- **File → Component Libraries…** lists folders of component files. Each
  folder shows in the palette as **Library — *folder***, from the next start
  (or when you close the dialog).
- **File → Export Selection as Component…** writes the selection as a
  component file:
    - **One instance** keeps its kind, physics and generator.
    - **Several instances** become one part. Its model is the circuit of the
      selected parts and the routes between them, its optical ports are the
      open ones (`o1`, `o2`, … from left to right), and its pins are the open
      pad and electrode ports.
    - A route from the selection to a part left out comes along, and its
      end at that part becomes a port or pin of the export.
    - A part photonfdtd has no model for (a ring, a grating coupler, …)
      is exported with its layout, pins and generator but no model, and
      Phidler says so.

  Tick *Add an S-parameter table* to also tabulate the part over a
  wavelength range.

Projects remember the component files they use and re-import them on open.

## What it leaves out

- **Steady optics at each instant.** Each time step solves the optics for the
  drives at that moment, so the drives must change slowly compared with the
  time light takes to cross the chip, about 10 ps per mm. A GHz drive on a
  centimetre chip is at the limit.
- **Lithium niobate models away from the build wavelength.** The cross-sections
  are solved at the tab's *Wavelength* when you click *Build* (its indices
  from the lithium niobate dispersion; the Pockels coefficients are the
  1.55 um ones). In a wavelength sweep their phases scale with wavelength,
  but their coupling is held at its build-wavelength value.
- **Electrode bandwidth.** It is a number you enter, not computed from the
  electrode design.
- **Heat between neighbours.** Each heater heats only its own waveguide;
  thermal crosstalk between nearby heaters is not modelled.
- **Sub-circuit currents.** A part exported from several instances drives
  its electrodes through its pins, but its heaters, resistors and detectors
  are not part of the outer circuit: the current they draw does not show in
  the drives, and a path through one of its resistors does not count as a
  detector's return path.
- **Electrode absorption and bend coupling.** Optical absorption by the
  electrodes and coupling in the fan-out bends are not modelled.

The same simulation is available from Python, and from the console:

```python
from photonfdtd import circuit
chip = circuit.Chip.from_gdsfactory(component)
chip.solve(1.55, drives={"pad": 4.4}, inputs={"laser": 1e-3})
```

The [photonfdtd chip-simulation guide](https://github.com/ngpaladi/photonfdtd)
covers the details, including exporting the circuit to sax.
