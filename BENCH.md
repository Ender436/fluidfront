# Benchmark calibration

Per-device solver throughput, measured with the pinned engine build
(`make bench`). The app uses these numbers to estimate case runtimes.

## Laptop — RTX 3050 Ti Laptop (4 GB, reports 3822 MB)

| Configuration | Result |
|---|---|
| FluidX3D v3.8 default benchmark (256³, D3Q19, FP16S, SRT) | **2318 MLUPs/s** (peak, stable over 10k steps) |
| Kernel memory bandwidth | 178 GB/s reported by FluidX3D |
| VRAM used by 256³ case | 984 MiB during run |
| Throughput at 256³ | ~138 steps/s |

Measured 2026-11-09 (log: `/tmp/opencode/fx3d_bench_laptop.log`).

## Desktop — RTX 3090 (24 GB), native Windows, driver 621.01

Upstream reference (FluidX3D v3.8 benchmark table): FP32/D3Q27 5418,
**FP16S 10732**, FP16C 10215 MLUPs/s at 256³-class.

Measured 2026-11-11 on the real Windows app (kart case, full_kart-2:
424 x 920 x 388 = 151.35M cells, D3Q19 FP16S, STL voxelize, ~37k steps):

| Metric | Value |
|---|---|
| Steady-state throughput | **~9,650-9,700 MLUPs/s** (up to ~64 steps/s) |
| Bandwidth | ~800-860 GB/s |
| Memory | GPU 9.67 GB, CPU host copies 4.19 GB |
| Single-buffer cap (driver) | 6143 MB global (vs 955 MB on the 3050 Ti!) |

Note the honest number for estimates is the ~9.7k real-case figure, not
the 10.7k 256³ peak: large in-scene cases sustain ~90% of benchmark rate.
Buffer cap of 6143 MB means max single-lattice case ~500M cells FP16S.

## What this means for UX

A 256³ case runs at ~138 steps/s on the laptop GPU; the validation cylinder
(~0.4 M cells, 2D) runs at several thousand steps/s. Runtime estimates in
the UI come from the live MLUPs progress line the engine prints, not from
these static tables — the tables are only for the pre-run "this will take
about ..." promise.
