import math
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from youtube_viral_radar import (
    APIError,
    Channel,
    Video,
    YouTubeAPI,
    breakout_reasons,
    composite_score,
    estimate_search_quota,
    format_duration,
    generate_report,
    is_short_form,
    load_daily_quota_limit,
    load_keywords,
    load_loose_keywords,
    load_quota_usage,
    parse_duration,
    quota_day_key,
    record_quota_usage,
    subscriber_ratio,
)


NOW = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)


def make_video(**overrides):
    # 建立共用的影片測試資料，各測試只覆寫關心的欄位。
    values = {
        "video_id": "video-1",
        "title": "Test | Video",
        "description": "",
        "channel_id": "channel-1",
        "channel_title": "Test Channel",
        "views": 10_000,
        "published_at": NOW - timedelta(days=1),
        "duration_seconds": 120,
        "keywords": {"Codex"},
    }
    values.update(overrides)
    return Video(**values)


def make_channel(**overrides):
    values = {
        "channel_id": "channel-1",
        "title": "Test Channel",
        "subscribers": 100,
        "subscribers_hidden": False,
        "total_views": None,
        "video_count": None,
    }
    values.update(overrides)
    return Channel(**values)


class RadarTests(unittest.TestCase):
    def test_keywords_are_read_one_per_line_and_deduplicated(self):
        # 驗證輸入檔會忽略空白行，並以不分大小寫方式移除重複關鍵字。
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "keywords.txt"
            path.write_text(
                "﻿Codex\n\nClaude Code\ncodex\n  Cursor  \n",
                encoding="utf-8",
            )
            self.assertEqual(load_keywords(path), ["Codex", "Claude Code", "Cursor"])

    def test_duration_parsing_and_formatting(self):
        # YouTube 使用 ISO 8601 片長格式，報表則轉為一般時間表示法。
        self.assertEqual(parse_duration("PT1M"), 60)
        self.assertEqual(parse_duration("PT1H2M3S"), 3723)
        self.assertEqual(format_duration(3723), "1:02:03")

    def test_small_and_large_channel_breakout_rules(self):
        # 分別覆蓋小型、中型與大型頻道的爆款判斷門檻。
        small = make_channel(subscribers=999)
        self.assertIn("小於 1 千訂閱，播放達 1 萬", breakout_reasons(make_video(), small))

        medium = make_channel(subscribers=9_999)
        self.assertIn(
            "小於 1 萬訂閱，播放達 10 萬",
            breakout_reasons(make_video(views=100_000), medium),
        )

        large = make_channel(subscribers=20_000)
        self.assertIn(
            "播放達訂閱數 5 倍",
            breakout_reasons(make_video(views=100_000), large),
        )

    def test_channel_average_rule_qualifies_video(self):
        # 第 4 條：播放量達頻道平均 5 倍且達絕對門檻（5 萬）才入選。
        channel = make_channel(
            subscribers=500_000, total_views=1_000_000, video_count=100
        )  # 平均播放 = 10,000
        self.assertIn(
            "播放達頻道平均 5 倍",
            breakout_reasons(make_video(views=60_000), channel),
        )
        self.assertNotIn(
            "播放達頻道平均 5 倍",
            breakout_reasons(make_video(views=40_000), channel),
        )

    def test_channel_average_rule_requires_absolute_view_floor(self):
        # 低基數頻道：雖達平均 5 倍，但播放未滿 5 萬就不算爆款，避免洗版。
        small_base = make_channel(
            subscribers=500_000, total_views=200_000, video_count=100
        )  # 平均播放 = 2,000
        self.assertEqual(breakout_reasons(make_video(views=12_000), small_base), [])
        self.assertIn(
            "播放達頻道平均 5 倍",
            breakout_reasons(make_video(views=60_000), small_base),
        )

    def test_short_form_excludes_by_duration_and_shorts_tag(self):
        # 時長 < 120 秒，或標題含 #short / #shorts，皆視為短影音。
        self.assertTrue(is_short_form(make_video(duration_seconds=90)))
        self.assertFalse(is_short_form(make_video(duration_seconds=120)))
        self.assertTrue(
            is_short_form(make_video(duration_seconds=300, title="Cool clip #shorts"))
        )
        self.assertTrue(
            is_short_form(make_video(duration_seconds=300, title="meme #Short funny"))
        )
        self.assertFalse(
            is_short_form(make_video(duration_seconds=300, title="A real long video"))
        )

    def test_shorts_tagged_video_is_kept_out_of_report(self):
        video = make_video(duration_seconds=300, title="ChatGPT meme #shorts")
        channel = make_channel(subscribers=100)  # 否則會觸發小頻道爆款條件
        report = generate_report(
            ["Codex"],
            {"Codex": [video.video_id]},
            {video.video_id: video},
            {channel.channel_id: channel},
            NOW,
            101,
        )
        self.assertIn("本週沒有找到符合條件的影片", report)

    def test_hidden_subscribers_can_qualify_via_channel_average(self):
        # 訂閱數隱藏不會誤觸前三條，但仍可憑「頻道平均 5 倍」入選，且不崩潰。
        hidden = make_channel(
            subscribers=None,
            subscribers_hidden=True,
            total_views=2_000_000,
            video_count=200,
        )  # 平均播放 = 10,000
        reasons = breakout_reasons(make_video(views=1_000_000), hidden)
        self.assertEqual(reasons, ["播放達頻道平均 5 倍"])

        # 沒有平均資料時，隱藏訂閱數的頻道不會入選。
        hidden_no_avg = make_channel(subscribers=None, subscribers_hidden=True)
        self.assertEqual(breakout_reasons(make_video(views=1_000_000), hidden_no_avg), [])

    def test_zero_video_count_does_not_crash(self):
        channel = make_channel(total_views=0, video_count=0)
        self.assertIsNone(channel.average_views)
        self.assertEqual(breakout_reasons(make_video(views=100), channel), [])

    def test_loose_keyword_lowers_thresholds(self):
        # 同一支影片在嚴格關鍵詞落榜，在寬鬆關鍵詞入選。
        channel = make_channel(
            subscribers=200_000, total_views=800_000, video_count=100
        )  # 頻道平均 = 8,000
        video = make_video(views=30_000, duration_seconds=600)

        strict_report = generate_report(
            ["Google Gemini"],
            {"Google Gemini": [video.video_id]},
            {video.video_id: video},
            {channel.channel_id: channel},
            NOW,
            101,
        )
        self.assertIn("本週沒有找到符合條件的影片", strict_report)

        loose_report = generate_report(
            ["Google Gemini"],
            {"Google Gemini": [video.video_id]},
            {video.video_id: video},
            {channel.channel_id: channel},
            NOW,
            101,
            loose_keywords=["google gemini"],  # 不分大小寫
        )
        self.assertIn("播放達頻道平均 3 倍", loose_report)
        self.assertIn("（寬鬆門檻）", loose_report)
        self.assertIn("寬鬆門檻關鍵詞：Google Gemini", loose_report)

    def test_report_includes_trend_section_with_common_term(self):
        # 趨勢區塊應出現，並抓出反覆出現的主題詞（fable）與最高熱度影片。
        channel = make_channel(subscribers=100)
        v1 = make_video(
            video_id="v1", title="Claude Fable 5 review deep dive", views=50_000
        )
        v2 = make_video(
            video_id="v2", title="Testing Fable on a real project", views=20_000
        )
        report = generate_report(
            ["Claude Code"],
            {"Claude Code": ["v1", "v2"]},
            {"v1": v1, "v2": v2},
            {channel.channel_id: channel},
            NOW,
            101,
        )
        self.assertIn("## 本週各領域趨勢發現", report)
        self.assertIn("**Claude Code**", report)
        self.assertIn("熱門詞：fable", report)
        self.assertIn("熱度最高", report)

    def test_trend_section_handles_keyword_with_no_videos(self):
        report = generate_report(
            ["Codex"], {"Codex": []}, {}, {}, NOW, 101
        )
        self.assertIn("## 本週各領域趨勢發現", report)
        self.assertIn("**Codex**：本週無相關影片資料。", report)

    def test_report_includes_per_keyword_diagnostic(self):
        video = make_video()
        channel = make_channel(subscribers=100)
        report = generate_report(
            ["Codex"],
            {"Codex": [video.video_id, "missing-id"]},
            {video.video_id: video},
            {channel.channel_id: channel},
            NOW,
            101,
        )
        # 搜尋 2 支（含一個未補抓到的 ID）→ 長影片 1 支 → 爆款 1 支
        self.assertIn(
            "診斷：搜尋 2 支 → 長影片 1 支（Shorts 0 支另計入 Shorts TOP 10）→ 爆款 1 支",
            report,
        )

    def test_shorts_get_their_own_top10_and_are_excluded_from_long_video_ranking(self):
        long_video = make_video(
            video_id="long-1", duration_seconds=600, views=10_000
        )
        short_video = make_video(
            video_id="short-1",
            title="Short Clip #shorts",
            duration_seconds=45,
            views=10_000,
        )
        channel = make_channel(subscribers=100)
        report = generate_report(
            ["Codex"],
            {"Codex": [long_video.video_id, short_video.video_id]},
            {long_video.video_id: long_video, short_video.video_id: short_video},
            {channel.channel_id: channel},
            NOW,
            101,
        )
        self.assertIn("## YouTube Shorts 本週爆款 TOP 10", report)
        self.assertIn("Short Clip", report)
        self.assertIn("- Shorts 候選（去重）：1", report)
        self.assertIn("- Shorts 爆款：1", report)
        self.assertIn("- 長影片候選（去重）：1", report)
        # Shorts 不應混入長影片的「本週爆款 TOP 10」區塊。
        top10_section = report.split("## 本週爆款 TOP 10")[1].split("## YouTube Shorts")[0]
        self.assertNotIn("Short Clip", top10_section)

    def test_load_loose_keywords_parses_and_casefolds(self):
        with patch.dict(
            os.environ, {"LOOSE_KEYWORDS": "ChatGPT, Google Gemini ,"}, clear=True
        ):
            self.assertEqual(
                load_loose_keywords(), {"chatgpt", "google gemini"}
            )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_loose_keywords(), set())

    def test_short_video_does_not_appear_in_report(self):
        video = make_video(duration_seconds=59)
        channel = make_channel(subscribers=100)
        report = generate_report(
            ["Codex"],
            {"Codex": [video.video_id]},
            {video.video_id: video},
            {channel.channel_id: channel},
            NOW,
            101,
        )
        self.assertIn("本週沒有找到符合條件的影片", report)

    def test_report_structure_and_escaped_title(self):
        video = make_video()
        channel = make_channel(subscribers=100)
        report = generate_report(
            ["Codex"],
            {"Codex": [video.video_id]},
            {video.video_id: video},
            {channel.channel_id: channel},
            NOW,
            101,
        )
        self.assertIn("# YouTube 爆款雷達週報", report)
        self.assertIn("## 本週爆款 TOP 10", report)
        self.assertIn("搜尋範圍：全球", report)
        self.assertIn("## Codex", report)
        self.assertIn("Test \\| Video", report)
        # 不再包含舊版的潛力爆款與語言/地區欄位。
        self.assertNotIn("潛力爆款", report)
        self.assertNotIn("影片語言", report)

    def test_newer_and_high_ratio_score_better(self):
        channel = make_channel(subscribers=100)
        better = make_video(views=10_000, published_at=NOW - timedelta(hours=1))
        worse = make_video(views=1_000, published_at=NOW - timedelta(days=6))
        self.assertGreater(
            composite_score(better, channel, NOW),
            composite_score(worse, channel, NOW),
        )

        zero_channel = make_channel(subscribers=0)
        self.assertTrue(
            math.isfinite(composite_score(make_video(), zero_channel, NOW))
        )

    def test_subscriber_ratio_edge_cases(self):
        self.assertIsNone(subscriber_ratio(make_video(), make_channel(subscribers=None)))
        self.assertTrue(
            math.isinf(subscriber_ratio(make_video(views=5), make_channel(subscribers=0)))
        )
        self.assertEqual(
            subscriber_ratio(make_video(views=200), make_channel(subscribers=100)), 2.0
        )

    def test_search_uses_weekly_global_parameters(self):
        # 攔截 API 請求，確認搜尋條件正確且維持全球範圍（不傳語言/地區）。
        api = YouTubeAPI("test-key")
        captured = {}

        def fake_request(resource, params):
            captured["resource"] = resource
            captured["params"] = params
            return {"items": [{"id": {"videoId": "abc"}}]}

        api.request = fake_request
        result = api.search("Codex", NOW - timedelta(days=7))
        self.assertEqual(result, ["abc"])
        self.assertEqual(captured["resource"], "search")
        self.assertEqual(captured["params"]["type"], "video")
        self.assertEqual(captured["params"]["order"], "viewCount")
        self.assertEqual(captured["params"]["maxResults"], 50)
        self.assertNotIn("regionCode", captured["params"])
        self.assertNotIn("relevanceLanguage", captured["params"])

    def test_quota_estimate_is_per_keyword(self):
        self.assertEqual(estimate_search_quota(7), 700)

    def test_quota_ledger_accumulates_and_reads_back_today(self):
        # 當日消耗會累加，並可在下一次執行前讀回「今日已消耗」。
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "quota_usage.json"
            self.assertEqual(load_quota_usage(NOW, path), 0)

            self.assertEqual(record_quota_usage(NOW, 700, path), 700)
            self.assertEqual(record_quota_usage(NOW, 100, path), 800)
            self.assertEqual(load_quota_usage(NOW, path), 800)

            # 不同日期各自獨立計算。
            tomorrow = NOW + timedelta(days=1)
            self.assertEqual(load_quota_usage(tomorrow, path), 0)
            self.assertEqual(record_quota_usage(tomorrow, 50, path), 50)
            self.assertEqual(load_quota_usage(NOW, path), 800)

    def test_quota_ledger_prunes_old_entries(self):
        # 超過保留天數的舊紀錄會在回寫時被清掉，避免帳本無限增長。
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "quota_usage.json"
            old_day = quota_day_key(NOW - timedelta(days=40))
            path.write_text(f'{{"{old_day}": 9999}}', encoding="utf-8")
            record_quota_usage(NOW, 100, path)
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(old_day, data)
            self.assertEqual(data[quota_day_key(NOW)], 100)

    def test_quota_ledger_tolerates_corrupt_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "quota_usage.json"
            path.write_text("not valid json", encoding="utf-8")
            self.assertEqual(load_quota_usage(NOW, path), 0)
            self.assertEqual(record_quota_usage(NOW, 200, path), 200)

    def test_daily_quota_limit_is_configurable_and_validated(self):
        with patch.dict(os.environ, {"YOUTUBE_DAILY_QUOTA_LIMIT": "12000"}, clear=True):
            self.assertEqual(load_daily_quota_limit(), 12_000)
        with patch.dict(os.environ, {"YOUTUBE_DAILY_QUOTA_LIMIT": "invalid"}, clear=True):
            with self.assertRaises(ValueError):
                load_daily_quota_limit()

    def test_fatal_api_configuration_errors_are_identified(self):
        error = APIError("bad key", 400, ["keyInvalid"])
        self.assertTrue(error.is_fatal_configuration_error)
        self.assertTrue(error.should_stop_run)

    def test_quota_error_is_identified(self):
        error = APIError("quota", 403, ["quotaExceeded"])
        self.assertTrue(error.is_quota_error)
        self.assertTrue(error.should_stop_run)

    def test_local_network_failures_do_not_count_as_api_quota(self):
        # 請求尚未抵達 Google 時不應消耗或累加 API 配額。
        api = YouTubeAPI("test-key", max_retries=0)
        with patch(
            "youtube_viral_radar.urlopen",
            side_effect=URLError("blocked before reaching Google"),
        ):
            with self.assertRaises(APIError):
                api.search("Codex", NOW - timedelta(days=7))
        self.assertEqual(api.quota_used, 0)

    def test_windows_socket_permission_error_stops_without_retries(self):
        # Windows 網路權限錯誤屬本機阻擋，應立即中止而不是重試。
        api = YouTubeAPI("test-key", max_retries=3)
        permission_error = PermissionError(13, "blocked")
        permission_error.winerror = 10013
        with patch(
            "youtube_viral_radar.urlopen",
            side_effect=URLError(permission_error),
        ) as mocked_urlopen:
            with self.assertRaises(APIError) as raised:
                api.search("Codex", NOW - timedelta(days=7))
        self.assertTrue(raised.exception.local_network_blocked)
        self.assertTrue(raised.exception.should_stop_run)
        self.assertEqual(mocked_urlopen.call_count, 1)
        self.assertEqual(api.quota_used, 0)

    def test_get_channels_parses_hidden_and_average(self):
        api = YouTubeAPI("test-key")

        def fake_request(resource, params):
            return {
                "items": [
                    {
                        "id": "channel-1",
                        "snippet": {"title": "Visible"},
                        "statistics": {
                            "subscriberCount": "5000",
                            "viewCount": "1000000",
                            "videoCount": "50",
                            "hiddenSubscriberCount": False,
                        },
                    },
                    {
                        "id": "channel-2",
                        "snippet": {"title": "Hidden"},
                        "statistics": {
                            "viewCount": "2000000",
                            "videoCount": "200",
                            "hiddenSubscriberCount": True,
                        },
                    },
                ]
            }

        api.request = fake_request
        channels = api.get_channels(["channel-1", "channel-2"])
        self.assertEqual(channels["channel-1"].subscribers, 5000)
        self.assertEqual(channels["channel-1"].average_views, 20000.0)
        self.assertIsNone(channels["channel-2"].subscribers)
        self.assertTrue(channels["channel-2"].subscribers_hidden)
        self.assertEqual(channels["channel-2"].average_views, 10000.0)


if __name__ == "__main__":
    unittest.main()
