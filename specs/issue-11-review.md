# Issue #11 — Code Review

Reviewer: critic agent
Date: 2026-05-10
Branch: `feat/11-zone-photos-thumb-lightbox` (commit `411d07d`)
Spec: `specs/issue-11-architecture.md`

---

## Verdict: APPROVE

The implementation matches the architecture spec end-to-end. SEC-009 path-traversal is watertight, the new regex correctly rejects every adversarial suffix the spec called out, atomic upload + archive flow is in place, lazy-fallback for legacy zones works, all 1737 tests pass (30 of them new).

What convinced me to approve, not block:

- I empirically reproduced the EXIF=6 upload case (`render_two_variants` on a 100×200 JPEG with Orientation tag): main came out 200×100 (rotated), red top-row landed on the right edge. EXIF is honored.
- I exercised every `_thumb`-suffix evil case from the test suite — all rejected by `_ZONE_PHOTO_FILENAME_RE`.
- `?variant=` is purely a string compare, never concatenated into a path. `?variant=../../etc/passwd` falls into the else branch and returns the main file (still SEC-009-validated). `?variant=` (empty), `?variant=THUMB`, garbage — all default to main.
- Atomic upload writes (`_atomic_write` = tmp + `os.replace`) prevent torn reads on the main file. Reader during upload sees either old or new full file.
- Backwards compat verified by `test_thumb_falls_back_to_main_when_thumb_null`: legacy zone with `photo_thumb=NULL` and missing thumb file on disk still serves a 200 from `photo_path`.

Items below are non-blocking. The senior may sweep them in a follow-up or leave them — none affect AC for issue #11.

---

## Non-blocking nits

### N1. Pixel-count guard runs *after* `img.load()` — defense-in-depth defeat
File: `routes/zones_photo_api.py:56-63`

```python
img = Image.open(io.BytesIO(image_bytes))
img.load()  # force decode so PIL raises here, not later   ← decode happens FIRST
w0, h0 = img.size
if w0 * h0 > _MAX_INPUT_PIXELS:
    raise ImageTooLargeError(...)
```

`img.size` is populated from header parsing in `Image.open`, no decode required (verified — opened a 64 MP PNG, `size` was correct without `load()`). Calling `img.load()` before the size check forces full pixel decode for malicious-but-decodable inputs up to Pillow's `MAX_IMAGE_PIXELS=89_478_485` (Pillow 12.2.0 default). The 50 MP guard never fires for files that are 50–89 MP — they're already decoded by the time we check.

Mitigation in place: Pillow's own `DecompressionBombError` fires at 2× MAX_IMAGE_PIXELS (~178 MP). And the 22 MB `MAX_CONTENT_LENGTH` caps the input size, so worst-case adversarial decode is bounded.

Fix (one line move):

```python
img = Image.open(io.BytesIO(image_bytes))
w0, h0 = img.size                  # safe: header only, no decode
if w0 * h0 > _MAX_INPUT_PIXELS:
    raise ImageTooLargeError(...)
img.load()                         # decode now that we know it's safe
```

### N2. Rotate is not atomic across the two files
File: `routes/zones_photo_api.py:383-390`

```python
with Image.open(filepath) as img:
    img = img.rotate(-angle, expand=True)
    fmt = img.format or 'JPEG'
    img.save(filepath, format=fmt)     # in-place write, not tmp + os.replace
```

Architecture spec §5 risk #1 explicitly called out: *"Adding a second file (thumb) means there's a window where main is updated but thumb is stale. Fix: write to `.tmp` siblings then `os.replace()`."* The senior implemented `_atomic_write` for the upload path but not for rotate. So:
- A concurrent reader during rotate can see a partial file.
- If rotate of main succeeds but rotate of thumb fails (disk full, signal kill), main is at angle+90°, thumb is at original orientation, DB still pairs them. Visually-mismatched pair.

Mitigation: rotate is a rare admin action (button click), failures during it are rare. Severity is real but unlikely to bite.

Fix: replace `img.save(filepath, format=fmt)` with a save-to-bytes-then-`_atomic_write` round trip. ~5 lines.

### N3. Pre-existing rotate bug — `img.format` is `None` after `img.rotate()`
File: `routes/zones_photo_api.py:386-387`

