"""SI <-> lattice-unit parameter computation and guard rails.

This is the app's *trust layer*: every case gets its physical Reynolds
number mapped into lattice units (nu_lu = u_lu * size_lu / Re). When the
real viscosity is sharper than the model resolution can carry (tau would
sink below the solver's stability floor), the app clamps to the lowest
stable tau and runs Smagorinsky LES -- exactly what FluidX3D users do for
real-world aero -- and reports the *effective* resolved Reynolds number
honestly instead of pretending the mapping stayed exact.

Mirrors FluidX3D v3.8 `units.set_m_kg_s()` conventions:
    unit_m  = si_x / x_lu
    unit_kg = si_rho * unit_m^3            (lattice rho = 1)
    unit_s  = u_lu / si_u * unit_m
    nu_lu   = si_nu * unit_s / unit_m^2
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Simple fluid tables at ~20 C, 1 atm (fine for hobby aero; refine later).
FLUIDS: dict[str, dict[str, float]] = {
    "air": {"rho_si": 1.204, "nu_si": 1.516e-5, "label": "air, 20 C"},
    "water": {"rho_si": 998.2, "nu_si": 1.004e-6, "label": "water, 20 C"},
}

# Guard-rail limits (lattice units).
TAU_SAFE_MIN = 0.545      # below this, plain SRT is under-resolved -> need LES
TAU_HARD_MIN = 0.515      # below this the solver goes unstable even with LES (measured on v3.8)
TAU_SAFE_MAX = 1.9
NU_LU_FLOOR = (TAU_HARD_MIN - 0.5) / 3.0  # lowest stable lattice viscosity (0.005)
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
    les_clamped: bool = False   # true when Re exceeded what size_lu can resolve


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
    if not math.isfinite(size_lu) or size_lu <= 0:
        raise CaseError(f"geometry.size_lu must be a positive number, got {size_lu}")
    if not math.isfinite(u_lu) or u_lu <= 0:
        raise CaseError(f"flow.u_lu must be a positive number, got {u_lu}")
    mode = str(flow.get("mode", "tunnel"))

    if mode == "volume_force":
        # Pressure-driven channel (validation): nu from tau knob, no Re input.
        tau = float(flow.get("tau", 1.0))
        if not math.isfinite(tau) or not 0.501 < tau < 2.0:
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
        if (not math.isfinite(u_si) or not math.isfinite(length_si)
                or u_si <= 0 or length_si <= 0):
            raise CaseError("u_si and length_si must be finite positive numbers")
        re_si = u_si * length_si / nu_si
        # exact Re similarity: nu_lu = u_lu * size_lu / Re
        nu_lu = u_lu * size_lu / re_si
        unit_m = length_si / size_lu
        unit_s = u_lu / u_si * unit_m
        if abs(nu_lu * unit_m * unit_m / unit_s - nu_si) > 1e-9 * nu_si:
            raise CaseError("internal unit inconsistency")  # tripwire

    tau = 3.0 * nu_lu + 0.5

    # --- LES clamp ----------------------------------------------------------
    # At real-world speeds Re is far above what a hobby-sized mesh resolves:
    # nu_lu underflows and tau sinks toward 0.5 (unstable). FluidX3D practice
    # is to run at the lowest stable tau with Smagorinsky LES supplying the
    # missing dissipation. We do that automatically and report the effective
    # resolved Re. (Turning "quality up" alone barely helps: max resolvable
    # Re grows only linearly with size_lu.)
    les_clamped = False
    if mode != "volume_force" and tau < TAU_HARD_MIN:
        tau_exact = tau
        nu_lu = NU_LU_FLOOR
        tau = TAU_HARD_MIN
        les_clamped = True

    re_lu = u_lu * size_lu / nu_lu
    d = Derived(
        nu_lu=nu_lu, tau=tau, u_lu=u_lu, size_lu=size_lu, re_si=re_si,
        re_lu=re_lu, unit_m=unit_m, unit_s=unit_s, rho_si=rho_si, u_si=u_si,
        les_clamped=les_clamped,
    )

    # ---- guard rails ----
    if les_clamped:
        d.warnings.append(
            f"Re = {re_si:,.0f} is too sharp for {size_lu:.0f} voxels across the "
            f"model (air's viscosity underflows the solver: tau would be "
            f"{tau_exact:.5f} < {TAU_HARD_MIN}). Running Smagorinsky LES at the "
            f"lowest stable viscosity; the flow sees an effective Re ~ {re_lu:,.0f}. "
            "Drag on blunt bodies stays a good match; boundary-layer detail does "
            "not. To resolve more: larger model in the box, or test a lower speed."
        )
    if tau < TAU_SAFE_MIN:
        d.subgrid_suggested = True
        if not les_clamped:
            d.warnings.append(
                f"relaxation time tau={tau:.4f} < {TAU_SAFE_MIN}: too close to "
                "inviscid for plain SRT; enabling Smagorinsky LES (subgrid) model")
    if tau > TAU_SAFE_MAX:
        d.warnings.append(
            f"tau={tau:.3f} very high (over-diffusive): the flow will look "
            "artificially viscous; raise the flow speed or use a coarser "
            "model resolution")
    if u_lu > U_LU_MAX:
        raise CaseError(f"u_lu={u_lu} exceeds stability limit {U_LU_MAX}")
    if u_lu > U_LU_WARN:
        d.warnings.append(
            f"u_lu={u_lu} is high (Ma>0.25): consider u_lu<=0.1")
    return d
