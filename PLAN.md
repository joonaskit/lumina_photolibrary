# Lumina plan

A self-hosted desktop photo library with semantic search and a small non-destructive editor, for large libraries (1 TB+) including Canon `.CR3` RAW files. Everything runs locally; nothing is uploaded.

This is the source of truth for design, current decisions and what comes next. Update it when a decision changes. How each feature was built (measurements, rationale, what was left open) is in [docs/history.md](docs/history.md); it is a record, not kept in step with the code.

## Decisions

| Area | Decision |
|---|---|
| Form | Desktop app, one Python process (plus worker processes). No HTTP server. |
| UI | PySide6, Qt Widgets (same as `../filehouse`). |
| Language | Python 3.12+. No Rust or custom GPU code for now. |
| Metadata and edits | SQLite. No PostgreSQL. |
| Vectors | No vector store: float16 blobs in the library database, searched by numpy in memory (decided in Phase 4 by benchmark). No server. |
| RAW | `rawpy` (LibRaw): embedded JPEG for indexing, full linear decode for editing. |
| EXIF | `exiftool`, long-running `-stay_open` process. |
| Embeddings | SigLIP 2 (`google/siglip2-base-patch16-224`, multilingual, Apache 2.0), fp16 ONNX through ONNX Runtime on the CPU. Weights downloaded once, with consent, not bundled. |
| Editing | Non-destructive: edit parameters in SQLite, rendered from the original. |
| Layout and tooling | Same as filehouse: `src/` layout, setuptools, ruff, pytest, pytest-qt offscreen, layer-rules test. |
| Layer rules | `ui/` shares a small domain model with `core/` and imports it directly: the edit (`core/edit/params.py`), the library's records and constants (`core/dbtypes.py`), keyword paths, smart collections' queries, export options and the errors the UI tells of. Those modules are plain data, importing only the standard library and each other; the rest of `core/` is reached through `qt_models/`. Decided over translating to view types in `qt_models/`, which would duplicate the edit for no gain (#109). |
| PySide6 source | PyPI `PySide6-Essentials` in a plain venv (Widgets without WebEngine). Tested on Python 3.14 with PySide6 6.12, rawpy 0.27.1 (LibRaw 0.22.1). |
| Theme | Same as filehouse: `integration/theme.py` reads Quickshell Island's `theme.json` over the same built-in defaults, `integration/style.py` maps it to the palette, font and a shapes-only stylesheet, `integration/theme_watcher.py` follows changes live. The two apps look alike with the shell running or not. The scheme, accent and corners can be chosen by hand instead (`integration/appearance.py`; [docs/history.md](docs/history.md), *Appearance chosen by hand*). |
| File dialogs | The desktop's, through the file chooser portal (`QT_QPA_PLATFORMTHEME=xdgdesktopportal`, set at start unless the user chose a theme): PyPI's Qt cannot load Plasma's dialogs and would draw its own. Whatever serves the portal (Plasma, or filehouse) gives the dialog, with its places and devices. |
| Worker processes | `concurrent.futures.ProcessPoolExecutor` with the `forkserver` context (not `fork`: Qt's threads). The forkserver re-imports the main module, so every entry point has an `if __name__ == "__main__"` guard. |
| Scheduling | One scheduler (`qt_models/workers.py`) runs all work on lanes, declared in one place: a lane is a pool of processes or threads for one kind of work (decoding, the Develop process, the embed and query processes, merges, the window's short reads, searches, a thread per kind of job and per chore). A lane takes requests by priority (`NOW` the photo looked at, `SOON` what is in view, `LATER` exports and profile fitting), then in order, and a request still waiting can be cancelled. When a worker dies, the requests in flight with it run again one at a time before anything else, so only the one to blame fails. Work is stopped with one `Token` type (a job's has the app's quitting as its parent), and work that runs once at a time and again if asked meanwhile uses `Coalescing`. Scans, indexing, imports, merges and exports are run by one `JobRunner` (`qt_models/jobs.py`) from the `jobs` table, reporting one `JobStatus`; each service only describes its jobs (#110). |

## Open decisions

- **Platforms.** Linux first (Fedora/KDE). Windows and macOS only if the app is distributed, which also brings signing and installers (#37, #38).
- **Sidecar conflicts.** When both the library and a sidecar changed since they last agreed, the newer whole state wins, compared by wall clock, so a machine whose clock is ahead wins (#103). The edits it replaces are kept as virtual copies, and a sidecar with a damaged or newer edit is refused, named. A per-group time, or a change counter with a machine id, would merge instead of replacing.
- **Deleting rejected photos.** Allowed or not, given that originals are read-only (#88).

## Architecture

```
ui/  →  qt_models/  →  core/
          integration/ (theme, desktop hand-off)
```

- `core/` imports no Qt. Plain Python, numpy, rawpy, sqlite3, onnxruntime. Reports through callbacks.
- `qt_models/` is the only place that runs core work on threads or processes and turns callbacks into queued Qt signals.
- `ui/` never touches the DB, rawpy or exiftool directly. From `core/` it imports only the domain model: `edit/params.py`, `dbtypes.py`, `keywords.py`, `smart.py`, `edit/export_options.py` and `errors.py`.
- `tests/test_layers.py` enforces the import rules, and that the domain model imports only the standard library and itself.

`core/` modules (the main ones):

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
- `hdr.py`: HDR merge of brackets (#27), with `linear_tiff.py` for its float TIFF.
- `panorama.py`: panoramas (#28); `derived.py`, what it and `hdr.py` share.

## RAW and CR3

- LibRaw reads CR3 since 0.20. Newer bodies (R5 II, R1) need a recent LibRaw: pin a recent `rawpy` and check the bundled LibRaw version.
- `extract_thumb()` may return a JPEG or a bitmap. Check `thumb.format`; fall back to a half-size decode.
- CR3 has a full-size preview JPEG (`PRVW`) and a small one (`THMB`). Use the larger one for indexing.
- C-RAW and HEIF previews on newer bodies: test with real files from the bodies the user owns.
- rawpy does not expose EXIF. Orientation comes from the EXIF tag or `raw.sizes.flip`.
- Canon lens corrections and picture-style profiles are not in LibRaw. Lens corrections come from lensfun's data (#55, see *Lens corrections* in [docs/history.md](docs/history.md)); the camera's look from camera profiles (DCP) or Lumina's own, or one fitted from the camera's JPEGs (#56, see *Camera profiles* in the same).
- Originals are never written to.

## Editing

Scope (first version): exposure, white balance, **Basic tone (contrast, highlights, shadows, whites, blacks)**, **vibrance and saturation**, **HSL (eight bands) and colour grading**, **texture, clarity and dehaze**, **sharpening and noise reduction**, **vignette and grain**, **tone curve (RGB and per channel)**, **matte look**, crop/rotate, export. Out of scope for now: local masks, healing, AI masks. (Lens corrections came later, #55.)

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

## Where it stands

Built, in the order it was built (details in [docs/history.md](docs/history.md)):

| Area | State |
|---|---|
| Library | Folders, scan job, volumes and unplugged drives, watcher, filters, grid, viewer, stacks, virtual copies, thumbs up/down, ratings, keywords, collections, XMP sidecars, database backups, import from card and camera. |
| Search | SigLIP 2 on the CPU, vectors in SQLite, text search, similar photos. |
| Develop | Exposure, white balance, Basic tone, colour, HSL, grading, curves, matte, presence, sharpening, noise reduction, vignette, grain, lens corrections, camera profiles (DCP, fitted, colour checker), perspective, crop, zoom, copy and paste, presets, export (JPEG, PNG, TIFF). |
| Merges | HDR (#27) and panoramas (#28), untested on many real shots. |
| Camera | Tethered folder, USB tethering, live view with focus (#57 to #59). |
| App | Settings, density, appearance, shortcut overlay and command palette (#76, #77). |

## Next

A code review (#113 and the issues it points to) found that the app grew in breadth faster than in depth. **Robustness comes before new features.** The order, with the reason for each step and where it stands:

1. **Data safety** (done on the `canary` branch, each with its fault test, #112):
   #99 atomic migrations; #102 an unplugged drive or card waits instead of failing photos for good; #101 a file that hangs exiftool is named, not the folder's scan; #100 a worker that dies fails only the file that did it; #104 the small fixes; #103 a sidecar conflict keeps the edits it replaces and a bad edit refuses the sidecar (the merge design waits for its open decision).
2. **A large library stays usable.** Done: #111 (ranges on the indexed time, schema 13, the watcher scans only the directories that changed), #105 (an edit saved updates its row; flags and ratings already did), and the 100,000-photo tests of #112 (`tests/ui/test_scale.py`). #106 (done): removing a folder, stacking bursts and finding RAW files run on threads, and the unfinished jobs are one query. The window's writes (flags, ratings, keywords, collections, edits, copies, pasted settings, stacks, presets) run on one writer thread (`_Writer` in `qt_models/library.py`) in the order queued and report through signals; one that gives something back (a copy's number, a collection's id, a paste's result, the keywords made) gives it to an `on_done` callback, and a refusal (a collection's name taken) to `on_error`. Reads of what they change call `settle()` first, and the grid's query settles on its own thread. Adding keywords the library has already changes the grid's rows at once; only new keywords wait for the writer to make them. A camera session is written on the writer thread too, with its collection in one transaction; the session is running meanwhile and its process starts once it is written. `Library.photos()` gives when each edit was saved (`PhotoRow.edited`), not its JSON: a thumbnail reads the edit when it is to be made. Measured at 200,000 photos (a third edited): the query takes 0.8 s on its thread, 0.56 s of it in SQLite (which lets go of the GIL) and 0.2 s building the rows, and 91 MB (194 MB with the JSON); the window takes the rows in in 0.18 s. A process would send the rows back pickled, to be read on the window's thread, so the query stays on a thread. Scans and Find Similar stay on threads too: measured, they hold up the window's thread 1.5 ms and 11 ms at most. A search ranks with numpy, whose sort lets go of the GIL (8 ms instead of 26 ms at 200,000 photos).
3. **Export as a job (#107)**, done: "Export Selected…" in the grid and Export in the Develop view queue a job (schema 14), one row a file, resumable, cancellable between files, never over an existing file unless Replace, every failure naming its file. The Develop view's export is a job of one file with the name chosen, rendered in the Develop process from the decode held there (another process decodes it only when the photo was closed meanwhile). Presets, names and watermarks (#97) come next on this base.
4. **Structure**, once the above has shown what is needed: #108 (done: every write goes through `Library.transaction()`, which nests with savepoints, and `volume_id()` no longer commits on its own; `Library` is made of one class per area in `core/stores/`, records in `core/dbtypes.py`; `photo()`, `photo_rows()` and `photos_near()` give `Photo` records, `presets()` `Preset`, `choices()` `Choices`; `MainWindow` builds the widgets and actions and connects them to a controller per page and per feature in `ui/controllers/`, which are handed the widgets and services they use and share the `Navigator` (page, photo looked at, photos chosen) instead of the window's `self`; shared constants in `ui/window_common.py`), #109 (done: the shared domain model above; `qt_models/` no longer re-exports core's names, only its services' own options and the few core functions the UI calls), #110 (done: one scheduler with lanes and priorities, the retry from #100 built in, and the job runner shared by scan, indexing, import, merge and export; see *Scheduling* above).
5. **The backlog** (#76 to #98, #60, #62, #49), chosen by what the author needs while using the app. Cheap and visible first: #79 sort, #80 counts, #95 colour labels, #81 clipping warnings, #82 type a value and reset, #96 auto tone.

New features that touch the database, jobs or export wait for step 4 where they can.

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
