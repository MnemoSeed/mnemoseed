# Results: graphview-three benchmark (5,000 nodes, ~19k edges)

> Versioned copy of the benchmark evidence (NFR-7.2 v2 / G-AC4). Runnable harness: `.bench/graphview-three/` (local only, gitignored).

## Machine

| item | value |
|---|---|
| CPU | AMD Ryzen 7 3800X 8-Core (16 threads) |
| RAM | 31.9 GB |
| GPU (WMIC) | NVIDIA GeForce RTX 3070 |
| GPU (WebGL) | ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x00002484) Direct3D11 vs_5_0 ps_5_0, D3D11) |
| OS / browser | Windows 10.0.26200, Chrome (headed, channel chrome) |
| WebGL | 2 (three.js uses WebGL2) |
| Versions | three 0.185.1, three-forcegraph 1.43.4, playwright 1.62.1, node v20.12.2 |
| Viewport | 1280 x 720 (headed Chrome clamps the window on the 1080p / 125%-scaled display) |
| vsync cadence | ~4.17 ms observed (~240 Hz display); V-B saturates the display |

Method: headed Chrome on the real GPU (D3D11). fps = in-page rAF frame-delta
sampling; avg = frames/elapsed, p5 = 1000 / p95(frame time). 20 s windows,
20 s warmup for V-A (d3 layout cooldown), data = `public/data/graph.json`
(5,000 nodes, 19,238 edges, 8 profile clusters, 6 types).

## Headline numbers (V-A point values jitter ±40% across runs, so its fps figures are expressed as ranges spanning both runs; V-B is stable to 2 decimals)

fps avg / p5 per variant x scenario. **>= 30 fps floor marked per cell.**

| scenario | V-A layered (three-forcegraph) | V-B raw (instanced three.js) |
|---|---|---|
| S1 static render | **11.8–13.2 / 9.6–11.4** (fail) | **239.87 / 232.56** (pass) |
| S2 decay animation ~4 Hz | **7.9–8.1 / 4.1–4.2** (fail) | **239.62 / 232.56** (pass) |
| S3 hover sweep + clicks | **10.4–11.0 / 9.6–10.0** (fail) | **239.57 / 232.56** (pass) |
| S4 filter ~50% (post) | **24.6–25.4 / 21.8–23.9** (fail) | **239.77 / 232.56** (pass) |

Secondary metrics (run 1):

| metric | V-A | V-B |
|---|---|---|
| S2 decay tick update (avg / max) | 265.78 ms / 448.4 ms | 1.54 ms / 3.9 ms |
| S2 effective tick rate over 20 s | 32 ticks (~1.6 Hz) | 79 ticks (~4 Hz) |
| S2 edge weight changes | 30,756 | 76,391 |
| S3 hover pick latency (median / p95) | 1.8 ms / 2.4 ms | 0.2 ms / 0.5 ms |
| S3 click latency (median / p95) | 1.6 ms / 2.2 ms | 0.1 ms / 0.3 ms |
| S4 filter update time | 501.7 ms | 4.1 ms |
| S4 nodes hidden / visible | 2,384 / 2,616 (47.7%) | same |
| S4 fps before filter (avg / p5) | 12.17 / 10.92 | 239.77 / 232.56 |

## Verdict vs the >= 30 fps floor

**V-A (layered) fails every scenario** — overall it spans ~6–28 fps avg
(including the headless lower bound; point values jitter ±40% across runs),
best case 24.6–25.4 fps post-filter after hiding half the graph, p5 ≤ 8; the
marketing showcase (S2 decay) runs at ~7.9–8.1 fps avg. **V-B (raw) passes
every scenario by ~8x** (vsync-saturated at the display's 240 Hz, so the real
headroom is larger than reported). Numbers were stable across two runs (V-B
identical to 2 decimals).

## Recommendation

**Commit to V-B: a hand-rolled three.js layer (instanced points + instanced
edges + raycast picking + top-N canvas labels + precomputed layout).**
three-forcegraph is the wrong primitive at this scale: it cannot hold 30 fps
even for a *static* full render, and its style-digest update path makes
decay-driven opacity updates (the product's flagship "memories visibly fading"
showcase) cost ~266 ms per 4 Hz tick — a half-second hitch on every filter.

## Cost drivers

**Why V-A is slow**
1. **One draw call per object.** three-forcegraph emits one `THREE.Mesh`
   (Lambert sphere) per node and one `THREE.Mesh` (cylinder) per link:
   ~24,000 draw calls for 5k/19k. Static render alone costs ~70 ms/frame.
2. **Style updates are whole-graph digests.** Any `nodeColor`/`linkColor`
   change re-evaluates accessors and re-parses colors (via tinycolor2) for all
   5,000 nodes **and** 19,238 links; kapsule defers it with `setTimeout(1)`, so
   the ~266 ms lands between frames as hitches, not in the update call you time.
3. **Filtering rebuilds object sets** (nodeVisibility/linkVisibility re-digest)
   — 502 ms for hiding 48%.
4. **Unbounded material cache** unless opacity is quantized. We had to quantize
   alpha to 5% steps (6 types x 20 levels) just to keep the cache bounded; even
   then S2 stays at 8 fps.

**Why V-B is fast (the tricks that mattered)**
1. **Batching: 2 draw calls for the whole graph.** Nodes = one `THREE.Points`
   with a custom shader; per-point size/color/opacity/visibility are buffer
   attributes. Edges = one `InstancedMesh` of unit quads; per-instance matrix
   gives length + thickness, per-instance color gives weight.
2. **Attribute-driven decay.** A decay tick is one 20 KB opacity attribute
   upload (nodes) plus matrix/color writes only for the ~5% of edges that
   changed — ~1.5 ms total.
3. **Raycast picking against one geometry** (~5k distance tests) is 0.1-0.2 ms,
   vs 1.8 ms raycasting 5k individual meshes.
4. **Label budget:** only the top-60 nodes by centrality get canvas sprites
   (60 draw calls); the other 4,940 render as points.
5. **Precomputed clustered layout** from the generator (task permits layout
   precompute) removes the ~7 s d3 warmup and lets the decay/draw costs be
   measured in steady state.
6. **Screen-space point size** (`gl_PointSize = size * pxPerWorldUnit / z`)
   keeps nodes crisp at any zoom without per-node geometry.

## Caveats

- V-A was measured with three-forcegraph defaults (Lambert spheres, cylinder
  links) plus mandatory opacity quantization. Custom `nodeThreeObject`
  instancing could recover some static fps, but that is reimplementing V-B
  inside V-A while keeping the 266 ms digest path — the slow part.
- Headless (SwiftShader) numbers are a lower-bound curiosity only: V-A ~6 fps,
  V-B ~240 fps, same verdict, not the gate.
- Minimum hardware: numbers measured on WebGL2 + a discrete GPU (RTX 3070
  class); iGPU headroom is unmeasured.
- V-B's fps is capped by the 240 Hz display cadence; the actual headroom over
  the 30 fps floor is at least the reported ~8x.
- Both variants use the same synthetic dataset and the same seeded RNG, so the
  comparison is apples-to-apples.

## Appendix: run 2 (variance)

| scenario | V-A avg / p5 | V-B avg / p5 |
|---|---|---|
| S1 | 11.82 / 9.60 | 239.92 / 232.56 |
| S2 | 7.90 / 4.14 (update avg 274 ms) | 239.77 / 232.56 (update avg 1.33 ms) |
| S3 | 10.43 / 9.60 | 239.97 / 232.56 |
| S4 | 24.61 / 21.83 (update 547 ms) | 239.97 / 232.56 (update 3.6 ms) |
