# Changelog

Downloadable builds live on the [releases page](https://github.com/juliansy/merge-media/releases).
Tags before v1.7.0 predate this file; their notes are on that page.

## v1.7.2 — 2026-11-25

Lossless-first follow-up to v1.7.1, whose AAC fallback re-encoded the audio.

- Audio is never re-encoded. When the MP4/M4V container cannot carry a
  sidecar's codec (Opus/Vorbis from `.webm`, FLAC, ...), the output is written
  next to the source as `name.mkv` and every stream is copied.
- MP4's image subtitles are no longer dropped: a video with PGS/VobSub/DVB
  subtitles also switches to MKV, so those tracks survive.
- The original video and the merged sidecars are removed only after the new
  MKV has been written and verified; `-k` keeps them.
- `--purge` never removes `.webm`/`.weba`, and a `.webm` that also contains a
  video track is kept after merging, as in v1.7.1.

## v1.7.1 — 2026-11-25 (superseded by v1.7.2)

- `.webm` and `.weba` audio sidecars are discovered. Opus/Vorbis from them is
  transcoded to 192 kbps AAC when the target is MP4; MKV still copies the
  original codec.
- `--purge` never removes a `.webm`/`.weba` sidecar, because it may be a second
  copy of the same video rather than a lone audio track. If the `.webm` itself
  contains a video track, it is kept after a successful merge as well.
- FLAC and other codecs the MP4 whitelist rejects are still skipped with a note.

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
