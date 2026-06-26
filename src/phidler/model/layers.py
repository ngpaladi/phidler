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


_LAYER_DESCRIPTIONS: dict[str, str] = {
    # Silicon etch layers
    "WG":           "Silicon waveguide core — full etch (~220 nm on standard SOI). Defines single-mode wire waveguides and grating teeth.",
    "SLAB150":      "Half-etch silicon slab — ~150 nm partial etch, leaving ~70 nm slab. Used under grating couplers and rib waveguides to improve coupling efficiency and mode confinement.",
    "SLAB90":       "Shallow-etch silicon slab — ~90 nm partial etch. Used for very low-loss rib waveguides where a thicker slab reduces scattering.",
    "SHALLOW_ETCH": "Process mask for the shallow (~90 nm) silicon etch step.",
    "DEEP_ETCH":    "Process mask for the full-depth (~220 nm) silicon etch step.",
    "DEEPTRENCH":   "Deep silicon isolation trench — etches through the entire device layer to optically and electrically isolate regions.",
    "UNDERCUT":     "Silicon undercut — removes buried oxide beneath the silicon to form suspended membranes for MEMS or ultra-low-loss waveguides.",
    # Germanium
    "GE":           "Germanium — grown selectively on silicon for photodetectors and electro-absorption modulators; absorbs near-IR light at 1.3–1.6 µm.",
    "GEN":          "N-doped germanium — cathode contact region of a Ge photodetector.",
    "GEP":          "P-doped germanium — anode contact region of a Ge photodetector.",
    # Doping implants
    "N":            "N-type silicon implant — moderate electron doping for PN junction phase shifters and PIN modulators.",
    "P":            "P-type silicon implant — moderate hole doping for PN junction phase shifters and PIN modulators.",
    "NP":           "N+ silicon implant — higher electron doping to reduce resistance adjacent to the waveguide.",
    "PP":           "P+ silicon implant — higher hole doping to reduce resistance adjacent to the waveguide.",
    "NPP":          "N++ silicon implant — heavy doping for low-resistance ohmic contact to metal vias.",
    "PPP":          "P++ silicon implant — heavy doping for low-resistance ohmic contact to metal vias.",
    # Silicon nitride
    "WGN":          "Silicon nitride (Si₃N₄) waveguide core — lower index contrast than Si gives lower propagation loss and broader bandwidth; used for visible-light and low-loss interconnect PICs.",
    "WGN_CLAD":     "Silicon nitride waveguide cladding.",
    "WGCLAD":       "Waveguide cladding material (typically SiO₂) surrounding the waveguide core.",
    # Metals and vias
    "VIAC":         "Contact via — metal plug from M1 down to silicon or germanium.",
    "VIA1":         "Via 1 — metal plug connecting M1 to M2.",
    "VIA2":         "Via 2 — metal plug connecting M2 to M3.",
    "M1":           "Metal 1 — lowest metal routing layer.",
    "M2":           "Metal 2 — second metal routing layer.",
    "M3":           "Metal 3 — top metal routing layer, typically used for bond pads and RF lines.",
    "PADOPEN":      "Pad opening — removes passivation over a bond pad to allow wire bonding or probe contact.",
    "HEATER":       "Resistive metal heater — placed over a waveguide for thermo-optic phase shifting via the silicon refractive index temperature dependence (~1.8×10⁻⁴ /K).",
    # Process / utility
    "FLOORPLAN":    "Chip floorplan boundary — defines the die extent.",
    "DICING":       "Dicing lane — kept free of devices so the wafer saw can cleave without damaging circuits.",
    "DEVREC":       "Device recognition bounding box — marks the exclusion zone around a cell for auto-placement and DRC.",
    "NO_TILE_SI":   "Inhibit dummy silicon fill in this region.",
    "PADDING":      "Extra clearance region around a device.",
    "TEXT":         "Text label layer — not a physical structure; used for human-readable identifiers on the layout.",
    "LABEL_INSTANCE": "Auto-generated instance reference label.",
    "LABEL_SETTINGS": "Stores simulation or PDK metadata in the GDS file.",
    # Ports / simulation markers
    "PORT":         "Optical port marker — indicates where a waveguide connects to an adjacent cell or the outside world.",
    "PORTE":        "Electrical port marker.",
    "PORTH":        "Horizontal port marker.",
    "SHOW_PORTS":   "Debug layer that makes port positions visible.",
    "TE":           "TE-polarization port — couples transverse-electric (in-plane E-field) light, the standard polarization for SOI strip waveguides.",
    "TM":           "TM-polarization port — couples transverse-magnetic (vertical E-field) light.",
    "SOURCE":       "FDTD source marker — where a simulation injects optical power.",
    "MONITOR":      "FDTD monitor marker — where field amplitude or power is recorded during simulation.",
    "DRC_MARKER":   "Design rule check error marker — not a physical layer; highlights layout rule violations.",
    "WAFER":        "Wafer outline.",
}


def layer_description(name: str) -> str | None:
    """Human-readable explanation of what a PDK layer does, for use as a tooltip."""
    return _LAYER_DESCRIPTIONS.get(name)


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