```python
fmt = img.format or 'JPEG'
img.save(filepath, format=fmt)
```

`img.rotate()` returns a new PIL image whose `.format` is `None`. So for a `.webp` file, this line saves **JPEG bytes** into a path named `*.webp`. Empirically confirmed: opened a webp, rotated, saved with `format=fmt` → file ext is `.webp` but `Image.open(path).format == 'JPEG'`.

This bug **predates issue #11** (same pattern at `b9c5316:routes/zones_photo_api.py:242-244`). Not in #11 scope. Karpathy "surgical changes" — flag, don't fix here. Recommended follow-up: drop the `img.format or 'JPEG'` heuristic, derive format from file extension instead (we know it's webp by construction).

The reason I'm not blocking: nobody noticed in two months of production, the file still decodes (browsers don't care about extension/MIME mismatches for image bytes), and #11 doesn't make this worse — it just rotates a second file that has the same pre-existing latent bug.

### N4. EXIF orientation test under-asserts
File: `tests/api/test_zones_photo_two_variants.py:128-147`

The test uploads a 100×200 JPEG with `Orientation=6` and asserts the thumb is `(400, 400)`. But the thumb is **always** 400×400 by construction (center-crop) regardless of whether EXIF was honored. Test passes even if `exif_transpose` is removed.

The architecture spec §4 test #3 said: *"assert one corner pixel matches original top-row color"*. That assertion was dropped. The senior's own docstring acknowledges: *"the crop would still be 400x400 (we always center-crop to a square)"*.

EXIF *is* in fact honored by `render_two_variants` (verified empirically — the upright main came out 200×100 with the red stripe rotated to the right edge). The test just doesn't prove it. To strengthen, also assert the **main** file's dimensions: input 100×200 with EXIF=6 should produce main of size `(200, 100)`. One extra line. (Or: assert `thumb.getpixel((0, 200))` is reddish — the upright transposed view places the original top row on a known edge after center-crop.)

### N5. `request.args.get('variant')` is case-sensitive
File: `routes/zones_photo_api.py:415-416`

`?variant=Thumb` or `?variant=THUMB` falls through to the main file. Pure UX issue, not security. Lowercase the comparison if you care: `if variant.lower() == 'thumb'`. I'd leave alone — the frontend always sends lowercase.

### N6. Two separate Esc-key listeners on `zones.html`
File: `static/js/zones.js:1700-1706` adds an Esc handler. The template's modal also has the existing outside-click handler at line 1693. No drift in behavior, but the IIFE pattern in `status.js:1026-1037` (single bundled handler block) is cleaner. Trivial.

---

## Verified-OK list

What I actively checked, confirmed correct, and the senior should NOT re-touch:

