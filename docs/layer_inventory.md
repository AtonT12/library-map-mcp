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
