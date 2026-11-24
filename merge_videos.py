#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge_videos.py -- 批量合并分离的视频、音频、字幕与封面。

扫描目标目录里的视频（.mp4/.m4v/.mkv），用 ffprobe 读出每个视频**已有**的
音轨 / 字幕轨 / 封面，再在目录里找同名的侧车文件（.m4a/.aac/.mp3/.opus/.flac/.ogg/.wav、
.ass/.ssa/.srt/.json/.vtt、.jpg/.jpeg/.png/.webp/.avif/.bmp/.gif/.tif/.tiff）。
只补“视频缺、目录有”的那一部分，一次 ffmpeg 调用完成封装，校验通过后原子替换原视频，
失败则原样保留所有源文件。最后逐条汇报：哪个视频合并了、补了哪几轨、是否齐全。

容器差异由 CONTAINERS 表集中管理：MP4 家族（.mp4/.m4v）字幕转 mov_text、
音频只接受 AAC/MP3/AC3/E-AC3/ALAC（其它编码跳过并提示改用 .mkv 或先转码）、
封面写 attached_pic；Matroska（.mkv）字幕与音频都原样 copy（PGS/ASS/Opus/FLAC 均可），
封面写成附件。
不收录 .mov：实测 ffmpeg 的 mov muxer 会静默丢弃 attached_pic 封面（返回 0 但产物
没有封面流），无法保证“只补缺轨”的承诺，遇到时按不支持的容器报错。

用法:
    merge_videos.py [路径 ...] [选项]

    路径可以是目录（处理其中的视频），也可以是单个视频文件；不带路径时处理脚本所在目录。

选项:
    -n, --dry-run       只分析并打印计划，不写任何文件
    -k, --keep          成功后也保留全部源文件（默认删除已并入的侧车）
    -p, --purge         额外删除“视频已有该轨、未使用”的侧车文件
    -j, --jobs N        并行处理的文件数（默认 1）
    -q, --quiet         只输出汇总
        --no-title      不写入 title 元数据（默认把文件名写进 title）
        --ffmpeg PATH   指定 ffmpeg 可执行文件
        --ffprobe PATH  指定 ffprobe 可执行文件
        --log FILE      同时把输出写入日志文件
        --no-pause      由启动器使用：结束不暂停
    -h, --help          显示本帮助
    -V, --version       显示版本

退出码: 0 全部成功或无需处理；1 有文件失败；2 参数或环境错误。
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
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence

VERSION = "1.5.0"

AUDIO_EXTS = (".m4a", ".aac", ".mp3", ".opus", ".flac", ".ogg", ".wav")
# 入口白名单 = "ffmpeg 能当文本/图形字幕读出来"的格式；不在这里的后缀一律不发现。
# .ssa 与 .ass 是同一套解析器，只是文件头版本不同（v4.00 vs v4.00+），必须成对放行。
SUB_EXTS = (".ass", ".ssa", ".srt", ".json", ".vtt")
# 封面只放 ffmpeg 常见可解码的静态图；HEIC / JXL 依赖编译选项，故意不放行。
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".gif", ".tif", ".tiff")
JPEG_EXTS = (".jpg", ".jpeg")


@dataclass(frozen=True)
class Container:
    """输出容器的差异只放在这张表里，命令拼装读它的字段。

    subtitle_codec: mp4 只认 mov_text；matroska 一律 copy（PGS/ASS 原样保留）。
    cover_mode:     mp4 把封面当 attached_pic 视频流；matroska 必须写成 -attach 附件，
                    因为 ffmpeg 重新 mux 时会把“视频流形式的封面”降级成普通 1 帧视频轨。
    muxer_args:     只对 mp4/m4v 有意义的封装参数（+faststart）。
    audio_codecs:   容器能直接 copy 的音频编码；空元组表示不限制（matroska）。
                    mp4 收 opus/flac/vorbis/pcm 时 muxer 可能不报错，但常见播放器
                    不认，所以入口按“主流播放器可播”的白名单过滤并在 dry-run 就说明。
    """

    suffix: str
    kind: str                       # "mp4" | "matroska"
    subtitle_codec: str             # "mov_text" | "copy"
    cover_mode: str                 # "video" | "attachment"
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
SIDECAR_EXTS = AUDIO_EXTS + SUB_EXTS + COVER_EXTS


def container_for(path: Path) -> Container:
    try:
        return CONTAINERS[path.suffix.lower()]
    except KeyError:
        raise ValueError(f"不支持的视频容器：{path.suffix or '(无扩展名)'}") from None

DURATION_TOLERANCE = 2.0     # 产物与原视频时长允许的差（秒）
MIN_OUTPUT_BYTES = 1024
FFPROBE_TIMEOUT = 180
FFMPEG_TIMEOUT = 6 * 3600
STALE_TEMP_SECONDS = 3600

TRACK_AUDIO = "音频"
TRACK_SUB = "字幕"
TRACK_COVER = "封面"
MARK_SHORT = {TRACK_AUDIO: "音", TRACK_SUB: "字", TRACK_COVER: "封"}


# --------------------------------------------------------------------------- #
# 基础设施
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
    def resolve(cls, ffmpeg: Optional[str], ffprobe: Optional[str]) -> "Tools":
        ff = ffmpeg or os.environ.get("MERGE_FFMPEG") or shutil.which("ffmpeg")
        fp = ffprobe or os.environ.get("MERGE_FFPROBE") or shutil.which("ffprobe")
        if not ff or not fp:
            raise ToolError(
                "未找到 ffmpeg / ffprobe。请安装并加入 PATH，"
                "或用 --ffmpeg / --ffprobe 指定路径。"
            )
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


def one_line(text: str, limit: int = 400) -> str:
    flat = " ".join(text.split())
    return flat[-limit:] if len(flat) > limit else flat


_ADDR_RE = re.compile(r"\[[^\]]*@ [0-9a-zA-Z]+\]\s*")


def brief_error(text: str, target: Optional[Path] = None, limit: int = 160) -> str:
    """把 ffmpeg/ffprobe 的长错误压成一句：去地址前缀、去绝对路径、截尾。"""
    flat = " ".join(text.split())
    flat = _ADDR_RE.sub("", flat)
    if target is not None:
        flat = flat.replace(str(target), target.name)
    if len(flat) > limit:
        flat = "…" + flat[-limit:]
    return flat.strip()


# --------------------------------------------------------------------------- #
# 探测：每个文件一次 ffprobe，拿全部轨道信息
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SubStream:
    index: int
    codec: str


@dataclass(frozen=True)
class CoverStream:
    index: int
    codec: str
    filename: str = ""
    mimetype: str = ""


@dataclass(frozen=True)
class Attachment:
    """要写进 matroska 的附件（-attach）：已落盘的封面或原有字体。"""

    path: Path
    filename: str
    mimetype: str


