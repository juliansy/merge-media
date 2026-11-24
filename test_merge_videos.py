# -*- coding: utf-8 -*-
"""merge_videos 的纯函数自测。

不依赖 ffmpeg、不引入 pytest，直接跑：

    python -m unittest test_merge_videos -v

这些用例只覆盖“读文件/拼字符串/判格式”的纯逻辑；真正封装视频要走
merge_videos.py 的回归场景（需要 ffmpeg）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import merge_videos as mv


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


class EnsurePlayableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _text(self, name: str, content: str, encoding: str = "utf-8") -> Path:
        path = Path(self.tmp.name) / name
        path.write_text(content, encoding=encoding, newline="\n")
        return path

    def test_srt_with_hours(self) -> None:
        path = self._text("a.srt", "1\n00:00:01,000 --> 00:00:03,000\n你好\n")
        mv.ensure_playable(path, ".srt")

    def test_vtt_without_hours(self) -> None:
        path = self._text("a.vtt", "WEBVTT\n\n00:01.000 --> 00:03.000\n你好\n")
        mv.ensure_playable(path, ".vtt")

    def test_vtt_with_hours(self) -> None:
        path = self._text("a.vtt", "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello\n")
        mv.ensure_playable(path, ".vtt")

    def test_srt_still_requires_hours(self) -> None:
        path = self._text("a.srt", "1\n00:01,000 --> 00:03,000\n你好\n")
        with self.assertRaises(ValueError):
            mv.ensure_playable(path, ".srt")

    def test_timeline_without_text_rejected(self) -> None:
        path = self._text("a.vtt", "WEBVTT\n\n00:01.000 --> 00:03.000\n")
        with self.assertRaises(ValueError):
            mv.ensure_playable(path, ".vtt")

    def test_vtt_settings_only_rejected(self) -> None:
        path = self._text("a.vtt", "WEBVTT\n\n00:01.000 --> 00:03.000 align:start position:0%\n")
        with self.assertRaises(ValueError):
            mv.ensure_playable(path, ".vtt")

    def test_unknown_suffix_rejected(self) -> None:
        path = self._text("a.sup", "not a text subtitle")
        with self.assertRaises(ValueError):
            mv.ensure_playable(path, ".sup")

    def test_binary_rejected(self) -> None:
        path = Path(self.tmp.name) / "a.srt"
        path.write_bytes(b"\x00" * 4096)
        with self.assertRaises(ValueError) as caught:
            mv.ensure_playable(path, ".srt")
        self.assertIn("二进制", str(caught.exception))

    def test_utf16_with_bom_accepted(self) -> None:
        path = Path(self.tmp.name) / "a.srt"
        text = "1\n00:00:01,000 --> 00:00:03,000\n你好\n"
        path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
        mv.ensure_playable(path, ".srt")


class JsonSubtitleTests(unittest.TestCase):
    def test_json_to_srt_keeps_valid_cues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "a.json"
            target = Path(tmp) / "a.srt"
            source.write_text(
                json.dumps(
                    {
                        "body": [
                            {"from": 1.2, "to": 3.4, "content": "<b>你好</b>"},
                            {"from": 4.0, "to": 4.0, "content": ""},      # 空正文应被跳过
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            count = mv.json_to_srt(source, target)
            self.assertEqual(count, 1)
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


class SidecarDiscoveryTests(unittest.TestCase):
    def test_exact_and_language_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            video = directory / "movie.mp4"
            video.write_bytes(b"x")                                # pick_subtitles 不读视频内容
            for name in (
                "movie.srt",
                "movie.en.vtt",
                "movie.zh-Hans.srt",
                "movie_zh.json",
                "movie.forced.srt",                                # forced 不是语言标签
            ):
                (directory / name).write_text("x", encoding="utf-8")

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


class PlanTests(unittest.TestCase):
    def test_existing_language_is_not_duplicated(self) -> None:
        probe = mv.Probe(
            0.0, True, False, True, False, 0, 1, "h264", "", "mov_text", "", 2,
            sub_streams=(mv.SubStream(1, "mov_text", "eng"),),
        )
        side = mv.Sidecars(subtitles=(
            mv.Subtitle(Path("x.en.vtt"), "en"),
            mv.Subtitle(Path("x.ja.vtt"), "ja"),
        ))
        plan = mv.make_plan(Path("x.mp4"), probe, side, mv.container_for(Path("x.mp4")))
        self.assertEqual([item.path.name for item in plan.add_subs], ["x.ja.vtt"])

    def test_all_subtitles_added_when_video_has_none(self) -> None:
        probe = mv.Probe(0.0, True, False, False, False, 0, 0, "h264", "", "", "", 1)
        side = mv.Sidecars(subtitles=(
            mv.Subtitle(Path("x.srt"), ""),
            mv.Subtitle(Path("x.en.vtt"), "en"),
        ))
        plan = mv.make_plan(Path("x.mp4"), probe, side, mv.container_for(Path("x.mp4")))
        self.assertEqual(len(plan.add_subs), 2)


class ContainerTests(unittest.TestCase):
    def test_table(self) -> None:
        self.assertEqual(mv.container_for(Path("a.MKV")).kind, "matroska")
        self.assertEqual(mv.container_for(Path("a.mp4")).subtitle_codec, "mov_text")
        self.assertEqual(mv.container_for(Path("a.m4v")).kind, "mp4")
        with self.assertRaises(ValueError):
            mv.container_for(Path("a.mov"))


if __name__ == "__main__":
    unittest.main()
