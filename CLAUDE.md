# CLAUDE.md

Lumina: a local desktop photo library with semantic search and a small non-destructive editor, built for large libraries including Canon `.CR3` RAW files. [PLAN.md](PLAN.md) is the source of truth for design, phases and open decisions; read the relevant section before implementing anything, and update it when a decision changes.

The sibling project `../filehouse` uses the same stack and conventions; look there for patterns (thread pool, thumbnails, theme, layer test) before inventing new ones.

## Stack

- Python 3.12+, **PySide6 with Qt Widgets** (not Qt Quick/QML)
- **rawpy (LibRaw)** for RAW, **exiftool** (`-stay_open`) for metadata
- **numpy** for the edit pipeline, **onnxruntime** for CLIP/SigLIP
- **SQLite** for metadata and edits; vector store is an embedded one, chosen in Phase 4
- pytest for `core/`, pytest-qt for the few UI tests, ruff for lint and format

## Setup and commands

```sh
python3 -m venv .venv
source .venv/bin/activate
sudo dnf install perl-Image-ExifTool   # metadata; its tests skip without it
pip install -e '.[dev]'     # PySide6-Essentials, rawpy, numpy, Pillow from PyPI

lumina [FOLDER]             # run the app; without a folder it opens the last one
ruff check . && ruff format --check .
pytest                      # all tests
pytest tests/core           # no display needed
pytest tests/ui             # runs offscreen
```

Every push runs `ruff` and `pytest` on GitHub (`.github/workflows/tests.yml`). The tests run in a `fedora:44` container with Fedora's exiftool, with Python 3.12 and 3.14, as an ordinary user; `LUMINA_REQUIRE_EXIFTOOL=1` makes a test that needs exiftool fail there instead of skipping. The checks must pass before a pull request can be merged into `main`.

Real camera files go in the git-ignored `tests/samples/` (or `$LUMINA_SAMPLES`): `tests/core/test_samples.py` runs on them and skips without them. Everything else uses the tiny DNGs `tests/rawfiles.py` writes.

Do not mix a PyPI PySide6 with the Fedora `python3-pyside6` in one environment.

## Layer rules

```
ui/  →  qt_models/  →  core/
          integration/ (theme, desktop hand-off)
```

- `core/` imports **no Qt**. Plain Python, numpy, rawpy, sqlite3, onnxruntime. It reports through callbacks.
- `qt_models/` is the only place that runs core work on threads or processes and turns callbacks into queued Qt signals.
- `ui/` never calls the DB, rawpy, exiftool or onnxruntime directly.
- `tests/test_layers.py` enforces these import rules.

## Safety rules

These are requirements, not guidelines. Any change touching files, the DB or export needs a test that covers them.

1. **Originals are read-only.** Never write to, rename, move or delete a user's photo.
2. Edits, ratings and keywords live in the Lumina database (and optional sidecars in our own XMP namespace), never in the original file.
3. A missing file or unplugged volume marks the record unavailable. It never deletes the record or its edits.
4. Export never overwrites an existing file unless the user chose Replace. Handle `EXISTS` at write time too, not only when planning.
5. A job reports `done` only when every step finished. Cancel stops the work before reporting `cancelled`. Every failure names the file.
6. Index and job state are persisted so a quit or crash resumes instead of restarting a multi-hour scan.

## Conventions

- **CR3**: use the larger embedded preview for indexing, check `extract_thumb()`'s `format` (JPEG or bitmap) and fall back to a half-size `postprocess`. Apply EXIF orientation. Editing uses a one-time linear 16-bit decode that is cached. Pin a recent `rawpy`; new Canon bodies need a recent LibRaw.
- **Edit parameters** are versioned JSON, with a migration for every schema change. The rendered image is always `render(original, params)`.
- **Pipeline** works in linear float32 and converts to display space last. Curves are 1D LUTs from monotone (PCHIP) splines, never ones that can overshoot; they act on sRGB-encoded values inside the tone step, which is one LUT per channel. Geometry runs first because every other step is per pixel.
- **Rendering** uses a ~2 MP proxy while a control moves (debounced, on a worker, cancelled when a newer request arrives) and full resolution on release. Never block the UI thread on a render, decode or DB scan.
- Heavy CPU work (CLIP, decode, thumbnailing) runs in worker processes, not threads, so the GIL does not stall the UI.
- Memory: a 45 MP linear float32 image is about 540 MB. Do not hold more than one at once; tile or use 16-bit for full-resolution passes.
- The look is filehouse's: `integration/theme.py` and `style.py` are ports of its modules and read the same `theme.json`. Keep them in step with filehouse rather than letting them drift.
- Colours and fonts come from `QPalette` and `QFont`; no literal colours in stylesheets and nothing that assumes a dark background. Stylesheet colours, radii and sizes come from `integration/style.py`. Sizes are unscaled pixels times the theme scale (`style.px`).
- Worker processes use the `forkserver` context, which re-imports the main module: every entry point and script needs an `if __name__ == "__main__"` guard, or starting a worker starts the app again.
- Photos are stored by path relative to a library root plus a volume id, so moved drives do not break the index.
- Tests run against `tmp_path` and small generated sample files; never touch the real home directory or a real photo library. Real CR3 samples stay out of git (see above).
- No network calls except the optional first-run model download.
