import inspect

from phidler.pdk_catalog import build_catalog, editable_defaults


def test_catalog_is_nonempty_and_placeable():
    catalog = build_catalog()
    assert "waveguides" in catalog
    assert len(catalog["waveguides"]) > 0

    total = sum(len(specs) for specs in catalog.values())
    assert total > 300  # gdsfactory 9.44 ships ~380 components; most should be placeable

    straight_specs = [s for specs in catalog.values() for s in specs if s.name == "straight"]
    assert len(straight_specs) == 1
    spec = straight_specs[0]
    assert spec.category == "waveguides"

    # every cataloged spec must be callable with zero args, by construction
    for specs in catalog.values():
        for spec in specs:
            for p in spec.signature.parameters.values():
                assert p.default is not inspect.Parameter.empty or p.kind not in (
                    p.POSITIONAL_OR_KEYWORD,
                    p.KEYWORD_ONLY,
                )


def test_editable_defaults_excludes_nonscalar_params():
    catalog = build_catalog()
    spec = next(s for specs in catalog.values() for s in specs if s.name == "straight")
    defaults = editable_defaults(spec)
    assert defaults["length"] == 10.0
    assert "width" not in defaults  # straight's width default is None, not a scalar type
    assert "cross_section" in defaults  # default value 'strip' is a plain str, still scalar-editable
