# Tutorial — Training a priority encoder

This walkthrough trains a small chip to be a **4-line priority encoder**,
entirely in the GUI, in about a minute on a GPU. Along the way it covers the
**Train** tab, which works like fitting a neural network: a box on your layout
is the trainable part, its etch pattern plays the role of the network's
weights, and gradient descent fits it to a task.

## What the chip has to do

Four input waveguides, lines 0 to 3, are each **lit** or **dark**. The chip has
four output waveguides. For every input pattern, the light has to leave
through the output numbered after the **highest lit line**:

| Lit lines | Output that must light up |
|---|---|
| 0 | 0 |
| 1 | 1 |
| 0 and 1 | 1 |
| 0, 2 and 3 | 3 |
| any pattern | the highest lit line |

That's a priority encoder in one-hot form: one detector per output port, and
whichever is brightest gives the answer. The thermometer codes that a tapped
delay line produces (lines `0..k` lit) are a special case of it, so the same
chip decodes the taps of a time-to-digital converter. There are 15
non-empty patterns of four lines, and the chip has to get all of them right.

## How training works

The chip is linear in the light, so for a fixed etch it is exactly a 4 × 4
complex **transfer matrix** `T`: the amplitude reaching each output from each
input. Phidler measures `T` with one simulation per input port, and every input
pattern `x` then gives output amplitudes `T x` with no further simulation. A
detector reads power, `|T x|²`, and that squaring is the network's only
nonlinearity. The loss is the cross-entropy of each output's share of the power
against the correct output, as in a classifier.