@dataclass(frozen=True)
class Probe:
    duration: float
    has_video: bool
    has_audio: bool
    has_subs: bool
    has_cover: bool
    audio_count: int
    sub_count: int
    video_codec: str
    audio_codec: str
    sub_codec: str
    cover_codec: str
    stream_count: int
    # 绝对流号（ffprobe streams 数组下标），-map 0:<idx> 直接用它。
    # 正片 = 第一条非 attached_pic 的视频流；封面 = 每条 attached_pic 视频流。
    # 不能假设“正片永远是 0:v:0、封面永远是 0:v:1”：第三方 muxer 可以换轨道顺序。
    main_video_index: int = 0
    sub_streams: tuple[SubStream, ...] = ()
    cover_streams: tuple[CoverStream, ...] = ()
    attachment_count: int = 0          # matroska 的字体等附件数量（-map 0:t 用）
    sub_duration: float = 0.0          # 字幕轨自身的时长，0 表示"有轨但没内容"

    @property
    def tracks(self) -> dict[str, bool]:
        return {TRACK_AUDIO: self.has_audio, TRACK_SUB: self.has_subs, TRACK_COVER: self.has_cover}

    @property
    def cover_indices(self) -> tuple[int, ...]:
        return tuple(cover.index for cover in self.cover_streams)


