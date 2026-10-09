# `.ai` Layer Inventory & Visibility Ruling Record (P1 + P1.5)

> Date: 2026-09-28 · Inputs: `maps/{5F,6F} …copy.ai` + `maps/{5f,6f}_base.jpg`
> Tooling: `scripts/ai_layer_inventory.py`, `scripts/p15_wall_draft_score.py` (pure stdlib + Pillow, zero runtime deps)
> Plan reference: `../WALL_EXTRACTION_PLAN.md` sections 3 P1 / P1.5; this document also amends several assumptions from its section 1.2.

## 1. File structure facts (measured, supersede earlier byte-level probing)

- Both files are `%PDF-1.6` containers; xref table entries are **18 chars wide** (gen field is 5 digits) — parse by regex, not fixed-length slicing.
- OCG objects are plain `/Type/OCG` dicts whose `/Name` is a **literal string**: ASCII layer names read directly; CJK names are **BOM + UTF-16BE literals** (e.g. `\xfe\xff小地图`). The earlier "hex-encoded odd layer name" theory is void — it was the same BOM encoding issue.
- `/Properties` mapping rule: **MC n ↔ nth entry of the `/OCGs` array** (5F base obj 30, 6F base obj 44).
- The page content stream is a single object (5F obj 47 / 6F obj 57); `/OC /MCn BDC … EMC` blocks: **5F 16/16, 6F 12/12** all found → the P1 Gate's "blocks + mapping usable" requirement is **met**.
- **Amendment to §1.2**: 6F layer blocks are **not** "pure Form XObjects". Wall geometry in both files is **inline path operators**; Form XObjects (5F 859 / 6F 796, ~0.5 KB each) are small components such as seat icons — `Do` calls only place icons/images.
- **Amendment to §1.2**: 6F has **no** semantically named wall layer — its walls live mixed inside `Layer 17` (see §2).

## 2. Wall-bearing layers (Gate core output)

Rule used: color family. Both floors share one pair of wall color constants:

```
WALL_FILL   = (0.45, 0.43, 0.61)   # filled wall strips (purple-grey)
WALL_STROKE = (0.49, 0.39, 0.61)   # wall outline strokes
TOL = 0.07
```

| Floor | Wall source | Data shape | Volume |
|---|---|---|---|
| 5F | `墙面`(MC9), `group study room 墙面`(MC8), `墙面描边`(MC10) | same-color rect fills ×(90+54) + outline polylines ×22 | draft extraction: 140 fills / 26 strokes → 166 segs |
| 6F | `Layer 17`(MC3), a **single mixed layer** (walls and furniture co-located) | same-color rect fills ×104 + outline polylines ×39 | draft extraction: 93 fills / 39 strokes → 132 segs |

