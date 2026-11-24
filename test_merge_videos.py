# -*- coding: utf-8 -*-
"""merge_videos 的自测。

    python -m unittest test_merge_videos -v

纯函数用例不需要 ffmpeg；字幕校验、预处理和失败降级要跑 ffmpeg，找不到时自动跳过。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import merge_videos as mv


def _resolve_tools():
    try:
        return mv.Tools.resolve()
    except mv.ToolError:
        return None


TOOLS = _resolve_tools()
needs_ffmpeg = unittest.skipUnless(TOOLS is not None, "需要 ffmpeg / ffprobe")


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name: str, content: str, encoding: str = "utf-8") -> Path:
        path = self.dir / name
        path.write_text(content, encoding=encoding, newline="\n")
        return path

    def write_bytes(self, name: str, content: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(content)
        return path


class TimestampTests(unittest.TestCase):
    def test_format(self) -> None:
        self.assertEqual(mv.format_timestamp(0), "00:00:00,000")
        self.assertEqual(mv.format_timestamp(1_000), "00:00:01,000")
        self.assertEqual(mv.format_timestamp(3_661_001), "01:01:01,001")

    def test_negative_is_clamped(self) -> None:
        self.assertEqual(mv.format_timestamp(-5), "00:00:00,000")


class CueTextTests(unittest.TestCase):
    def test_collapses_controls_and_whitespace(self) -> None:
        self.assertEqual(mv.clean_cue_text("  a\x00 b\n\tc  "), "a b c")

    def test_unicode_line_separators_become_spaces(self) -> None:
        self.assertEqual(mv.clean_cue_text("a\u2028b\u2029c"), "a b c")


class DecodeTests(TempDirTestCase):
    def test_utf8_bom_is_stripped(self) -> None:
        self.assertEqual(mv.decode_text(b"\xef\xbb\xbfhello"), "hello")

    def test_utf16_bom(self) -> None:
        raw = b"\xff\xfe" + "你好".encode("utf-16-le")
        self.assertEqual(mv.decode_text(raw), "你好")

    def test_gbk_falls_back_to_gb18030(self) -> None:
        self.assertEqual(mv.decode_text("中文字幕".encode("gb18030")), "中文字幕")

    def test_binary_looking_text_is_rejected(self) -> None:
        path = self.write_bytes("a.srt", b"\x00" * 4096)
        with self.assertRaises(ValueError) as caught:
            mv._read_subtitle_text(path)
        self.assertIn("二进制", str(caught.exception))


class LanguageTests(unittest.TestCase):
    def test_guess_by_script(self) -> None:
        self.assertEqual(mv.guess_subtitle_language("こんにちは世界"), "jpn")
        self.assertEqual(mv.guess_subtitle_language("안녕하세요"), "kor")
        self.assertEqual(mv.guess_subtitle_language("你好，世界"), "chi")
        self.assertEqual(mv.guess_subtitle_language("hello world"), "eng")
        self.assertEqual(mv.guess_subtitle_language("12345"), "und")

    def test_tag_to_iso639_2(self) -> None:
        self.assertEqual(mv.language_code("en"), "eng")
        self.assertEqual(mv.language_code("en-orig"), "eng")     # 取主子标签
        self.assertEqual(mv.language_code("zh-Hans"), "chi")
        self.assertEqual(mv.language_code("zh-Hant"), "chi")
        self.assertEqual(mv.language_code("fr-CA"), "fra")
        self.assertEqual(mv.language_code("jpn"), "jpn")         # 已是三字码
        self.assertEqual(mv.language_code(""), "und")


@needs_ffmpeg
class SubtitleValidationTests(TempDirTestCase):
    """校验交给 ffmpeg 自己解析；这里锁定“什么算有效字幕”。"""

    def test_srt_with_hours(self) -> None:
        mv.validate_subtitle(TOOLS, self.write("a.srt", "1\n00:00:01,000 --> 00:00:03,000\n你好\n"))

    def test_vtt_without_hours(self) -> None:
        mv.validate_subtitle(TOOLS, self.write("a.vtt", "WEBVTT\n\n00:01.000 --> 00:03.000\n你好\n"))

    def test_numeric_cue_text_is_valid(self) -> None:
        mv.validate_subtitle(TOOLS, self.write("a.srt", "1\n00:00:01,000 --> 00:00:03,000\n42\n"))

    def test_srt_still_requires_hours(self) -> None:
        with self.assertRaises(ValueError):
            mv.validate_subtitle(TOOLS, self.write("a.srt", "1\n00:01,000 --> 00:03,000\n你好\n"))

    def test_timeline_without_text_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            mv.validate_subtitle(TOOLS, self.write("a.vtt", "WEBVTT\n\n00:01.000 --> 00:03.000\n"))
        self.assertIn("没有文字", str(caught.exception))

    def test_vtt_settings_only_rejected(self) -> None:
        text = "WEBVTT\n\n00:01.000 --> 00:03.000 align:start position:0%\n"
        with self.assertRaises(ValueError):
            mv.validate_subtitle(TOOLS, self.write("a.vtt", text))

    def test_garbage_rejected(self) -> None:
        with self.assertRaises(ValueError):
            mv.validate_subtitle(TOOLS, self.write("a.srt", "not a subtitle at all\n"))

    def test_ass_without_text_rejected(self) -> None:
        path = self.write("a.ass", (
            "[Script Info]\nTitle: t\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\an8}\n"
        ))
        with self.assertRaises(ValueError):
            mv.validate_subtitle(TOOLS, path)


@needs_ffmpeg
class SubtitlePreparationTests(TempDirTestCase):
    def test_gbk_srt_is_transcoded_to_utf8(self) -> None:
        source = self.write_bytes("a.srt", "1\n00:00:01,000 --> 00:00:03,000\n中文字幕\n".encode("gb18030"))
        prepared, entries = mv.prepare_subtitle(TOOLS, source, self.dir, 0)
        self.assertIn("中文字幕", prepared.read_text(encoding="utf-8"))
        self.assertEqual(entries, 0)

    def test_utf16_srt_is_transcoded(self) -> None:
        text = "1\n00:00:01,000 --> 00:00:03,000\n你好\n"
        source = self.write_bytes("a.srt", b"\xff\xfe" + text.encode("utf-16-le"))
        prepared, entries = mv.prepare_subtitle(TOOLS, source, self.dir, 0)
        self.assertIn("你好", prepared.read_text(encoding="utf-8"))
        self.assertEqual(entries, 0)

    def test_binary_subtitle_is_rejected(self) -> None:
        source = self.write_bytes("a.srt", b"\x00" * 4096)
        with self.assertRaises(ValueError):
            mv.prepare_subtitle(TOOLS, source, self.dir, 0)

    def test_json_becomes_srt(self) -> None:
        source = self.write("a.json", json.dumps(
            {"body": [{"from": 1.2, "to": 3.4, "content": "你好"}]}, ensure_ascii=False
        ))
        prepared, entries = mv.prepare_subtitle(TOOLS, source, self.dir, 0)
        self.assertEqual(entries, 1)
        self.assertEqual(prepared.suffix, ".srt")
        self.assertIn("00:00:01,200 --> 00:00:03,400", prepared.read_text(encoding="utf-8"))


class JsonSubtitleTests(TempDirTestCase):
    def test_json_to_srt_keeps_valid_cues(self) -> None:
        source = self.write("a.json", json.dumps(
            {
                "body": [
                    {"from": 1.2, "to": 3.4, "content": "<b>你好</b>"},
                    {"from": 4.0, "to": 4.0, "content": ""},      # 空正文应被跳过
                ]
            },
            ensure_ascii=False,
        ))
        target = self.dir / "a.srt"
        self.assertEqual(mv.json_to_srt(source, target), 1)
        text = target.read_text(encoding="utf-8")
        self.assertIn("00:00:01,200 --> 00:00:03,400", text)
        self.assertIn("<b>你好</b>", text)


class ClusterTests(unittest.TestCase):
    def test_emoji_sequences_are_one_cell(self) -> None:
        self.assertEqual(len(list(mv.iterate_clusters("👨‍👩‍👧‍👦"))), 1)
        self.assertEqual(mv.display_width("👨‍👩‍👧‍👦"), 2)     # 家庭：ZWJ 连成一簇
        self.assertEqual(mv.display_width("🇨🇳"), 2)            # 旗帜：两个区域指示符
        self.assertEqual(mv.display_width("👍🏽"), 2)            # 肤色修饰
        self.assertEqual(mv.display_width("e\u0301"), 1)        # 组合重音
        self.assertEqual(mv.display_width("中文"), 4)


class SidecarDiscoveryTests(TempDirTestCase):
    def test_exact_and_language_suffix(self) -> None:
        video = self.dir / "movie.mp4"
        video.write_bytes(b"x")                                # 发现侧车不读视频内容
        for name in (
            "movie.srt",
            "movie.en.vtt",
            "movie.zh-Hans.srt",
            "movie_zh.json",
            "movie.forced.srt",                                # forced 不是语言标签
        ):
            self.write(name, "x")

        subtitles = mv.pick_subtitles(video)
        self.assertEqual(
            [item.path.name for item in subtitles],
            ["movie.srt", "movie.en.vtt", "movie_zh.json", "movie.zh-Hans.srt"],
        )
        self.assertEqual([item.tag for item in subtitles], ["", "en", "zh", "zh-hans"])

    def test_language_tag_filter(self) -> None:
        self.assertEqual(mv._language_tag("en"), "en")
        self.assertEqual(mv._language_tag("zh-Hans"), "zh-hans")
        self.assertEqual(mv._language_tag("en-orig"), "en-orig")
        self.assertEqual(mv._language_tag("forced"), "")

    def test_extension_priority(self) -> None:
        video = self.dir / "movie.mp4"
        video.write_bytes(b"x")
        for name in ("movie.aac", "movie.m4a", "movie.png", "movie.jpg"):
            self.write(name, "x")
        side = mv.find_sidecars(mv.build_index(self.dir), video)
        self.assertEqual(side.audio.name, "movie.m4a")
        self.assertEqual(side.cover.name, "movie.jpg")

    def test_webm_audio_is_discovered(self) -> None:
        video = self.dir / "movie.mp4"
        video.write_bytes(b"x")
        self.write("movie.webm", "x")
        side = mv.find_sidecars(mv.build_index(self.dir), video)
        self.assertEqual(side.audio.name, "movie.webm")

    def test_empty_sidecar_is_ignored(self) -> None:
        video = self.dir / "movie.mp4"
        video.write_bytes(b"x")
        self.write("movie.srt", "")
        self.assertEqual(mv.pick_subtitles(video), ())


class OrphanTests(TempDirTestCase):
    def test_language_sidecar_is_claimed_but_forced_is_not(self) -> None:
        video = self.dir / "N.mp4"
        video.write_bytes(b"x")
        for name in ("N.en.srt", "N.forced.srt", "lonely.srt", "lonely.jpg"):
            self.write(name, "x")
        orphans = mv.find_orphans([self.dir], [video])
        self.assertEqual([item.name for item in orphans[self.dir.resolve()]],
                         ["lonely.jpg", "lonely.srt", "N.forced.srt"])


class PlanTests(unittest.TestCase):
    def test_existing_language_is_not_duplicated(self) -> None:
        probe = mv.Probe(main_index=0, subtitles=(mv.SubStream(1, "mov_text", "eng"),))
        side = mv.Sidecars(subtitles=(
            mv.Subtitle(Path("x.en.vtt"), "en"),
            mv.Subtitle(Path("x.ja.vtt"), "ja"),
        ))
        plan = mv.make_plan(Path("x.mp4"), probe, side, mv.container_for(Path("x.mp4")))
        self.assertEqual([item.path.name for item in plan.add_subs], ["x.ja.vtt"])

    def test_all_subtitles_added_when_video_has_none(self) -> None:
        probe = mv.Probe(main_index=0)
        side = mv.Sidecars(subtitles=(
            mv.Subtitle(Path("x.srt"), ""),
            mv.Subtitle(Path("x.en.vtt"), "en"),
        ))
        plan = mv.make_plan(Path("x.mp4"), probe, side, mv.container_for(Path("x.mp4")))
        self.assertEqual(len(plan.add_subs), 2)

    def test_bitmap_subtitles_are_dropped_for_mp4_but_kept_for_mkv(self) -> None:
        probe = mv.Probe(main_index=0, subtitles=(mv.SubStream(1, "hdmv_pgs_subtitle"),))
        mp4 = mv.make_plan(Path("x.mp4"), probe, mv.Sidecars(), mv.container_for(Path("x.mp4")))
        mkv = mv.make_plan(Path("x.mkv"), probe, mv.Sidecars(), mv.container_for(Path("x.mkv")))
        self.assertEqual(mp4.kept_subs, ())
        self.assertEqual(mp4.dropped_subs, ("hdmv_pgs_subtitle",))
        self.assertEqual(len(mkv.kept_subs), 1)
        self.assertEqual(mkv.dropped_subs, ())

    def test_dropped_bitmap_subtitle_does_not_block_text_sidecar(self) -> None:
        # MP4 里只有 PGS eng：这条会被剔除，不能因此把 x.en.srt 也当成“已有同语言”。
        probe = mv.Probe(main_index=0, subtitles=(mv.SubStream(1, "hdmv_pgs_subtitle", "eng"),))
        side = mv.Sidecars(subtitles=(mv.Subtitle(Path("x.en.srt"), "en"),))
        plan = mv.make_plan(Path("x.mp4"), probe, side, mv.container_for(Path("x.mp4")))
        self.assertEqual(plan.kept_subs, ())
        self.assertEqual([item.path.name for item in plan.add_subs], ["x.en.srt"])

    def test_skip_reasons(self) -> None:
        complete = mv.Probe(main_index=0, audio_count=1, covers=(mv.CoverStream(1, "mjpeg"),),
                            subtitles=(mv.SubStream(2, "mov_text"),))
        plan = mv.make_plan(Path("x.mp4"), complete, mv.Sidecars(), mv.container_for(Path("x.mp4")))
        self.assertEqual(plan.skip_reason, "已齐全，无需处理")

        empty = mv.Probe(main_index=0)
        plan = mv.make_plan(Path("x.mp4"), empty, mv.Sidecars(), mv.container_for(Path("x.mp4")))
        self.assertEqual(plan.skip_reason, "无可合并素材（缺 音频/字幕/封面）")


class ContainerTests(unittest.TestCase):
    def test_table(self) -> None:
        self.assertEqual(mv.container_for(Path("a.MKV")).kind, "matroska")
        self.assertEqual(mv.container_for(Path("a.mp4")).subtitle_codec, "mov_text")
        self.assertEqual(mv.container_for(Path("a.m4v")).kind, "mp4")
        with self.assertRaises(ValueError):
            mv.container_for(Path("a.mov"))


class BuildCommandTests(unittest.TestCase):
    """命令拼装里最容易悄悄丢轨：已有轨道必须显式 -map，封面输出顺序必须对。"""

    def setUp(self) -> None:
        self.tools = mv.Tools("ffmpeg", "ffprobe")
        self.sub = mv.SubtitleInput(Path("work/sub0.srt"), "eng", Path("x.en.srt"))

    def test_mp4_keeps_existing_tracks_and_appends_new_ones(self) -> None:
        probe = mv.Probe(
            main_index=0, audio_count=1,
            subtitles=(mv.SubStream(1, "mov_text", "chi"),),
            covers=(mv.CoverStream(2, "mjpeg", "cover.jpg", "image/jpeg"),),
        )
        plan = mv.make_plan(Path("x.mp4"), probe, mv.Sidecars(), mv.container_for(Path("x.mp4")))
        cmd = mv.build_command(self.tools, plan, (self.sub,), Path("work/cover.jpg"),
                               Path("out.mp4"), with_title=True)
        text = " ".join(cmd)
        self.assertIn("-map 0:0 -map 0:2 -map 0:a -map 0:1 -map 1:s:0 -map 2:v:0", text)
        self.assertIn("-c:s mov_text", text)
        self.assertIn("-metadata:s:s:1 language=eng", text)
        self.assertIn("-disposition:v:1 attached_pic -disposition:v:2 attached_pic", text)
        self.assertTrue(text.endswith("-metadata title=x out.mp4"))

    def test_mkv_attaches_covers_and_keeps_font_attachments(self) -> None:
        probe = mv.Probe(
            main_index=0,
            covers=(mv.CoverStream(1, "mjpeg", "cover.jpg", "image/jpeg"),),
            attachments=1,
        )
        plan = mv.make_plan(Path("x.mkv"), probe, mv.Sidecars(), mv.container_for(Path("x.mkv")))
        old_cover = mv.Attachment(Path("work/cover0.jpg"), "cover.jpg", "image/jpeg")
        subtitle = mv.SubtitleInput(Path("work/sub0.ass"), "jpn", Path("x.ja.ass"))
        cmd = mv.build_command(self.tools, plan, (subtitle,), Path("work/new.jpg"), Path("out.mkv"),
                               with_title=False, attachments=(old_cover,))
        text = " ".join(cmd)
        self.assertIn("-map 0:t?", text)
        self.assertIn("-map 1:s:0", text)
        self.assertIn(f"-attach {Path('work/cover0.jpg')} -metadata:s:t:1 filename=cover.jpg", text)
        self.assertIn(f"-attach {Path('work/new.jpg')} -metadata:s:t:2 filename=cover.jpg", text)
        self.assertIn("-c:s copy", text)

    def test_webm_audio_is_transcoded_for_mp4(self) -> None:
        plan = mv.make_plan(Path("x.mp4"), mv.Probe(main_index=0),
                            mv.Sidecars(audio=Path("x.webm")), mv.container_for(Path("x.mp4")))
        plan.transcode_audio = True
        cmd = mv.build_command(self.tools, plan, (), None, Path("out.mp4"), with_title=False)
        text = " ".join(cmd)
        self.assertIn("-c:a aac -b:a 192k", text)
        self.assertNotIn("-c:a copy", text)

    def test_expected_tracks_reflects_degraded_assets(self) -> None:
        probe = mv.Probe(main_index=0)
        plan = mv.make_plan(Path("x.mp4"), probe, mv.Sidecars(subtitles=(mv.Subtitle(Path("x.srt")),)),
                            mv.container_for(Path("x.mp4")))
        self.assertEqual(mv.expected_tracks(plan, mv.Assets()), {
            mv.TRACK_AUDIO: False, mv.TRACK_SUB: False, mv.TRACK_COVER: False,
        })
        self.assertEqual(mv.merged_tracks(plan, mv.Assets()), [])


if __name__ == "__main__":
    unittest.main()
