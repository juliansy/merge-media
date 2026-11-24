#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge_videos.py -- 把分离的视频、音频、字幕和封面封装成一个文件。

扫描目录里的 .mp4/.m4v/.mkv，用 ffprobe 读出视频**已有**的音轨 / 字幕 / 封面，
再找同名的侧车文件（音频 .m4a/.aac/.mp3/.opus/.flac/.ogg/.wav/.webm/.weba，字幕
.ass/.ssa/.srt/.json/.vtt，封面 .jpg/.jpeg/.png/.webp/.avif/.bmp/.gif/.tif/.tiff），
只补“视频缺、目录有”的部分，一次 ffmpeg 调用完成封装；产物校验通过后原子替换
原视频，任何一步失败都原样保留所有源文件。MP4 装不下某个流时改写成同名 .mkv，
所有流仍然 copy，不转码。

同名字幕与“同名 + 语言后缀”的字幕（x.en.vtt / x.zh-Hans.srt / x_zh.json）都会发现，
一次可并入多条不同语言（同名文件有多种格式时只取优先级最高的一种）。容器差异集中在
CONTAINERS：MP4 字幕转 mov_text、封面写 attached_pic、音频只收 AAC/MP3/AC3/E-AC3/ALAC；
装不下时整体改用 Matroska（字幕与音频原样 copy、封面写成附件）。.mov 不支持。

命令行见 `-h`；退出码 0 = 全部成功或无需处理，1 = 有文件失败，2 = 参数或环境错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional, Sequence

VERSION = "1.7.2"

AUDIO_EXTS = (".m4a", ".aac", ".mp3", ".opus", ".flac", ".ogg", ".wav", ".webm", ".weba")
# 入口白名单 = ffmpeg 能当文本/图形字幕读出来的格式；.ssa/.ass 是同一套解析器，成对放行。
SUB_EXTS = (".ass", ".ssa", ".srt", ".json", ".vtt")
# 封面只放 ffmpeg 常见可解码的静态图；HEIC / JXL 依赖编译选项，故意不放行。
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".gif", ".tif", ".tiff")
JPEG_EXTS = (".jpg", ".jpeg")
SIDECAR_EXTS = AUDIO_EXTS + SUB_EXTS + COVER_EXTS

DURATION_TOLERANCE = 2.0     # 产物与原视频时长允许的差（秒）
MIN_OUTPUT_BYTES = 1024
PROBE_TIMEOUT = 180
MERGE_TIMEOUT = 6 * 3600
STALE_TEMP_SECONDS = 3600

TRACK_AUDIO = "音频"
TRACK_SUB = "字幕"
TRACK_COVER = "封面"
MARK_SHORT = {TRACK_AUDIO: "音", TRACK_SUB: "字", TRACK_COVER: "封"}
BITMAP_SUB_CODECS = frozenset({"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"})
# .webm/.weba 可能本来就是同名视频的另一份下载，--purge 不删它们。
AMBIGUOUS_AUDIO_EXTS = (".webm", ".weba")


@dataclass(frozen=True)
class Container:
    """输出容器的差异只放在这张表里，命令拼装读它的字段。

    subtitle_codec: mp4 只认 mov_text；matroska 一律 copy（PGS/ASS 原样保留）。
    cover_mode:     mp4 把封面当 attached_pic 视频流；matroska 必须写成 -attach 附件。
    audio_codecs:   容器能直接 copy 且主流播放器认得的音频编码；空表示不限制。
    """

    suffix: str
    kind: str                         # "mp4" | "matroska"
    subtitle_codec: str               # "mov_text" | "copy"
    cover_mode: str                   # "video" | "attachment"
    muxer_args: tuple[str, ...]
    audio_codecs: tuple[str, ...] = ()


CONTAINERS: dict[str, Container] = {
    container.suffix: container
    for container in (
        Container(".mp4", "mp4", "mov_text", "video", ("-movflags", "+faststart"),
                  ("aac", "mp3", "ac3", "eac3", "alac")),
        Container(".m4v", "mp4", "mov_text", "video", ("-movflags", "+faststart"),
                  ("aac", "mp3", "ac3", "eac3", "alac")),
        Container(".mkv", "matroska", "copy", "attachment", ()),
    )
}
VIDEO_EXTS = tuple(CONTAINERS)


def container_for(path: Path) -> Container:
    try:
        return CONTAINERS[path.suffix.lower()]
    except KeyError:
        raise ValueError(f"不支持的视频容器：{path.suffix or '(无扩展名)'}") from None


# --------------------------------------------------------------------------- #
# 外部命令
# --------------------------------------------------------------------------- #
class ToolError(RuntimeError):
    """ffmpeg / ffprobe 不可用或执行失败。"""


class ProbeError(RuntimeError):
    """ffprobe 无法解析该文件。"""


@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str

    @classmethod
    def resolve(cls, ffmpeg: Optional[str] = None, ffprobe: Optional[str] = None) -> "Tools":
        ff = ffmpeg or os.environ.get("MERGE_FFMPEG") or shutil.which("ffmpeg")
        fp = ffprobe or os.environ.get("MERGE_FFPROBE") or shutil.which("ffprobe")
        if not ff or not fp:
            raise ToolError("未找到 ffmpeg / ffprobe。请安装并加入 PATH，或用 --ffmpeg / --ffprobe 指定路径。")
        return cls(ff, fp)


def run_command(cmd: Sequence[str], timeout: int) -> subprocess.CompletedProcess:
    """执行外部命令；stdin 关闭，避免 ffmpeg 等待输入导致挂起。"""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            list(cmd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"命令超时（{timeout}s）：{cmd[0]}") from exc
    except OSError as exc:
        raise ToolError(f"无法启动 {cmd[0]}：{exc}") from exc


def decode(data: Optional[bytes]) -> str:
    return data.decode("utf-8", "replace") if data else ""


_ADDRESS_RE = re.compile(r"\[[^\]]*@ [0-9a-zA-Z]+\]\s*")


def brief_error(text: str, target: Optional[Path] = None, limit: int = 160) -> str:
    """把 ffmpeg/ffprobe 的长错误压成一句：去地址前缀、去绝对路径、截尾。"""
    flat = " ".join(text.split())
    flat = _ADDRESS_RE.sub("", flat)
    if target is not None:
        flat = flat.replace(str(target), target.name)
    return ("…" + flat[-limit:]) if len(flat) > limit else flat


# --------------------------------------------------------------------------- #
# 探测：每个文件一次 ffprobe，拿全部轨道信息
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SubStream:
    index: int
    codec: str
    language: str = ""
    duration: float = 0.0


@dataclass(frozen=True)
class CoverStream:
    index: int
    codec: str
    filename: str = ""
    mimetype: str = ""


@dataclass(frozen=True)
class Probe:
    duration: float = 0.0
    main_index: int = -1               # 第一条非 attached_pic 视频流的绝对号；-1 = 没有正片
    audio_count: int = 0
    subtitles: tuple[SubStream, ...] = ()
    covers: tuple[CoverStream, ...] = ()
    attachments: int = 0               # matroska 的字体等附件数量

    @property
    def has_video(self) -> bool:
        return self.main_index >= 0

    @property
    def has_audio(self) -> bool:
        return self.audio_count > 0

    @property
    def has_subs(self) -> bool:
        return bool(self.subtitles)

    @property
    def has_cover(self) -> bool:
        return bool(self.covers)

    @property
    def tracks(self) -> dict[str, bool]:
        return {TRACK_AUDIO: self.has_audio, TRACK_SUB: self.has_subs, TRACK_COVER: self.has_cover}

    @property
    def cover_indices(self) -> tuple[int, ...]:
        return tuple(cover.index for cover in self.covers)