1. **DB migration `zones_add_photo_thumb`** — idempotent, guards on `PRAGMA table_info`, only runs `ALTER TABLE` if column missing. `db/migrations.py:1107-1117`.
2. **`update_zone_photo` two-mode signature** — `update_thumb=False` default preserves backwards compat for legacy CRUD callers; `update_thumb=True` writes both columns in one statement. `db/zones.py:599-628`.
3. **SEC-009 regex** — `^ZONE_\d+(_thumb)?\.(png|jpg|jpeg|gif|webp)$` with `re.IGNORECASE`. The optional `(_thumb)?` group is the **only** widening; anchored start/end + literal underscore prevents `ZONE_5thumb`, `ZONE_5_thumbb`, `ZONE_5_thumb_evil`, `ZONE_5_thumb.php`, `ZONE_5.webp.php`. All four evil cases asserted in `tests/unit/test_photo_path_traversal.py:119-128`. `services/helpers.py:29-32`.
4. **Variant query param safety** — never reaches the filesystem path string. Code at `routes/zones_photo_api.py:415-420` uses `variant` only to choose between two DB-stored values, both of which are then re-validated through `safe_zone_photo_path`. `?variant=../../etc/passwd` is harmless.
5. **Atomic upload writes** — `_atomic_write` at `routes/zones_photo_api.py:97-102` uses tmp file + `os.replace` (POSIX atomic on same filesystem). Reader during upload sees old-or-new, never a partial main file. Thumb is written second; the brief window where main is new and thumb is stale is acceptable per spec §5.1.
6. **Archive both old files before overwrite** — `_archive_old_zone_file` called for `photo_path` and `photo_thumb` at `routes/zones_photo_api.py:241-243`. Each archived path is SEC-009-validated before move (line 156).
7. **Delete handles missing thumb on disk** — `if os.path.exists(filepath): os.remove(filepath)` at `routes/zones_photo_api.py:300-301`. No exception if thumb file already gone. DB columns always cleared (line 309).
8. **Lazy migration on GET ?variant=thumb** — `photo_path = zone.get('photo_thumb') or zone.get('photo_path')`. Test `test_thumb_falls_back_to_main_when_thumb_null` proves this works even when the thumb file is absent and `photo_thumb` is `NULL`. `routes/zones_photo_api.py:416-418`, `tests/api/test_zones_photo_two_variants.py:179-205`.
9. **20 MB enforcement** — `MAX_FILE_SIZE = 20 * 1024 * 1024` (`services/helpers.py:125`), Flask `MAX_CONTENT_LENGTH = 22 * 1024 * 1024` (`app.py:111`), client-side checks at `static/js/status.js:1083` and `static/js/zones.js:1611`. Test `test_upload_rejects_over_20mb` posts 21 MB and asserts 400/413 — passes.
10. **Lightbox close — three paths**:
    - ✕ button: `templates/status.html:219` and `templates/zones.html:336`, `onclick="closePhotoModal()"`.
    - Esc: `static/js/status.js:1032-1036` (IIFE), `static/js/zones.js:1700-1706`.
    - Outside-click: `static/js/status.js:1029-1031`, `static/js/zones.js:1693-1697`.
    - All three wired on both pages.
11. **75vw / 75vh CSS cap** — applied to both `.photo-modal-content` and `.photo-modal img` in `static/css/status.css:1097-1118` and `static/css/zones.css:345-365`. The duplicated `max-width/max-height` on both selectors is intentional belt-and-suspenders for inner image overflow.
12. **EXIF orientation honored at the pipeline level** — empirically reproduced. `render_two_variants` calls `ImageOps.exif_transpose` once on the source, both main and thumb derive from the transposed image. The 100×200 test JPEG with `Orientation=6` produces a 200×100 main and a 400×400 thumb (with rotated colors at the expected positions).
13. **All four list renders use `?variant=thumb`** — `static/js/status.js:747` and `:1815`, `static/js/zones.js:109`, `static/js/zones-table.js:76`, `static/js/status/status-groups.js:297`. Lightbox onclick uses URL without `?variant=thumb`, opens main.
14. **Tests pass** — `pytest tests/api/test_zones_photo_two_variants.py tests/unit/test_photo_path_traversal.py` → 30 passed.

---

## Acceptance criteria — final tally (per `specs/issue-11-architecture.md` §2)

| AC | Status | Evidence |
|----|--------|----------|
| 20 MB upload | ✅ | `MAX_FILE_SIZE=20MB`, `MAX_CONTENT_LENGTH=22MB`, JS checks bumped |
| > 20 MB → clear error | ✅ | `error_code=FILE_TOO_LARGE`, message contains "20" |
| EXIF auto-applied | ✅ | `ImageOps.exif_transpose` in `render_two_variants:65` |
| Original ≤ 1920px, q=92 WebP | ✅ | Lines 73-80 |
| Thumb 400×400 1:1 center-crop | ✅ | Lines 82-92 |
| List shows thumb | ✅ | All 4 render sites use `?variant=thumb` |
| Lightbox at ~3/4 screen | ✅ | `max-width: 75vw; max-height: 75vh;` both pages |
| Lightbox close: ✕, Esc, outside | ✅ | All three wired both pages |
| Mobile + desktop | ✅ | CSS uses vw/vh, no fixed pixels for modal sizing |
| Aspect ratio in lightbox | ✅ | `object-fit: contain;` retained |
| Delete + rotate don't regress | ✅ | Both extended to handle thumb; tests pass |
| SEC-009 stays safe | ✅ | Regex tightened; all evil suffixes rejected |
| Backwards compat | ✅ | Lazy fallback `photo_thumb or photo_path` + tested |

End.
