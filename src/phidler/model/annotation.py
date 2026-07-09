"""Notes and their callout drawings — a review/markup layer over the layout.

An :class:`Annotation` is a text note pinned to a point on the canvas plus the
callout drawings that point out what it refers to (a rectangle enclosing a
region, an arrow toward a target). Annotations are *not* fabricated geometry:
they never enter the gdsfactory top cell or the GDS export — they live in their
own ``LayoutDocument.annotations`` dict and are persisted only in the .phidler
project file, the same way the reference backdrop is kept out of ``top``.

All coordinates are plain µm in the top-cell frame, matching the rest of the
document. A note's pin is ``(x, y)``; each callout shape's points are stored
*relative to that pin* so a note and its drawings move as one unit — moving the
note is a single position change, no per-shape bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The callout drawing kinds a note can carry. Each is defined by exactly two
# points — (start, end) in µm relative to the note's pin: for "rect" they are
# opposite corners of the enclosing box; for "arrow" they are the tail (near the
# note) and the head (the target being pointed at).
CALLOUT_KINDS = ("rect", "arrow")

# Default note colour (a warm amber that reads over both the dark canvas and the
# semi-transparent layer fills). Shared by a note and all of its callouts.
DEFAULT_ANNOTATION_COLOR = "#f4b400"


@dataclass
class CalloutShape:
    """One drawing that points out what a note encompasses. ``points`` is
    ``[(x0, y0), (x1, y1)]`` in µm, relative to the owning note's pin."""

    kind: str
    points: list[tuple[float, float]]


@dataclass
class Annotation:
    """A text note pinned to ``(x, y)`` (µm, top-cell frame) plus the callout
    drawings that point out what it refers to. ``shapes`` are owned by the note:
    they render in ``color``, are tied to the pin by a leader line, and are
    removed with it."""

    id: int
    text: str
    x: float
    y: float
    shapes: list[CalloutShape] = field(default_factory=list)
    color: str = DEFAULT_ANNOTATION_COLOR
