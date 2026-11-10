"""SI <-> lattice-unit parameter computation and guard rails.

This is the app's *trust layer*: every case gets its physical Reynolds
number mapped exactly into lattice units (nu_lu = u_lu * size_lu / Re),
and the resulting relaxation time / Mach number are checked so the UI
can warn the user before a long run starts.

Mirrors FluidX3D v3.8 `units.set_m_kg_s()` conventions:
    unit_m  = si_x / x_lu
    unit_kg = si_rho * unit_m^3            (lattice rho = 1)
    unit_s  = u_lu / si_u * unit_m
    nu_lu   = si_nu * unit_s / unit_m^2
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Simple fluid tables at ~20 C, 1 atm (fine for hobby aero; refine later).
FLUIDS: dict[str, dict[str, float]] = {
    "air": {"rho_si": 1.204, "nu_si": 1.516e-5, "label": "air, 20 C"},
    "water": {"rho_si": 998.2, "nu_si": 1.004e-6, "label": "water, 20 C"},
}

# Guard-rail limits (lattice units).
TAU_SAFE_MIN = 0.545      # below this, plain SRT is under-resolved -> need LES
TAU_HARD_MIN = 0.512      # below this, not even Smagorinsky LES stays stable -> refuse
TAU_SAFE_MAX = 1.9
NU_LU_ABS_MIN = 1e-6      # hard floor (with subgrid LES)
U_LU_WARN = 0.15          # Ma = u / (1/sqrt(3)); 0.15 -> Ma ~ 0.26
U_LU_MAX = 0.3            # beyond this compressibility corrupts results
U_LU_DEFAULT_TUNNEL = 0.1


class CaseError(Exception):
    """Invalid case definition: refuse to build/run."""


@dataclass
class Derived:
    nu_lu: float
    tau: float
    u_lu: float
    size_lu: float
    re_si: float
    re_lu: float
    unit_m: float          # meters per lattice cell
    unit_s: float          # seconds per lattice step
    rho_si: float
    u_si: float | None
    warnings: list[str] = field(default_factory=list)
    subgrid_suggested: bool = False


def fluid_properties(fluid: dict) -> tuple[float, float, str]:
    kind = str(fluid.get("kind", "air")).lower()
    if kind in FLUIDS:
        f = FLUIDS[kind]
        return f["rho_si"], f["nu_si"], f["label"]
    if "rho_si" in fluid and "nu_si" in fluid:
        return float(fluid["rho_si"]), float(fluid["nu_si"]), "custom fluid"
    raise CaseError(f"unknown fluid {kind!r}; use air/water or give rho_si/nu_si")


def derive(case: dict) -> Derived:
    """Compute and sanity-check all lattice parameters for a case dict."""
    geom = case["geometry"]
    flow = case["flow"]
    rho_si, nu_si, _ = fluid_properties(case.get("fluid", {"kind": "air"}))

    size_lu = float(geom["size_lu"])
    u_lu = float(flow.get("u_lu", U_LU_DEFAULT_TUNNEL))
    mode = str(flow.get("mode", "tunnel"))

    if mode == "volume_force":
        # Pressure-driven channel (validation): nu from tau knob, no Re input.
        tau = float(flow.get("tau", 1.0))
        if not 0.501 < tau < 2.0:
            raise CaseError(f"tau={tau} outside (0.501, 2.0)")
        nu_lu = (tau - 0.5) / 3.0
        re_si = size_lu * u_lu / nu_si * 0.0  # meaningless here; keep 0
        re_lu = size_lu * u_lu / nu_lu
        u_si = None
        unit_m = 1.0
        unit_s = 1.0
    else:
        u_si = float(flow["u_si"])
        length_si = float(flow["length_si"])  # physical reference length (model size)
        if u_si <= 0 or length_si <= 0:
            raise CaseError("u_si and length_si must be positive")
        re_si = u_si * length_si / nu_si
        # exact Re similarity: nu_lu = u_lu * size_lu / Re
        nu_lu = u_lu * size_lu / re_si
        re_lu = re_si
        unit_m = length_si / size_lu
        unit_s = u_lu / u_si * unit_m
        if abs(nu_lu * unit_m * unit_m / unit_s - nu_si) > 1e-9 * nu_si:
            raise CaseError("internal unit inconsistency")  # tripwire

    tau = 3.0 * nu_lu + 0.5
    d = Derived(
        nu_lu=nu_lu, tau=tau, u_lu=u_lu, size_lu=size_lu, re_si=re_si,
        re_lu=re_lu, unit_m=unit_m, unit_s=unit_s, rho_si=rho_si, u_si=u_si,
    )

    # ---- guard rails ----
    if nu_lu < NU_LU_ABS_MIN:
        raise CaseError(
            f"nu_lu={nu_lu:.2e} too small even for LES: reduce speed or "
            f"increase model resolution (cells-per-model: {size_lu:.0f})")
    if tau < TAU_HARD_MIN:
        raise CaseError(
            f"flow at Re = {re_si:,.0f} cannot be simulated with only "
            f"{size_lu:.0f} voxels across the model (viscosity underflows, "
            f"tau = {tau:.4f}): pick a higher quality, or model a smaller / "
            "slower object")
    if tau < TAU_SAFE_MIN:
        d.subgrid_suggested = True
        d.warnings.append(
            f"relaxation time tau={tau:.4f} < {TAU_SAFE_MIN}: too close to "
            "inviscid for plain SRT; enabling Smagorinsky LES (subgrid) model")
    if tau > TAU_SAFE_MAX:
        d.warnings.append(
            f"tau={tau:.3f} very high (over-diffusive): model will look "
            "artificially viscous; lower speed or raise resolution")
    if u_lu > U_LU_MAX:
        raise CaseError(f"u_lu={u_lu} exceeds stability limit {U_LU_MAX}")
    if u_lu > U_LU_WARN:
        d.warnings.append(
            f"u_lu={u_lu} is high (Ma>0.25): consider u_lu<=0.1")
    return d
