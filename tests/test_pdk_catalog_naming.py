from phidler.pdk_catalog import build_catalog, category_display_name, prettify_component_name


def test_prettify_component_name_known_cases():
    assert prettify_component_name("mmi1x2") == "MMI 1x2"
    assert prettify_component_name("bend_euler") == "Bend Euler"
    assert prettify_component_name("via_stack_slab_m1") == "Via Stack Slab M1"
    assert prettify_component_name("via_stack_corner45_extended") == "Via Stack Corner 45 Extended"
    assert prettify_component_name("grating_coupler_te") == "Grating Coupler TE"
    assert prettify_component_name("straight") == "Straight"
    assert prettify_component_name("awg") == "AWG"


def test_category_display_name_known_and_fallback():
    assert category_display_name("mmis") == "MMIs"
    assert category_display_name("edge_couplers") == "Edge Couplers"
    assert category_display_name("totally_unknown_category") == "Totally Unknown Category"


def test_prettify_handles_every_real_catalog_name_without_raising(qapp):
    """A weaker but exhaustive check alongside the hand-picked cases above:
    the heuristic should run cleanly (not crash, not return an empty
    string) for every one of the ~300 real catalog names, even though it
    won't prettify every single one perfectly."""
    catalog = build_catalog()
    for specs in catalog.values():
        for spec in specs:
            pretty = prettify_component_name(spec.name)
            assert pretty, f"empty prettified name for {spec.name!r}"
