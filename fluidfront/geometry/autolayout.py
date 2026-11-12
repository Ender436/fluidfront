"""Auto-layout: model bbox -> domain size + placement, so the user never picks
a domain. Rules follow good wind-tunnel practice (blockage & development
distance) expressed as multiples of the model size.

Tunnel convention (as in gen/setup_cpp.py): flow along +y, body centered in
x/z, at ~1/3 of the tunnel length from the inlet. `bbox_si` must already be
measured AFTER the model's rotation (i.e. in tunnel frame: y = streamwise).
"""

from __future__ import annotations

from dataclasses import dataclass

# longest model edge -> voxels, per quality tier (sized so the auto-fit
# domain stays in consumer VRAM; see vram_estimate_mi)
QUALITY_TIERS = {"preview": 48, "standard": 96, "detailed": 192}

# tunnel padding, in largest-lateral-model-size multiples (clearance per side /
# streamwise sections): body sits in a ~4x-wide, ~8x-long tunnel (open-jet
# practice; blockage ~6% for a compact body, equilibrium-BC sides are open)
SIDE_CLEARANCE = 1.5    # lateral: half-width around body >= 1.5 x lateral size
INLET_DISTANCE = 2.0    # inlet -> body front
OUTLET_DISTANCE = 5.0   # body rear -> outlet


def _round4(n: float) -> int:
    return max(16, int(round(n / 4.0)) * 4)


@dataclass
class Layout:
    nx: int
    ny: int
    nz: int
    size_lu: int
    center: tuple[float, float, float]   # fractions of (nx, ny, nz)
    blockage: float                      # bbox projected area / cross-section
    notes: list[str]

    def as_domain(self) -> dict:
        return {"nx": self.nx, "ny": self.ny, "nz": self.nz}


def fit_tunnel(bbox_dims_si, tier: str = "standard",
               size_lu: int | None = None, max_cells: int | None = None) -> Layout:
    """bbox_dims_si: (dx, dy, dz) physical extents in meters, tunnel frame
    (y = streamwise). Returns the domain + body placement.
    max_cells: VRAM-driven cap; the model resolution is lowered to fit."""
    tier = str(tier).lower()
    if size_lu is None:
        size_lu = QUALITY_TIERS.get(tier, QUALITY_TIERS["standard"])

    dx, dy, dz = (float(v) for v in bbox_dims_si)
    longest = max(dx, dy, dz)
    if longest <= 0:
        raise ValueError("empty bounding box")
    lat_max = max(dx, dz)

    notes: list[str] = []
    original_size_lu = int(size_lu)
    size_lu = max(16, original_size_lu)

    def dims_for(sl: int):
        scale = sl / longest                 # voxels per meter
        lat_lu = lat_max * scale
        stream_lu = dy * scale
        # streamwise sections follow the larger of lateral size and body length
        # (long slender bodies need long inlet/outlet development lengths)
        ref_lu = max(lat_lu, 0.35 * stream_lu)
        nx = _round4(dx * scale + 2 * SIDE_CLEARANCE * lat_lu)
        nz = _round4(dz * scale + 2 * SIDE_CLEARANCE * lat_lu)
        ny = _round4(INLET_DISTANCE * ref_lu + stream_lu + OUTLET_DISTANCE * ref_lu)
        return nx, ny, nz, scale

    # Lower the model resolution until the domain fits the GPU budget. Always
    # recompute the domain from the *final* size_lu so the returned dims and
    # the reported blockage/scale stay consistent.
    while True:
        nx, ny, nz, scale = dims_for(size_lu)
        cells = nx * ny * nz
        if max_cells is None or cells <= max_cells or size_lu <= 16:
            break
        new_size = int(size_lu * (max_cells / cells) ** (1 / 3))
        if new_size >= size_lu:  # guarantee progress (int truncation)
            new_size = size_lu - 1
        size_lu = max(16, new_size)
    if max_cells is not None and nx * ny * nz > max_cells:
        notes.append("even the minimum model resolution does not fit the "
                     "available GPU memory - reduce the model or use a smaller "
                     "quality setting")
    if size_lu < original_size_lu:
        notes.append(f"model resolution lowered from {original_size_lu} to "
                     f"{size_lu} voxels to fit the available GPU memory")

    blockage = (dx * dz) / ((nx / scale) * (nz / scale))
    widened = False
    while blockage > 0.10:  # virtual walls would squeeze the flow: widen
        widened = True
        nx = _round4(nx * 1.1)
        nz = _round4(nz * 1.1)
        blockage = (dx * dz) / ((nx / scale) * (nz / scale))
    if widened:
        notes.append("model too wide for the default tunnel - it was widened "
                     "automatically (runtime grows)")
    if blockage > 0.07:
        notes.append(f"tunnel blockage (bounding box) is {100 * blockage:.0f}%: "
                     "the side walls will be felt by the flow")

    return Layout(
        nx=nx, ny=ny, nz=nz, size_lu=int(size_lu),
        center=(0.5, 1 / 3, 0.5),
        blockage=blockage, notes=notes,
    )


def vram_estimate_mi(n_cells: int, precision: str = "fp32",
                     velocity_set: str = "d3q19") -> float:
    """Rough VRAM footprint (FluidX3D allocates ~93 B/cell for D3Q19 FP32)."""
    per_cell = {"d2q9": {"fp32": 53, "fp16s": 35, "fp16c": 35},
                "d3q19": {"fp32": 93, "fp16s": 55, "fp16c": 55},
                "d3q27": {"fp32": 125, "fp16s": 71, "fp16c": 71}}
    b = per_cell.get(velocity_set, per_cell["d3q19"]).get(precision, 93)
    return n_cells * b / (1024 * 1024)
