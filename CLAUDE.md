# CLAUDE.md

Lumina: a local desktop photo library with semantic search and a small non-destructive editor, built for large libraries including Canon `.CR3` RAW files. [PLAN.md](PLAN.md) is the source of truth for design, current decisions and what comes next ([docs/history.md](docs/history.md) is the record of how it was built); read the relevant section before implementing anything, and update it when a decision changes.

The sibling project `../filehouse` uses the same stack and conventions; look there for patterns (thread pool, thumbnails, theme, layer test) before inventing new ones.

## Stack

The dependencies and their versions are in `pyproject.toml`. What it does not say:

- **Qt Widgets**, not Qt Quick/QML.
- **exiftool** runs with `-stay_open`. **lensfun**'s XML data (`/usr/share/lensfun`) is read by us, not linked. **DCP camera profiles** are read and written by us (RawTherapee's are in `/usr/share/rawtherapee/dcpprofiles` when installed).
- **SQLite** holds metadata, edits and search vectors (float16 blobs, searched with numpy; no vector store).

## Setup and commands

```sh
python3 -m venv .venv
source .venv/bin/activate
sudo dnf install perl-Image-ExifTool lensfun   # their tests skip without them
pip install -e '.[dev]'

lumina [PATH...]           # run the app; without a path it opens the last folder
./install.sh                # lumina on PATH, launcher entry and icon
ruff check . && ruff format --check .
pytest                      # tests/ui runs offscreen
```

Every push runs the checks in `.github/workflows/tests.yml`, where a test that needs exiftool or lensfun fails instead of skipping. They must pass before a pull request can be merged into `main`.

Real camera files go in the git-ignored `tests/samples/` (or `$LUMINA_SAMPLES`): `tests/core/test_samples.py` runs on them and skips without them. Everything else uses the tiny DNGs `tests/rawfiles.py` writes. Likewise the search model (785 MB): `tests/core/test_embed.py` uses it from `$LUMINA_MODELS` (a models folder, as `$XDG_DATA_HOME/lumina/models`) and skips without it; everything else uses `FakeEncoder` from `tests/fakes.py`.

Do not mix a PyPI PySide6 with the Fedora `python3-pyside6` in one environment.

## Branches

- **`dev`** is where development happens: commit and push to it directly. Every push runs the checks (see above).
- **`canary`** is only for large changes and refactors, work that would leave `dev` broken or half-done for a while. It is merged into `dev` by pull request (`canary` → `dev`), and `dev` is brought up to date with it before starting.
- **`main`** is protected: changes reach it by pull request from `dev` only, once the checks pass. Never push to it.
- The user reviews and tests, and merges (or asks for a merge) themselves. Open a pull request only when asked. After a merge, fast-forward the local branches from their remotes before the next piece.

## Layer rules

```
ui/  →  qt_models/  →  core/
          integration/ (theme, desktop hand-off, island, notifications)
```

- `core/` imports **no Qt**. Plain Python, numpy, OpenCV, rawpy, sqlite3, onnxruntime. It reports through callbacks.
- `qt_models/` is the only place that runs core work on threads or processes and turns callbacks into queued Qt signals: through the scheduler in `qt_models/workers.py`, on a lane declared there, never a pool or thread of its own (a camera session's process and the threads that read its queues aside). A job kept in the `jobs` table runs on a `JobRunner` (`qt_models/jobs.py`); work is stopped with a `workers.Token`.
- `ui/` never calls the DB, rawpy, exiftool or onnxruntime directly. From `core/` it imports only the domain model: `core/edit/params.py`, `core/dbtypes.py`, `core/keywords.py`, `core/smart.py`, `core/edit/export_options.py` and `core/errors.py`, which are plain data and import only the standard library and each other. Don't re-export core names from `qt_models/` for the UI.
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
- Colours and fonts come from `QPalette` and `QFont`; no literal colours in stylesheets and nothing that assumes a dark background. Stylesheet colours, radii and sizes come from `integration/style.py`. Sizes are unscaled pixels times the theme scale (`style.px`). Paddings, margins and spacing use `style.gap` instead, which also follows the density setting (comfortable or compact); the height of the toolbar's and filters' row is `style.row_height()`. A size fixed in a widget when it is made goes through `style.follow_density(owner, apply)`, so the density applies at once.
- Worker processes use the `forkserver` context, which re-imports the main module: every entry point and script needs an `if __name__ == "__main__"` guard, or starting a worker starts the app again.
- Photos are stored by path relative to a library root plus a volume id, so moved drives do not break the index.
- Tests run against `tmp_path` and small generated sample files; never touch the real home directory or a real photo library. Real CR3 samples stay out of git (see above).
- No network calls except the optional first-run model download.

## Agent skills

### Issue tracker

Issues live in GitHub Issues (joonaskit/lumina_photolibrary), through the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
