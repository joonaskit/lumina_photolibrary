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
- `thumbs.py`: WebP thumbnails, cache keyed by content id, invalidated on edit.
- `db.py`: schema and migrations (photos, edits, keywords, jobs).
- `scanner.py`: incremental library walk, skip unchanged (mtime+size, hash on change), handle missing volumes by marking, not deleting.
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

**Phase 0: scaffold.** `pyproject.toml`, `src/lumina/` layout, `CLAUDE.md`, ruff, pytest, CI, layer test, rewrite `README.md`. Resolve the PySide6 source decision. Done except CI.

**Proof of concept (done).** A thin slice through every layer, ahead of Phases 1 and 2: open a folder, scan it on a thread (`core/scanner.py`), a lazy thumbnail grid (`qt_models/photo_model.py`, decoded in worker processes by `core/images.py`, bounded in-memory cache), a viewer with arrow-key stepping (`qt_models/previews.py`, newest request wins), the filehouse theme. RAW goes through `core/raw.py` (embedded preview, LibRaw flip, half-size fallback). No database, no disk cache, no metadata. Known gaps: not yet tried on real CR3 files; thumbnail requests for rows scrolled past are not cancelled; no `.desktop` file or app icon.

**Phase 1: CR3 ingest, no UI.** `raw.py`, `metadata.py`, `thumbs.py`, tests with small sample CR3, JPEG, a RAW with no JPEG preview, and a corrupt file. Proves CR3 handling first.

**Phase 2: library.** `db.py`, `scanner.py`, persistent job queue with progress, grid view (`QListView` IconMode, lazy thumbnails), basic filters (date, camera, lens), single-photo viewer. Watch folders, handle unplugged drives.

**Phase 3: Develop view.** Edit params and storage, linear decode and cache, proxy renderer, curve widget (draggable points, histogram backdrop), matte control, exposure/WB, undo/redo, presets, export, thumbnail invalidation on edit.

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
