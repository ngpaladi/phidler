"""The out-of-process FDTD runner (used for the GPU backend) rebuilds the
document from the saved project and returns a result identical to running
in-process — exercised here on the CPU engine so it needs no GPU."""

import numpy as np

from phidler.fdtd_sim import FdtdParams, SourceSpec, build_simulation, run_simulation
from phidler.fdtd_subprocess import _params_from_dict, _params_to_dict, run_in_subprocess
from phidler.model.document import LayoutDocument


def test_params_round_trip_through_json_dict():
    params = FdtdParams(
        cell_size_um=0.06,
        use_numba=True,
        sources=(SourceSpec(x_um=-5.0, y_um=1.0, kind="cherenkov", velocity_beta=0.9),),
    )
    restored = _params_from_dict(_params_to_dict(params))
    assert restored == params  # frozen dataclasses compare by value, incl. the source tuple


def test_subprocess_run_matches_in_process(qapp):
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 12.0, "width": 0.5})
    doc.add_instance("mmi1x2", {}, x=-18.0, y=0.0)
    params = FdtdParams(
        cell_size_um=0.08,
        use_numba=True,
        sources=(SourceSpec(x_um=-15.0, y_um=0.0, kind="cherenkov"),),
    )

    in_process = run_simulation(build_simulation(doc, params)).fields["field"]["Ez"]
    sim_stub, result_stub, elapsed = run_in_subprocess(doc, params)

    sub = result_stub.fields["field"]["Ez"]
    assert sub.shape == in_process.shape
    assert np.allclose(sub, in_process)  # the child rebuilt the exact same simulation
    assert elapsed > 0
    # the stub exposes just what the display reads
    assert len(sim_stub.grid.coords) == 3
    assert sim_stub.grid.shape[0] == sub.shape[1]
