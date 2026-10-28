# Agent notes

## Language convention
- Everything written into this repo is **English**: docs, comments, docstrings, log/error messages, new identifiers and copy.
- Conversing with the user may use Chinese; anything landed in the codebase must be English.

## Exceptions (data, deliberately untouched)
- Byte-frozen fixtures and frozen route text: `dev/fixtures/route_n607.json`, the four frozen values in `src/test_fallbacks.py`.
- Asserted UI string literals: `src/view/viewer.js`, `dev/viewer.test.js` (changing them breaks the viewer test suite; all such literals are English as of the 2026-10 Englishization, and any edit must update both files in the same change).
- Test input literals acting as real-world queries, e.g. the bilingual lookups in `src/test_route_schema.py`.
- Upstream/source data: `locations.csv` (never modified), the two `.ai` floor plans, and layer-name values quoted in docs (they are data, not prose).
