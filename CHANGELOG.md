# Changelog

Downloadable builds live on the [releases page](https://github.com/juliansy/merge-media/releases).
Tags before v1.7.0 predate this file; their notes are on that page.

## v1.7.0 — 2026-11-25

Refactor release. The merged output is unchanged for the cases the tool already
handled; the internals are smaller and the failure paths are more honest.

- Subtitle validation round-trips through ffmpeg instead of a hand-written
  SRT/VTT/ASS parser. Empty timelines, cue-settings-only files and binary files
  disguised as subtitles are still rejected; a cue whose text is only digits is
  now accepted.
- One directory index serves audio, cover and subtitle discovery, so a folder
  is scanned once instead of once per video.
- Dry-run and a real run share the same preprocessing. A file whose every
  sidecar was degraded is counted as skipped, not as "will merge", so the
  dry-run summary matches the real run.
- `--purge` keeps a sidecar whose merge failed. It still removes sidecars the
  video had no use for.
- JSON subtitle cue counts are shown in dry-run notes again.
- Both READMEs document every public flag and keep the same section order;
  example paths are placeholders, not a real download folder.
- Unit tests cover the pure helpers and, when ffmpeg is on `PATH`, subtitle
  validation and preparation.

This release also contains the work that sat on `main` after v1.4.1 but was
never tagged: Matroska output, multi-language subtitle sidecars, cover and font
attachment handling, upfront audio/subtitle/cover validation, and UTF-16
subtitle support.