def probe(tools: Tools, path: Path) -> Probe:
    cmd = [
        tools.ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    proc = run_command(cmd, FFPROBE_TIMEOUT)
    if proc.returncode != 0:
        raise ProbeError(one_line(decode(proc.stderr)) or "ffprobe 返回非零")
    try:
        data = json.loads(decode(proc.stdout) or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe 输出不是合法 JSON：{exc}") from exc

    streams = data.get("streams")
    if not isinstance(streams, list):
        raise ProbeError("ffprobe 输出缺少 streams")
    fmt = data.get("format") or {}

    def kind(stream: dict) -> str:
        return str(stream.get("codec_type") or "")

    def attached(stream: dict) -> bool:
        return int((stream.get("disposition") or {}).get("attached_pic") or 0) == 1

    video_streams = [(index, stream) for index, stream in enumerate(streams) if kind(stream) == "video"]
    videos = [stream for _, stream in video_streams if not attached(stream)]
    audios = [s for s in streams if kind(s) == "audio"]
    subs = [s for s in streams if kind(s) == "subtitle"]
    covers = [stream for _, stream in video_streams if attached(stream)]
    main_video_index = next((index for index, stream in video_streams if not attached(stream)), 0)

    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    def codec(item: Optional[dict]) -> str:
        return str((item or {}).get("codec_name") or "")

    def stream_duration(item: Optional[dict]) -> float:
        """优先读 duration，读不到再看 matroska 的 DURATION tag。

        输出里带附件（-map 0:t?）时，ffprobe 会把同文件的字幕流 duration 报成 N/A，
        但 DURATION tag 仍是准的；不兜底就会把正常字幕误判成“空字幕”。
        """
        stream = item or {}
        try:
            value = float(stream.get("duration") or 0.0)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
        tags = stream.get("tags") or {}
        stamp = str(tags.get("DURATION") or tags.get("duration") or "")
        try:
            hours, minutes, seconds = stamp.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except (TypeError, ValueError):
            return 0.0

    def tag(stream: dict, name: str) -> str:
        tags = stream.get("tags") or {}
        return str(tags.get(name) or tags.get(name.upper()) or "")

    sub_streams = tuple(
        SubStream(index, codec(stream))
        for index, stream in enumerate(streams)
        if kind(stream) == "subtitle"
    )
    cover_streams = tuple(
        CoverStream(index, codec(stream), tag(stream, "filename"), tag(stream, "mimetype"))
        for index, stream in video_streams
        if attached(stream)
    )
    attachment_count = sum(1 for stream in streams if kind(stream) == "attachment")

    return Probe(
        duration=duration,
        has_video=bool(videos),
        has_audio=bool(audios),
        has_subs=bool(subs),
        has_cover=bool(covers),
        audio_count=len(audios),
        sub_count=len(subs),
        video_codec=codec(videos[0] if videos else None),
        audio_codec=codec(audios[0] if audios else None),
        sub_codec=codec(subs[0] if subs else None),
        cover_codec=codec(covers[0] if covers else None),
        stream_count=len(streams),
        main_video_index=main_video_index,
        sub_streams=sub_streams,
        cover_streams=cover_streams,
        attachment_count=attachment_count,
        sub_duration=stream_duration(subs[0] if subs else None),
    )


# --------------------------------------------------------------------------- #
# 侧车文件发现
# --------------------------------------------------------------------------- #
def probe_audio_codec(tools: Tools, path: Path) -> str:
    """读侧车音频第一条音轨的编码名；文件损坏或没有音轨时抛 ProbeError。"""
    cmd = [
        tools.ffprobe, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name", "-of", "json", str(path),
    ]
    proc = run_command(cmd, FFPROBE_TIMEOUT)
    if proc.returncode != 0:
        raise ProbeError(one_line(decode(proc.stderr)) or "ffprobe 返回非零")
    try:
        streams = json.loads(decode(proc.stdout) or "{}").get("streams") or []
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe 输出不是合法 JSON：{exc}") from exc
    codec = str(streams[0].get("codec_name") or "") if streams else ""
    if not codec:
        raise ProbeError("音频文件里没有可用的音轨")
    return codec


@dataclass(frozen=True)
class Sidecars:
    audio: Optional[Path] = None
    subtitle: Optional[Path] = None
    cover: Optional[Path] = None

    @property
    def tracks(self) -> dict[str, Optional[Path]]:
        return {TRACK_AUDIO: self.audio, TRACK_SUB: self.subtitle, TRACK_COVER: self.cover}

    def paths(self) -> list[Path]:
        return [p for p in self.tracks.values() if p is not None]


def _usable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


@lru_cache(maxsize=256)
def directory_sidecar_index(directory: str) -> tuple[tuple[str, str, str], ...]:
    """目录内文件的 (NFC 化 stem, 小写后缀, 文件名) 索引。

    macOS / 某些解压工具写出的文件名是 NFD（é 拆成 e + 组合符），
    与 Windows 上的 NFC 视频名逐字符比较会不相等，这里做一次规范化回退。
    """
    entries: list[tuple[str, str, str]] = []
    try:
        for item in Path(directory).iterdir():
            if _usable(item):
                entries.append((unicodedata.normalize("NFC", item.stem), item.suffix.lower(), item.name))
    except OSError:
        return ()
    return tuple(entries)


def pick_sidecar(video: Path, exts: Iterable[str]) -> Optional[Path]:
    """按优先级找第一个存在且非空的同名文件。注意不能改扩展名以外的部分。"""
    for ext in exts:
        candidate = video.with_name(video.stem + ext)
        if _usable(candidate):
            return candidate

    # 规范化回退：同目录里找 NFC(stem) 相同、后缀匹配的文件
    target = unicodedata.normalize("NFC", video.stem)
    wanted = {ext.lower() for ext in exts}
    for nfc_stem, suffix, name in directory_sidecar_index(str(video.parent)):
        if suffix in wanted and nfc_stem == target:
            candidate = video.parent / name
            if _usable(candidate):
                return candidate
    return None


def find_sidecars(video: Path) -> Sidecars:
    return Sidecars(
        audio=pick_sidecar(video, AUDIO_EXTS),
        subtitle=pick_sidecar(video, SUB_EXTS),
        cover=pick_sidecar(video, COVER_EXTS),
    )


# --------------------------------------------------------------------------- #
# 预处理：JSON 字幕转 SRT、非 JPEG 封面转 JPEG
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


# BOM 是"这是文本"的强信号；只按 BOM 认 UTF-16/UTF-32，不做统计猜测。
_TEXT_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),      # 必须排在 utf-16-le 前面
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def _looks_binary(raw: bytes) -> bool:
    """没有 BOM 且 NUL 占比很高时按二进制处理，避免把 .sup 之类当文本字幕。"""
    sample = raw[:8192]
    if not sample or any(sample.startswith(bom) for bom, _ in _TEXT_BOMS):
        return False
    nuls = sample.count(b"\x00")
    return nuls > 0 and nuls / len(sample) > 0.10


def detect_text_encoding(raw: bytes) -> str:
    """按 BOM → UTF-8 → GB18030 判编码；全失败时返回 utf-8 交给 strict 解码报错。"""
    for bom, encoding in _TEXT_BOMS:
        if raw.startswith(bom):
            return encoding
    for encoding in ("utf-8", "gb18030"):
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8"


def decode_text(raw: bytes, strict: bool = False) -> str:
    """按 BOM/UTF-8/GB18030 解码；strict=False 时用替换字符兜底。

    中文 .srt 很多是 GBK 编码，直接按 UTF-8 读会得到乱码或让 ffmpeg 解析失败。
    """
    encoding = detect_text_encoding(raw)
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        if strict:
            raise ValueError(
                "字幕编码无法识别（已尝试 UTF-8 / UTF-16(BOM) / GB18030）；"
                "请把字幕另存为 UTF-8 后重试"
            ) from exc
        return raw.decode("utf-8", "replace")
    # utf-16-le / utf-32-le 这类显式端序解码不会吞掉 BOM，自己去掉，
    # 否则 json.loads 会报 "Unexpected UTF-8 BOM"，时间轴首行也会多一个字符。
    return text[1:] if text.startswith("\ufeff") else text


def load_cues(path: Path) -> list[dict]:
    text = decode_text(path.read_bytes())
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("body", "subtitles", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise ValueError("JSON 顶层既不是数组，也没有 body/subtitles/data 数组")
    cues = [c for c in data if isinstance(c, dict) and "from" in c and "to" in c]
    if not cues:
        raise ValueError("JSON 里没有 from/to 字幕条目")
    return cues


def json_to_srt(source: Path, target: Path) -> int:
    """把哔哩哔哩字幕 JSON 转成 SRT，返回写入的条数。"""
    blocks: list[str] = []
    for cue in load_cues(source):
        try:
            start = int(round(float(cue["from"]) * 1000))
            end = int(round(float(cue["to"]) * 1000))
        except (TypeError, ValueError):
            continue
        if end <= start:
            end = start + 200
        text = clean_cue_text(cue.get("content"))
        text = text.replace("-->", "- ->")       # 避免字幕正文伪造时间行
        if not text:
            continue
        index = len(blocks) + 1
        blocks.append(f"{index}\n{format_timestamp(start)} --> {format_timestamp(end)}\n{text}\n")
    if not blocks:
        raise ValueError("没有可用的字幕条目")
    target.write_text("\n".join(blocks), encoding="utf-8", newline="\n")
    return len(blocks)


_SRT_CUE_RE = re.compile(
    r"^\s*\d{2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{1,3}(?P<rest>.*)$",
    re.MULTILINE,
)
# WebVTT 允许省略小时段（MM:SS.mmm）；SRT 规范要求 HH:MM:SS,mmm，所以两条正则必须分开。
_VTT_CUE_RE = re.compile(
    r"^\s*(?:\d{2}:)?\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*(?:\d{2}:)?\d{2}:\d{2}[,.]\d{1,3}(?P<rest>.*)$",
    re.MULTILINE,
)
# 时间轴行末尾的 cue 设置（align:start / position:0% / line:0 ...）不是字幕正文。
_VTT_SETTING_RE = re.compile(r"^(?:align|line|position|size|region|vertical):\S+$", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_ASS_TAG_RE = re.compile(r"\{[^}]*\}")


def _cue_pattern(suffix: str) -> "re.Pattern[str]":
    return _VTT_CUE_RE if suffix.lower() == ".vtt" else _SRT_CUE_RE


def _visible_text(value: str) -> str:
    """去掉 HTML / ASS 覆盖标签与强制换行后，真正会显示出来的文字。"""
    text = _ASS_TAG_RE.sub("", _HTML_TAG_RE.sub("", value))
    return text.replace("\\N", " ").replace("\\n", " ").strip()


def _cue_text(rest: str) -> str:
    """时间轴同一行里，时间戳之后的部分；先剔除 cue 设置再判断有没有文字。"""
    tokens = [token for token in rest.split() if not _VTT_SETTING_RE.match(token)]
    return _visible_text(" ".join(tokens))


def ensure_playable(path: Path, suffix: str) -> None:
    """粗校验字幕文件里确实有可解析的条目。

    ffmpeg 对"格式不对但扩展名对"的文件不会报错，只会产出一条空字幕轨，
    结果就是视频合并成功、字幕却什么都没留下——这里提前拦住。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"字幕无法读取：{exc}") from exc
    if _looks_binary(raw):
        raise ValueError("字幕文件看起来是二进制（不是文本字幕），请确认扩展名与内容一致")
    text = decode_text(raw)
    suffix = suffix.lower()
    if suffix in (".srt", ".vtt"):
        if not _cue_pattern(suffix).search(text):
            raise ValueError("字幕里没有可识别的时间轴（不是有效的 SRT/VTT）")
        if not _has_subtitle_text(text, suffix):
            raise ValueError("字幕里只有时间轴、没有文字")
    elif suffix in (".ass", ".ssa"):
        if "Dialogue:" not in text:
            raise ValueError("ASS/SSA 字幕里没有 Dialogue 行")
        if not _has_subtitle_text(text, suffix):
            raise ValueError("ASS/SSA 字幕的 Dialogue 行没有文字")
    elif suffix == ".json":
        raise ValueError("JSON 字幕应当走转换流程")
    else:
        # 入口白名单已经过滤过一遍，这里再拒绝一次是纵深防御：
        # 以后加字幕格式如果忘了补校验分支，至少不会把未知内容静默塞进 ffmpeg。
        raise ValueError(f"不支持的字幕格式：{suffix or '(无扩展名)'}")


def _has_subtitle_text(text: str, suffix: str = "") -> bool:
    """判断字幕里是否有"会显示出来"的文字。

    只认正文：时间轴、序号、cue identifier、NOTE/STYLE/REGION 块、cue 设置都不算；
    否则"只有时间轴"的空字幕会被当成正常字幕塞进 ffmpeg，产物是一条空轨，
    而报告会谎称"字幕已并入"。
    """
    if suffix.lower() in (".ass", ".ssa"):
        for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw_line.strip()
            if not line.startswith("Dialogue:"):
                continue
            # 形如 Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\an8}正文
            parts = line.split(",", 9)
            if len(parts) >= 10 and _visible_text(parts[9]):
                return True
        return False

    vtt = suffix.lower() == ".vtt"
    cue_re = _cue_pattern(suffix)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        raw = lines[index]
        line = raw.strip()
        if not line:
            index += 1
            continue
        if vtt and (line.upper().startswith("WEBVTT")
                    or line.upper().startswith("NOTE")
                    or line.split()[0].upper() in ("STYLE", "REGION")):
            # NOTE / STYLE / REGION 是块内容，跳到空行为止。
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        match = cue_re.match(raw)
        if match is None:
            index += 1          # 序号、cue identifier、其它注释行，都不算正文
            continue
        if _cue_text(match.group("rest") or ""):
            return True
        index += 1
        # 正文可以写在时间轴下面（SRT 与 VTT 都允许），直到空行或下一条时间轴。
        while index < len(lines):
            following = lines[index].strip()
            if not following or cue_re.match(following):
                break
            if _visible_text(following):
                return True
            index += 1
    return False


def prepare_subtitle(tools: Tools, subtitle: Path, workdir: Path) -> tuple[Path, Optional[Path]]:
    """返回 (给 ffmpeg 用的字幕路径, 需要额外清理的临时文件)。

    ffmpeg 按 UTF-8 读文本字幕，GBK 的 .srt/.vtt 会乱码，所以先探测编码，
    需要时转存成临时 UTF-8 文件；.json 则整份转成 SRT。
    """
    if subtitle.suffix.lower() == ".json":
        target = workdir / "subtitle.srt"
        count = json_to_srt(subtitle, target)
        if count <= 0:
            raise ValueError("JSON 字幕转换后为空")
        ensure_playable(target, ".srt")
        return target, target

    raw = subtitle.read_bytes()
    if _looks_binary(raw):
        raise ValueError("字幕文件看起来是二进制（不是文本字幕），请确认扩展名与内容一致")
    encoding = detect_text_encoding(raw)
    text = decode_text(raw, strict=True)        # UTF-8 / UTF-16(BOM) / GB18030

    had_nul = "\x00" in text
    if had_nul:
        text = text.replace("\x00", "")         # NUL 会让 SRT 解析器整条失效

    if encoding in ("utf-8", "utf-8-sig") and not had_nul:
        ensure_playable(subtitle, subtitle.suffix)
        return subtitle, None                    # 本来就是 UTF-8，直接用原文件

    target = workdir / ("subtitle" + subtitle.suffix.lower())
    target.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    ensure_playable(target, subtitle.suffix)
    return target, target


def prepare_cover(tools: Tools, cover: Path, workdir: Path) -> tuple[Path, Optional[Path]]:
    """封面统一转成 mjpeg 临时 JPEG：mp4 的 attached_pic 与 mkv 的 -attach 都用它。"""
    if cover.suffix.lower() in JPEG_EXTS:
        return cover, None
    target = workdir / "cover.jpg"
    cmd = [
        tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(cover),
        "-frames:v", "1", "-update", "1", "-q:v", "2",
        str(target),
    ]
    proc = run_command(cmd, FFPROBE_TIMEOUT)
    if proc.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise ValueError(f"封面转换失败：{one_line(decode(proc.stderr))}")
    return target, target


_COVER_EXTRACT = {
    "mjpeg": ("jpg", "image/jpeg"),
    "png": ("png", "image/png"),
    "webp": ("webp", "image/webp"),
    "bmp": ("bmp", "image/bmp"),
}


def extract_covers(tools: Tools, video: Path, pr: Probe,
                   workdir: Path) -> tuple[list[Attachment], list[Path]]:
    """把 mkv 里已有的 attached_pic 落成临时图片，供 -attach 回挂。

    ffmpeg 的 matroska muxer 不能把“视频流形式的封面”再写成附件：直接
    -map 0:v:N 只会得到一条 attached_pic=0 的普通 1 帧视频轨。所以先按流号
    提取成图片，再 -attach；mjpeg/png 等用 -c copy 保留原始字节，失败才转 JPEG。
    """
    attachments: list[Attachment] = []
    temps: list[Path] = []
    for ordinal, cover in enumerate(pr.cover_streams):
        attempts: list[tuple[str, str, list[str], str]] = []
        if cover.codec in _COVER_EXTRACT:
            ext, mime = _COVER_EXTRACT[cover.codec]
            original = Path(cover.filename).name if cover.filename else ""
            attempts.append((ext, mime, ["-c:v", "copy"], original or f"cover{ordinal}.{ext}"))
        attempts.append(("jpg", "image/jpeg", ["-q:v", "2"], f"cover{ordinal}.jpg"))
        error = ""
        for ext, mime, encoding_args, filename in attempts:
            target = workdir / f"cover{ordinal}.{ext}"
            cmd = [
                tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                "-i", str(video), "-map", f"0:{cover.index}", "-frames:v", "1",
                *encoding_args, str(target),
            ]
            proc = run_command(cmd, FFMPEG_TIMEOUT)
            if proc.returncode == 0 and target.is_file() and target.stat().st_size > 0:
                attachments.append(Attachment(target, filename, mime))
                temps.append(target)
                break
            error = one_line(decode(proc.stderr)) or f"退出码 {proc.returncode}"
        else:
            raise ValueError(f"原有封面提取失败：{error}")
    return attachments, temps


# --------------------------------------------------------------------------- #
# 计划
# --------------------------------------------------------------------------- #
# 图像字幕不能转 mov_text；MP4 里出现就丢弃并告知（MKV 可原样 copy）。
BITMAP_SUB_CODECS = frozenset({"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"})


@dataclass
class Plan:
    video: Path
    probe: Probe
    side: Sidecars
    container: Container
    add_audio: Optional[Path] = None
    add_sub: Optional[Path] = None
    add_cover: Optional[Path] = None
    skip_reason: str = ""
    # 已有字幕里真正会写进产物的那些（MP4 下图像字幕被剔除），命令拼装与校验都以它为准。
    kept_subtitles: tuple[SubStream, ...] = ()
    dropped_subtitles: tuple[str, ...] = ()
    covers_kept: bool = True          # mkv 原有封面提取失败时置 False，避免校验误报

    @property
    def additions(self) -> dict[str, Optional[Path]]:
        return {TRACK_AUDIO: self.add_audio, TRACK_SUB: self.add_sub, TRACK_COVER: self.add_cover}

    @property
    def has_work(self) -> bool:
        return bool(self.add_audio or self.add_sub or self.add_cover)

    def expected_tracks(self) -> dict[str, bool]:
        """处理完成后应存在的轨道。"""
        return {
            TRACK_AUDIO: self.probe.has_audio or bool(self.add_audio),
            TRACK_SUB: bool(self.kept_subtitles) or bool(self.add_sub),
            TRACK_COVER: (self.probe.has_cover and self.covers_kept) or bool(self.add_cover),
        }


def make_plan(video: Path, pr: Probe, side: Sidecars, container: Container) -> Plan:
    kept = tuple(
        sub for sub in pr.sub_streams
        if not (container.kind == "mp4" and sub.codec in BITMAP_SUB_CODECS)
    )
    dropped = tuple(sub.codec for sub in pr.sub_streams if sub not in kept)
    plan = Plan(
        video=video,
        probe=pr,
        side=side,
        container=container,
        add_audio=None if pr.has_audio else side.audio,
        add_sub=None if pr.has_subs else side.subtitle,
        add_cover=None if pr.has_cover else side.cover,
        kept_subtitles=kept,
        dropped_subtitles=dropped,
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
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_HANGUL_RE = re.compile(r"[\u1100-\u11ff\uac00-\ud7a3]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ARABIC_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f]")
_HEBREW_RE = re.compile(r"[\u0590-\u05ff]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_THAI_RE = re.compile(r"[\u0e00-\u0e7f]")


def guess_subtitle_language(text: str) -> str:
    """按字符集猜字幕语言（ISO 639-2/B 三字码）。

    日文里有汉字，所以先看假名；韩文有谚文；其余按占比依次判断。
    猜错也只是元数据不对，不影响画面和文字；可以用 --sub-lang 强制指定。
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return "und"
    total = len(letters)
    ratio = lambda pattern: len(pattern.findall(text)) / total
    if ratio(_KANA_RE) >= 0.02:
        return "jpn"
    if ratio(_HANGUL_RE) >= 0.05:
        return "kor"
    if ratio(_CJK_RE) >= 0.05:
        return "chi"
    if ratio(_ARABIC_RE) >= 0.05:
        return "ara"
    if ratio(_HEBREW_RE) >= 0.05:
        return "heb"
    if ratio(_CYRILLIC_RE) >= 0.05:
        return "rus"
    if ratio(_THAI_RE) >= 0.05:
        return "tha"
    return "eng"


def build_command(tools: Tools, plan: Plan, subtitle: Optional[Path],
                  cover: Optional[Path], dest: Path, with_title: bool,
                  sub_lang: str = "auto",
                  attachments: Sequence[Attachment] = ()) -> list[str]:
    """组织 ffmpeg 参数。

    关键点：视频里**已有**的音轨 / 字幕 / 封面必须显式保留，否则只 map 新加的
    那一路会把它们丢掉。视频流一律用绝对流号（0:<idx>）定位：正片 = 第一条非
    attached_pic 的视频流，封面 = 每条 attached_pic 的视频流——不假设它们是
    v:0 / v:1（第三方 muxer 可以换轨道顺序）。输出顺序固定为 [正片, 封面...]。

    容器差异（见 CONTAINERS）：
    - MP4 家族：字幕必须转 mov_text（mp4 不接受 PGS/ASS），封面是 attached_pic 视频流；
    - Matroska：字幕 copy（PGS/ASS/WebVTT 原样保留），封面必须用 -attach 写成附件，
      因为 mkv 的“视频流封面”在 remux 后会退化成普通 1 帧视频轨。
    """
    pr = plan.probe
    container = plan.container
    inputs: list[tuple[Path, str]] = [(plan.video, "video")]
    index = {"video": 0}

    if plan.add_audio:
        index["audio"] = len(inputs)
        inputs.append((plan.add_audio, "audio"))
    if subtitle:
        index["sub"] = len(inputs)
        inputs.append((subtitle, "sub"))
    if cover and container.cover_mode == "video":
        index["cover"] = len(inputs)
        inputs.append((cover, "cover"))

    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    for path, _ in inputs:
        cmd += ["-i", str(path)]

    cmd += ["-map", f"0:{pr.main_video_index}"]     # 正片：第一条非 attached_pic 的视频流
    if container.cover_mode == "video":
        for cover_index in pr.cover_indices:        # 原封面：可能不止一条，逐条按绝对下标 map
            cmd += ["-map", f"0:{cover_index}"]
    if pr.audio_count:
        cmd += ["-map", "0:a"]                     # 原有全部音轨
    for sub in plan.kept_subtitles:                # 原有字幕逐条 map（MP4 下图像字幕已剔除）
        cmd += ["-map", f"0:{sub.index}"]
    if container.kind == "matroska":
        cmd += ["-map", "0:t?"]                    # 字体等附件；? 让没有附件时也不报错
    if plan.add_audio:
        cmd += ["-map", f"{index['audio']}:a:0"]
    if subtitle:
        cmd += ["-map", f"{index['sub']}:s:0"]
    if cover and container.cover_mode == "video":
        cmd += ["-map", f"{index['cover']}:v:0"]

    cmd += ["-c:v", "copy", "-c:a", "copy"]
    if plan.kept_subtitles or subtitle:
        cmd += ["-c:s", container.subtitle_codec]
    if subtitle:
        # 新增字幕排在原有字幕之后；语言用 --sub-lang，默认按文字内容识别
        language = sub_lang
        if language == "auto":
            try:
                language = guess_subtitle_language(subtitle.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                language = "und"
        cmd += ["-metadata:s:s:%d" % len(plan.kept_subtitles), f"language={language}"]

    if container.cover_mode == "video":
        # 输出视频流 [正片, 封面...]：正片固定 v:0，封面从 v:1 开始逐条打 attached_pic。
        cover_outputs = len(pr.cover_indices) + (1 if cover else 0)
        for offset in range(cover_outputs):
            cmd += [f"-disposition:v:{offset + 1}", "attached_pic"]
    else:
        # mkv：附件（字体）先由 -map 0:t? 带过去，新附件从 attachment_count 开始编号。
        attach_index = pr.attachment_count
        for attachment in attachments:
            cmd += ["-attach", str(attachment.path),
                    f"-metadata:s:t:{attach_index}", f"filename={attachment.filename}",
                    f"-metadata:s:t:{attach_index}", f"mimetype={attachment.mimetype}"]
            attach_index += 1
        if cover:
            cmd += ["-attach", str(cover),
                    f"-metadata:s:t:{attach_index}", "filename=cover.jpg",
                    f"-metadata:s:t:{attach_index}", "mimetype=image/jpeg"]

    cmd += ["-map_metadata", "0"]
    cmd += list(container.muxer_args)              # 只有 mp4/m4v 需要 +faststart
    cmd += ["-max_muxing_queue_size", "1024"]      # 大文件 remux 的 interleave 缓冲
    if with_title:
        cmd += ["-metadata", f"title={plan.video.stem}"]
    cmd.append(str(dest))
    return cmd


def verify_output(tools: Tools, dest: Path, plan: Plan) -> list[str]:
    """校验产物：轨道、时长、体积。只要求“计划里应当有的”轨道存在。"""
    try:
        pr = probe(tools, dest)
    except (ProbeError, ToolError) as exc:
        return [f"产物无法解析：{exc}"]

    expected = plan.expected_tracks()
    problems: list[str] = []
    if not pr.has_video:
        problems.append("缺视频流")
    if expected[TRACK_AUDIO] and not pr.has_audio:
        problems.append("缺音轨")
    if expected[TRACK_SUB] and not pr.has_subs:
        problems.append("缺字幕轨")
    if plan.add_sub is not None and pr.has_subs and pr.sub_duration <= 0.0:
        problems.append("字幕轨是空的（源字幕没有可解析的条目）")
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
# 结果
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    video: Path
    action: str                      # merged / planned / skipped / failed
    before: dict[str, bool]
    available: dict[str, bool]
    added: list[str]
    missing: list[str]
    seconds: float
    note: str = ""
    deleted: list[Path] = field(default_factory=list)


EMPTY_PROBE = Probe(0.0, True, False, False, False, 0, 0, "", "", "", "", 0)


def marks(tracks: dict[str, bool]) -> str:
    return " ".join(f"{MARK_SHORT[name]}{'✓' if ok else '✗'}" for name, ok in tracks.items())


def sidecar_marks(side: Sidecars) -> dict[str, bool]:
    return {name: path is not None for name, path in side.tracks.items()}


def clean_stale_temps(directory: Path, stem: str, suffix: str) -> None:
    """清理上次异常中断留下的临时封装文件（超过 1 小时的才算，避免误删并发实例的）。

    临时文件后缀跟目标容器一致（.mkv 不会被 .mp4 的清理逻辑漏掉）。
    """
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


def process_video(ctx: "Context", video: Path) -> Result:
    started = time.perf_counter()
    try:
        container = container_for(video)
    except ValueError as exc:
        return Result(video, "failed", EMPTY_PROBE.tracks, sidecar_marks(Sidecars()),
                      [], [], time.perf_counter() - started, str(exc))
    try:
        pr = probe(ctx.tools, video)
    except (ProbeError, ToolError) as exc:
        return Result(video, "failed", EMPTY_PROBE.tracks, sidecar_marks(Sidecars()),
                      [], [], time.perf_counter() - started, f"视频无法解析：{brief_error(str(exc), video)}")

    side = find_sidecars(video)
    plan = make_plan(video, pr, side, container)
    before = dict(pr.tracks)
    available = sidecar_marks(side)

    # 音频侧车能不能直接 copy 进目标容器，在 plan 阶段就判定（dry-run 也能看见）。
    audio_problem = ""
    if plan.add_audio and container.audio_codecs:
        try:
            codec = probe_audio_codec(ctx.tools, plan.add_audio)
        except (ProbeError, ToolError) as exc:
            audio_problem = f"音频未并入（{brief_error(str(exc), plan.add_audio)}）"
            plan.add_audio = None
        else:
            if codec not in container.audio_codecs:
                audio_problem = (
                    f"音频未并入（{codec} 不适合放进 {container.suffix} 容器；"
                    f"可改用 .mkv，或先把音频转成 AAC/MP3）"
                )
                plan.add_audio = None

    if plan.skip_reason:
        note = plan.skip_reason
        deleted: list[Path] = []
        if not ctx.dry_run:
            clean_stale_temps(video.parent, video.stem, container.suffix)   # 跳过也清残留，否则永远没人收拾
        if ctx.purge and not ctx.dry_run and not ctx.keep and side.paths():
            deleted = remove_files(ctx, side.paths())
            if deleted:
                note += f"；清理 {len(deleted)} 个未使用附件"
        return Result(video, "skipped", before, available, [],
                      [name for name, ok in before.items() if not ok],
                      time.perf_counter() - started, note, deleted)

    if ctx.dry_run:
        added = [name for name, path in plan.additions.items() if path]
        note = ("将并入 " + "+".join(added)) if added else "没有可并入的内容"
        if plan.dropped_subtitles:
            note += f"；原图形字幕（{'/'.join(sorted(set(plan.dropped_subtitles)))}）无法放入 MP4，将丢弃"
        if plan.add_sub and plan.add_sub.suffix.lower() == ".json":
            try:
                note += f"（字幕 {len(load_cues(plan.add_sub))} 条）"
            except (ValueError, OSError) as exc:
                plan.add_sub = None              # 否则“结果”列会声称齐全
                added = [name for name in added if name != TRACK_SUB]
                note = f"预演：字幕 JSON 无法读取（{exc}），其余将并入 " + ("+".join(added) or "无")
        if audio_problem:
            note += "；" + audio_problem
        missing = [name for name, ok in plan.expected_tracks().items() if not ok and name not in added]
        return Result(video, "planned", before, available, added, missing,
                      time.perf_counter() - started, note)

    clean_stale_temps(video.parent, video.stem, container.suffix)
    workdir = Path(tempfile.mkdtemp(prefix="mergevid_"))
    dest = video.with_name(f".{video.stem}.mergetmp.{os.getpid()}{container.suffix}")
    temporary: list[Path] = [dest]
    warnings: list[str] = []
    if audio_problem:
        warnings.append(audio_problem)
    if plan.dropped_subtitles:
        warnings.append(f"原图形字幕（{'/'.join(sorted(set(plan.dropped_subtitles)))}）无法放入 MP4，已丢弃")

    try:
        subtitle: Optional[Path] = None
        cover: Optional[Path] = None
        if plan.add_sub:
            try:
                subtitle, tmp = prepare_subtitle(ctx.tools, plan.add_sub, workdir)
                if tmp:
                    temporary.append(tmp)
            except (ValueError, OSError) as exc:
                warnings.append(f"字幕未并入（{exc}）")
                plan.add_sub = None

        if plan.add_cover:
            try:
                cover, tmp = prepare_cover(ctx.tools, plan.add_cover, workdir)
                if tmp:
                    temporary.append(tmp)
            except (ValueError, OSError) as exc:
                warnings.append(f"封面未并入（{exc}）")
                plan.add_cover = None

        attachments: list[Attachment] = []
        if container.cover_mode == "attachment" and plan.probe.has_cover:
            try:
                attachments, tmp_covers = extract_covers(ctx.tools, video, plan.probe, workdir)
                temporary.extend(tmp_covers)
            except (ValueError, OSError) as exc:
                warnings.append(f"原有封面未能保留（{exc}）")
                plan.covers_kept = False

        if not plan.has_work:
            return Result(video, "skipped", before, available, [],
                          [name for name, ok in before.items() if not ok],
                          time.perf_counter() - started,
                          warnings_note(warnings) or "没有可并入的内容")

        cmd = build_command(ctx.tools, plan, subtitle, cover, dest, ctx.with_title,
                            ctx.sub_lang, attachments)
        proc = run_command(cmd, FFMPEG_TIMEOUT)
        if proc.returncode != 0:
            return Result(video, "failed", before, available, [], [],
                          time.perf_counter() - started,
                          note="ffmpeg 失败：" + (brief_error(decode(proc.stderr), video) or f"退出码 {proc.returncode}")
                          + (("；" + warnings_note(warnings)) if warnings else ""))

        problems = verify_output(ctx.tools, dest, plan)
        if problems:
            return Result(video, "failed", before, available, [], [],
                          time.perf_counter() - started,
                          note="产物校验失败：" + "，".join(problems)
                          + (("；" + warnings_note(warnings)) if warnings else ""))

        os.replace(dest, video)                    # 同目录替换，原子

        added = [name for name, path in plan.additions.items() if path]
        deleted = delete_sources(ctx, plan, added) if not ctx.keep else []
        expected = plan.expected_tracks()
        missing = [name for name, ok in expected.items() if not ok]

        note = warnings_note(warnings)
        if deleted:
            note = (note + "；" if note else "") + f"删除 {len(deleted)} 个源文件"
        return Result(video, "merged", before, available, added, missing,
                      time.perf_counter() - started, note, deleted)
    except (ValueError, ToolError) as exc:
        return Result(video, "failed", before, available, [], [],
                      time.perf_counter() - started,
                      note=str(exc) + (("；" + warnings_note(warnings)) if warnings else ""))
    except OSError as exc:
        return Result(video, "failed", before, available, [], [],
                      time.perf_counter() - started,
                      note=f"文件操作失败：{exc}" + (("；" + warnings_note(warnings)) if warnings else ""))
    finally:
        for tmp in temporary:
            try:
                if tmp.is_file():
                    tmp.unlink()
            except OSError:
                pass
        shutil.rmtree(workdir, ignore_errors=True)


def warnings_note(warnings: Sequence[str]) -> str:
    return "；".join(warnings)


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


def delete_sources(ctx: "Context", plan: Plan, added: Sequence[str]) -> list[Path]:
    """删除已并入的侧车；--purge 时再删“视频已有该轨、所以没用上”的那部分。

    purge 只删“视频本来就有这轨”的侧车：视频缺轨、但侧车因为编码/解析失败没并进去时
    必须留下，否则用户丢的是唯一一份素材。
    """
    candidates: list[Path] = []
    for name in added:
        path = plan.additions[name]
        if path:
            candidates.append(path)
    if ctx.purge:
        for name, path in plan.side.tracks.items():
            if path and name not in added and plan.probe.tracks[name]:
                candidates.append(path)
    return remove_files(ctx, candidates)


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def display_width(text: str) -> int:
    return sum(cluster_width(cluster) for cluster in iterate_clusters(text))


def pad(text: str, width: int) -> str:
    gap = width - display_width(text)
    return text + " " * gap if gap > 0 else text


_ZWJ = "\u200d"
_VARIATION = ("\ufe0e", "\ufe0f")
_KEYCAP = "\u20e3"


def _is_combining(ch: str) -> bool:
    return unicodedata.combining(ch) != 0 or unicodedata.category(ch) in ("Mn", "Me")


def iterate_clusters(text: str):
    """把字符串切成"用户眼里的一个字"。

    终端里 👨‍👩‍👧‍👦 / 👍🏽 / 1️⃣ / 🇨🇳 / e+́ 各自只占 1 个字形宽度，
    但按码点算长度是 7 / 2 / 3 / 2 / 2，直接数字符会让表格全部错位。
    """
    cluster = ""
    for ch in text:
        if not cluster:
            cluster = ch
            continue
        prev = cluster[-1]
        joins = (
            prev == _ZWJ
            or ch == _ZWJ
            or _is_combining(ch)
            or ch in _VARIATION
            or ch == _KEYCAP
            or 0x1F3FB <= ord(ch) <= 0x1F3FF                       # 肤色修饰
            or (0x1F1E6 <= ord(ch) <= 0x1F1FF                       # 旗帜：两个区域指示符成对
                and len(cluster) == 1 and 0x1F1E6 <= ord(prev) <= 0x1F1FF)
        )
        if joins:
            cluster += ch
        else:
            yield cluster
            cluster = ch
    if cluster:
        yield cluster


def cluster_width(cluster: str) -> int:
    marks = (_ZWJ, _KEYCAP, *_VARIATION)
    if all(_is_combining(ch) or ch in marks for ch in cluster):
        return 0
    if any(ch == _ZWJ or ch in _VARIATION or ch == _KEYCAP for ch in cluster):
        return 2
    if len(cluster) == 2 and all(0x1F1E6 <= ord(ch) <= 0x1F1FF for ch in cluster):
        return 2
    if any(0x1F3FB <= ord(ch) <= 0x1F3FF for ch in cluster):
        return 2
    return max(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in cluster)


def shorten(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for cluster in iterate_clusters(text):
        w = cluster_width(cluster)
        if used + w > width - 1:
            break
        kept.append(cluster)
        used += w
    return "".join(kept) + "…"


def action_text(result: Result) -> str:
    if result.action == "merged":
        return "合并"
    if result.action == "planned":
        return "预演"
    if result.action == "skipped":
        return "跳过"
    return "失败"


def result_text(result: Result) -> str:
    if result.action == "failed":
        return "失败"
    if result.missing:
        return "缺" + "/".join(result.missing)
    return "齐全"


def render_table(results: Sequence[Result], dry_run: bool) -> list[str]:
    if not results:
        return ["没有找到需要处理的视频。"]

    name_width = min(46, max(14, max(display_width(r.video.name) for r in results)))
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

    for i, r in enumerate(results, 1):
        merged = "+".join(r.added) if r.added else "—"
        lines.append(
            f"{i:<3}{pad(shorten(r.video.name, name_width), name_width)}  "
            f"{pad(marks(r.before), before_width)}{pad(marks(r.available), avail_width)}"
            f"{pad(action_text(r), action_width)}{pad(shorten(merged, added_width), added_width)}"
            f"{pad(result_text(r), verdict_width)}{r.seconds:>6.1f}s"
        )
        if r.note:
            lines.append(f"     └ {r.note}")
    return lines


def render_summary(results: Sequence[Result], orphans: dict[Path, list[Path]], dry_run: bool) -> list[str]:
    merged = [r for r in results if r.action == "merged"]
    planned = [r for r in results if r.action == "planned"]
    skipped = [r for r in results if r.action == "skipped"]
    failed = [r for r in results if r.action == "failed"]
    pool = merged or planned

    lines = [""]
    if dry_run:
        lines.append(f"预演结果：将合并 {len(planned)} 个，无需处理 {len(skipped)} 个，"
                     f"无法处理 {len(failed)} 个。")
    else:
        lines.append(f"合并 {len(merged)} 个，跳过 {len(skipped)} 个，失败 {len(failed)} 个。")

    counts = {name: sum(1 for r in pool if name in r.added) for name in (TRACK_AUDIO, TRACK_SUB, TRACK_COVER)}
    lines.append(f"本次并入：{TRACK_AUDIO} {counts[TRACK_AUDIO]} 个，"
                 f"{TRACK_SUB} {counts[TRACK_SUB]} 个，{TRACK_COVER} {counts[TRACK_COVER]} 个。")

    complete = [r for r in merged if not r.missing]
    incomplete = [r for r in merged if r.missing]
    if merged:
        lines.append(f"齐全 {len(complete)} 个，不齐全 {len(incomplete)} 个。")
        for r in incomplete:
            lines.append(f"  · {r.video.name} — 缺 {'/'.join(r.missing)}")

    idle = [r for r in skipped if r.missing]
    if idle:
        lines.append("未处理且不齐全的文件：")
        for r in idle:
            lines.append(f"  · {r.video.name} — 缺 {'/'.join(r.missing)}；{r.note}")

    if failed:
        lines.append("失败：")
        for r in failed:
            lines.append(f"  · {r.video.name} — {r.note}")

    if orphans:
        lines.append("孤立附件（没有同名视频，未处理）：")
        for directory, items in orphans.items():
            for item in items:
                lines.append(f"  · {item.name}    [{directory}]")
    return lines


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
@dataclass
class Context:
    tools: Tools
    dry_run: bool
    keep: bool
    purge: bool
    with_title: bool
    log: Callable[[str], None]
    emit: Callable[[str], None]
    sub_lang: str = "auto"


def collect_videos(paths: Sequence[Path], recursive: bool = False) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            entries = path.rglob("*") if recursive else path.iterdir()
            found += [
                item for item in sorted(entries)
                if item.is_file() and item.suffix.lower() in VIDEO_EXTS and not item.name.startswith(".")
            ]
        elif path.is_file() and path.suffix.lower() in VIDEO_EXTS:
            found.append(path)
        else:
            raise ValueError(f"不是目录也不是支持的视频文件（{'/'.join(VIDEO_EXTS)}）：{path}")

    unique: list[Path] = []
    seen: set[str] = set()
    for item in found:
        key = str(item).lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def find_orphans(directories: Iterable[Path], videos: Sequence[Path]) -> dict[Path, list[Path]]:
    stems = {unicodedata.normalize("NFC", str(v.with_suffix(""))).casefold() for v in videos}
    result: dict[Path, list[Path]] = {}
    for directory in {d.resolve() for d in directories}:
        if not directory.is_dir():
            continue
        items = []
        for item in sorted(directory.iterdir()):
            if not item.is_file() or item.suffix.lower() not in SIDECAR_EXTS:
                continue
            if item.name.startswith("."):
                continue
            if unicodedata.normalize("NFC", str(item.with_suffix(""))).casefold() not in stems:
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
            return Result(video, "failed", EMPTY_PROBE.tracks, sidecar_marks(Sidecars()),
                          [], [], 0.0, f"未预期错误：{exc!r}")

    results: list[Result] = []
    if jobs <= 1:
        for i, video in enumerate(videos, 1):
            ctx.log(f"[{i}/{len(videos)}] {video.name}")
            result = work(video)
            results.append(result)
            ctx.log(f"      → {status_line(result)}")
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(work, video) for video in videos]
            for i, future in enumerate(futures, 1):
                result = future.result()
                results.append(result)
                ctx.log(f"[{i}/{len(videos)}] {result.video.name} → {status_line(result)}")

    order = {str(v): i for i, v in enumerate(videos)}
    results.sort(key=lambda r: order.get(str(r.video), 0))

    ctx.log("")
    for line in render_table(results, ctx.dry_run):
        ctx.log(line)
    for line in render_summary(results, find_orphans({v.parent for v in videos}, videos), ctx.dry_run):
        ctx.emit(line)

    return 1 if any(r.action == "failed" for r in results) else 0


def status_line(result: Result) -> str:
    if result.action == "merged":
        state = "齐全" if not result.missing else "缺" + "/".join(result.missing)
        head = "已合并（" + ("+".join(result.added) or "无新增") + "）"
        return f"{head}{state}，{result.seconds:.1f}s"
    if result.action == "planned":
        head = "预演，将并入 " + ("+".join(result.added) or "无")
        if "；" in result.note:                     # 预演阶段发现的降级原因也要显示
            return head + "；" + result.note.split("；", 1)[1]
        return head
    if result.action == "skipped":
        return f"跳过：{result.note}"
    return f"失败：{result.note}"


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
LAUNCHER_HINTS = {
    "1": "[退出码 1] 有文件未能合并，源文件未改动。详见上方汇总。",
    "2": "[退出码 2] 参数或运行环境有问题。详见上方错误信息。",
    "130": "[退出码 130] 运行被中断，源文件未改动。",
}


def emit_hint(code: str) -> None:
    """启动器专用：把退出码翻成一句中文提示。

    启动器 .bat 必须保持纯 ASCII——UTF-8 批处理文件里执行 chcp 会让 cmd
    在解析中途切换解码器、丢掉字节偏移，最后把某一行的尾巴当成命令执行。
    中文提示因此全部放在这里打印。
    """
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
            "  merge_videos.py -n D:\\Download\\Aria2   只分析，不写入\n"
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
                        help="新增字幕的语言标记：auto 自动识别，或 chi/eng/jpn…")
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

    log_path: Optional[Path] = None
    if args.log:
        log_path = Path(args.log).expanduser().resolve()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:   # 追加，不覆盖历史
                handle.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
        except OSError as exc:
            print(f"[警告] 无法写入日志 {log_path}：{exc}")
            log_path = None

    def emit(message: str) -> None:
        try:
            print(message, flush=True)
        except OSError:
            pass                       # 控制台被关掉 / 管道断开时不要打断处理
        if log_path is not None:
            try:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(message + "\n")
            except OSError:
                pass

    try:
        tools = Tools.resolve(args.ffmpeg, args.ffprobe)
    except ToolError as exc:
        emit(f"[错误] {exc}")
        return 2

    paths = [Path(p).expanduser().resolve() for p in args.paths]
    if not paths:
        paths = [Path(__file__).resolve().parent]

    bad = [p for p in paths if not p.exists()]
    if bad:
        for path in bad:
            emit(f"[错误] 路径不存在：{path}")
        return 2

    ctx = Context(
        tools=tools,
        dry_run=args.dry_run,
        keep=args.keep,
        purge=args.purge,
        with_title=not args.no_title,
        log=emit,
        emit=emit,
        sub_lang=args.sub_lang,
    )
    if args.quiet:
        ctx.log = lambda message: None

    try:
        code = run(paths, ctx, max(1, args.jobs), args.recursive)
    except ValueError as exc:
        emit(f"[错误] {exc}")
        return 2
    except KeyboardInterrupt:
        emit("\n已中断。")
        return 130

    if args.quiet:
        emit("")
    emit(f"完成于 {datetime.now():%Y-%m-%d %H:%M:%S}")
    return code


if __name__ == "__main__":
    sys.exit(main())
