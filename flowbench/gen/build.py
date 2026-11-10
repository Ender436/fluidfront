"""Write generated case files into the engine and compile it.

Single shared build tree (engine/FluidX3D): cases build serially. The
generator only writes src/setup.cpp + src/defines.hpp; the solver core
stays untouched. Copies of both are kept under <case>/gen/ for
reproducibility (diff against upstream: `git -C engine/FluidX3D diff`).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .schema import CaseSpec
from .setup_cpp import generate_defines, generate_setup

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine" / "FluidX3D"
SRC = ENGINE / "src"
BIN = ENGINE / "bin" / "FluidX3D"


class BuildError(Exception):
    pass


def write_case_files(spec: CaseSpec) -> None:
    gen_dir = spec.case_dir / "gen"
    gen_dir.mkdir(exist_ok=True)

    setup = generate_setup(spec)
    # defines must always be regenerated from the pristine upstream file;
    # the engine is pinned, so read the original straight from its git object.
    base_defines = subprocess.run(
        ["git", "-C", str(ENGINE), "show", "HEAD:src/defines.hpp"],
        capture_output=True, text=True, check=True,
    ).stdout
    defines = generate_defines(spec, base_defines)

    (gen_dir / "setup.cpp").write_text(setup)
    (gen_dir / "defines.hpp").write_text(defines)
    (SRC / "setup.cpp").write_text(setup)
    (SRC / "defines.hpp").write_text(defines)


def build(jobs: int | None = None) -> Path:
    """Compile the engine (`make Linux`). Returns binary path."""
    if not ENGINE.is_dir():
        raise BuildError(f"engine not found at {ENGINE}")
    nproc = jobs or (os.cpu_count() or 4)
    r = subprocess.run(
        ["make", "Linux", f"-j{nproc}"], cwd=ENGINE,
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        tail = "\n".join((r.stdout + "\n" + r.stderr).splitlines()[-40:])
        raise BuildError(f"engine build failed:\n{tail}")
    if not BIN.is_file():
        raise BuildError(f"build succeeded but {BIN} missing")
    return BIN