def ffprobe_json(tools: Tools, path: Path, *options: str) -> dict:
    cmd = [tools.ffprobe, "-v", "error", *options, "-print_format", "json", str(path)]
    proc = run_command(cmd, PROBE_TIMEOUT)
    if proc.returncode != 0:
        raise ProbeError(" ".join(decode(proc.stderr).split()) or "ffprobe 返回非零")
    try:
        return json.loads(decode(proc.stdout) or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe 输出不是合法 JSON：{exc}") from exc


def _codec(stream: Optional[dict]) -> str:
    return str((stream or {}).get("codec_name") or "")


def _tag(stream: dict, name: str) -> str:
    tags = stream.get("tags") or {}
    return str(tags.get(name) or tags.get(name.upper()) or tags.get(name.lower()) or "")


def _stream_seconds(stream: dict) -> float:
    """优先读 duration；读不到再看 matroska 的 DURATION tag。

    产物带附件时 ffprobe 会把字幕流 duration 报成 N/A，但 DURATION tag 仍准确；
    不兜底就会把正常字幕误判成“空字幕”。
    """
    try:
        value = float(stream.get("duration") or 0.0)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    stamp = str(_tag(stream, "DURATION"))
    try:
        hours, minutes, seconds = stamp.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return 0.0


def probe(tools: Tools, path: Path) -> Probe:
    data = ffprobe_json(tools, path, "-show_format", "-show_streams")
    streams = data.get("streams")
    if not isinstance(streams, list):
        raise ProbeError("ffprobe 输出缺少 streams")

    def kind(stream: dict) -> str:
        return str(stream.get("codec_type") or "")

    def attached(stream: dict) -> bool:
        return int((stream.get("disposition") or {}).get("attached_pic") or 0) == 1

    main = next((index for index, stream in enumerate(streams)
                 if kind(stream) == "video" and not attached(stream)), -1)
    subtitles = tuple(
        SubStream(index, _codec(stream), _tag(stream, "language"), _stream_seconds(stream))
        for index, stream in enumerate(streams) if kind(stream) == "subtitle"
    )
    covers = tuple(
        CoverStream(index, _codec(stream), _tag(stream, "filename"), _tag(stream, "mimetype"))
        for index, stream in enumerate(streams) if kind(stream) == "video" and attached(stream)
    )
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return Probe(
        duration=duration,
        main_index=main,
        audio_count=sum(1 for stream in streams if kind(stream) == "audio"),
        subtitles=subtitles,
        covers=covers,
        attachments=sum(1 for stream in streams if kind(stream) == "attachment"),
    )


def audio_codec(tools: Tools, path: Path) -> tuple[str, bool]:
    """读第一条音轨的编码名，以及文件里是否还有视频流。

    同名 .webm 可能是整段视频而不是单独的音轨；有视频时合并后不要删它。
    文件损坏或没有音轨时抛 ProbeError。
    """
    streams = ffprobe_json(
        tools, path, "-show_entries", "stream=codec_type,codec_name"
    ).get("streams") or []
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    codec = str((audio or {}).get("codec_name") or "")
    if not codec:
        raise ProbeError("音频文件里没有可用的音轨")
    has_video = any(stream.get("codec_type") == "video" for stream in streams)
    return codec, has_video


# --------------------------------------------------------------------------- #
# 侧车文件发现
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Subtitle:
    """一个字幕侧车；tag 是文件名里的语言标签（x.en.vtt → "en"），空表示同名无标签。"""

    path: Path
    tag: str = ""


@dataclass(frozen=True)
class Sidecars:
    audio: Optional[Path] = None
    subtitles: tuple[Subtitle, ...] = ()
    cover: Optional[Path] = None

    def paths(self) -> tuple[Path, ...]:
        found = [self.audio] if self.audio else []
        found += [sub.path for sub in self.subtitles]
        if self.cover:
            found.append(self.cover)
        return tuple(found)


@dataclass(frozen=True)
class SidecarIndex:
    """一次目录扫描的结果，按用途分成两张表，查找都是 O(1)。"""

    exact: dict[str, list[tuple[str, Path]]]        # NFC/casefold stem → [(后缀, 路径)]
    tagged: dict[str, tuple["Subtitle", ...]]       # 视频 stem → 语言后缀字幕


def _language_bases(stem: str) -> Iterable[tuple[str, str]]:
    """枚举“把某个分隔符后的内容当语言标签”的拆法：x.en → (x, en)，x_zh → (x, zh)。

    与逐文件扫描的旧语义一致：x_en_zh 既可拆成 (x, en_zh) 也可拆成 (x_en, zh)。
    """
    for index, char in enumerate(stem):
        if char not in "._":
            continue
        language = _language_tag(stem[index + 1:])
        if language:
            yield stem[:index], language


@lru_cache(maxsize=64)
def build_index(directory: Path) -> SidecarIndex:
    """扫描一次目录，建立“精确同名”和“语言后缀字幕”两张表。

    只读缓存：本进程只删不加侧车，删掉的条目最多让索引多出一个无人认领的名字。

    macOS / 某些解压工具写出的文件名是 NFD（é 拆成 e + 组合符），与 Windows 上的
    NFC 视频名逐字符比较会不相等，所以统一规范化后再比。
    """
    exact: dict[str, list[tuple[str, Path]]] = {}
    tagged: dict[str, list[Subtitle]] = {}
    try:
        items = list(directory.iterdir())
    except OSError:
        return SidecarIndex(exact, {})
    for item in items:
        suffix = item.suffix.lower()
        if suffix not in SIDECAR_EXTS:            # 先看后缀，避免对无关文件 stat
            continue
        try:
            if not item.is_file() or item.stat().st_size == 0:
                continue
        except OSError:
            continue
        stem = unicodedata.normalize("NFC", item.stem).casefold()
        exact.setdefault(stem, []).append((suffix, item))
        if suffix in SUB_EXTS:
            for base, language in _language_bases(stem):
                tagged.setdefault(base, []).append(Subtitle(item, language))
    frozen = {}
    for base, found in tagged.items():
        found.sort(key=lambda sub: (sub.tag, sub.path.name.casefold()))
        frozen[base] = tuple(found)
    return SidecarIndex(exact, frozen)


def _exact(index: dict[str, list[tuple[str, Path]]], stem: str, exts: Sequence[str]) -> Optional[Path]:
    """按扩展名优先级取同名的第一个文件（.m4a 优先于 .aac，.jpg 优先于 .png）。"""
    entries = index.get(stem, ())
    for ext in exts:
        for suffix, path in entries:
            if suffix == ext:
                return path
    return None


def find_sidecars(index: SidecarIndex, video: Path) -> Sidecars:
    stem = unicodedata.normalize("NFC", video.stem).casefold()
    exact = _exact(index.exact, stem, SUB_EXTS)
    subtitles = ((Subtitle(exact, ""),) if exact else ()) + index.tagged.get(stem, ())
    return Sidecars(
        audio=_exact(index.exact, stem, AUDIO_EXTS),
        subtitles=subtitles,
        cover=_exact(index.exact, stem, COVER_EXTS),
    )


def pick_subtitles(video: Path) -> tuple[Subtitle, ...]:
    """单文件入口（测试与调试用）：只返回该视频能用的字幕侧车。"""
    return find_sidecars(build_index(video.parent), video).subtitles


# 语言标签：en / zh-Hans / en-orig / chs 这类；点或下划线分隔都接受。
_LANG_TAG_RE = re.compile(r"^[a-z]{2,3}(?:[-_][a-z0-9]{1,8})*$", re.IGNORECASE)


def _language_tag(rest: str) -> str:
    """把文件名里剥出来的语言段规范成小写标签；不合格返回空串。"""
    tag = rest.strip("._- ")
    return tag.lower() if _LANG_TAG_RE.match(tag) else ""


# BCP-47 / 常见二字码 → ISO 639-2/B（mp4 的 language tag 不接受 BCP-47 原样）。
_LANGUAGE_CODES = {
    "en": "eng", "eng": "eng",
    "zh": "chi", "zh-hans": "chi", "zh-hant": "chi", "zh-cn": "chi", "zh-tw": "chi",
    "zh-hk": "chi", "chs": "chi", "cht": "chi", "chi": "chi",
    "ja": "jpn", "jp": "jpn", "jpn": "jpn", "ko": "kor", "kor": "kor",
    "fr": "fra", "fre": "fra", "fra": "fra", "de": "deu", "ger": "deu", "deu": "deu",
    "es": "spa", "spa": "spa", "ru": "rus", "rus": "rus", "pt": "por", "por": "por",
    "it": "ita", "ita": "ita", "ar": "ara", "ara": "ara", "hi": "hin", "hin": "hin",
    "th": "tha", "tha": "tha", "vi": "vie", "vie": "vie", "id": "ind", "ind": "ind",
    "tr": "tur", "tur": "tur", "pl": "pol", "pol": "pol", "nl": "nld", "dut": "nld",
    "nld": "nld", "sv": "swe", "swe": "swe", "uk": "ukr", "ukr": "ukr",
}


def language_code(tag: str) -> str:
    """把文件名里的语言标签（en / zh-Hans / en-orig）转成三字码；未知返回 und。"""
    key = tag.strip().lower()
    if not key:
        return "und"
    if key in _LANGUAGE_CODES:
        return _LANGUAGE_CODES[key]
    primary = re.split(r"[-_]", key)[0]
    if primary in _LANGUAGE_CODES:
        return _LANGUAGE_CODES[primary]
    return key if len(key) == 3 and key.isalpha() else "und"


# 按字符集猜语言：日文有汉字，所以先看假名；韩文看谚文；其余按占比依次判断。
# 猜错只影响元数据，不影响画面与文字；--sub-lang 可以强制指定。
_SCRIPT_TESTS = (
    (re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]"), 0.02, "jpn"),
    (re.compile(r"[\u1100-\u11ff\uac00-\ud7a3]"), 0.05, "kor"),
    (re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"), 0.05, "chi"),
    (re.compile(r"[\u0600-\u06ff\u0750-\u077f]"), 0.05, "ara"),
    (re.compile(r"[\u0590-\u05ff]"), 0.05, "heb"),
    (re.compile(r"[\u0400-\u04ff]"), 0.05, "rus"),
    (re.compile(r"[\u0e00-\u0e7f]"), 0.05, "tha"),
)


def guess_subtitle_language(text: str) -> str:
    letters = sum(1 for char in text if char.isalpha())
    if not letters:
        return "und"
    for pattern, threshold, code in _SCRIPT_TESTS:
        if len(pattern.findall(text)) / letters >= threshold:
            return code
    return "eng"


# --------------------------------------------------------------------------- #
# 字幕预处理与校验
# --------------------------------------------------------------------------- #
def format_timestamp(ms: int) -> str:
    ms = max(0, int(ms))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, ms = divmod(ms, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_cue_text(value: object) -> str:
    """字幕正文清洗。

    NUL 会让 ffmpeg 的 SRT 解析器整条流失效（产物里只剩一条空字幕），
    其它 C0 控制字符同样没有意义；U+2028/U+2029 按空格处理。
    """
    text = str(value or "").replace("\u2028", " ").replace("\u2029", " ")
    return " ".join(_CONTROL_RE.sub(" ", text).split())


_TEXT_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),      # 必须排在 utf-16-le 前面
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def decode_text(raw: bytes) -> str:
    """按 BOM / UTF-8 / GB18030 解码字幕文本。

    只按 BOM 认 UTF-16/32：无 BOM 的 UTF-16 和 GB18030 无法可靠区分，而 GBK 字幕
    比无 BOM 的 UTF-16 常见得多，优先保证前者。
    """
    for bom, encoding in _TEXT_BOMS:
        if raw.startswith(bom):
            return raw.decode(encoding).lstrip("\ufeff")
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("字幕编码无法识别（已尝试 UTF-8 / UTF-16(BOM) / GB18030）；请把字幕另存为 UTF-8 后重试")


def _read_subtitle_text(path: Path) -> str:
    text = decode_text(path.read_bytes())
    if text.count("\x00") * 10 > len(text):
        raise ValueError("字幕文件看起来是二进制（不是文本字幕），请确认扩展名与内容一致")
    return text.replace("\x00", "")


def _json_cues(text: str) -> list[dict]:
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("body", "subtitles", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise ValueError("JSON 顶层既不是数组，也没有 body/subtitles/data 数组")
    cues = [cue for cue in data if isinstance(cue, dict) and "from" in cue and "to" in cue]
    if not cues:
        raise ValueError("JSON 里没有 from/to 字幕条目")
    return cues


def json_to_srt(source: Path, target: Path) -> int:
    """把哔哩哔哩字幕 JSON 转成 SRT，返回写入的条数。"""
    blocks: list[str] = []
    for cue in _json_cues(_read_subtitle_text(source)):
        try:
            start = int(round(float(cue["from"]) * 1000))
            end = int(round(float(cue["to"]) * 1000))
        except (TypeError, ValueError):
            continue
        if end <= start:
            end = start + 200
        text = clean_cue_text(cue.get("content")).replace("-->", "- ->")
        if not text:
            continue
        index = len(blocks) + 1
        blocks.append(f"{index}\n{format_timestamp(start)} --> {format_timestamp(end)}\n{text}\n")
    if not blocks:
        raise ValueError("没有可用的字幕条目")
    target.write_text("\n".join(blocks), encoding="utf-8", newline="\n")
    return len(blocks)


# 字幕正文里会显示出来的文字；HTML 与 ASS 覆盖标签都不算。
_TAG_RE = re.compile(r"<[^>]*>|\{[^}]*\}")


def validate_subtitle(tools: Tools, path: Path) -> None:
    """让 ffmpeg 自己把字幕解析一遍，确认有可显示的文字。

    ffmpeg 对“格式不对但扩展名对”的文件不会报错，只会产出一条空字幕轨；这里转成
    SRT 看输出，解析失败或只有时间轴都提前拦住，避免整单合并出“字幕已并入”的假象。
    """
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
           "-i", str(path), "-map", "0:s:0", "-f", "srt", "-"]
    proc = run_command(cmd, PROBE_TIMEOUT)
    if proc.returncode != 0:
        detail = brief_error(decode(proc.stderr), path)
        raise ValueError("字幕无法解析（不是有效的 SRT/VTT/ASS）" + (f"：{detail}" if detail else ""))
    for block in re.split(r"\n\s*\n", decode(proc.stdout)):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if lines and lines[0].isdigit():
            lines = lines[1:]                       # 序号行
        if lines and "-->" in lines[0]:
            lines = lines[1:]                       # 时间轴行
        if any(_TAG_RE.sub("", line).strip() for line in lines):
            return
    raise ValueError("字幕里只有时间轴、没有文字")


def prepare_subtitle(tools: Tools, source: Path, workdir: Path, ordinal: int) -> tuple[Path, int]:
    """把字幕落成 ffmpeg 可读的 UTF-8 临时文件（.json 先转 SRT），并校验正文。

    返回 (临时文件, JSON 条目数)；非 JSON 的条目数为 0。
    """
    suffix = source.suffix.lower()
    target = workdir / (f"sub{ordinal}" + (".srt" if suffix == ".json" else suffix))
    entries = 0
    if suffix == ".json":
        entries = json_to_srt(source, target)
    else:
        target.write_text(_read_subtitle_text(source), encoding="utf-8", newline="\n")
    validate_subtitle(tools, target)
    return target, entries


# --------------------------------------------------------------------------- #
# 封面预处理
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Attachment:
    """要写进 matroska 的附件（-attach）：已落盘的封面或原有字体。"""

    path: Path
    filename: str
    mimetype: str


def check_image(tools: Tools, image: Path) -> None:
    """真正解码一帧：坏 JPEG / 截断 PNG 在这里就降级，而不是让最终 mux 整单失败。"""
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
           "-i", str(image), "-frames:v", "1", "-f", "null", "-"]
    proc = run_command(cmd, PROBE_TIMEOUT)
    if proc.returncode == 0:
        return
    detail = brief_error(decode(proc.stderr), image)
    # ffmpeg 对坏图常只回一句 "Nothing was written..."，对用户没有指导意义。
    if not detail or "Nothing was written" in detail or "return code" in detail:
        detail = "文件可能已损坏，或不是有效的图片"
    if image.suffix.lower() == ".avif":
        detail += "；AVIF 需要带 AV1 解码器的 full 版 ffmpeg，或先把封面转成 JPG/PNG"
    raise ValueError(f"封面无法解码：{detail}；请换一张 JPG/PNG 后重试")


def prepare_cover(tools: Tools, cover: Path, workdir: Path) -> Path:
    """封面统一成 mjpeg JPEG：mp4 的 attached_pic 与 mkv 的 -attach 都接受。"""
    check_image(tools, cover)
    if cover.suffix.lower() in JPEG_EXTS:
        return cover
    target = workdir / "cover.jpg"
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-i", str(cover), "-frames:v", "1", "-update", "1", "-q:v", "2", str(target)]
    proc = run_command(cmd, PROBE_TIMEOUT)
    if proc.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise ValueError(f"封面转换失败：{brief_error(decode(proc.stderr), cover)}")
    return target


_IMAGE_BY_CODEC = {
    "mjpeg": ("jpg", "image/jpeg"),
    "png": ("png", "image/png"),
    "webp": ("webp", "image/webp"),
    "bmp": ("bmp", "image/bmp"),
}


def _grab_frame(tools: Tools, video: Path, index: int, target: Path, copy: bool) -> subprocess.CompletedProcess:
    encoding = ["-c:v", "copy"] if copy else ["-q:v", "2"]
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-i", str(video), "-map", f"0:{index}", "-frames:v", "1", *encoding, str(target)]
    return run_command(cmd, MERGE_TIMEOUT)


def extract_covers(tools: Tools, video: Path, pr: Probe, workdir: Path) -> tuple[Attachment, ...]:
    """把 mkv 里已有的图片封面落成临时文件，供 -attach 回挂。

    ffmpeg 的 matroska muxer 不能把“视频流形式的封面”再写成附件，直接 -map 只会得到
    一条 attached_pic=0 的普通 1 帧视频轨；先提取再 -attach，并尽量保留原始字节。
    """
    attachments: list[Attachment] = []
    for ordinal, cover in enumerate(pr.covers):
        ext, mimetype = _IMAGE_BY_CODEC.get(cover.codec, ("jpg", "image/jpeg"))
        target = workdir / f"cover{ordinal}.{ext}"
        proc = _grab_frame(tools, video, cover.index, target, copy=ext != "jpg")
        if proc.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            target = workdir / f"cover{ordinal}.jpg"
            mimetype = "image/jpeg"
            proc = _grab_frame(tools, video, cover.index, target, copy=False)
        if proc.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            raise ValueError(f"原有封面提取失败：{brief_error(decode(proc.stderr), video)}")
        name = Path(cover.filename).name if cover.filename else target.name
        attachments.append(Attachment(target, name, mimetype))
    return tuple(attachments)


# --------------------------------------------------------------------------- #
# 计划
# --------------------------------------------------------------------------- #
@dataclass
class Plan:
    video: Path
    probe: Probe
    side: Sidecars
    container: Container
    add_audio: Optional[Path] = None
    add_subs: tuple[Subtitle, ...] = ()
    add_cover: Optional[Path] = None
    skip_reason: str = ""
    kept_subs: tuple[SubStream, ...] = ()   # 已有字幕里会写进产物的（MP4 下图像字幕被剔除）
    dropped_subs: tuple[str, ...] = ()      # 被剔除的图像字幕编码
    covers_kept: bool = True                # mkv 原有封面提取失败时置 False，避免校验误报
    keep_audio_source: bool = False         # 同名 webm 里还有视频时，合并后保留它

    @property
    def has_work(self) -> bool:
        return bool(self.add_audio or self.add_subs or self.add_cover)


def make_plan(video: Path, pr: Probe, side: Sidecars, container: Container) -> Plan:
    kept = tuple(sub for sub in pr.subtitles
                 if not (container.kind == "mp4" and sub.codec in BITMAP_SUB_CODECS))
    dropped = tuple(sub.codec for sub in pr.subtitles
                    if container.kind == "mp4" and sub.codec in BITMAP_SUB_CODECS)
    if pr.has_subs:
        # 已有字幕时不重复同语言：只补文件名里带新语言标签的侧车；同名无标签的仍跳过。
        # 用 kept 而不是 pr.subtitles：MP4 下会被剔除的图像字幕不能算“已有”。
        existing = {language_code(sub.language) for sub in kept
                    if sub.language and sub.language != "und"}
        add_subs = tuple(sub for sub in side.subtitles
                         if sub.tag and language_code(sub.tag) not in existing)
    else:
        add_subs = side.subtitles

    plan = Plan(
        video=video, probe=pr, side=side, container=container,
        add_audio=None if pr.has_audio else side.audio,
        add_subs=add_subs,
        add_cover=None if pr.has_cover else side.cover,
        kept_subs=kept,
        dropped_subs=dropped,
    )
    if not pr.has_video:
        plan.skip_reason = "没有可用的视频流"
    elif plan.has_work:
        plan.skip_reason = ""
    else:
        lacking = [name for name, ok in pr.tracks.items() if not ok]
        if not lacking:
            plan.skip_reason = "已齐全，无需处理"
        elif not side.paths():
            plan.skip_reason = f"无可合并素材（缺 {'/'.join(lacking)}）"
        else:
            plan.skip_reason = f"缺少的轨道目录里也没有（缺 {'/'.join(lacking)}）"
    return plan


# --------------------------------------------------------------------------- #
# 封装命令
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SubtitleInput:
    """已预处理、可直接丢给 ffmpeg 的字幕，以及它最终要写的语言码。"""

    path: Path        # 临时文件（.json 已转 SRT，文本字幕已转 UTF-8）
    language: str
    source: Path      # 原始侧车，成功后按它删除
    entries: int = 0  # JSON 转出的字幕条数；其它格式为 0


def build_command(tools: Tools, plan: Plan, subtitles: Sequence[SubtitleInput],
                  cover: Optional[Path], dest: Path, with_title: bool,
                  attachments: Sequence[Attachment] = ()) -> list[str]:
    """组织 ffmpeg 参数。

    视频里**已有**的音轨 / 字幕 / 封面必须显式保留，否则只 map 新加的那一路会把它们
    丢掉。视频流一律用绝对流号（0:<idx>）定位：正片 = 第一条非 attached_pic 的视频流，
    封面 = 每条 attached_pic 的视频流——不假设它们是 v:0 / v:1。
    """
    pr = plan.probe
    container = plan.container
    inputs = [plan.video]
    audio_input = None
    if plan.add_audio:
        audio_input = len(inputs)
        inputs.append(plan.add_audio)
    sub_base = len(inputs)
    inputs += [sub.path for sub in subtitles]
    cover_input = None
    if cover is not None and container.cover_mode == "video":
        cover_input = len(inputs)
        inputs.append(cover)

    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    for path in inputs:
        cmd += ["-i", str(path)]

    maps = [f"0:{pr.main_index}"]
    if container.cover_mode == "video":
        maps += [f"0:{index}" for index in pr.cover_indices]
    if pr.audio_count:
        maps += ["0:a"]
    maps += [f"0:{sub.index}" for sub in plan.kept_subs]
    if container.kind == "matroska":
        maps += ["0:t?"]                       # 字体等附件；? 让没有附件时也不报错
    if audio_input is not None:
        maps += [f"{audio_input}:a:0"]
    maps += [f"{sub_base + offset}:s:0" for offset in range(len(subtitles))]
    if cover_input is not None:
        maps += [f"{cover_input}:v:0"]
    for item in maps:
        cmd += ["-map", item]

    cmd += ["-c:v", "copy", "-c:a", "copy"]
    if plan.kept_subs or subtitles:
        cmd += ["-c:s", container.subtitle_codec]
    for offset, sub in enumerate(subtitles):
        cmd += [f"-metadata:s:s:{len(plan.kept_subs) + offset}", f"language={sub.language}"]

    if container.cover_mode == "video":
        # 输出视频流 [正片, 封面...]：封面逐条打 attached_pic。
        for offset in range(len(pr.covers) + (1 if cover else 0)):
            cmd += [f"-disposition:v:{offset + 1}", "attached_pic"]
    else:
        index = pr.attachments
        for attachment in attachments:
            cmd += ["-attach", str(attachment.path),
                    f"-metadata:s:t:{index}", f"filename={attachment.filename}",
                    f"-metadata:s:t:{index}", f"mimetype={attachment.mimetype}"]
            index += 1
        if cover is not None:
            cmd += ["-attach", str(cover),
                    f"-metadata:s:t:{index}", "filename=cover.jpg",
                    f"-metadata:s:t:{index}", "mimetype=image/jpeg"]

    cmd += ["-map_metadata", "0", *container.muxer_args]
    cmd += ["-max_muxing_queue_size", "1024"]  # 大文件 remux 的 interleave 缓冲
    if with_title:
        cmd += ["-metadata", f"title={plan.video.stem}"]
    cmd.append(str(dest))
    return cmd


def expected_tracks(plan: Plan, assets: "Assets") -> dict[str, bool]:
    """处理完成后应存在的轨道。"""
    return {
        TRACK_AUDIO: plan.probe.has_audio or plan.add_audio is not None,
        TRACK_SUB: bool(plan.kept_subs) or bool(assets.subtitles),
        TRACK_COVER: (plan.probe.has_cover and plan.covers_kept) or assets.cover is not None,
    }


def merged_tracks(plan: Plan, assets: "Assets") -> list[str]:
    tracks = []
    if plan.add_audio:
        tracks.append(TRACK_AUDIO)
    if assets.subtitles:
        tracks.append(TRACK_SUB)
    if assets.cover:
        tracks.append(TRACK_COVER)
    return tracks


def verify_output(tools: Tools, dest: Path, plan: Plan, assets: "Assets") -> list[str]:
    """校验产物：轨道、时长、体积。只要求“计划里应当有的”轨道存在。"""
    try:
        pr = probe(tools, dest)
    except (ProbeError, ToolError) as exc:
        return [f"产物无法解析：{brief_error(str(exc), dest)}"]

    expected = expected_tracks(plan, assets)
    problems: list[str] = []
    if not pr.has_video:
        problems.append("缺视频流")
    if expected[TRACK_AUDIO] and not pr.has_audio:
        problems.append("缺音轨")
    expected_subs = len(plan.kept_subs) + len(assets.subtitles)
    if expected[TRACK_SUB] and len(pr.subtitles) < expected_subs:
        problems.append(f"字幕轨数量不足（期望 {expected_subs}，实际 {len(pr.subtitles)}）")
    new_subs = pr.subtitles[len(plan.kept_subs):]
    if assets.subtitles and any(sub.duration <= 0.0 for sub in new_subs[:len(assets.subtitles)]):
        problems.append("新增字幕轨是空的（源字幕没有可解析的条目）")
    if expected[TRACK_COVER] and not pr.has_cover:
        problems.append("缺封面")
    if plan.probe.duration > 0 and abs(pr.duration - plan.probe.duration) > DURATION_TOLERANCE:
        problems.append(f"时长偏差 {pr.duration - plan.probe.duration:+.1f}s")
    try:
        if dest.stat().st_size < MIN_OUTPUT_BYTES:
            problems.append("产物过小")
    except OSError:
        problems.append("产物不可读")
    return problems


# --------------------------------------------------------------------------- #
# 单个视频的处理
# --------------------------------------------------------------------------- #
@dataclass
class Assets:
    """真正能交给 ffmpeg 的素材；预处理失败的侧车在这里被剔除并记一条警告。"""

    subtitles: tuple[SubtitleInput, ...] = ()
    cover: Optional[Path] = None
    attachments: tuple[Attachment, ...] = ()
    warnings: list[str] = field(default_factory=list)


@dataclass
class Result:
    video: Path
    action: str                       # merged / planned / skipped / failed
    before: dict[str, bool]
    available: dict[str, bool]
    added: list[str]
    missing: list[str]
    seconds: float
    note: str = ""
    deleted: list[Path] = field(default_factory=list)


def _subtitle_language(entry: Subtitle, prepared: Path, sub_lang: str) -> str:
    if entry.tag:
        return language_code(entry.tag)          # 文件名标签优先
    if sub_lang != "auto":
        return sub_lang                          # --sub-lang 只作用于无标签字幕
    try:
        return guess_subtitle_language(prepared.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return "und"


def prepare_assets(tools: Tools, plan: Plan, workdir: Path, sub_lang: str) -> Assets:
    """把计划里的侧车全部落成 ffmpeg 可用的临时文件；坏的降级、不拖垮整单。

    dry-run 与真实执行走同一段代码，所以“预演说能并、真跑却说不能”的分支不会存在。
    """
    assets = Assets()
    subtitles: list[SubtitleInput] = []
    for ordinal, entry in enumerate(plan.add_subs):
        try:
            prepared, entries = prepare_subtitle(tools, entry.path, workdir, ordinal)
        except (ValueError, OSError, ToolError) as exc:
            assets.warnings.append(f"字幕未并入（{entry.path.name}：{exc}）")
            continue
        language = _subtitle_language(entry, prepared, sub_lang)
        subtitles.append(SubtitleInput(prepared, language, entry.path, entries))
    assets.subtitles = tuple(subtitles)

    if plan.add_cover:
        try:
            assets.cover = prepare_cover(tools, plan.add_cover, workdir)
        except (ValueError, OSError, ToolError) as exc:
            assets.warnings.append(f"封面未并入（{plan.add_cover.name}：{exc}）")

    if plan.container.cover_mode == "attachment" and plan.probe.has_cover:
        try:
            assets.attachments = extract_covers(tools, plan.video, plan.probe, workdir)
        except (ValueError, OSError, ToolError) as exc:
            assets.warnings.append(f"原有封面未能保留（{exc}）")
            plan.covers_kept = False
    return assets


def choose_container(base: Container, pr: Probe, audio: str) -> tuple[Container, list[str]]:
    """MP4 装不下的流改走 MKV，绝不转码。

    返回 (实际容器, 说明)；没有冲突时说明为空。
    """
    if base.kind != "mp4":
        return base, []
    reasons: list[str] = []
    if any(sub.codec in BITMAP_SUB_CODECS for sub in pr.subtitles):
        reasons.append("图像字幕")
    if audio and audio not in base.audio_codecs:
        reasons.append(f"{audio} 音频")
    if not reasons:
        return base, []
    return CONTAINERS[".mkv"], [f"MP4 装不下 {'、'.join(reasons)}，改用 MKV 输出（不转码）"]


def process_video(ctx: "Context", video: Path) -> Result:
    started = time.perf_counter()
    try:
        base = container_for(video)
    except ValueError as exc:
        return _failed(video, started, str(exc))
    try:
        pr = probe(ctx.tools, video)
    except (ProbeError, ToolError) as exc:
        return _failed(video, started, f"视频无法解析：{brief_error(str(exc), video)}")

    side = find_sidecars(build_index(video.parent), video)
    before, available = pr.tracks, sidecar_marks(side)
    warnings: list[str] = []
    audio_codec_name = ""
    keep_audio_source = False

    if side.audio and not pr.has_audio:
        audio_path = side.audio
        try:
            audio_codec_name, has_video = audio_codec(ctx.tools, audio_path)
        except (ProbeError, ToolError) as exc:
            warnings.append(f"音频未并入（{audio_path.name}：{brief_error(str(exc), audio_path)}）")
            side = Sidecars(None, side.subtitles, side.cover)
        else:
            if has_video and audio_path.suffix.lower() in AMBIGUOUS_AUDIO_EXTS:
                keep_audio_source = True
                warnings.append(f"同名 {audio_path.suffix} 里还有视频轨，合并后保留该文件")

    container, container_notes = choose_container(base, pr, audio_codec_name)
    warnings += container_notes
    plan = make_plan(video, pr, side, container)
    plan.keep_audio_source = keep_audio_source
    target = video if video.suffix.lower() == container.suffix else video.with_suffix(container.suffix)

    if plan.skip_reason:
        deleted: list[Path] = []
        if not ctx.dry_run:
            clean_stale_temps(video.parent, video.stem, container.suffix)   # 跳过也清残留
            if ctx.purge and not ctx.keep:
                # .webm/.weba 可能是另一份同名视频，不能当“未使用附件”删掉。
                purgeable = [path for path in side.paths()
                             if path.suffix.lower() not in AMBIGUOUS_AUDIO_EXTS]
                deleted = remove_files(ctx, purgeable)
        note = plan.skip_reason
        if deleted:
            note += f"；清理 {len(deleted)} 个未使用附件"
        missing = [name for name, ok in before.items() if not ok]
        return Result(video, "skipped", before, available, [], missing,
                      time.perf_counter() - started, note, deleted)

    if target != video and target.exists():
        return _failed(video, started, f"目标已存在：{target.name}；请先移走它", before, available)

    workdir = Path(tempfile.mkdtemp(prefix="mergevid_dry_" if ctx.dry_run else "mergevid_"))
    dest = video.with_name(f".{video.stem}.mergetmp.{os.getpid()}{container.suffix}")
    try:
        if not ctx.dry_run:
            clean_stale_temps(video.parent, video.stem, container.suffix)
        assets = prepare_assets(ctx.tools, plan, workdir, ctx.sub_lang)
        warnings += assets.warnings
        added = merged_tracks(plan, assets)

        if not added:
            # 预演与真实执行一致：素材全部降级时不算“将合并”，按跳过汇报。
            missing = [name for name, ok in before.items() if not ok]
            note = "；".join(warnings) or "没有可并入的内容"
            return Result(video, "skipped", before, available, [], missing,
                          time.perf_counter() - started, note)

        if ctx.dry_run:
            note = _dry_note(plan, assets, added, warnings)
            if target != video:
                note += f"；输出 {target.name}"
            expected = expected_tracks(plan, assets)
            missing = [name for name, ok in expected.items() if not ok and name not in added]
            return Result(video, "planned", before, available, added, missing,
                          time.perf_counter() - started, note)

        if plan.dropped_subs:
            warnings.insert(0, f"原图形字幕（{'/'.join(sorted(set(plan.dropped_subs)))}）无法放入 MP4，已丢弃")

        cmd = build_command(ctx.tools, plan, assets.subtitles, assets.cover, dest,
                            ctx.with_title, assets.attachments)
        proc = run_command(cmd, MERGE_TIMEOUT)
        if proc.returncode != 0:
            detail = brief_error(decode(proc.stderr), video) or f"退出码 {proc.returncode}"
            return _failed(video, started, _with_warnings(f"ffmpeg 失败：{detail}", warnings),
                           before, available)
        problems = verify_output(ctx.tools, dest, plan, assets)
        if problems:
            return _failed(video, started,
                           _with_warnings("产物校验失败：" + "，".join(problems), warnings),
                           before, available)

        os.replace(dest, target)                    # 同目录替换，原子
        deleted = [] if ctx.keep else delete_sources(ctx, plan, assets, added)
        if target != video and not ctx.keep:
            try:
                video.unlink()
                deleted.append(video)
            except OSError as exc:
                warnings.append(f"源视频未删除（{exc}）")
        missing = [name for name, ok in expected_tracks(plan, assets).items() if not ok]
        notes = list(warnings)
        if target != video:
            notes.append(f"输出 {target.name}（未转码）")
        if deleted:
            notes.append(f"删除 {len(deleted)} 个源文件")
        return Result(target, "merged", before, available, added, missing,
                      time.perf_counter() - started, "；".join(notes), deleted)
    except (ValueError, ToolError) as exc:
        return _failed(video, started, _with_warnings(str(exc), warnings), before, available)
    except OSError as exc:
        return _failed(video, started, _with_warnings(f"文件操作失败：{exc}", warnings),
                       before, available)
    finally:
        if not ctx.dry_run:
            try:
                if dest.is_file():
                    dest.unlink()
            except OSError:
                pass
        shutil.rmtree(workdir, ignore_errors=True)


def _with_warnings(note: str, warnings: Sequence[str]) -> str:
    return "；".join([note, *warnings]) if warnings else note


def _dry_note(plan: Plan, assets: Assets, added: Sequence[str], warnings: Sequence[str]) -> str:
    note = f"将并入 {'+'.join(added)}" if added else "没有可并入的内容"
    note += "".join(f"（{sub.source.name} {sub.entries} 条）"
                    for sub in assets.subtitles if sub.entries)
    if plan.dropped_subs:
        note += f"；原图形字幕（{'/'.join(sorted(set(plan.dropped_subs)))}）无法放入 MP4，将丢弃"
    return _with_warnings(note, warnings)


def _failed(video: Path, started: float, note: str,
            before: Optional[dict[str, bool]] = None,
            available: Optional[dict[str, bool]] = None) -> Result:
    return Result(video, "failed", before if before is not None else Probe().tracks,
                  available if available is not None else sidecar_marks(Sidecars()), [],
                  [], time.perf_counter() - started, note)


def sidecar_marks(side: Sidecars) -> dict[str, bool]:
    return {
        TRACK_AUDIO: side.audio is not None,
        TRACK_SUB: bool(side.subtitles),
        TRACK_COVER: side.cover is not None,
    }


def clean_stale_temps(directory: Path, stem: str, suffix: str) -> None:
    """清理上次异常中断留下的临时封装文件（超过 1 小时的才算，避免误删并发实例的）。"""
    now = time.time()
    prefix = f".{stem}.mergetmp."
    try:
        for item in directory.iterdir():
            if not item.name.startswith(prefix) or not item.name.lower().endswith(suffix.lower()):
                continue
            try:
                if now - item.stat().st_mtime > STALE_TEMP_SECONDS:
                    item.unlink()
            except OSError:
                continue
    except OSError:
        pass


def remove_files(ctx: "Context", paths: Iterable[Path]) -> list[Path]:
    deleted: list[Path] = []
    for path in paths:
        try:
            if path.is_file():
                path.unlink()
                deleted.append(path)
        except OSError as exc:
            ctx.log(f"      ! 删除失败 {path.name}：{exc}")
    return deleted


def delete_sources(ctx: "Context", plan: Plan, assets: Assets, added: Sequence[str]) -> list[Path]:
    """删除已并入的侧车；--purge 时再删“视频已有该轨、所以没用上”的那部分。

    purge 只删“视频本来就有这轨”的侧车：视频缺轨、但侧车因为编码/解析失败没并进去时
    必须留下，否则用户丢的是唯一一份素材。
    """
    candidates: list[Path] = []
    if plan.add_audio and TRACK_AUDIO in added and not plan.keep_audio_source:
        candidates.append(plan.add_audio)
    if plan.add_cover and TRACK_COVER in added:
        candidates.append(plan.add_cover)
    candidates += [sub.source for sub in assets.subtitles]

    if ctx.purge:
        if (plan.side.audio and plan.add_audio is None and plan.probe.has_audio
                and plan.side.audio.suffix.lower() not in AMBIGUOUS_AUDIO_EXTS):
            candidates.append(plan.side.audio)
        if plan.side.cover and plan.add_cover is None and plan.probe.has_cover:
            candidates.append(plan.side.cover)
        if plan.probe.has_subs:
            # 只删“因为视频已有同语言字幕而没用上”的侧车；计划了但解析失败的仍保留。
            planned = {str(sub.path).casefold() for sub in plan.add_subs}
            candidates += [sub.path for sub in plan.side.subtitles
                           if str(sub.path).casefold() not in planned]

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return remove_files(ctx, unique)


# --------------------------------------------------------------------------- #
# 汇报
# --------------------------------------------------------------------------- #
def iterate_clusters(text: str):
    """把字符串切成“用户眼里的一个字”。

    终端里 👨‍👩‍👧‍👦 / 👍🏽 / 1️⃣ / 🇨🇳 / e+́ 各自只占 1 个字形宽度，
    按码点算长度会让表格全部错位。
    """
    cluster = ""
    for char in text:
        if not cluster or _joins(cluster[-1], char, cluster):
            cluster += char
        else:
            yield cluster
            cluster = char
    if cluster:
        yield cluster


_ZWJ = "\u200d"
_VARIATION = ("\ufe0e", "\ufe0f")
_KEYCAP = "\u20e3"


def _is_combining(char: str) -> bool:
    return unicodedata.combining(char) != 0 or unicodedata.category(char) in ("Mn", "Me")


def _joins(previous: str, char: str, cluster: str) -> bool:
    code = ord(char)
    return (
        previous == _ZWJ
        or char == _ZWJ
        or _is_combining(char)
        or char in _VARIATION
        or char == _KEYCAP
        or 0x1F3FB <= code <= 0x1F3FF                       # 肤色修饰
        or (0x1F1E6 <= code <= 0x1F1FF                       # 旗帜：两个区域指示符成对
            and len(cluster) == 1 and 0x1F1E6 <= ord(previous) <= 0x1F1FF)
    )


def cluster_width(cluster: str) -> int:
    marks = (_ZWJ, _KEYCAP, *_VARIATION)
    if all(_is_combining(char) or char in marks for char in cluster):
        return 0
    if any(char == _ZWJ or char in _VARIATION or char == _KEYCAP for char in cluster):
        return 2
    if len(cluster) == 2 and all(0x1F1E6 <= ord(char) <= 0x1F1FF for char in cluster):
        return 2
    if any(0x1F3FB <= ord(char) <= 0x1F3FF for char in cluster):
        return 2
    return max(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in cluster)


def display_width(text: str) -> int:
    return sum(cluster_width(cluster) for cluster in iterate_clusters(text))


def pad(text: str, width: int) -> str:
    gap = width - display_width(text)
    return text + " " * gap if gap > 0 else text


def shorten(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for cluster in iterate_clusters(text):
        size = cluster_width(cluster)
        if used + size > width - 1:
            break
        kept.append(cluster)
        used += size
    return "".join(kept) + "…"


def marks(tracks: dict[str, bool]) -> str:
    return " ".join(f"{MARK_SHORT[name]}{'✓' if ok else '✗'}" for name, ok in tracks.items())


def action_text(result: Result) -> str:
    return {"merged": "合并", "planned": "预演", "skipped": "跳过"}.get(result.action, "失败")


def result_text(result: Result) -> str:
    if result.action == "failed":
        return "失败"
    return "缺" + "/".join(result.missing) if result.missing else "齐全"


def render_table(results: Sequence[Result]) -> list[str]:
    if not results:
        return ["没有找到需要处理的视频。"]

    name_width = min(46, max(14, max(display_width(result.video.name) for result in results)))
    before_width = max(display_width("原有"), max(display_width(marks(r.before)) for r in results)) + 1
    avail_width = max(display_width("目录"), max(display_width(marks(r.available)) for r in results)) + 1
    added_width = max(display_width("并入"),
                      min(24, max(display_width("+".join(r.added) or "—") for r in results))) + 1
    verdict_width = max(display_width("结果"), max(display_width(result_text(r)) for r in results)) + 1
    action_width = max(display_width("动作"), max(display_width(action_text(r)) for r in results)) + 1

    header = (
        f"{'#':<3}{pad('文件', name_width)}  "
        f"{pad('原有', before_width)}{pad('目录', avail_width)}"
        f"{pad('动作', action_width)}{pad('并入', added_width)}"
        f"{pad('结果', verdict_width)}{'耗时':>7}"
    )
    lines = [header, "-" * display_width(header)]

    for index, result in enumerate(results, 1):
        merged = "+".join(result.added) if result.added else "—"
        lines.append(
            f"{index:<3}{pad(shorten(result.video.name, name_width), name_width)}  "
            f"{pad(marks(result.before), before_width)}{pad(marks(result.available), avail_width)}"
            f"{pad(action_text(result), action_width)}{pad(shorten(merged, added_width), added_width)}"
            f"{pad(result_text(result), verdict_width)}{result.seconds:>6.1f}s"
        )
        if result.note:
            lines.append(f"     └ {result.note}")
    return lines


def render_summary(results: Sequence[Result], orphans: dict[Path, list[Path]], dry_run: bool) -> list[str]:
    merged = [r for r in results if r.action == "merged"]
    planned = [r for r in results if r.action == "planned"]
    skipped = [r for r in results if r.action == "skipped"]
    failed = [r for r in results if r.action == "failed"]
    pool = merged or planned

    lines = [""]
    if dry_run:
        lines.append(f"预演结果：将合并 {len(planned)} 个，跳过 {len(skipped)} 个，"
                     f"无法处理 {len(failed)} 个。")
    else:
        lines.append(f"合并 {len(merged)} 个，跳过 {len(skipped)} 个，失败 {len(failed)} 个。")

    counts = {name: sum(1 for result in pool if name in result.added)
              for name in (TRACK_AUDIO, TRACK_SUB, TRACK_COVER)}
    lines.append(f"本次并入：{TRACK_AUDIO} {counts[TRACK_AUDIO]} 个，"
                 f"{TRACK_SUB} {counts[TRACK_SUB]} 个，{TRACK_COVER} {counts[TRACK_COVER]} 个。")

    incomplete = [r for r in merged if r.missing]
    if merged:
        lines.append(f"齐全 {len(merged) - len(incomplete)} 个，不齐全 {len(incomplete)} 个。")
        lines += [f"  · {r.video.name} — 缺 {'/'.join(r.missing)}" for r in incomplete]

    idle = [r for r in skipped if r.missing]
    if idle:
        lines.append("未处理且不齐全的文件：")
        lines += [f"  · {r.video.name} — 缺 {'/'.join(r.missing)}；{r.note}" for r in idle]

    if failed:
        lines.append("失败：")
        lines += [f"  · {r.video.name} — {r.note}" for r in failed]

    if orphans:
        lines.append("孤立附件（没有同名视频，未处理）：")
        for directory, items in orphans.items():
            lines += [f"  · {item.name}    [{directory}]" for item in items]
    return lines


def status_line(result: Result) -> str:
    if result.action == "merged":
        state = "齐全" if not result.missing else "缺" + "/".join(result.missing)
        return f"已合并（{'+'.join(result.added) or '无新增'}）{state}，{result.seconds:.1f}s"
    if result.action == "planned":
        return f"预演，{result.note}"
    if result.action == "skipped":
        return f"跳过：{result.note}"
    return f"失败：{result.note}"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
class Reporter:
    """控制台 + 可选日志文件；step() 是进度（--quiet 时静默），say() 一定输出。"""

    def __init__(self, log_path: Optional[Path] = None, quiet: bool = False) -> None:
        self._quiet = quiet
        self._lock = threading.Lock()
        self._stream = None
        if log_path is not None:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                self._stream = log_path.open("a", encoding="utf-8", buffering=1)   # 追加，不覆盖历史
                self._stream.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
            except OSError as exc:
                print(f"[警告] 无法写入日志 {log_path}：{exc}")

    def _write(self, message: str) -> None:
        with self._lock:
            try:
                print(message, flush=True)
            except OSError:
                pass                        # 控制台被关掉 / 管道断开时不要打断处理
            if self._stream is not None:
                try:
                    self._stream.write(message + "\n")
                except OSError:
                    pass

    def step(self, message: str) -> None:
        if not self._quiet:
            self._write(message)

    def say(self, message: str) -> None:
        self._write(message)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()


@dataclass
class Context:
    tools: Tools
    reporter: Reporter
    dry_run: bool = False
    keep: bool = False
    purge: bool = False
    with_title: bool = True
    sub_lang: str = "auto"

    def log(self, message: str) -> None:
        self.reporter.step(message)


def collect_videos(paths: Sequence[Path], recursive: bool = False) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_file():
            if path.suffix.lower() not in VIDEO_EXTS:
                raise ValueError(f"不是目录也不是支持的视频文件（{'/'.join(VIDEO_EXTS)}）：{path}")
            found.append(path)
        elif path.is_dir():
            entries = path.rglob("*") if recursive else path.iterdir()
            found += [item for item in sorted(entries)
                      if item.is_file() and item.suffix.lower() in VIDEO_EXTS
                      and not item.name.startswith(".")]
        else:
            raise ValueError(f"不是目录也不是支持的视频文件（{'/'.join(VIDEO_EXTS)}）：{path}")

    unique: dict[str, Path] = {}
    for item in found:
        unique.setdefault(str(item).casefold(), item)
    return list(unique.values())


def find_orphans(directories: Iterable[Path], videos: Sequence[Path]) -> dict[Path, list[Path]]:
    stems = {unicodedata.normalize("NFC", video.stem).casefold() for video in videos}

    def claimed(stem: str, suffix: str) -> bool:
        if stem in stems:
            return True
        # 语言后缀字幕（x.en.vtt / x_zh.json）也算被对应视频“认领”。
        if suffix not in SUB_EXTS:
            return False
        for separator in (".", "_"):
            if separator in stem:
                base, tag = stem.rsplit(separator, 1)
                if base in stems and _language_tag(tag):
                    return True
        return False

    result: dict[Path, list[Path]] = {}
    for directory in sorted({path.resolve() for path in directories}):
        if not directory.is_dir():
            continue
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        items = []
        for item in entries:
            if not item.is_file() or item.suffix.lower() not in SIDECAR_EXTS:
                continue
            if item.name.startswith("."):
                continue
            stem = unicodedata.normalize("NFC", item.stem).casefold()
            if not claimed(stem, item.suffix.lower()):
                items.append(item)
        if items:
            result[directory] = items
    return result


def run(paths: Sequence[Path], ctx: Context, jobs: int, recursive: bool = False) -> int:
    videos = collect_videos(paths, recursive)
    if not videos:
        ctx.log(f"没有找到可处理的视频文件（{'/'.join(VIDEO_EXTS)}）。")
        return 0

    ctx.log(f"== merge_videos {VERSION} ==")
    ctx.log(f"目标：{paths[0] if len(paths) == 1 else '多个路径'}    共 {len(videos)} 个视频")
    if ctx.dry_run:
        ctx.log("模式：预演（只分析，不写入）")

    def work(video: Path) -> Result:
        try:
            return process_video(ctx, video)
        except Exception as exc:                       # 兜底：任何未预期异常只影响单个文件
            return Result(video, "failed", Probe().tracks, sidecar_marks(Sidecars()), [], [],
                          0.0, f"未预期错误：{exc!r}")

    results: list[Result] = []
    if jobs <= 1:
        for index, video in enumerate(videos, 1):
            ctx.log(f"[{index}/{len(videos)}] {video.name}")
            results.append(work(video))
            ctx.log(f"      → {status_line(results[-1])}")
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for index, result in enumerate(pool.map(work, videos), 1):
                results.append(result)
                ctx.log(f"[{index}/{len(videos)}] {result.video.name} → {status_line(result)}")

    ctx.log("")
    for line in render_table(results):
        ctx.log(line)
    for line in render_summary(results, find_orphans({video.parent for video in videos}, videos),
                               ctx.dry_run):
        ctx.reporter.say(line)
    return 1 if any(result.action == "failed" for result in results) else 0


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
LAUNCHER_HINTS = {
    "1": "[退出码 1] 有文件未能合并，源文件未改动。详见上方汇总。",
    "2": "[退出码 2] 参数或运行环境有问题。详见上方错误信息。",
    "130": "[退出码 130] 运行被中断，源文件未改动。",
}


def emit_hint(code: str) -> None:
    """启动器专用：把退出码翻成一句中文提示（.bat 保持纯 ASCII，中文全在这里打印）。"""
    text = LAUNCHER_HINTS.get(code) or f"[退出码 {code}] 运行结束。"
    try:
        print(text, flush=True)
    except OSError:
        pass


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="merge_videos.py",
        description="批量合并视频与同名音频、字幕、封面（.mp4/.m4v/.mkv），补齐视频缺的轨道。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  merge_videos.py .                      处理当前目录\n"
            "  merge_videos.py -n D:\\Videos           只分析，不写入\n"
            "  merge_videos.py -j 4 .                 4 个文件并行\n"
        ),
    )
    parser.add_argument("paths", nargs="*", help="目录或单个视频（.mp4/.m4v/.mkv）；留空表示脚本所在目录")
    parser.add_argument("-n", "--dry-run", action="store_true", help="只分析，不写文件")
    parser.add_argument("-k", "--keep", action="store_true", help="保留全部源文件")
    parser.add_argument("-p", "--purge", action="store_true", help="连未使用的侧车一起删")
    parser.add_argument("-j", "--jobs", type=int, default=1, help="并行数，默认 1")
    parser.add_argument("-R", "--recursive", action="store_true", help="递归处理子目录")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出汇总")
    parser.add_argument("--no-title", action="store_true", help="不写入 title 元数据")
    parser.add_argument("--sub-lang", default="auto",
                        help="无语言后缀的新增字幕语言标记：auto 自动识别，或 chi/eng/jpn…")
    parser.add_argument("--ffmpeg", help="ffmpeg 路径")
    parser.add_argument("--ffprobe", help="ffprobe 路径")
    parser.add_argument("--log", help="同时写入日志文件")
    parser.add_argument("--no-pause", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--hint", metavar="CODE", help=argparse.SUPPRESS)
    parser.add_argument("-V", "--version", action="version", version=f"merge_videos {VERSION}")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = parse_args(argv)
    if args.hint is not None:
        emit_hint(args.hint)
        return 0

    reporter = Reporter(Path(args.log).expanduser().resolve() if args.log else None, args.quiet)
    try:
        try:
            tools = Tools.resolve(args.ffmpeg, args.ffprobe)
        except ToolError as exc:
            reporter.say(f"[错误] {exc}")
            return 2

        paths = [Path(path).expanduser().resolve() for path in args.paths]
        if not paths:
            paths = [Path(__file__).resolve().parent]
        missing = [path for path in paths if not path.exists()]
        if missing:
            for path in missing:
                reporter.say(f"[错误] 路径不存在：{path}")
            return 2

        ctx = Context(tools=tools, reporter=reporter, dry_run=args.dry_run, keep=args.keep,
                      purge=args.purge, with_title=not args.no_title, sub_lang=args.sub_lang)
        try:
            code = run(paths, ctx, max(1, args.jobs), args.recursive)
        except ValueError as exc:
            reporter.say(f"[错误] {exc}")
            return 2
        except KeyboardInterrupt:
            reporter.say("\n已中断。")
            return 130

        if args.quiet:
            reporter.say("")
        reporter.say(f"完成于 {datetime.now():%Y-%m-%d %H:%M:%S}")
        return code
    finally:
        reporter.close()


if __name__ == "__main__":
    sys.exit(main())