Each training step runs the 4 port simulations forward, then 4 adjoint
simulations that give the gradient of the loss for every cell of the box at
once, and takes an Adam step. **A step costs 8 simulations however many
patterns you train on.** Runs use the 2.5-D effective-index reduction (see
[A much bigger run](guide.md#a-much-bigger-run-25-d-effective-index)): a
chip-plane grid at full-height core / fully etched, which keeps a step to about
a second.

## 1. Open the Train tab

Open the simulation window with the **Simulate** button in the toolbar and switch to the **Train
(inverse design)** tab. Check the **Acceleration** row on the **Propagation**
tab: it should say **JAX on the GPU**. If it says CPU and you have an NVIDIA
GPU, click **Check JAX GPU…** to see why (see
[It says "JAX on the CPU"](faq.md#it-says-jax-on-the-cpu-but-i-have-a-gpu)).
Training on the CPU works, but takes several times longer.

## 2. Insert the demo

Set **Lines** (under **Task**) to 4 and click **Insert priority-encoder demo**.
Phidler places eight straight waveguides, four inputs on the left and four
outputs on the right, with a 4 µm gap between them. It marks the inputs and
outputs in the **Ports** table and sets the **design box** to the gap, drawn on
the canvas as an orange dashed rectangle. The demo is one undo step, so
**Ctrl+Z** removes all of it.

![The demo on the canvas: four input stubs on the left and four outputs on the right, with the orange dashed design box in the gap between them](screenshots/train_demo_canvas.png)

The tab now holds everything training needs:

![The Train tab after inserting the demo: the design box coordinates, the Ports table with four inputs marked, the priority-encoder task with 4 lines, and the training settings](screenshots/train_tab_setup.png)

- **Design box**: the region whose etch is trained. Everything outside it stays
  exactly as drawn. Type coordinates, or select components and click **From
  selection**.
- **Ports**: every open (unrouted) waveguide port on the layout. **Input** ports
  are where light is launched, **Output** ports are where it's detected.
  Order matters: the first input listed is line 0, and the list runs left to
  right, then bottom to top, so line 0 is the bottom input here. **Auto**
  marks every port facing left as an input and every port facing right as an
  output.
- **Task**: **Priority encoder (built-in)** with 4 lines. Ticking
  **Thermometer codes only** trains on just the 4 thermometer patterns instead
  of all 15.
- **Training**: the defaults suit this demo. **Min. feature** is roughly the
  smallest feature the etch may contain. **Steps** is how many optimizer steps to
  take. **Final sharpness β** controls how hard the design is pushed to a clean
  etched/unetched pattern by the end (it starts soft and doubles over the first
  three quarters of the run).

## 3. Train

Click **Train**. The status line reports the grid it built, then each step's
loss and accuracy. The plot shows the loss (blue) falling and the accuracy
(orange), and the panel beside it shows the etch pattern evolving (light =
core, dark = etched). **Stop** ends the run after the current step and keeps
the design so far.

![The Train tab after 60 steps: "Done after 60 steps in 54 s: accuracy 100%, binarization 0.89", the loss curve falling to near zero with the accuracy at 1, and the trained etch pattern](screenshots/train_tab_trained.png)

On an RTX 4080 the 60 steps take **54 s** and reach **100 %** accuracy. The
untrained grey box gets 9 of the 15 patterns right, because light mostly runs
straight across and some patterns happen to favour the right output; after
about 10 steps it gets all of them. The spikes in the loss curve come right after
β doubles: sharpening the design changes it a little, and the next steps
recover. **Binarization 0.89** means the final pattern is close to cleanly
etched or unetched everywhere (1.0 would be fully binary).

The table under the plot shows every pattern. **Input** lists lines 0 to 3 left
to right, and the **P0–P3 share** columns are the fraction of the detected power
in each output:

![The results table: all 15 input patterns, the target output, the predicted output in green, and each output's share of the power, with at least 0.96 in the correct output every time](screenshots/train_results_table.png)

Every pattern sends at least **96 %** of its detected power to the correct
output.

## 4. Place the design

Click **Place design on layout**. The trained etch becomes a `trained_design`
instance filling the box: real polygons on the core layer, with holes where the
pattern is etched through. It's an ordinary component from then on. You can move
it, it's saved with the project (the polygons are stored in the instance, so the
project reopens anywhere), it's exported with **File → Export GDS**, and you can
run the **Propagation** tab on it to watch the light route. **Ctrl+Z** removes it.

![The placed design on the canvas: an irregular core pattern filling the design box between the input and output stubs](screenshots/train_placed_canvas.png)

## Your own tasks

The **Task** dropdown has three more options.

**Classify a dataset (CSV / NPZ).** Train on your own input/output examples. A
CSV has a header row and one sample per row: columns `in0`, `in1`, … are the
complex input amplitudes (`1`, `0`, `0.5+0.5j`, `-1j`) and `label` is the
output port that should light up. An NPZ holds arrays `X` (samples × inputs,
complex) and `Y` (integer labels).

```
in0,in1,label
1,1,0
1,-1,1
1,1j,0
```

**Output power fractions (CSV / NPZ).** Instead of a class, give the share of
power each output should get: columns `out0`, `out1`, … (each row is normalized
to fractions), or `Y` with one column per output. Useful for splitters with
input-dependent ratios.

**Target transfer matrix.** Make the chip implement a fixed linear map, such as a
50:50 coupler, a swap, or a 4-point DFT (presets are provided), entered as one
row per output: row `m`, column `n` is the amplitude from input `n` to output
`m`. The score is the **fidelity** `|⟨T, T_target⟩|² / (|T|² |T_target|²)`, which
ignores the overall scale and global phase.

Port readouts aren't calibrated to absolute power (light is launched by a point
source, as on the Propagation tab), so every task is trained on the *relative*
output powers. A design that loses light evenly scores the same as a lossless
one. Check the transmission of the placed design on the **Propagation** tab.

## From the console

The same pipeline is scriptable from the built-in console, e.g. a larger sweep
than the tab offers:

```python
from phidler import chip_train as ct

box, ins, outs = ct.demo_priority_encoder_layout(doc, n=4, center_um=(50, 0))
model = ct.ChipModel(doc, ct.TrainConfig(box_um=box, inputs=ins, outputs=outs))
result = ct.train_chip(model, ct.priority_encoder_task(4), ct.TrainOptions(steps=60))
print(result.metrics["accuracy"])
ct.place_trained_design(doc, model, result.rho_phys)
```

## Limits

- Training uses the 2.5-D reduction, so the design is a single full-height etch
  layer, with no partial (rib) etch.
- Ports must face along x or y.
- Training needs a box with real waveguides leading into it: the ports' launch
  and readout planes sit 0.3 µm inside each port's waveguide.
