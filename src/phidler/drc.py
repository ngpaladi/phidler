from __future__ import annotations

from dataclasses import dataclass

import gdsfactory as gf

from .model.document import LayoutDocument
from .model.layers import LayerKey


@dataclass
class DrcViolation:
    kind: str  # "width" or "spacing"
    layer: LayerKey
    bbox: tuple[float, float, float, float]  # left, bottom, right, top — microns


def run_drc(document: LayoutDocument, layer: LayerKey, min_width: float, min_spacing: float) -> list[DrcViolation]:
    """Checks `layer` against the min_width/min_spacing the user supplied.

    These thresholds are NOT pulled from any foundry rule deck: the active
    generic PDK doesn't expose authoritative 2D design rules (its LAYER_STACK
    only has fabrication-stack info like thickness/zmin, not min width/space).
    So a result here means "violates the numbers you entered," not "passes
    a real process's DRC" — callers must label it that way rather than
    presenting it as a sign-off.
    """
    top = document.top
    dbu = top.kcl.dbu
    layer_index = top.kcl.layer(*layer)
    region = gf.kdb.Region(top.begin_shapes_rec(layer_index))

    violations: list[DrcViolation] = []
    if min_width > 0:
        for edge_pair in region.width_check(round(min_width / dbu)).each():
            b = edge_pair.bbox().to_dtype(dbu)
            violations.append(DrcViolation("width", layer, (b.left, b.bottom, b.right, b.top)))
    if min_spacing > 0:
        for edge_pair in region.space_check(round(min_spacing / dbu)).each():
            b = edge_pair.bbox().to_dtype(dbu)
            violations.append(DrcViolation("spacing", layer, (b.left, b.bottom, b.right, b.top)))
    return violations