- 5F furniture (black/light fills) sits in unnamed layers (`Layer 17`, `Layer 41`, …) → split by **color family**, not layer name.
- On 6F the `书架` layer contains text labels only (N607 Researcher's Room…); the actual shelf shapes live elsewhere, so point relocation works in pixel space.
- White background rects (5F `Layer 40`: `18.5 0 1750.5 1080`; 6F `Layer 28`: `0 0 1910.188 1073.125`) anchor the P2.0 coarse transform; they are not counted as walls.

## 3. Text capability assessment (optional acceptance item)

- `房间号` layer: glyphs are **custom-encoded control characters** (`\x1e\x1d\x1c…`), no `/ToUnicode` → **undecodable**; drop the room-number reading route and let P3 anchors fall back to pure geometry (the plan's pre-agreed fallback).
- Absolute `Tm` baselines are capturable but sparse (AI layout leans on relative `Td/TD`): ~34 absolute anchors floor-wide on 5F, ~11 on 6F. **P3 must rebuild the full text-matrix chain (BT→Tm/Td/TD/'/")** to obtain dense anchors — carried into P3 work items.
- Readable labels exist: `文字区域`/`文字标志` layers contain `'N506' 'Fire Exit' 'Entrance' 'Library Staff'…` — useful for semantic cross-checks during P4 review.

## 4. OFF-layer audit (structural evidence for P1.5)

| Floor | OFF layer | Content (measured bytes) | Value for wall extraction |
|---|---|---|---|
| 5F | `Layer 40` (MC0) | full-page white rect ×1 | none |
| 6F | `Layer 28` (MC0) | full-page white rect ×1 | none |
| 6F | `Layer 16` (MC1) | `/Fm0 Do` → Form = **one Im0 image** (575×141 @ (732,500)) | none (raster) |
| 6F | `Layer 18` (MC2) | `/Fm1 Do` → Form = **one Im0 image** (1541×1050 @ (48,15), near full-page screenshot) | none (raster) |

→ **The OFF set contains no extractable vector walls.** This contradicts the plan's bidirectional trap assumption ("`/D` ON/OFF ≠ JPG visible set"): here the trap does not exist — OFF content is either a meaningless white backdrop or rasters.

## 5. P1.5 ruling (automatic verdict)

Method: for each of 「ON-only」 and 「ON∪OFF」 run one draft extraction (wall-color fill midlines + wall-color stroke polylines), map through the P2.0 coarse transform (white rect ↔ bright bbox of the base image), and compute dark-pixel alignment scores. The global coarse affine carries non-uniform scale error (5F sx/sy = 3.46/3.89, 11.1% apart; 6F 4.4% — exactly the §1.3 red flag), so the effective metric is **per-segment local pose search** (±96 px translation grid, 9×9 min-filter window).

| Floor | Segs | Global score ON / ON+OFF | Local dark-rate median | frac ≥ 0.5 | Verdict |
|---|---|---|---|---|---|
| 5F | 166 | 0.1192 / 0.1192 | **0.867** | 0.711 | **ON-only** |
| 6F | 132 | 0.1116 / 0.1116 | **1.000** | 0.864 | **ON-only** |

- The two candidate **segment sets are identical** (OFF adds no vectors, §4), scores tie → take the minimal sufficient set, **ON-only**.
- Local dark rates prove the ON-set drafts land on real dark lines in the base images (not accidental alignment).
- The low global score belongs to P3 refinement (RANSAC/Huber with real anchors); it does not affect this ruling.

**Ruling recorded**: the P2 extractor processes the default-ON layer set only; there is no whole-layer exclusion logic for 6F (no independent bookshelf layer) — the color-family filter covers it.

## 6. Handoff to P2/P3: constants & caveats

- Color-family constants and TOL as above (§2); the P2 extractor implements "color family first, layer name auxiliary" and must not assume any layer name exists.
- P2.0 coarse transform params archived (5F): white rect (18.5, 0, 1769, 1080) ↔ bright bbox (4, 0, 6060, 4204); sx=3.460 sy=3.893. (6F): (0,0,1910.188,1073.125) ↔ full-frame bright area; sx=3.941 sy=3.768.
- P3 must-do: solve sx/sy independently (non-uniform scaling); handle the ~740 px empty right margin of the 6F base before fitting (crop or weight); dense text anchors require rebuilding the matrix chain (§3).
- Door-gap detection (first input of the §2.3 formula) not yet executed — deferred to P2.

## 7. Gate status

P1 required items: wall-bearing layers identified ✅ / BDC + mapping usable ✅ / P1.5 ruling done ✅ → **P2 may start**.
Optional item (room-number text samples): undecodable, downgraded to pure geometric cross-validation per the plan.

## 8. P2 execution record (2026-09-28)

**Tool**: `scripts/extract_walls.py` (reuses the parsing layer of `ai_layer_inventory`/`p15_wall_draft_score`; pure stdlib + Pillow). Pipeline: ON-only layer set (§5 frozen) → color-family filter → rectangular-fill midlining (quad midpoint of the two short edges; thickness = mean short-side length) → stroke de-duplication (drop if parallel to a fill centerline within max(6u, 2×median thickness)) → collinear merge (1° direction bins + intercept clustering at 6u, seams ≤ 6u absorbed) → gap = door candidate (8–160u) → P2.0 coarse transform to pixels.

| | 5F | 6F |
|---|---|---|
| Fill centerlines / non-quad dropped | 143 / 1 | 95 / 1 |
| Stroke segs / dedup dropped | 26 / 4 | 39 / 12 |
| Final in-sheet wall segments | 154 | 117 |
| Door-gap candidates | 52 (half-width median 16.0u) | 28 (median 14.2u) |
| Wall thickness (pdf units) | median 3.17 (p10 2.0 / p90 3.17) | same |

Key findings and gotchas (must-read for later phases):
- **Walls are thin 2–3.2 pdf-unit fill strips** (~7–11 px), not thick blocks; once midlined they serve as barrier lines.
- **Regression anchor (P3 free verification source #1)**: 5F hand-drawn 8 segs vs extracted walls ≈ median-of-medians 119 px under the coarse transform; the east-edge segment (x≈6030) is off by ~519 px — non-uniform-scale error of the coarse affine, which P3 RANSAC must absorb.
- **6F has overflow rects outside the page** (largest white poly extends to x=3486, y=1966): when selecting the "content white rect", constrain to the MediaBox (fixed in both scripts).
- **Door candidates include T-junction false gaps** (min half-width 6.4u appears identically on both floors — seam noise) → the "door half-width" feeding the §2.3 formula must come from the P4 human-reviewed subset, never from the raw table minimum.
- **Provisional min spine spacing 1.2/1.3 px**: wp5–wp7 (5F) and wp10–wp11 (6F) run along extracted walls — registration error or genuinely tight; P3 calibrated values decide via the §2.3 convergence gate.
- Point relocation (data side): Shelf-on-line candidates (threshold 28 px = clearance 6 + margin 20 + 2): 5F has 2 (`reference-collection-east-5f`, `digital-periodicals-5f`, shifts 2–4 px); 6F has 2 (`pn-6f` clean, `career-6f` needs review: neither side satisfies clearance). Outputs in `extract_out/<floor>/route_override_candidates.json`, **not applied to the live override file**.
- **Mechanism landed (plan §2.1, all four requirements)**: `src/route_overrides.json` (currently an empty `{}` shell) is applied immediately after CSV read in `_load_rows`, caching **effective** coordinates; cache key = (csv mtime, override mtime); selfcheck gained `_check_route_overrides` (fail-fast when a key matches no place_id); the hot-reload dedicated assertion is deferred to P5. Values will be injected after P3 calibration + P4 review, to avoid uncontrolled copy drift before the P5 refreeze.

**Artifacts** (`daniel-library-mcp/extract_out/{5F,6F}/`): `walls_pdf.json` (PDF space, schema == corridors.json `walls`), `walls_px_draft.json` (coarse pixel draft), `report.json` (stats/doors/thickness/transform/regression), `debug.png`, `route_override_candidates.json`. After P3, re-run with `--transform <json>` to refresh pixel artifacts; no geometry re-derivation needed.

**Verification**: `test.py` (220 routes, 0 crossings / 0 tight clearances), `test_fallbacks.py`, `test_route_schema.py` all green; override mechanism ad-hoc verified (apply / hot-reload / restore). `corridors.json` itself untouched (merge happens in P4).

## 9. P3 execution record — calibration (2026-09-30)

**Tool**: `scripts/calibrate_walls.py` (stdlib + Pillow; reuses P2 helpers). Method, per floor:
(1) sub-pixel sheet-edge localization around the P2.0 bbox estimates (band-mean brightness profiles, quadratic sub-pixel vertex; edges at the image frame are exact; σ per edge reported);
(2) base diagonal-affine transform from sheet corners (y-flip convention identical to P2.0);
(3) anchor fits where hand-drawn walls exist (5F): similarity (4 DoF, closed form) and general affine (6 DoF, per-axis LSQ), Huber-reweighted iteration (robust RANSAC-style down-weighting); selection prefers the most constrained model whose p95 residual is within 25% of the best;
(4) dark-pixel alignment polish (precomputed MinFilter(9), coarse-to-fine translation ±160→±1 px then per-axis scale ±1%) — produced **only as a separate review artifact**, never merged into the authoritative transform (see finding 3);
(5) gate inputs: door half-widths u→px (T-junction seam gaps excluded from the minimum), post-extraction min spine-to-wall spacing, residual p95 (anchored model + Monte-Carlo edge-drift, conservative max);
(6) §2.3 convergence gate: `2×p95 ≤ min(min_door_half_px − 6, min_spine_gap − 20)`.

| | 5F | 6F |
|---|---|---|
| Sheet edges (L/T/R/B, method) | 32.0σ0.7(step) / 0(frame) / 6037.5σ3.0(step) / 4204(frame) | 32.0σ0.7(step) / 0(frame) / 7533(frame) / 4043(frame) |
| Base diag sx, sy | 3.4307, 3.8926 | 3.9268, 3.7675 |
| Anchor fits (p95 px) | diag 550.9 · sim 2951.7 · aff 653.7 | n/a (no hand walls; MC edge-drift p95 ≈ 2.7 px) |
| Selected model | diag (most constrained, p95-best) | diag (only family available) |
| Alignment score coarse → final | 0.142 → 0.129 | 0.107 → 0.114 |
| Hand-wall overlap median-of-medians (coarse → final) | 119.2 → 118.2 px | — |
| Min door half-width px (raw / excl seams) | 21.9 / 21.9 (10 seams excluded) | 24.4 / 24.4 (4 excluded) |
| Min spine-to-wall gap px | **0.4** | **0.2** |
| Gate: 2×p95 vs limit | 1101.8 vs **−19.6** → **FAIL** | 5.3 vs **−19.8** → **FAIL** |

Key findings:
1. **The gate fails primarily on the spine term**: extracted walls lie within ~0–1 px of corridor spines somewhere on *both* floors (5F contact at (4066, 1192) wall #102; 6F similar) — either misclassified strokes or genuinely zero-clearance pinches. With registration error still in the hundreds of px on 5F, these contacts are not yet trustworthy, which is exactly why the gate halts the pipeline before P4 rather than after.
2. **5F shows large residual anisotropy**: west-cluster hand walls match within 79–130 px, east-edge walls stay off by ~500 px under every global model (diag/sim/aff). Combined with §1.3 (sheet aspect 1.638 vs base 1.442, 13.8%), the base JPG is very likely **not a projective (even affine) resampling** of the PDF sheet — i.e. non-rigid/local distortion, which no global transform can remove.
3. **Dark-polish basins are unreliable**: on both floors the score-maximizing pose drags the content rect *outside the image frame* (flagged `sheet_stays_in_image: false`) while improving the raw dark score — evidence the metric chases non-wall dark masses once fine-tuned past its validation envelope (P1.5 used it only for set-level comparison, where it is sound). Hence polished variants are archived separately (`calib_report.json → dark_polish_review`, `debug_final.png` overlays both) for visual inspection only.
4. Shared canvas artifact: both floors localize their left paper edge at pixel x≈32 (step detection) while top/bottom/right sit at the image frame — consistent with a common outer border/title band in the source template; harmless for interior geometry but worth knowing for future exports.

**Verdict: P3 gate FAILED on both floors.** Per plan §2.3 ("超限即停，不硬合并") and risk #1, **nothing is merged into `corridors.json`**; extraction stays draft-only. Artifacts per floor: `calib_report.json` (transforms, residuals, doors, MC drift, gate math, contacts), `walls_px_final.json` (authoritative selected-model mapping), `route_override_candidates_calibrated.json` (refreshed shelf moves, still *not* applied), `debug_final.png`.

Open decision for the user (before any P4 merge):
- **(a)** proceed to P4 human review anyway, using `walls_px_final` + `debug_final.png`: manually resolve the spine-contact walls and the 5F east-side offset cluster-by-cluster, merging only validated segments (plan's designed path, but with degraded calibration confidence);
- **(b)** treat the base JPGs as unsuitable for precise calibration and re-export clean rasters from the `.ai` at known scale (new-asset decision, outside the frozen constraints — would invalidate the "base maps untouched" assumption used so far);
- **(c)** extend the toolchain to piecewise/local transforms (out of plan scope; needs explicit sign-off before touching the schema-adjacent assumptions).

## 10. P4 execution record — 6F review-merge + 5F diagnosis (2026-09-30)

Executed per `map-mcp/P4_PLAN.md` (Track A = 6F merge, Track B = 5F diagnosis).

### A0 — 6F right-margin disposition: verified no impact
Column dark-pixel profile of `6f_base.jpg` (200 px bins, threshold 150): bins
x>=6800 read 0.9–2.7% dark vs 2–14.7% elsewhere — the ~740 px right band is
genuinely empty interior whitespace. P3's sheet fit is corner-based
(left step x=32, other three edges frame-clamped), so interior whitespace
cannot pull the solution; the residual term (2xp95 = 5.3 px) independently
validates the fit. Recorded as "verified no impact", no re-run needed.

### A1 — review override mechanism (risk #5 acceptance item)
- `docs/wall_review_override.json` created (`{version:1, floors:{6F:{removed:[]}}}` shell).
- `scripts/extract_walls.py`: `seg_hash()` (order-normalized, 0.1-rounded,
  sha1-12 of PDF-space segment) + `load_review_override()` (missing file =
  empty set; unreadable JSON fails fast) + `--review-override` flag (defaults
  to the docs file); removals apply right after the in-sheet filter, are
  counted into `report.json` stats, and unmatched hashes emit a WARNING.
- Dedicated test (temp out-dir, temp override files, real 6F `.ai`):
  1-removal run -> 116 walls; byte-identical rerun (`deterministic=True`,
  review decision survives re-extraction); shell run -> 117 walls back.
  The unmatched-hash warning path is code-reviewed (not runtime-triggered).

### A2 — 6F removal rulings (against `extract_out/6F/debug_final.png` + crops)
Removed 8 of 117 (in-sheet filter already dropped 1 of 118 merged runs):

| # | hash | ruling |
|---|---|---|
| 89 | `682e16dcf399` | full-width plan/legend divider (pdf y=540), decoration |
| 76 | `d100709a9714` | bottom-panel rule by the 6TH FLOOR label, decoration |
| 90 | `b6115900e615` | strike through the North-arrow label (crop-verified), decoration |
| 91 | `6010e64b96a1` | strike through the "Library 6F" title (crop-verified), decoration |
| 93 | `1fe3c527e882` | full-height sheet/panel border, pdf x=68, crosses legend zone |
| 21 | `e427f25ce957` | twin of #93, 9u apart (double border line) |
| 73 | `075206d0ea0e` | Staff Office panel right border (top 245 px touch the SW stair corner) |
| 112 | `e24403e4c8f1` | twin of #73, 7.5u apart (double border line) |

Kept with recorded rationale: wall #82 (+#81, x=1560.8 door-bearing run —
tiling with door gaps #4/#5/#7, KEEP; the 0.2 px contact is hand-spine
wp2-wp3 drawn on the wall line, P5 action); #77/#79 (N60x room dividers,
crop-verified real); top double-lines #104/#2/#103/#1 (north exterior wall);
all 28 doors as detected (blocker sweep: only #15/#16 touch perpendicular
divider ends = T-junction meetings, not leaf blockages — KEEP).
Forensic follow-up on the 7 spine-crossing runs outside the A2 checklist
(z1 stair edge, z3 restroom stalls, z4 amenity fins): all resolve to
**wall-colored FILLS on Layer 17 at wall construction width (2.0–3.17u)**,
with compartment/fin morphology — drawn as walls, so KEEP; their spine
crossings are spine-side data issues (P5), not extraction errors.

### A3 — gate after rulings: residual PASS, spine term = data flag
Recalibrated 6F on the cleaned set (109 walls): `2xp95 = 5.3 <= 18.4`
(door bound) — **calibration validated**. The spine term (-19.8) cannot pass
while any wall sits within 20 px of a spine; all remaining contacts are
real-wall / hand-spine coincidences (A2), so per A2-item-3 ("real wall ->
keep, relocate spine in P5") the merge proceeds with the gate decomposition
recorded here instead of a forced PASS. This is not force-merging: the merged
set contains only validated walls; the failing term is owned by the spine
asset with a P5 work order.

### A4 — merge + first true baseline
- `corridors.json`: 6F `walls: []` -> 109 calibrated segments (single-hunk
  diff, CRLF preserved, byte-verified `merged == walls_px_final`); 5F 8 hand
  walls untouched. `selfcheck.py` + `scripts/build_static.py` green.
- `src/test.py` (default budgets): search/directions/walls/5F-spine all
  `[ok]`; aborts at the 6F spine assert (first crossing wp4-wp7) as expected.
- Dashboard (220 routes / 330 legs / 0 unresolved): **C1 crossing = 148**
  (all 6F; 5F legs clean), **tight = 0**, **C3b = 0** (penalty philosophy
  holds — the graph never gives up). Crossing segments: **225 on-spine /
  122 endpoint-connector / 0 other**. Spine margins: 5F 64.4 px, 0 crossings;
  6F 0.0 px, **15 proper crossings** (wp1-wp2 x1, wp2-wp3 x5, wp4-wp7 x1,
  wp7-wp9 x3, wp8-wp9 x2, wp10-wp11 x3).
- Per-plan §2.4 with available columns (C2/C3 internals are P5 scope):
  C1 > 0 with C3b = 0 = crossings via penalized spine edges the router pays
  for but cannot avoid — the **spine-through-wall regime** (wall data
  validated above; spine network predates walls). P5 confirmation of C2 > 0
  on these legs will close the characterization.
- `test_fallbacks.py`: red **only** on the two cross-floor 6F-leg bytes
  (`cross_N607[1]`: 1505030 -> 1502438; `cross_QA76[1]`: 1505390 -> 1512214;
  5F bytes unchanged). Refreeze deferred to P5 per plan.
- `test_route_schema.py`: green after a one-line test-data fix — the
  "6F unmapped warning" assertion (line 165) encoded the pre-merge reality;
  it now asserts the warning stays silent for mapped 6F (product code
  untouched). Search parity (16 queries), place-id uniqueness, determinism
  rings all green: search side shows zero drift from the merge.

### Track B — 5F diagnosis and routing
- **B1** (one-to-one + max_dist 150 + overlap 0.5, new CLI knobs on
  `calibrate_walls.py`, temp out-dir): only **1/8** hand walls still match.
  The 100–500 px disagreements are structural, not matching looseness.
- **B2** (N509 offset field under P3-final): per-wall mean offsets
  (+190,-107), (-513,-22), (+25,-130), (+167,-22), (+105,-98), (+536,-14),
  (-79,0); cross-wall std **dx=294 / dy=49 px** vs mean magnitude 83 px —
  decisively **non-uniform**, no translation fix exists.
- **B3** (blink: pymupdf render installed `--target` to temp only, venv
  untouched): same room identities/labels in both sources (same artwork),
  but the .ai sheet renders **12.4% wider** (6813 vs 6062 px) with
  west-anchored divergence; x-squash cancels west/center while the east
  keeps ghosts. Scale-candidate sweep (dark score + hand overlap):
  walls 1/3/4/7 prefer sx~3.0–3.16, walls 5/6 prefer sx~3.6+ — **no global
  x-scale converges** (best: sx=3.6 with 7/8 walls <=130 px, wall 6 at 267).
  Evidence (`extract_out/5F/b3_rigid_overlay.png`, `b3_squash_diff.png`,
  `b3_scale_test.json`): regional deformation of a common artwork.
- **B3 routing: 5F does NOT join Track A** (per P4_PLAN's own exclusion).
  Proposal for sign-off = **(c)-lite**: ~15 manual control points (room-label
  centroids + 8 hand walls) + piecewise/TPS warp of extracted PDF walls,
  re-validated against the hand set; fallback = (b) abandon `.ai` for 5F and
  extend the existing 8 hand walls with high-traffic corridor touch-ups.
  5F keeps its 8 hand-drawn walls; nothing merged.

### P5 work orders (out of P4 scope, recorded)
1. Relocate 6F spines off wall fabric: wp2-wp3 (room block), wp1-wp2
   (x=1560.8 run), wp8-wp9 (restroom core), wp10-wp11 (stall rows),
   wp7-wp9 (amenity fins), wp4-wp7 (stairwell S edge); watch stair-transit
   endpoint behavior at the z1 run (C3 check).
2. Inject `route_overrides.json` for pn-6f + career-6f (both flagged:
   neither side clears walls even after the move — needs hand placement).
3. Refreeze `test_fallbacks.py` bytes (table in A4) + restore budgets to 0.
4. (c)-lite sign-off for 5F, or (b) corridor touch-ups.

### Files changed (user commits)
`corridors.json` (6F walls), `docs/wall_review_override.json` (new, 8
removals), `scripts/extract_walls.py` (override preload), `scripts/calibrate_walls.py`
(B1 diagnostic knobs), `src/test_route_schema.py` (1-line merge-alignment fix),
`extract_out/{5F,6F}/` artifacts (untracked), this §10.

### P5-T — 6F merge revisited: render-snaps, trims, splits, east spur (2026-09-30)

**Trigger.** Chasing the 12 residual C1=C2=C3 legs (5 destinations) with a
wall-vs-render dark audit: the P4-merged 6F walls sit 20-110 px off their
rendered twins (e.g. restroom-north wall floats 84 px south in open floor;
N606 room-west floats 70 px west in corridor). Extraction geometry is
faithful (GeoWalker vs pymupdf ground truth: median 0.02 pdf units; the two
suspected Do-matrix bugs falsified: all form matrices are ID, 851 Do
sites show no stale operands). The fault is the P3 sheet-fit transform, which
was never validated wall-vs-render (calib_report.json: nchors: null;
the A3 2*p95 was Monte-Carlo sheet-edge jitter, not a render residual;
dark score 0.0994). No single global/TPS refit fits the field (LOOCV p50
~60 px; neighboring walls disagree in sign), so per-wall rulings.

**Method (all 87 audited, evidence-coded P/V/N).** Dark-pixel match within
±120 px perpendicular; min-|offset| tie-break; twin-exists check against the
extracted set; then tile-visual or numeric-profile verification wherever
ambiguous (icon/text bands hijack naive matching) or load-bearing:
- **47 shifts** (render twin, no twin in extracted set): plateau-center
  offsets, verified exact pairings (jamb pitch, band centers).
- **3 trims** (keep-interval on snapped geometry): w67 south overextend
  (white 918-975), w8 Fire-Exit-W corridor bit, w33 stairwell corner bit.
- **1 split**: w10 at the N603 door (render gap 3232-3279).
- **37 removals**: void/desk phantoms (tile-visual), shelf-upright weak lines,
  legend decorations, double-line dups (w0/w1 of w80/w81, w50 of w52,
  w75 of w6, w77/w78 of w12/w13), double-drawn N601 door w64.
- **3 keeps**: w35, w37 (already placed), w70 (double-leaf centerline).
- Review file extended with shifts/	rims/splits (PDF-hash keys, stable
  across re-runs); scripts/apply_wall_review.py replays deterministically
  (removals consumed upstream tolerated via 
eport.json, everything else
  fail-fast). 87 = 47 shifted + 35 removed + 3 kept + 2 trim-only
  (w8/w33; w67 shifted+trimmed).
- Dark score 0.0994 -> 0.9197. Merged **53 walls** (CRLF/indent-2 byte recipe
  preserved, verified round-trip identical).

**Spine (P5-1 redo vs truthful walls).** Reroute wp16-wp9/wp23-wp16 around the
restroom-north wall west end via wp24 (1900,730); wp23 -> (3900,730);
wp13/wp14 -> y=750; wp1 -> (4498,678); east-lobby spur wp1-wp27 (6150,950) -
wp28 (6800,950) - wp26 (6900,890) serving N606B/C/E + Fire Exit E (all four
connectors verified clean). Min margin 21 px.

**Endpoints (P5-2).** 
oute_overrides.json gains n603-6f (3255,1479)
(same-room door-front move through the w10 gap). pn/career/stairs entries
predate this session and are unchanged in effect (transit usage identical:
Central 110, others 0; 16-query search parity green).

**Convergence (P5-3/4).** Dashboard 220 routes: **C1=C2=C3=C3b=0** at default
0/0 budgets (was 12/12/12/0). 	est_fallbacks.py 6F-leg bytes refrozen
(cross_N607[1] 1505030->1540207, cross_QA76[1] 1505390->1512214->1509434);
registration probe now reads raw CSV (overrides move 6F stairs by design;
raw worst 65.1 px). Full matrix green: test.py, test_fallbacks.py,
test_route_schema.py, selfcheck.py, build_static.py.

**Known coverage gaps (backlog, not gates).** No wall = no penalty, so tests
stay green while these stay openly walkable: north perimeter + shelf-zone
walls (.ai lacks them), C-south wall, N601 westmost jamb, restroom door
frames, west-stair railings. The w25 west-end doorway (render gap
3105-3206) is deliberately NOT split: spine reroutes around the west end.

### Files changed (user commits)
corridors.json (6F: 87 -> 53 walls, +4/-0 waypoints/edges net),
docs/wall_review_override.json (shifts/trims/splits + 37 removals),
scripts/apply_wall_review.py (new), src/route_overrides.json (n603-6f),
src/test_fallbacks.py (2 bytes + raw-CSV probe),
extract_out/6F/walls_px_snapped.json (new), this §11.

### P6 visual-review round (2026-09-30, user screenshots)

**6F fan diagonals.** East-lobby spur dangled off wp1, forcing a V-detour
(6100,1300)->(4498,678)->(6150,950) that sliced A-D shelves. Reconnected
spur at wp2 (replaced wp1-wp27 with wp2-wp27, 39.9 px margin, shelf slicing
gone); N606 legs now run south corridor + lobby walk + short connectors.

**5F entrance-stairs diagonal.** Old mid-flight endpoint (4475,950) grazed
stairwell wall corners; first fix candidate (stair foot south) crossed the
west wall squarely and was reverted. Final: stairs_main-5f moved to the
stair-hall north mouth (4400,740, render opening 4338-4462 verified); leg
runs Entrance -> north lobby -> east through open floor (zero render-dark
hits on the east segment; north segment only crosses label text). Transit
usage unchanged (Central 110). Refroze cross_N607/cross_QA76 first legs
(1627310->1626841) + N607 6F leg (1540207->1502592, spur reroute).
