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

## Desktop — RTX 3090 (24 GB) — from FluidX3D's own benchmark table (v3.8)

| Precision | MLUPs/s (256³-class benchmark) |
|---|---|
| FP32 (D3Q27) | 5418 |
| FP16S | 10732 |
| FP16C | 10215 |

Max resolution at 24 GB: ~641³ (FP32) / ~765³ (FP16S). To be re-measured
on the 3090 when it's set up as the server machine.

## What this means for UX

A 256³ case runs at ~138 steps/s on the laptop GPU; the validation cylinder
(~0.4 M cells, 2D) runs at several thousand steps/s. Runtime estimates in
the UI come from the live MLUPs progress line the engine prints, not from
these static tables — the tables are only for the pre-run "this will take
about ..." promise.
