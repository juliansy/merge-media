<div align="center">

<br>

[简体中文](README.zh-CN.md) · **English**

<br>

</div>

<div align="center">

![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows&logoColor=white)
![FFmpeg](https://img.shields.io/badge/ffmpeg-required-4C566A)
![License](https://img.shields.io/badge/license-MIT-2EA043)
[![Release](https://img.shields.io/github/v/release/juliansy/merge-media?color=orange&label=release)](https://github.com/juliansy/merge-media/releases)
![Stars](https://img.shields.io/github/stars/juliansy/merge-media?color=yellow)

</div>

# Merge Audio and Video

This tool is for a download that finished without being assembled: one file holds the picture, another holds the sound, and the subtitles and cover art sit beside them. Put `merge_videos.py` and `merge.bat` into that folder and run it — you get one video (`.mp4`, `.m4v` or `.mkv`) with all of them inside. Video and audio are copied as they are, so nothing is re-encoded.

**Download:** [latest release](https://github.com/juliansy/merge-media/releases/latest) — a zip with `merge_videos.py`, `merge.bat`, both READMEs and the license.

## What it does

- Merges `name.mp4` and `name.m4a` into one file, keeping the video's container (`.mp4`/`.m4v`/`.mkv`).
- Audio sidecars: `.m4a`, `.aac`, `.mp3`, `.opus`, `.flac`, `.ogg`, `.wav`. MP4 only accepts AAC/MP3/AC3/E-AC3/ALAC; other codecs are skipped with a note (use `.mkv`, or convert the audio), while the rest of the merge continues.
- Embeds subtitles: `.json` (Bilibili subtitle export), `.ass`, `.ssa`, `.srt`, `.vtt` — the plain same-name file plus any number of language-suffixed files (`name.en.vtt`, `name.zh-Hans.srt`, `name_zh.json`). Each becomes its own track, tagged with the language from its suffix.
- Attaches a cover: `.jpg`, `.jpeg`, `.png`, `.webp`, `.avif`, `.bmp`, `.gif`, `.tif`, `.tiff`; anything that is not JPEG is converted first. AVIF needs an ffmpeg with an AV1 decoder (the "full" build).
- Adds only what is missing: audio, subtitles and cover already inside the video stay untouched. A sidecar whose language already exists is skipped instead of duplicated.
- Prints what it merged and whether the result is complete.

## What a folder looks like

Put both script files into the folder. The media it picks up shares a name and differs only by extension; subtitle files may add a language suffix.

```text
D:\Videos\
├── merge.bat           the launcher — double-click this
├── merge_videos.py     the script it runs — keep both together
├── lecture-01.mp4      the video (required)
├── lecture-01.m4a      the audio (optional; .aac .mp3 .opus .flac .ogg .wav)
├── lecture-01.json     subtitles (optional; .ass .ssa .srt .vtt)
├── lecture-01.en.vtt   same video, English track — the language suffix is optional
├── lecture-01.zh-Hans.srt
├── lecture-01.jpg      cover (optional; .jpeg .png .webp .avif .bmp .gif .tif .tiff)
└── podcast-02.mp4      a second video, same run
```

After a successful run the sidecars are gone and the videos carry everything:

```text
D:\Videos\
├── merge.bat
├── merge_videos.py
├── lecture-01.mp4      video + audio + subtitles + cover
└── podcast-02.mp4      video only, nothing to add
```

Use `-k` if you want the sidecars kept. A file whose name matches no video is left alone and listed at the end.

## Requirements

- Windows 10 or 11
- [FFmpeg](https://ffmpeg.org/download.html): `ffmpeg.exe` and `ffprobe.exe` on `PATH`
- Python 3.9 or newer; no extra packages

## How to use

Put both files in the folder that holds the video, audio, subtitles and cover, then pick the line for your terminal.

| Where | What to type |
| --- | --- |
| File Explorer | double-click `merge.bat` |
| cmd | `merge.bat -n "D:\Videos"` |
| PowerShell / pwsh | `.\merge.bat -n "D:\Videos"` or `& "D:\Videos\merge.bat" -n` |
| Git Bash | `./merge.bat -n "D:/Videos"` |
| WSL | `cmd.exe /c merge.bat -n "D:\Videos"` |

PowerShell does not run programs from the current folder, so a bare `merge.bat` fails with `CommandNotFoundException` — that is what the `.\` is for. In cmd the bare name is fine.

Dropping things on the icon works too:

| Action | Result |
| --- | --- |
| Drop a folder onto `merge.bat` | Merges that folder; add `-R` to include subfolders |
| Drop a single video file onto `merge.bat` | Merges only that file |

### Options

| Option | Meaning |
| --- | --- |
| `-n`, `--dry-run` | Analyze only; nothing is written |
| `-k`, `--keep` | Keep every source file |
| `-p`, `--purge` | Also delete sidecars the video did not need |
| `-j N` | Process N files at the same time |
| `-R` | Include subfolders |
| `--sub-lang LANG` | Language tag for added subtitles without a language suffix; `auto` guesses from the text |
| `-V` | Print the version |

`python merge_videos.py -h` lists every option.

### Output

```text
#  文件            原有        目录        动作 并入           结果                  耗时
-----------------------------------------------------------------------------------------
1  lecture-01.mp4  音✗ 字✗ 封✗ 音✓ 字✓ 封✓ 合并 音频+字幕+封面 齐全                0.4s
     └ 删除 3 个源文件
2  podcast-02.mp4  音✗ 字✗ 封✗ 音✗ 字✗ 封✗ 跳过 —              缺音频/字幕/封面    0.1s
     └ 无可合并素材（缺 音频/字幕/封面）

合并 1 个，跳过 1 个，失败 0 个。
本次并入：音频 1 个，字幕 1 个，封面 1 个。
齐全 1 个，不齐全 0 个。
```

The console output is in Chinese. Read the columns left to right: what the video **has**, what the **folder** offers, what was **merged in**, and whether the result is **complete** now.

## Safety

- Source files are never edited in place. The result is written to a temp file, checked, then swapped in.
- If one file fails, its sources stay exactly as they were; the exit code is `1`.
- Sidecars are deleted only after a merge has been verified. `-p` also removes sidecars the video had no use for; a sidecar that failed to merge is always kept.
- Files that are already complete are skipped, never rewritten.

## Limitations

- MP4 stores subtitles as `mov_text` (the subtitle format most mp4 players accept): text and timing survive, styling and positioning do not. MKV keeps the original subtitle codecs instead, so PGS/ASS/VobSub keep their styling.
- MP4 audio is limited to AAC/MP3/AC3/E-AC3/ALAC; other codecs need MKV or a transcode. MKV accepts any codec.
- `.mov` is not supported: ffmpeg's mov muxer silently drops attached cover art, so a merge would lose the cover. Remux to `.mp4`/`.m4v`/`.mkv` first.
- AVIF covers need an ffmpeg with an AV1 decoder; without one the cover is skipped with a message and the other tracks still merge.
- Paths longer than 260 characters can fail inside FFmpeg on older Windows builds.

## Tests

The pure helpers (timestamps, subtitle parsing, language tags, sidecar discovery) have unit tests that need no ffmpeg:

```text
python -m unittest test_merge_videos -v
```

## License

MIT — see [LICENSE](LICENSE).
