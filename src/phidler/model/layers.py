from __future__ import annotations

import colorsys
from dataclasses import dataclass

LayerKey = tuple[int, int]


@dataclass
class LayerInfo:
    layer: int
    datatype: int
    name: str
    color: str
    visible: bool = True

    @property
    def key(self) -> LayerKey:
        return (self.layer, self.datatype)


def _color_for(layer: int, datatype: int) -> str:
    """Deterministic, visually-distinct color for a (layer, datatype) pair."""
    hue = ((layer * 7 + datatype * 31) % 360) / 360.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.85)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


_PDK_LAYER_NAMES: dict[LayerKey, str] | None = None


def _pdk_layer_name(key: LayerKey) -> str | None:
    """Looks up the active PDK's name for a layer (e.g. "WG" for (1, 0)),
    cached on first use, so layers still get a readable name even though
    they're no longer all pre-seeded into every document up front."""
    global _PDK_LAYER_NAMES
    if _PDK_LAYER_NAMES is None:
        from gdsfactory.gpdk import LAYER

        _PDK_LAYER_NAMES = {(member.layer, member.datatype): member.name for member in LAYER}
    return _PDK_LAYER_NAMES.get(key)


def layer_info_for(key: LayerKey, known: dict[LayerKey, LayerInfo]) -> LayerInfo:
    """Get the LayerInfo for `key`, creating it (named from the active PDK's
    layer map if known there, otherwise a generic "L<layer>/<datatype>")
    the first time it's encountered."""
    info = known.get(key)
    if info is None:
        name = _pdk_layer_name(key) or f"L{key[0]}/{key[1]}"
        info = LayerInfo(layer=key[0], datatype=key[1], name=name, color=_color_for(*key))
        known[key] = info
    return info
