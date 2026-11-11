"""Write generated case files into the engine and compile it.

Single shared build tree (engine/FluidX3D): cases build serially. The
generator only writes src/setup.cpp + src/defines.hpp; the solver core
stays untouched. Copies of both are kept under <case>/gen/ for
reproducibility (diff against upstream: `git -C engine/FluidX3D diff`).

Two build backends: Linux/macOS use `make Linux` (g++); Windows uses
MSBuild against the upstream FluidX3D.vcxproj (Release|x64) - found via
Microsoft's vswhere helper. FluidX3D bundles OpenCL headers + import lib,
so the only Windows prerequisite is the Visual Studio C++ toolchain.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .schema import CaseSpec
from .setup_cpp import generate_defines, generate_setup

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine" / "FluidX3D"
SRC = ENGINE / "src"
IS_WINDOWS = sys.platform == "win32"
BIN = ENGINE / "bin" / ("FluidX3D.exe" if IS_WINDOWS else "FluidX3D")


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


def _msbuild() -> str:
    """Locate MSBuild.exe via vswhere; clear guidance if the toolchain is
    missing (the #1 Windows setup pitfall)."""
    pf = (os.environ.get("ProgramFiles(x86)")
          or os.environ.get("ProgramFiles", r"C:\Program Files (x86)"))
    vswhere = Path(pf) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        raise BuildError(
            "no Visual Studio / MSVC found. Install 'Visual Studio 2022 Build "
            "Tools' with the 'Desktop development with C++' workload: "
            "https://visualstudio.microsoft.com/downloads/")
    r = subprocess.run(
        [str(vswhere), "-latest", "-products", "*",
         "-requires", "Microsoft.Component.MSBuild",
         "-property", "installationPath"],
        capture_output=True, text=True)
    root = r.stdout.strip()
    if not root:
        raise BuildError(
            "Visual Studio found but MSBuild is missing - add the 'Desktop "
            "development with C++' workload in Visual Studio Installer.")
    for sub in (Path("MSBuild") / "Current" / "Bin" / "MSBuild.exe",
                Path("MSBuild") / "15.0" / "Bin" / "MSBuild.exe"):
        p = Path(root) / sub
        if p.is_file():
            return str(p)
    raise BuildError(f"MSBuild.exe not found under {root}")


def build(jobs: int | None = None) -> Path:
    """Compile the engine (make Linux / MSBuild). Returns binary path."""
    if not ENGINE.is_dir():
        raise BuildError(f"engine not found at {ENGINE} - clone it first "
                         "(see README: git clone ... engine/FluidX3D)")
    if IS_WINDOWS:
        cmd = [_msbuild(), "FluidX3D.vcxproj",
               "/p:Configuration=Release", "/p:Platform=x64",
               # vcxproj writes to $(SolutionDir)bin\; SolutionDir is not
               # guaranteed when MSBuild targets the project directly
               f"/p:SolutionDir={ENGINE}\\",
               "/m", "/nologo", "/v:minimal"]
    else:
        nproc = jobs or (os.cpu_count() or 4)
        cmd = ["make", "Linux", f"-j{nproc}"]
    r = subprocess.run(cmd, cwd=ENGINE, capture_output=True, text=True,
                       errors="replace")
    if r.returncode != 0:
        tail = "\n".join((r.stdout + "\n" + r.stderr).splitlines()[-40:])
        raise BuildError(f"engine build failed:\n{tail}")
    if not BIN.is_file():
        raise BuildError(f"build succeeded but {BIN} missing")
    return BIN
