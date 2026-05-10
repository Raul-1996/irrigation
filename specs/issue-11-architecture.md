# Issue #11 — Architecture Spec

Title: Photos for zones: 20 MB upload, auto-compress, 1:1 thumb, lightbox at ~3/4 screen.

Author: backend-architect agent
Date: 2026-05-10
Base: `main`
Repo: `/opt/claude-agents/irrigation`

---

## 1. Audit of existing code

A lot already exists. Quote-and-line audit.

### 1.1 Upload pipeline (backend) — EXISTS

`routes/zones_photo_api.py`:
- `POST /api/zones/<id>/photo` (line 81) — upload handler. Validates ext + MIME, reads file, calls `normalize_image(file_data, target_size=(800,600), fmt='WEBP', quality=90)` (line 111). Writes ONE file `static/media/zones/ZONE_<id>.webp`, archives old to `OLD/`. Updates DB column `photo_path`.
- `DELETE /api/zones/<id>/photo` (line 152) — deletes file + nulls DB.
- `POST /api/zones/<id>/photo/rotate` (line 192) — rotates in-place via Pillow. SEC-014 angle clamp present.
- `GET /api/zones/<id>/photo` (line 258) — returns image (Accept: image/*) or JSON metadata.
- All endpoints use `safe_zone_photo_path()` for SEC-009 path-traversal guard.

### 1.2 Image helpers — PARTIALLY DONE

`routes/zones_photo_api.py:37 normalize_image()`:
- Calls `ImageOps.exif_transpose()` — EXIF orientation already honored (line 42).
- Converts RGBA/LA/P → RGB (line 45).
- If `target_size`: scale + center-crop to exact W×H. Else: fit `max_long_side`.
- Encodes WebP via Pillow with `quality`, `lossless`, `method=6`.

`services/helpers.py`:
- `MAX_FILE_SIZE = 5 * 1024 * 1024` (line 119) — 5 MB hard cap.
- `MEDIA_ROOT='static/media'`, `ZONE_MEDIA_SUBDIR='zones'`, `UPLOAD_FOLDER='static/media/zones'`.
- `ALLOWED_EXTENSIONS = {'png','jpg','jpeg','gif','webp'}`, `ALLOWED_MIME_TYPES` matching.
- `safe_zone_photo_path()` (line 65) with whitelist regex `_ZONE_PHOTO_FILENAME_RE = ^ZONE_\d+\.(png|jpg|jpeg|gif|webp)$` (line 26).
- `safe_media_subpath()` (line 29) — generic base-dir containment check.

### 1.3 DB schema — INCOMPLETE

`db/migrations.py:34-48`: `zones` table has column `photo_path TEXT` (line 44). NO `photo_thumb` column.
`db/zones.py:599 update_zone_photo()` updates only `photo_path`.

### 1.4 Frontend — LIGHTBOX EXISTS, THUMB RENDERING EXISTS

`templates/status.html:217-224` — `<div id="photoModal" class="photo-modal">` with `<img id="photoModalImg">` and a single Close button.
`templates/zones.html:334-342` — same modal, slightly different markup (extra `<h3>`).
`static/css/status.css:1083-1138` — `.photo-modal { display:none; position:fixed; background:rgba(0,0,0,0.9); }`, `.photo-modal-content { max-width:min(92vw,720px); max-height:85vh; }`, `.photo-modal img { max-width:95vw; max-height:85vh; object-fit:contain; }`.
`static/js/status.js:1013 showPhotoModal()` / `:1020 closePhotoModal()` — set/clear `display`. NO outside-click handler, NO Esc handler in status.js.
`static/js/zones.js:1680 showPhotoModal()` + `:1693` outside-click handler. NO Esc.
`static/js/status.js:1796-1807` — zone list render: shows `<img>` thumb when `z.photo_path`, else emoji icon. Thumb URL: `/api/zones/<id>/photo?ts=<bust>`.
`static/css/status.css:1389-1398` — `.zc-photo { width:40px; height:40px; ... overflow:hidden }` with `.zc-photo img { object-fit:cover; }`.

### 1.5 Upload UI — EXISTS but with wrong client-side limit

- `static/js/zones.js:1611` and `static/js/status.js:1068`: client checks `file.size > 5 * 1024 * 1024` (5 MB).
- `templates/zones.html:345`, `templates/status.html:213`: `<input type="file" accept="image/*" hidden>`.
- Sheet edit UI has Replace / Delete / Rotate buttons (`status.js:1047-1151 uploadStatusPhoto / deleteStatusPhoto / rotateStatusPhoto`).

### 1.6 Tests — partial

- `tests/unit/test_photo_path_traversal.py` — SEC-009 path-traversal coverage (good, keep).
- `tests/api/test_zones_api.py:110-128` — `test_get_photo_info_no_photo`, `test_upload_photo_invalid_format`, `test_delete_photo_no_photo`. No tests for size limit, EXIF, two-file output, or thumb.

### 1.7 Flask config — too small for 20 MB

`app.py:111`: `app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024`. Comment claims route-level enforces 5 MB. Must bump to ≥ 21 MB to allow 20 MB upload + multipart envelope overhead.

### 1.8 HEIC support — ABSENT

No `pillow_heif` / `pyheif` in `requirements.txt`. Pillow alone cannot decode HEIC. Issue says "HEIC if possible" — soft requirement.

---

## 2. Gap analysis (per acceptance criterion)

| AC | Status | Gap |
|----|--------|-----|
| 20 MB upload | MISSING | `MAX_FILE_SIZE=5MB`, `MAX_CONTENT_LENGTH=10MB`. Bump both. Adjust client checks. |
| Bigger than 20 MB → clear error | PARTIAL | Route returns generic "Файл слишком большой". Need explicit error_code + size in message. Also handle Flask's 413 from `MAX_CONTENT_LENGTH`. |
| EXIF orientation auto-applied | DONE | `ImageOps.exif_transpose()` in `normalize_image`. Keep. Add test. |
| Original ≤ 1920px, q=90-95 WebP | MISSING | Currently writes 800×600 cropped as the "main" file. Need second pipeline branch: long-edge resize to 1920, no crop, WebP q=92. |
| Thumb 400×400 1:1 center-crop | MISSING | Currently 800×600 (used as both thumb + main). Need separate 400×400 file `ZONE_<id>_thumb.webp`. |
| List shows thumb instead of icon | DONE | `status.js:1797` already swaps. Need to point `<img>` at the thumb URL (new endpoint or query param). |
| Click thumb opens lightbox ~3/4 screen | PARTIAL | Modal exists. Sized `min(92vw, 720px)`. Spec says ~3/4. Cap at `75vw`/`75vh`, change CSS only. |
| Lightbox close: ✕, outside click, Esc | PARTIAL | `zones.js` has outside-click; `status.js` does NOT. Neither has Esc. Add ✕ button (currently only "Закрыть" text button). Unify. |
| Mobile + desktop work | PARTIAL | Mobile OK. Need explicit Esc-key wiring for desktop. |
| Aspect ratio in lightbox | DONE | `object-fit:contain`. Keep. |
| Delete + rotate don't regress | RISK | Rotate currently rewrites only one file. Must rotate BOTH (original + thumb). Delete must remove BOTH. |
| Path traversal stays safe (SEC-009) | RISK | Adding `_thumb` suffix breaks current regex `^ZONE_\d+\.(ext)$`. Must extend regex to also accept `ZONE_<id>_thumb.<ext>`. |

HEIC: leave OUT of scope for v1 (explicitly call out as deferred — adding `pillow-heif` is a Pillow plugin install, not invasive, but adds a binary dep on libheif. Recommend: add only if Raul confirms iPhone users actually upload .HEIC instead of letting iOS auto-convert to JPEG on share).

---

## 3. Design (gaps only)

### 3.1 DB schema change

Add column `photo_thumb TEXT` to `zones`. New named migration in `db/migrations.py`:

```python
self._apply_named_migration(conn, 'zones_add_photo_thumb', self._migrate_add_photo_thumb)

def _migrate_add_photo_thumb(self, conn):
    cur = conn.execute("PRAGMA table_info(zones)")
    cols = [c[1] for c in cur.fetchall()]
    if 'photo_thumb' not in cols:
        conn.execute('ALTER TABLE zones ADD COLUMN photo_thumb TEXT')
        conn.commit()
```

Idempotent. Backfill (see §6) is a separate one-shot.

### 3.2 Service: split helper into two functions, keep backwards-compat

`routes/zones_photo_api.py` (or move to `services/image.py` if preferred — decision: keep where it is, do NOT introduce a new file just for two helpers).

Replace single-call `normalize_image(...)` invocation in `upload_zone_photo` with two calls:

```python
# After EXIF read once, produce two outputs from the SAME PIL image.
def render_two_variants(image_bytes) -> tuple[bytes, bytes]:
    """Return (resized_webp, thumb_webp). Long edge ≤ 1920 / 400x400 center-crop."""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode in ('RGBA','LA','P'):
        img = img.convert('RGB')

    # Resized original: long edge ≤ 1920, preserve aspect.
    w, h = img.size
    if max(w, h) > 1920:
        scale = 1920 / float(max(w, h))
        resized = img.resize((int(w*scale), int(h*scale)), Image.Resampling.LANCZOS)
    else:
        resized = img.copy()
    out_main = io.BytesIO()
    resized.save(out_main, format='WEBP', quality=92, method=6)

    # Thumb: 400x400 center-crop (no stretching).
    tw = th = 400
    rw, rh = img.size
    scale = max(tw / rw, th / rh)
    sized = img.resize((int(rw*scale), int(rh*scale)), Image.Resampling.LANCZOS)
    left = max(0, (sized.size[0] - tw) // 2)
    top  = max(0, (sized.size[1] - th) // 2)
    cropped = sized.crop((left, top, left+tw, top+th))
    out_thumb = io.BytesIO()
    cropped.save(out_thumb, format='WEBP', quality=90, method=6)

    return out_main.getvalue(), out_thumb.getvalue()
```

Diff for upload handler (sketch):

```python
# routes/zones_photo_api.py upload_zone_photo()
out_main, out_thumb = render_two_variants(file_data)
main_name  = f"ZONE_{zone_id}.webp"
thumb_name = f"ZONE_{zone_id}_thumb.webp"
write(UPLOAD_FOLDER / main_name,  out_main)
write(UPLOAD_FOLDER / thumb_name, out_thumb)
db.update_zone_photo(zone_id, f"media/zones/{main_name}", thumb_path=f"media/zones/{thumb_name}")
```

`db.update_zone_photo` extended to accept optional `thumb_path` arg → updates both columns in a single UPDATE.

Keep existing `normalize_image()` function in place — it is still used in the test path (`is_testing` branch) and is referenced in audit. Don't delete (Karpathy: surgical changes).

### 3.3 Constants + Flask config

`services/helpers.py`:
```python
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB (issue #11)
```

`app.py:111`:
```python
app.config['MAX_CONTENT_LENGTH'] = 22 * 1024 * 1024  # 22MB envelope (route-level enforces 20MB payload)
```

22 MB chosen, not 21 — multipart boundary + filename headers add ~hundreds of bytes; 1 MB margin is safe and trivial.

### 3.4 Path-traversal whitelist regex

`services/helpers.py:26`:
```python
_ZONE_PHOTO_FILENAME_RE = re.compile(
    r'^ZONE_\d+(_thumb)?\.(png|jpg|jpeg|gif|webp)$',
    re.IGNORECASE,
)
```

This is the ONLY change to SEC-009. Existing tests in `tests/unit/test_photo_path_traversal.py` continue to pass (they use `ZONE_5.webp`); add one new positive test for `ZONE_5_thumb.webp` and a negative test for `ZONE_5_thumbb.webp` (typo) and `ZONE_5_thumb_evil.webp`.

### 3.5 New endpoint for thumb fetch

Two options:

**Option A (preferred, minimal):** Reuse `GET /api/zones/<id>/photo` with query param `?variant=thumb`. Default = main.
- Frontend list passes `?variant=thumb`, lightbox uses default.
- Single endpoint, single set of validations.

**Option B (rejected):** Separate `/api/zones/<id>/photo/thumb`. More handlers, more SEC reviews, no benefit.

Diff sketch for `get_zone_photo`:
```python
variant = request.args.get('variant', 'main')
if variant == 'thumb':
    photo_path = zone.get('photo_thumb') or zone.get('photo_path')  # fallback for legacy zones
else:
    photo_path = zone.get('photo_path')
```

Backwards compat: if `photo_thumb` is NULL (existing zone uploaded before this change), fall back to `photo_path`. List will work, just with a slightly larger image — see §6.

### 3.6 Rotate must rotate BOTH files

`rotate_zone_photo()` currently opens one file. Extend to rotate `photo_path` AND `photo_thumb` if present. Both use the same angle, same Pillow flow.

### 3.7 Delete must remove BOTH files

`delete_zone_photo()` currently removes `photo_path`. Extend to also remove `photo_thumb` (validate path with `safe_zone_photo_path`).

### 3.8 Frontend changes

`static/js/status.js:1797` — change thumb URL to:
```js
var _photoUrl = '/api/zones/' + z.id + '/photo?variant=thumb' + (_ts ? '&ts=' + _ts : '');
var _fullUrl  = '/api/zones/' + z.id + '/photo'                + (_ts ? '?ts='   + _ts : '');
// thumb in the list, full image opens in lightbox:
html += '<div class="zc-photo" onclick="event.stopPropagation();showPhotoModal(\'' + _fullUrl + '\')">';
html += '<img src="' + _photoUrl + '" ...>';
```

Same change in `static/js/zones-table.js:76`, `static/js/zones.js:109`, `static/js/status/status-groups.js:297`.

`static/js/zones.js:1611` and `static/js/status.js:1068` — bump client-side check to 20 MB:
```js
if (file.size > 20 * 1024 * 1024) {
    showZoneToast('Файл больше 20 МБ', 'error');
    return;
}
```

### 3.9 Lightbox — minimal CSS+JS upgrade

The modal already exists. Surgical changes only.

CSS (`static/css/status.css:1097` and mirrored block in `static/css/zones.css:345`):
```css
.photo-modal-content {
    /* ~3/4 of screen */
    max-width: 75vw;
    max-height: 75vh;
    /* keep existing flex/gap rules */
}
.photo-modal img {
    max-width: 75vw;
    max-height: 75vh;
    /* object-fit:contain already present */
}
/* New ✕ button */
.photo-modal-close {
    position: absolute;
    top: 12px; right: 12px;
    width: 36px; height: 36px;
    border-radius: 50%;
    background: rgba(0,0,0,0.5);
    color: #fff;
    font-size: 20px;
    border: none; cursor: pointer;
}
```

HTML (`templates/status.html`, `templates/zones.html`):
- Add `<button class="photo-modal-close" onclick="closePhotoModal()" aria-label="Закрыть">✕</button>` inside `.photo-modal`.
- Keep existing "Закрыть" text button (or remove — Raul preference; default: keep, costs 0).

JS — add Esc + outside-click handlers to BOTH pages. Single shared snippet at file end:
```js
(function () {
    var modal = document.getElementById('photoModal');
    if (!modal) return;
    // outside click
    modal.addEventListener('click', function(e) {
        if (e.target === modal) closePhotoModal();
    });
    // Esc
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && modal.style.display === 'flex') {
            closePhotoModal();
        }
    });
})();
```

`zones.js` already has outside-click (line 1693) — leave alone. Just add Esc in same IIFE. `status.js` — add full snippet.

No JS framework needed. Total lightbox JS additions: ~10 lines per page.

---

## 4. Test plan (6 tests)

Aim: cover every NEW behavior + the regression risks. Skip exhaustive edge cases.

| # | File | Test name | Assertion |
|---|------|-----------|-----------|
| 1 | `tests/api/test_zones_photo_two_variants.py` (NEW) | `test_upload_produces_main_and_thumb` | After POST 1024×768 PNG → both `static/media/zones/ZONE_<id>.webp` and `ZONE_<id>_thumb.webp` exist; main long-edge ≤ 1920 (here unchanged), thumb is exactly 400×400; DB row has both `photo_path` and `photo_thumb` set. |
| 2 | same | `test_upload_rejects_over_20mb` | POST 21 MB JPEG → 400 with `error_code=FILE_TOO_LARGE` (or string match) and message mentioning 20. POST 5 MB → 200 success. |
| 3 | same | `test_upload_honors_exif_orientation` | Build a 100×200 JPEG with EXIF orientation=6 (rotate-90). Upload. Open the resulting thumb with PIL — width should equal height (square 400×400) and the tall side of the original should now be on top (assert one corner pixel matches original top-row color). |
| 4 | same | `test_thumb_endpoint_returns_400x400` | GET `/api/zones/<id>/photo?variant=thumb` — `Image.open(io.BytesIO(resp.data)).size == (400, 400)`. GET without variant returns the larger main file. |
| 5 | same | `test_delete_removes_both_files` and `test_rotate_rotates_both_files` (1 test fn, 2 assert blocks acceptable, OR keep as 2 tests — go with 2 to keep "1 test = 1 behavior") | After DELETE: both files gone, both DB columns NULL. After POST rotate angle=90: both files have new dimensions (h, w swapped). |
| 6 | `tests/unit/test_photo_path_traversal.py` (EXTEND existing) | `test_thumb_filename_accepted` and `test_evil_thumb_suffix_rejected` | `safe_zone_photo_path('media/zones/ZONE_5_thumb.webp')` returns OK. `ZONE_5_thumb_evil.webp` and `ZONE_5_thumbb.webp` raise `UnsafePathError`. |

Test 5 may be split into two functions if test 4 is split — total stays at 6 logical behaviors / ≤ 8 functions.

NO frontend e2e tests for lightbox in this issue. Manually verify on iPhone Safari + Chrome desktop. Reasoning: existing modal is JS-light, framework-free, and Selenium/Playwright is not yet wired in this repo for status.html flows. Cost > value.

---

## 5. Risks

1. **Race condition: upload + concurrent read.** Currently writes the main file atomically (single `open(...).write()`). Adding a second file (thumb) means there's a window where main is updated but thumb is stale. Fix: write to `.tmp` siblings then `os.replace()`. Cheap and removes the gotcha. Apply to both files.
2. **Old file leak.** `OLD/` archive dir already exists for the main file. Decision: also archive old thumb (use `ZONE_<id>_thumb.<ext>` → `OLD/`). Otherwise `OLD/` only contains main, weird.
3. **Pillow OOM on huge inputs.** A 20 MB file can be a 50 megapixel image; Pillow decompression bomb. Pillow has `Image.MAX_IMAGE_PIXELS` (default ~178 MP) which raises a warning. Add explicit guard: reject `img.width * img.height > 50_000_000` with a clear error before resize. Cheap, prevents OOM on the WB controller (low-memory).
4. **EXIF strip.** `ImageOps.exif_transpose` returns image without EXIF metadata in the output WebP — that's correct (no GPS leak from photo metadata). Don't accidentally re-add EXIF on save.
5. **SEC-009 regression from regex change.** Adding `(_thumb)?` widens the regex. Mitigation: explicit positive+negative tests (test 6).
6. **MAX_CONTENT_LENGTH bump from 10→22 MB.** This applies to ALL endpoints, not just photo upload. Other JSON endpoints could now accept 22 MB. Mitigation: route-level checks already enforce smaller payloads where relevant; document in app.py comment that the global cap is for photo upload, route-level still enforces tighter limits.
7. **Path traversal via `?variant=`.** Don't pass variant into filesystem paths directly. Only branch in code, never concatenate into a path string. Already designed that way in §3.5.
8. **HEIC explicitly out of scope.** If Raul wants it later: `pip install pillow-heif`, register opener at app boot. ~5 lines. Not in this issue.

---

## 6. Migration / backwards compat

Existing zones that have `photo_path` set but no `photo_thumb`:

**Decision: lazy backfill, NOT batch.**

- The new `GET /photo?variant=thumb` falls back to `photo_path` when `photo_thumb` is NULL (§3.5). So legacy zones keep working immediately — they just serve the bigger file as a list thumb (small visual penalty).
- On the next user-initiated upload OR rotate of that zone, both files get regenerated → `photo_thumb` is populated. That covers the active set quickly.
- For zones whose photo is never re-uploaded, leave them. The fallback is acceptable — list still shows a thumb (just larger), the lightbox still works.

**Rejected: write a one-shot Python script that re-renders thumbs for all existing zones.**
- Reasons: extra code path, requires reading source images that have already been re-encoded once (lossy → lossy → lossy), and the user-visible cost of the fallback is one slightly-bigger image in a list. Not worth the ETL.

If Raul disagrees and wants eager backfill: trivial 30-line one-shot in `scripts/backfill_zone_thumbs.py` — iterate `zones` where `photo_path IS NOT NULL AND photo_thumb IS NULL`, re-render thumb only (keep main as-is). Easy to add later.

Schema migration is forward-only (column add). No downgrade.

---

## 7. Out of scope (explicit)

- HEIC decoding (deferred — add `pillow-heif` if user evidence shows iPhone users upload `.heic` directly).
- Multiple photos per zone (issue says one photo).
- Photo crop UI / aspect-picker (auto center-crop is fine per AC).
- Focus trap / a11y improvements in lightbox (audit findings flagged this in `irrigation-audit/findings/a11y.md` but it's a separate ticket).
- Rate limiting on upload endpoint (already covered by Flask-Limiter? — verify; not in this issue).

---

## 8. Implementation order for the senior

1. DB migration `zones_add_photo_thumb` + `db.update_zone_photo` accepts thumb. → Run unit migration test.
2. `services/helpers.py`: bump `MAX_FILE_SIZE` to 20 MB; extend filename regex.
3. `app.py`: bump `MAX_CONTENT_LENGTH` to 22 MB.
4. `routes/zones_photo_api.py`: `render_two_variants`, write both files atomically, archive both old files, extend rotate + delete + GET `?variant=thumb`. → Run new API tests (tests 1-5).
5. `tests/unit/test_photo_path_traversal.py`: extend with thumb-suffix tests (test 6).
6. Frontend: bump JS 5 MB → 20 MB; thumb URL → `?variant=thumb`; CSS `75vw/75vh`; add `✕` button + Esc handler.
7. Manual smoke: iPhone Safari upload (HEIC will fail with "Неподдерживаемый формат" — expected), JPEG with rotated EXIF, lightbox close via 3 paths.

End.
