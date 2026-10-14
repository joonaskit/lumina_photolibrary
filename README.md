# Lumina

A local desktop photo library with semantic search and a small non-destructive editor, built for large libraries (1 TB+) including Canon `.CR3` RAW files.

Lumina indexes your photos offline, lets you search them in plain language ("sunset over a lake") using local CLIP/SigLIP embeddings, and lets you adjust tone curves and matte looks without ever touching the originals. Nothing is uploaded anywhere.

**Status:** early. A library of folders, kept in step with the disk (new, changed and missing photos, unplugged drives), with metadata from exiftool, a thumbnail grid filtered by year, camera and lens, and a viewer, for JPEG, PNG, TIFF and RAW files including CR3. A Develop view edits photos non-destructively (exposure, white balance, curves, matte, crop and straighten) and exports them. Search finds photos by a description in English, Finnish or other languages, or by likeness to a photo; it needs a one-time 785 MB model download, offered when search is first used, after which it runs entirely on your computer. See [PLAN.md](PLAN.md) for the design and phases.

## Install

On Fedora, from a clone:

```sh
./install.sh
```

It makes a venv with what the app needs, installs exiftool with dnf if it is missing, puts `lumina` in `~/.local/bin`, and adds Lumina with its icon to the app launcher. Run it again after pulling to update. Then start Lumina from the launcher, or `lumina [FOLDER]` from a terminal.

For development, the tests and the linter:

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
sudo dnf install perl-Image-ExifTool   # for metadata
lumina ~/Pictures
```

## Planned features

- Fast indexing of RAW and JPEG libraries, including `.CR3`, using the embedded preview instead of a full decode
- EXIF and GPS metadata, filters by date, camera and lens
- Thumbnail grid that stays responsive with hundreds of thousands of photos
- Natural-language search and "similar photos", running locally
- Non-destructive Develop view: exposure, white balance, tone curves (RGB and per channel), matte look, crop, export
- Resumable background indexing that copes with unplugged drives

## Stack

Python 3.12+, PySide6 (Qt Widgets), rawpy/LibRaw, exiftool, numpy, ONNX Runtime, SQLite.

## How it works

```text
                              ┌─> embedded JPEG ─> WebP thumbnails ─> CLIP embeddings ─┐
.CR3 / RAW / JPEG ────────────┤                                                        ├─> SQLite + vector store
                              └─> EXIF / GPS (exiftool) ───────────────────────────────┘

Develop view:  original RAW ─> linear decode (cached) ─> pipeline(edit params) ─> preview / export
```

Originals are read-only. Edits are stored as small parameter sets in the Lumina database and rendered on demand.

## Docs

- [PLAN.md](PLAN.md): design, decisions, phases
- [CLAUDE.md](CLAUDE.md): conventions and rules for working in this repo
