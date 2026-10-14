# Lumina plan

A self-hosted desktop photo library with semantic search and a small non-destructive editor, for large libraries (1 TB+) including Canon `.CR3` RAW files. Everything runs locally; nothing is uploaded.

This is the source of truth for design, phases and open decisions. Update it when a decision changes.

## Decisions

| Area | Decision |
|---|---|
| Form | Desktop app, one Python process (plus worker processes). No HTTP server. |
| UI | PySide6, Qt Widgets (same as `../filehouse`). |
| Language | Python 3.12+. No Rust or custom GPU code for now. |
| Metadata and edits | SQLite. No PostgreSQL. |
| Vectors | Embedded store: `sqlite-vec` or Qdrant local mode (`QdrantClient(path=...)`). Chosen in Phase 4. No server. |
| RAW | `rawpy` (LibRaw): embedded JPEG for indexing, full linear decode for editing. |
| EXIF | `exiftool`, long-running `-stay_open` process. |
| Embeddings | CLIP/SigLIP through ONNX Runtime (CPU by default). Weights downloaded on first run, not bundled. |
| Editing | Non-destructive: edit parameters in SQLite, rendered from the original. |
| Layout and tooling | Same as filehouse: `src/` layout, setuptools, ruff, pytest, pytest-qt offscreen, layer-rules test. |
| PySide6 source | PyPI `PySide6-Essentials` in a plain venv (Widgets without WebEngine). Tested on Python 3.14 with PySide6 6.12, rawpy 0.27.1 (LibRaw 0.22.1). |
| Theme | Same as filehouse: `integration/theme.py` reads Quickshell Island's `theme.json` over the same built-in defaults, `integration/style.py` maps it to the palette, font and a shapes-only stylesheet, `integration/theme_watcher.py` follows changes live. The two apps look alike with the shell running or not. |
| Worker processes | `concurrent.futures.ProcessPoolExecutor` with the `forkserver` context (not `fork`: Qt's threads). The forkserver re-imports the main module, so every entry point has an `if __name__ == "__main__"` guard. |

## Open decisions

- **Vector store**: `sqlite-vec` (one file, one dependency) vs Qdrant local mode. Decide with a benchmark of ~200k vectors in Phase 4.
- **CLIP vs SigLIP** and which ONNX export to use.
- **Platforms.** Linux first (Fedora/KDE). Windows and macOS only if the app is distributed, which also brings signing and installers.

## Architecture

```
ui/  →  qt_models/  →  core/
          integration/ (theme, desktop hand-off)
```

- `core/` imports no Qt. Plain Python, numpy, rawpy, sqlite3, onnxruntime. Reports through callbacks.
- `qt_models/` is the only place that runs core work on threads or processes and turns callbacks into queued Qt signals.
- `ui/` never touches the DB, rawpy or exiftool directly.
- `tests/test_layers.py` enforces the import rules.

Planned `core/` modules:

- `raw.py`: `load_preview(path)` (embedded JPEG, EXIF orientation applied; falls back to `postprocess(half_size=True)` when there is no JPEG) and `load_linear(path)` (16-bit linear, for editing).
- `metadata.py`: batched exiftool wrapper: capture time, GPS, camera, lens, orientation.
- `fingerprint.py`: content id (size plus the first and last 64 KiB), the same for a moved or renamed file.
- `thumbs.py`: WebP thumbnails, cache keyed by content id, invalidated on edit.
- `db.py`: schema and migrations (photos, edits, keywords, jobs).
- `scanner.py`: the walk: photo files under a folder, unreadable folders reported.
- `volumes.py`: volume id and path from the filesystem root, from `/proc/self/mountinfo`.
- `library.py`: the scan job: incremental (mtime+size, content id on change), missing files and volumes marked, not deleted.
- `embed.py`: CLIP encoder, batching, text and image.
- `search.py`: text query, filters, ranking.
- `edit/params.py`: versioned edit parameters (JSON) and migrations.
- `edit/curves.py`: monotone spline (PCHIP) to 1D LUT.
- `edit/pipeline.py`: render(linear image, params, size) for proxy and full passes.
- `export.py`: JPEG/TIFF/PNG output with colour profile and resize.

## RAW and CR3

- LibRaw reads CR3 since 0.20. Newer bodies (R5 II, R1) need a recent LibRaw: pin a recent `rawpy` and check the bundled LibRaw version.
- `extract_thumb()` may return a JPEG or a bitmap. Check `thumb.format`; fall back to a half-size decode.
- CR3 has a full-size preview JPEG (`PRVW`) and a small one (`THMB`). Use the larger one for indexing.
- C-RAW and HEIF previews on newer bodies: test with real files from the bodies the user owns.
- rawpy does not expose EXIF. Orientation comes from the EXIF tag or `raw.sizes.flip`.
- Canon lens corrections and picture-style profiles are not in LibRaw: `lensfun` and DCP/ICC are later work.
- Originals are never written to.

## Editing

Scope (first version): exposure, white balance, **tone curve (RGB and per channel)**, **matte look**, crop/rotate, export. HSL, split toning, vignette and grain follow. Out of scope for now: local masks, healing, denoise, AI masks, lens corrections.

Model:

- An edit is a small JSON parameter set with a schema version, stored per photo. Rendered image = `render(original, params)`.
- Undo/redo is a stack of parameter states. Presets are saved parameter subsets. Virtual copies are extra edit rows.
- Sidecar XMP export uses our own namespace, no Adobe `crs:` compatibility promise.

Pipeline:

- Decode once to linear 16-bit with rawpy (`output_bps=16`, `gamma=(1,1)`, `no_auto_bright=True`), cache it. Work in linear float32, convert to display space last.
- Order: white balance → exposure → curves/matte → colour → geometry → output transform.
- Curves are 1D LUTs built from monotone splines, applied with `np.take` or `np.interp`.
- Matte look = raised black point, optionally lowered white point, optional shadow desaturation, expressed as part of the curve so it stays one editable thing.
- Proxy render (~2 MP) while a control moves, debounced, on a worker thread, cancelled when a newer request arrives. Full-resolution render on release and at 100% zoom.
- Memory: a 45 MP linear float32 image is about 540 MB. Keep full-resolution renders tiled or 16-bit and the proxy in float32.
- If NumPy is too slow: `QOpenGLWidget` with a GLSL shader (curve as a 1D texture), still inside PySide6.

Display colour management (monitor ICC) and output profiles (sRGB, Display P3, Adobe RGB) via `lcms2`/Pillow `ImageCms` in a later phase.

## Phases

**Phase 0: scaffold.** `pyproject.toml`, `src/lumina/` layout, `CLAUDE.md`, ruff, pytest, CI, layer test, rewrite `README.md`. Resolve the PySide6 source decision. Done.

**Proof of concept (done).** A thin slice through every layer, ahead of Phases 1 and 2: open a folder, scan it on a thread (`core/scanner.py`), a lazy thumbnail grid (`qt_models/photo_model.py`, decoded in worker processes by `core/images.py`, bounded in-memory cache), a viewer with arrow-key stepping (`qt_models/previews.py`, newest request wins), the filehouse theme. RAW goes through `core/raw.py` (embedded preview, LibRaw flip, half-size fallback). No database, no disk cache, no metadata. Known gaps: not yet tried on real CR3 files; thumbnail requests for rows scrolled past are not cancelled; no `.desktop` file or app icon.

**Phase 1: CR3 ingest, no UI.** `raw.py`, `metadata.py`, `thumbs.py`, tests with small sample CR3, JPEG, a RAW with no JPEG preview, and a corrupt file. Proves CR3 handling first. Built, except the check against real CR3 files:

- **RAW samples.** Real CR3s are 20 to 50 MB, so they are not in git: `tests/core/test_samples.py` reads them from the git-ignored `tests/samples/` (or `$LUMINA_SAMPLES`) and skips without them. It checks that the full-size preview is used (not the 160 x 120 one), that it is upright, and that the files are left as they were. Everywhere else, including CI, RAW handling is tested with tiny DNG files the tests write (`tests/rawfiles.py`): JPEG preview, bitmap preview, no preview (half-size decode), turned, cut short. LibRaw reads them through the same paths as CR3.
- **Metadata.** One `exiftool -stay_open` process fed batches through stdin, `-json -n`, only reading. Paths are passed absolute (a name starting with `-` is never an option) and as `#[CSTR]` lines when they have a newline or leading or trailing space. A missing or unreadable file is a `PhotoError` naming it; a file exiftool reads as something else (`FileType` "TXT") or reads past a fault in (`Warning`) is not an error here: telling a bad photo apart is the decoder's job. A process that dies is started again; one that hangs is killed after a timeout.
- **Thumbnail cache.** `$XDG_CACHE_HOME/lumina/thumbnails/v1/<id[:2]>/<id>-<side>.webp`, written to a temporary file and renamed, never next to a photo. A damaged file is removed and made again; a cache that cannot be written still gives thumbnails. About 10 KiB a thumbnail at 320 px; a hit is about 1 ms against 18 ms to decode a 1 MB JPEG. No size limit or pruning yet.
- **CI** runs the tests in a `fedora:44` container with Fedora's exiftool, as an ordinary user, and fails rather than skips a test that needs exiftool (`LUMINA_REQUIRE_EXIFTOOL=1`).

**Phase 2: library.** `db.py`, `scanner.py`, persistent job queue with progress, grid view (`QListView` IconMode, lazy thumbnails), basic filters (date, camera, lens), single-photo viewer. Watch folders, handle unplugged drives.

**Phase 2 as built.**

- **Photo identity.** A volume id and the path from that filesystem's root (`core/volumes.py`, from `/proc/self/mountinfo` and `/dev/disk/by-uuid`). The id is `uuid:<filesystem UUID>`, or `mount:<mount point>` for filesystems without one (tmpfs, most network mounts). The path includes mountinfo's root field, so btrfs subvolumes sharing one UUID (`/` and `/home` on Fedora) do not collide. A drive mounted somewhere else keeps its photos. Linux only.
- **Database** (`core/db.py`): `$XDG_DATA_HOME/lumina/library.sqlite3`, WAL, one connection per thread. Schema version in `PRAGMA user_version`, one migration per version, append only; a file from a newer Lumina is refused before anything touches it. Tables: volumes, folders (the library's roots), photos (file state, availability, content id, metadata), jobs, job_failures. Edits and keywords come with the phases that need them, as migrations.
- **Folders.** Adding a folder inside one already in the library adds nothing; adding one around others merges them. Removing a folder from the library forgets its photos after asking; the files are not touched. Names that are not valid UTF-8 are reported as failures and skipped.
- **Scan job** (`core/library.py`): find (new, changed by size or mtime, missing), then details (content id and exiftool metadata in batches of 50). Missing photos are marked unavailable with everything known kept, never deleted; nothing is marked missing after a cancel or below a folder that could not be read; a folder gone from a connected drive fails the job and marks its photos unavailable. Every failure is a row naming the file. Without exiftool the photos are listed and their details read by a later scan.
- **Jobs** (`qt_models/library.py`): one at a time, on a thread. At the start every connected folder is queued for a rescan and unfinished jobs (a crash) are run again; a quit in the middle leaves the job queued. A job whose drive is not connected waits for it. The mounts are read every 3 s; a drive plugged in gets its folders rescanned. Cancel stops the running job and drops the queued ones.
- **Watching.** No live file watching yet: changes are picked up at the start, when a drive is plugged in, and by Rescan. inotify needs a watch per directory and does not see changes made while the app is closed or on unplugged drives, so the scan-on-start is needed anyway; live watching can come later.
- **UI.** Sidebar with All Photos and the folders (a not-connected one is dimmed, with its drive's label in the tooltip); year, camera and lens filters; the grid, read on a thread and refreshed every 2 s while a scan writes, keeping its place; the viewer, with capture time, camera, lens and exposure in the status bar; the job's progress and Cancel in the status bar, failures listed in its tooltip. Photos on an unplugged drive keep their thumbnails, from the disk cache by content id.
- **Not yet:** tracked in issue #5: a size limit for the thumbnail cache, cancelling thumbnails for rows scrolled past, throttling very large first imports, live watching, keeping the grid's place exactly, an orientation cross-check, a `.desktop` file.

**Phase 3: Develop view.** Edit params and storage, linear decode and cache, proxy renderer, curve widget (draggable points, histogram backdrop), matte control, exposure/WB, undo/redo, presets, export, thumbnail invalidation on edit.

**Phase 3a as built: the engine, no UI** (`core/edit/`).

- **Params** (`params.py`): frozen dataclasses with `to_json`/`from_json`, schema `version` 1, a migration table (`_MIGRATIONS`, a test checks every version has its step), a newer document refused (`NewerParams`), bad values clamped or defaulted field by field. Fields: exposure (stops), white balance temperature and tint (-100 to 100), curves (RGB and per channel), matte (black, white, shadow desaturation), quarter turns, crop. `fingerprint()` for caches of rendered images. Presets are documents with some of the sections, applied over another edit.
- **Curves** (`curves.py`): Fritsch-Carlson PCHIP in numpy (no scipy), on sRGB-encoded values.
- **Pipeline** (`pipeline.py`): geometry first (every later step is per pixel, so cropping first gives the same image for less work), then one gain per channel (white balance: the white point moved along the black body curve, in mireds from D65, luminance kept; tint on green; exposure), then one tone LUT per channel (encode, RGB curve, channel curve, matte range, decode), then shadow desaturation, then sRGB out. 8-bit output through LUTs, 16-bit through interpolated tables (max error 0.03 of a 16-bit code). Full-size renders in tiles of 512 rows.
- **Source** (`source.py`): RAW decoded once to linear 16-bit sRGB primaries (camera white balance, `no_auto_bright`), other files decoded from sRGB; a ~2 MP float proxy resampled in linear light.
- **Export** (`export.py`): JPEG, PNG 8/16, TIFF 8/16, all with an sRGB ICC profile; 16-bit PNG and TIFF writers of our own (Pillow can't write 16-bit colour). Resizing to a longest side applies after the crop. Written beside the destination, then placed by an exclusive create, so a file that appears meanwhile is never overwritten; the original is refused even with Replace; a cancel or failure leaves nothing.
- **Storage**: database schema 2 adds `edits` (photo, copy number, params JSON; copy 0 is the photo's own edit, more are virtual copies) and `presets`. Edits go with their photo when its folder is removed from the library, and stay when the file goes missing.
- **Speed**, 32 MP CR3 from a 90D: decode 0.75 s, proxy 0.4 s, proxy render 25 to 70 ms (fast enough to drag controls; no GPU path needed for now), full 8-bit render 0.85 s, full 16-bit 2.4 s; exports: JPEG 2.0 s, TIFF 16 2.4 s, PNG 16 5.1 s (zlib). Peak memory about 670 MB.
- **Decided: a RAW starts plain.** A new edit has no default settings: with no edit, a RAW is the plain linear decode in sRGB, about 1 EV darker than the camera's JPEG (measured on three 90D files: 0.93 to 1.04 EV), since the camera adds its own tone curve. Exposure and the curve are the user's to set. The grid shows the camera's embedded JPEG until a photo has an edit, so a RAW looks brighter in the grid than when it is first opened in the Develop view; the Develop view should not hide that by adjusting anything.
- **Later:** HSL and the rest of Phase 5. (Straightening and mirroring came with 3b, schema 2.)

**Phase 3b as built: the Develop view.**

- **Render process** (`core/edit/session.py`, `workers.DEVELOP`): one worker process of its own keeps the open photo decoded, its proxy, and a screen-sized copy, and holds one photo at a time. Thumbnails never queue in front of a render.
- **Session** (`qt_models/develop.py`): at most one render in flight; while it runs, a newer request replaces the waiting one, so the image follows a dragged control and never falls behind. The proxy while a control moves, the screen-sized render when it stops. An edit is saved when a change is committed (a control let go, a point dropped, a preset, undo), not on every move; an edit back at no change is removed, so the photo is its original again. Undo and redo are a stack of committed states, per opening of a photo. An edit from a newer Lumina is not opened, so never saved over.
- **View** (`ui/develop_view.py`): the photo, and a card of controls: exposure; temperature and tint; the curve (RGB, R, G, B) over the histogram of the current render; matte; rotate left and right, flip horizontal and vertical, straighten (-45 to 45 degrees); crop on the photo itself, with the whole photo shown and dimmed outside the frame, thirds, and handles at corners and edges; presets (apply, save the white balance, curve and matte of this edit, delete). Toolbar while editing: Undo, Redo, Reset, Export. E edits the photo chosen in the grid or shown in the viewer; in the crop, C or Enter accepts and Escape cancels (back to the crop and straightening it had when opened, as one undoable step); C opens the crop; Escape otherwise leaves the view; the arrows step to the previous or next photo, the edit saved as it goes.
- **Controls** (`ui/controls.py`, `ui/curve_editor.py`): a slider previews while dragged and commits when let go, or 400 ms after a key or the wheel; a double click resets. Curve: click adds a point on the curve, drag moves it between its neighbours, double click, right click or dragging it out of the square removes it; the end points move only up and down.
- **Export** (`ui/export_dialog.py`): format, depth (what the format can hold), JPEG quality, full size or a long side, folder and name, remembered. The name is never the original's own; an existing file is replaced only after asking, also when it appears while the export is written.
- **Edits elsewhere:** the grid's thumbnail and the viewer show the edit, rendered from a half-size decode (`core/edit/preview.py`). An edited thumbnail is cached under the content id and the edit's fingerprint, so a new edit is a new thumbnail; a photo on an unplugged drive shows its last edited thumbnail if there is one.
- **Fixed on the way:** selected thumbnails were tinted with the highlight colour by the style (they now look the same selected); sliders around zero no longer fill from the left.
- **Geometry, schema 2.** Edit parameters gained `flip` (horizontal, vertical) and `straighten` (degrees, clockwise), with the first real migration (1 to 2: none of either, so old edits render as before; a migration only fills in missing fields). Order: quarter turns, mirroring, straightening, crop. Straightening turns about the centre and scales up just enough to fill the frame (no empty corners; Lightroom's "constrain to image"), and samples bilinearly only the pixels inside the crop, by flat index in blocks of 256 rows. `Params.turned()` and `flipped()` give the edit that shows the result turned or mirrored, crop and all (turning swaps the two flips; mirroring negates the angle); tests check them pixel for pixel against turning and mirroring the render. Cost on a 32 MP CR3: proxy render about 100 ms with straightening (25 without), full 16-bit 2.8 s.
- **Not yet:** virtual copies in the UI (the database has them), zoom to 100 %, crop aspect ratios, a fine grid while straightening, choosing which sections a preset keeps, cancelling an export in progress.

**Phase 4: semantic search.** First-run model download, ONNX CLIP, batch embedding job, vector store choice, text search UI, "similar photos".

**Phase 5: later.** HSL and colour grading, crop tools, lens corrections, GPU rendering, soft proof, packaging (AppImage/Flatpak), keywords and collections, ratings.

## Risks

- Python render speed on 24–45 MP images: mitigated by proxy rendering and cached stages, GPU shader as fallback.
- First index of 1 TB takes hours: the job queue must be resumable and throttled.
- rawpy/LibRaw lagging behind new Canon bodies.
- Wheels (`rawpy`, `onnxruntime`) for the chosen Python version.
- Model size and licence if the app is ever distributed.

## Safety rules

- Never write to, rename or delete original photos.
- Edits and metadata live in the Lumina database (and optional sidecars), never in the originals.
- A missing file or volume marks the record unavailable. It never deletes the record or its edits.
- Export never overwrites an existing file unless the user chose Replace.
