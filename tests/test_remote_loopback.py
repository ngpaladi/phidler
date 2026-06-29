"""End-to-end verification of the remote-offload setup against a *real* SSH
host — no mocks. Skipped unless ``PHIDLER_REMOTE_LOOPBACK=1`` is set, because it
needs a reachable host (CI stands up passwordless SSH to localhost; see
.github/workflows/remote-setup.yml).

What it proves that the mocked unit tests can't: that the actual ssh/scp/rsync
transport works, that a remote Python can run the shipped job bundle, and that
progress markers stream back over the wire. Configuration comes entirely from
the environment so the same test serves the CI localhost host and any real one:

  PHIDLER_REMOTE_LOOPBACK=1     enable this module
  PHIDLER_REMOTE_ALIAS          ssh host alias (default "phidler-ci")
  PHIDLER_REMOTE_PYTHON         remote venv python (required)
  PHIDLER_REMOTE_DIR            remote install dir (required for the deploy step)
  PHIDLER_LOCAL_PHOTONFDTD      local photonfdtd checkout to upload (optional)
  PHIDLER_REMOTE_TEST_DEPLOY=1  also exercise deploy_to_remote (a full
                                `pip install -e` into the remote venv)
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("PHIDLER_REMOTE_LOOPBACK") != "1",
    reason="set PHIDLER_REMOTE_LOOPBACK=1 (CI provides a localhost SSH host) to run",
)


def _cfg():
    from phidler.remote_config import RemoteConfig

    remote_python = os.environ.get("PHIDLER_REMOTE_PYTHON")
    assert remote_python, "PHIDLER_REMOTE_PYTHON must point at the remote venv's python"
    return RemoteConfig(
        alias=os.environ.get("PHIDLER_REMOTE_ALIAS", "phidler-ci"),
        remote_dir=os.environ.get("PHIDLER_REMOTE_DIR", ""),
        remote_python=remote_python,
        use_gpu=False,
        local_photonfdtd_dir=os.environ.get("PHIDLER_LOCAL_PHOTONFDTD", ""),
    )


def test_remote_setup_end_to_end_over_real_ssh():
    """Deploy (optional) → check → run a real solve on the remote, and confirm
    the field movie and the streamed progress come back."""
    from phidler.fdtd_remote import check_remote, deploy_to_remote, run_on_remote
    from phidler.fdtd_sim import FdtdParams, SourceSpec
    from phidler.model.document import LayoutDocument

    cfg = _cfg()

    # 1) One-time setup: rsync the checkouts and pip install -e into the remote
    #    venv. Gated separately so a host that's already provisioned can skip it.
    if os.environ.get("PHIDLER_REMOTE_TEST_DEPLOY") == "1":
        lines: list[str] = []
        assert cfg.remote_dir, "PHIDLER_REMOTE_DIR is required to test deploy"
        ok = deploy_to_remote(cfg, lines.append)
        assert ok, "deploy_to_remote failed:\n" + "\n".join(lines[-30:])

    # 2) The remote can import both packages.
    ok, message = check_remote(cfg)
    assert ok, message

    # 3) A real solve runs on the remote and the result + progress come back.
    from gdsfactory.gpdk import get_generic_pdk

    get_generic_pdk().activate()
    doc = LayoutDocument()
    doc.add_instance("straight", {"length": 6.0, "width": 0.5})
    params = FdtdParams(
        cell_size_um=0.1, use_numba=False,  # plain NumPy: no remote JIT-compile wait
        sources=(SourceSpec(x_um=-4.0, y_um=0.0),),
    )

    ticks: list[tuple[int, int]] = []
    sim, result, elapsed = run_on_remote(
        doc, params, None, cfg, progress_callback=lambda i, n: ticks.append((i, n))
    )

    ez = result.fields["field"]["Ez"]
    assert ez.shape[0] > 0  # a non-empty field movie came back over scp
    assert len(sim.grid.shape) == 3  # full 3D grid metadata parsed from the npz
    assert elapsed >= 0
    assert ticks, (
        "no progress streamed back from the remote — the deployed photonfdtd is "
        "likely too old to have Simulation.progress_callback (the rest of the "
        "offload worked: a result came back)."
    )
    assert ticks[-1][0] == ticks[-1][1]  # progress reached 100%
