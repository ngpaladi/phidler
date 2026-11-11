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
