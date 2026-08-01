# Lumina Photo Core

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Engine: FastAPI](https://img.shields.io/badge/backend-FastAPI-009688.svg)](https://fastapi.tiangolo.com/)
[![Vector DB: Qdrant](https://img.shields.io/badge/vector--db-Qdrant-red.svg)](https://qdrant.tech/)
[![ML: OpenAI CLIP](https://img.shields.io/badge/ml-OpenAI%20CLIP-violet.svg)](https://github.com/openai/CLIP)
[![License: MIT](https://img.shields.io/badge/license-MIT-lightgrey.svg)](LICENSE)

An intelligent, self-hosted local photo indexing and semantic search system built to handle high-volume photo libraries (~1 TB+), including modern Canon `.CR3` RAW files. 

Lumina uses local vision-language embeddings (CLIP/SigLIP) to enable natural language search over your entire offline photo collection—without uploading anything to the cloud.

---

## 🏗️ Architecture & RAW Processing Pipeline

To process terabytes of data efficiently, Lumina uses a dual-path ingestion engine. Instead of performing costly full sensor demosaicing on `.CR3` RAW files during ingestion, it extracts embedded full-resolution JPEGs using `rawpy`/`LibRaw` for lightning-fast indexing and thumbnail generation.

```text
                                 ┌──> Extract Embedded JPEG ──> Generate WebP Thumbnails
                                 │    (~10-50ms / file)             │
.CR3 / RAW / JPEG File ──────────┤                                  ▼
                                 │                         CLIP Vector Encoder
                                 │                                  │
                                 └──> Extract EXIF/GPS Metadata    ▼
                                              │             Qdrant Vector Store
                                              ▼                     │
                                     SQLite / PostgreSQL <──────────┘
