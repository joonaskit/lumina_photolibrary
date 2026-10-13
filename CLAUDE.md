# CLAUDE.md

Lumina: a local desktop photo library with semantic search and a small non-destructive editor, built for large libraries including Canon `.CR3` RAW files. [PLAN.md](PLAN.md) is the source of truth for design, phases and open decisions; read the relevant section before implementing anything, and update it when a decision changes. `README.md` is out of date (it describes an earlier server design) until Phase 0 rewrites it.

The sibling project `../filehouse` uses the same stack and conventions; look there for patterns (thread pool, thumbnails, theme, layer test) before inventing new ones.

## Stack

- Python 3.12+, **PySide6 with Qt Widgets** (not Qt Quick/QML)
- **rawpy (LibRaw)** for RAW, **exiftool** (`-stay_open`) for metadata
- **numpy** for the edit pipeline, **onnxruntime** for CLIP/SigLIP
- **SQLite** for metadata and edits; vector store is an embedded one, chosen in Phase 4
- pytest for `core/`, pytest-qt for the few UI tests, ruff for lint and format

## Setup and commands

Not final until Phase 0 settles the PySide6 source (see PLAN.md, open decisions). Default:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

lumina                      # run the app
ruff check . && ruff format --check .
pytest                      # all tests
pytest tests/core           # no display needed
pytest tests/ui             # runs offscreen
```

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
- **Pipeline** works in linear float32 and converts to display space last. Curves are 1D LUTs from monotone (PCHIP) splines, never ones that can overshoot.
- **Rendering** uses a ~2 MP proxy while a control moves (debounced, on a worker, cancelled when a newer request arrives) and full resolution on release. Never block the UI thread on a render, decode or DB scan.
- Heavy CPU work (CLIP, decode, thumbnailing) runs in worker processes, not threads, so the GIL does not stall the UI.
- Memory: a 45 MP linear float32 image is about 540 MB. Do not hold more than one at once; tile or use 16-bit for full-resolution passes.
- Colours and fonts come from `QPalette` and `QFont`; no literal colours in stylesheets and nothing that assumes a dark background. Sizes are unscaled pixels times the theme scale.
- Photos are stored by path relative to a library root plus a volume id, so moved drives do not break the index.
- Tests run against `tmp_path` and small generated or checked-in sample files; never touch the real home directory or a real photo library. Keep sample CR3 files small and few.
- No network calls except the optional first-run model download.
