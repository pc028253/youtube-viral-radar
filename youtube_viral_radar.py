#!/usr/bin/env python3
"""產生 YouTube 每週爆款影片週報。

規格（依需求截圖）：
1. 從同目錄 .env 讀取 YOUTUBE_API_KEY，金鑰不寫死在程式碼。
2. 從同目錄 keywords.txt 讀取關鍵詞，一行一個。
3. 每個關鍵詞用 search.list 搜尋：publishedAfter=近 7 天、type=video、
   order=viewCount、每詞 50 支；不設 regionCode 與 relevanceLanguage（全球）。
4. 用 videos.list 批次（一次最多 50）補抓播放量、發布日期、時長、標題、頻道 ID。
5. 用 channels.list 批次（一次最多 50）補抓訂閱數、頻道總觀看與影片數；
   訂閱數隱藏者標記「訂閱數隱藏」，不報錯、不跳過。
6. 篩出符合任一條件的爆款影片：
   ① 訂閱 < 1 千且播放 ≥ 1 萬
   ② 訂閱 < 1 萬且播放 ≥ 10 萬
   ③ 訂閱 > 1 萬且播放 ≥ 訂閱數 5 倍
   ④ 播放 ≥ 該頻道平均播放量 5 倍，且播放 ≥ 5 萬
7. 去掉時長 < 120 秒、或標題標註 #shorts 的影片（排除 Shorts）。
8. 產出 Markdown 週報，按關鍵詞分區，區內以「發布新鮮度 + 播放/訂閱比」綜合排序。
9. 報表最上方加「本週爆款 TOP 10」跨關鍵詞綜合排序。
10. search.list 配額較貴：印出本次估算與實際配額；單一影片/頻道失敗即跳過不崩潰；
    輸出檔名為 週報-YYYY-MM-DD.md，存於專案資料夾。
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
API_BASE_URL = "https://www.googleapis.com/youtube/v3"
# 本機配額帳本：累計每日（太平洋時間）實際消耗，供「確認當日已消耗額度」使用。
QUOTA_USAGE_FILE = BASE_DIR / "quota_usage.json"
QUOTA_HISTORY_DAYS = 14

# 搜尋範圍、API 批次大小與爆款影片的基本門檻。
SEARCH_DAYS = 7
BATCH_SIZE = 50
SEARCH_RESULTS_PER_KEYWORD = 50
MIN_LONG_VIDEO_SECONDS = 120
# 第 4 條（播放達頻道平均 5 倍）另要求的絕對播放門檻，避免低基數頻道洗版。
MIN_AVERAGE_BREAKOUT_VIEWS = 50_000
DEFAULT_DAILY_QUOTA_LIMIT = 10_000

# YouTube Data API 各端點的配額成本，用來預估與記錄本次執行量。
QUOTA_COSTS = {
    "search": 100,
    "videos": 1,
    "channels": 1,
}


class APIError(RuntimeError):
    # 將 API 回傳原因整理成可判斷的狀態，方便上層決定是否中止流程。
    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        reasons: Iterable[str] = (),
        local_network_blocked: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.reasons = set(reasons)
        self.local_network_blocked = local_network_blocked

    @property
    def is_quota_error(self) -> bool:
        return bool(self.reasons & {"quotaExceeded", "dailyLimitExceeded"})

    @property
    def is_fatal_configuration_error(self) -> bool:
        return bool(
            self.reasons
            & {
                "accessNotConfigured",
                "API_KEY_INVALID",
                "forbidden",
                "ipRefererBlocked",
                "keyInvalid",
            }
        ) or self.status == 401

    @property
    def should_stop_run(self) -> bool:
        return (
            self.is_quota_error
            or self.is_fatal_configuration_error
            or self.local_network_blocked
        )


@dataclass
class Video:
    # 儲存報表排序與輸出所需的影片欄位。
    video_id: str
    title: str
    description: str
    channel_id: str
    channel_title: str
    views: int
    published_at: datetime
    duration_seconds: int
    keywords: set[str] = field(default_factory=set)


@dataclass
class Channel:
    # 訂閱數可能被頻道隱藏，因此使用 Optional[int] 表示未知狀態；
    # 頻道總觀看與影片數則用來計算「頻道平均播放量」。
    channel_id: str
    title: str
    subscribers: Optional[int]
    subscribers_hidden: bool
    total_views: Optional[int] = None
    video_count: Optional[int] = None

    @property
    def average_views(self) -> Optional[float]:
        if self.total_views is None or not self.video_count:
            return None
        return self.total_views / self.video_count


def log(message: str) -> None:
    print(message, file=sys.stderr)


def load_dotenv(path: Path) -> None:
    # 僅補入尚未存在的環境變數，避免覆蓋系統層已設定的值。
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def load_keywords(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"找不到關鍵詞檔案：{path}")
    seen: set[str] = set()
    keywords: list[str] = []
    # 使用 casefold 去除不分大小寫的重複關鍵字，並保留首次出現的寫法。
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        keyword = raw_line.strip()
        folded = keyword.casefold()
        if keyword and folded not in seen:
            seen.add(folded)
            keywords.append(keyword)
    if not keywords:
        raise ValueError("keywords.txt 沒有可用的關鍵詞。")
    return keywords


def load_daily_quota_limit() -> int:
    raw_value = os.environ.get(
        "YOUTUBE_DAILY_QUOTA_LIMIT", str(DEFAULT_DAILY_QUOTA_LIMIT)
    ).strip()
    try:
        limit = int(raw_value)
    except ValueError as exc:
        raise ValueError("YOUTUBE_DAILY_QUOTA_LIMIT 必須是正整數。") from exc
    if limit <= 0:
        raise ValueError("YOUTUBE_DAILY_QUOTA_LIMIT 必須大於 0。")
    return limit


def estimate_search_quota(keyword_count: int) -> int:
    # 每個關鍵字產生一次 search API 呼叫（全球搜尋，不分語言地區）。
    return keyword_count * QUOTA_COSTS["search"]


def load_loose_keywords() -> set[str]:
    # 從環境變數讀取要套用寬鬆門檻的關鍵詞（不分大小寫，以逗號分隔）。
    raw = os.environ.get("LOOSE_KEYWORDS", "").strip()
    return {value.strip().casefold() for value in raw.split(",") if value.strip()}


def _quota_reset_tz() -> timezone:
    # YouTube 配額於太平洋時間午夜重置；有 tzdata 時用正確時區，否則退回固定 UTC-8。
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/Los_Angeles")  # type: ignore[return-value]
    except Exception:
        return timezone(timedelta(hours=-8))


def quota_day_key(now: datetime) -> str:
    # 以太平洋時間的日期作為帳本鍵，與 YouTube 配額重置邊界一致。
    return now.astimezone(_quota_reset_tz()).strftime("%Y-%m-%d")


def load_quota_usage(now: datetime, path: Path = QUOTA_USAGE_FILE) -> int:
    # 讀取當日（太平洋時間）本機已記錄的配額消耗；檔案缺失或毀損時視為 0。
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get(quota_day_key(now), 0))
    except (TypeError, ValueError):
        return 0


def record_quota_usage(
    now: datetime, points: int, path: Path = QUOTA_USAGE_FILE
) -> int:
    # 將本次實際消耗累加到當日總量並回寫，回傳累加後的當日累計；只保留近期紀錄。
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}

    today = quota_day_key(now)
    try:
        previous = int(data.get(today, 0))
    except (TypeError, ValueError):
        previous = 0
    data[today] = previous + int(points)

    cutoff = (
        now.astimezone(_quota_reset_tz()) - timedelta(days=QUOTA_HISTORY_DAYS)
    ).strftime("%Y-%m-%d")
    pruned = {day: value for day, value in data.items() if str(day) >= cutoff}

    try:
        path.write_text(
            json.dumps(pruned, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log(f"警告：無法寫入配額帳本 {path.name}：{exc}")
    return int(pruned[today])


def chunks(values: list[str], size: int = BATCH_SIZE) -> Iterable[list[str]]:
    # videos 與 channels 端點一次最多查詢固定數量的 ID。
    for index in range(0, len(values), size):
        yield values[index : index + size]


def parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
# 標題自我標註為 Shorts（#short / #shorts）的影片視為短影音。
_SHORTS_TITLE_RE = re.compile(r"#shorts?\b", re.IGNORECASE)


def parse_duration(value: str) -> int:
    match = _DURATION_RE.fullmatch(value)
    if not match:
        raise ValueError(f"無法解析影片時長：{value}")
    parts = {name: int(number or 0) for name, number in match.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def format_duration(total_seconds: int) -> str:
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_number(value: Optional[int]) -> str:
    return "訂閱數隱藏" if value is None else f"{value:,}"


def markdown_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("|", "\\|")
    )


def is_short_form(video: Video) -> bool:
    # 時長過短，或標題自我標註為 Shorts，皆視為短影音而排除。
    return (
        video.duration_seconds < MIN_LONG_VIDEO_SECONDS
        or bool(_SHORTS_TITLE_RE.search(video.title))
    )


class YouTubeAPI:
    def __init__(self, api_key: str, timeout: int = 30, max_retries: int = 3):
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.quota_used = 0

    def request(self, resource: str, params: dict[str, Any]) -> dict[str, Any]:
        query = dict(params)
        query["key"] = self.api_key
        url = f"{API_BASE_URL}/{resource}?{urlencode(query)}"
        last_error: Optional[APIError] = None

        # 暫時性網路或伺服器錯誤採指數退避；配額與設定錯誤則立即停止。
        for attempt in range(self.max_retries + 1):
            try:
                request = Request(url, headers={"Accept": "application/json"})
                with urlopen(request, timeout=self.timeout) as response:
                    self.quota_used += QUOTA_COSTS[resource]
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                # 收到 HTTP 回應代表 Google 已接收並處理請求，配額照算。
                self.quota_used += QUOTA_COSTS[resource]
                body = exc.read().decode("utf-8", errors="replace")
                reasons: list[str] = []
                message = f"YouTube API HTTP {exc.code}"
                try:
                    payload = json.loads(body)
                    error = payload.get("error", {})
                    message = error.get("message", message)
                    reasons = [
                        item.get("reason", "")
                        for item in error.get("errors", [])
                        if item.get("reason")
                    ]
                except json.JSONDecodeError:
                    pass
                last_error = APIError(message, exc.code, reasons)
                retryable = exc.code in {408, 429, 500, 502, 503, 504}
                if (
                    last_error.is_quota_error
                    or not retryable
                    or attempt == self.max_retries
                ):
                    raise last_error
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                reason = exc.reason if isinstance(exc, URLError) else exc
                winerror = getattr(reason, "winerror", None)
                local_network_blocked = (
                    isinstance(reason, PermissionError) or winerror == 10013
                )
                if local_network_blocked:
                    raise APIError(
                        "本機網路權限阻擋連線（WinError 10013）。"
                        "請允許 Python 連線到 www.googleapis.com:443，"
                        "或在非受限的終端機執行。",
                        local_network_blocked=True,
                    ) from exc
                last_error = APIError(f"網路或回應解析錯誤：{exc}")
                if attempt == self.max_retries:
                    raise last_error

            time.sleep(2**attempt)

        raise last_error or APIError("未知的 YouTube API 錯誤")

    def search(self, keyword: str, published_after: datetime) -> list[str]:
        # 不設 regionCode 與 relevanceLanguage，維持全球搜尋範圍。
        params: dict[str, Any] = {
            "part": "snippet",
            "q": keyword,
            "publishedAfter": published_after.isoformat().replace("+00:00", "Z"),
            "type": "video",
            "order": "viewCount",
            "maxResults": SEARCH_RESULTS_PER_KEYWORD,
        }
        data = self.request("search", params)
        return [
            item["id"]["videoId"]
            for item in data.get("items", [])
            if item.get("id", {}).get("videoId")
        ]

    def resilient_id_batches(
        self,
        resource: str,
        ids: list[str],
        part: str,
        parser: Callable[[dict[str, Any]], Any],
    ) -> dict[str, Any]:
        output: dict[str, Any] = {}

        def fetch(batch: list[str]) -> None:
            # 批次失敗時二分重試，避免單一無效 ID 讓整批資料都被捨棄。
            if not batch:
                return
            try:
                data = self.request(resource, {"part": part, "id": ",".join(batch)})
                returned_ids: set[str] = set()
                for item in data.get("items", []):
                    item_id = item.get("id")
                    if not item_id:
                        continue
                    try:
                        output[item_id] = parser(item)
                        returned_ids.add(item_id)
                    except (KeyError, TypeError, ValueError) as exc:
                        log(f"略過無法解析的 {resource} 項目 {item_id}：{exc}")
                for missing_id in set(batch) - returned_ids:
                    log(f"{resource} 未回傳 ID {missing_id}，已略過。")
            except APIError as exc:
                if exc.should_stop_run:
                    raise
                if len(batch) == 1:
                    log(f"略過無法取得的 {resource} ID {batch[0]}：{exc}")
                    return
                middle = len(batch) // 2
                fetch(batch[:middle])
                fetch(batch[middle:])

        for batch in chunks(ids):
            fetch(batch)
        return output

    def get_videos(self, video_ids: list[str]) -> dict[str, Video]:
        def parser(item: dict[str, Any]) -> Video:
            snippet = item["snippet"]
            statistics = item.get("statistics", {})
            return Video(
                video_id=item["id"],
                title=snippet["title"],
                description=snippet.get("description", ""),
                channel_id=snippet["channelId"],
                channel_title=snippet.get("channelTitle", ""),
                views=int(statistics.get("viewCount", 0)),
                published_at=parse_iso_datetime(snippet["publishedAt"]),
                duration_seconds=parse_duration(item["contentDetails"]["duration"]),
            )

        return self.resilient_id_batches(
            "videos", video_ids, "snippet,statistics,contentDetails", parser
        )

    def get_channels(self, channel_ids: list[str]) -> dict[str, Channel]:
        def parser(item: dict[str, Any]) -> Channel:
            statistics = item.get("statistics", {})
            hidden = bool(statistics.get("hiddenSubscriberCount", False))
            subscriber_value = statistics.get("subscriberCount")
            subscribers = (
                None if hidden or subscriber_value is None else int(subscriber_value)
            )
            view_value = statistics.get("viewCount")
            count_value = statistics.get("videoCount")
            return Channel(
                channel_id=item["id"],
                title=item.get("snippet", {}).get("title", ""),
                subscribers=subscribers,
                subscribers_hidden=hidden,
                total_views=int(view_value) if view_value is not None else None,
                video_count=int(count_value) if count_value is not None else None,
            )

        return self.resilient_id_batches(
            "channels", channel_ids, "snippet,statistics", parser
        )


def subscriber_ratio(video: Video, channel: Channel) -> Optional[float]:
    if channel.subscribers is None:
        return None
    if channel.subscribers == 0:
        return math.inf if video.views else 0.0
    return video.views / channel.subscribers


@dataclass(frozen=True)
class Thresholds:
    # 各級爆款門檻；寬鬆設定只是把觀看要求與倍數調低，不改變判定邏輯。
    tiny_subs: int      # 訂閱低於此視為極小頻道
    tiny_views: int     # 極小頻道的觀看門檻
    small_subs: int     # 訂閱低於此視為小頻道
    small_views: int    # 小頻道的觀看門檻
    big_ratio: float    # 中大頻道：觀看 ≥ 訂閱數 × 此倍數
    avg_ratio: float    # 觀看 ≥ 頻道平均 × 此倍數
    avg_floor: int      # 且觀看 ≥ 此絕對門檻


STRICT_THRESHOLDS = Thresholds(
    tiny_subs=1_000,
    tiny_views=10_000,
    small_subs=10_000,
    small_views=100_000,
    big_ratio=5.0,
    avg_ratio=5.0,
    avg_floor=MIN_AVERAGE_BREAKOUT_VIEWS,
)
# 寬鬆門檻：給競爭激烈或成熟主題的關鍵詞，讓它們較容易上榜。
LOOSE_THRESHOLDS = Thresholds(
    tiny_subs=1_000,
    tiny_views=5_000,
    small_subs=10_000,
    small_views=50_000,
    big_ratio=3.0,
    avg_ratio=3.0,
    avg_floor=20_000,
)


def _cn_amount(value: int) -> str:
    # 將常用整數轉成「萬／千」表示，供爆款原因文字使用。
    if value >= 10_000 and value % 10_000 == 0:
        return f"{value // 10_000} 萬"
    if value >= 1_000 and value % 1_000 == 0:
        return f"{value // 1_000} 千"
    return f"{value:,}"


def _cn_ratio(value: float) -> str:
    return f"{value:g}"


def breakout_reasons(
    video: Video, channel: Channel, thresholds: Thresholds = STRICT_THRESHOLDS
) -> list[str]:
    # 符合任一條件即視為爆款；訂閱數隱藏者仍可憑「頻道平均」門檻入選。
    t = thresholds
    reasons: list[str] = []
    subscribers = channel.subscribers
    if subscribers is not None:
        if subscribers < t.tiny_subs and video.views >= t.tiny_views:
            reasons.append(
                f"小於 {_cn_amount(t.tiny_subs)}訂閱，播放達 {_cn_amount(t.tiny_views)}"
            )
        if subscribers < t.small_subs and video.views >= t.small_views:
            reasons.append(
                f"小於 {_cn_amount(t.small_subs)}訂閱，播放達 {_cn_amount(t.small_views)}"
            )
        if subscribers > t.small_subs and video.views >= subscribers * t.big_ratio:
            reasons.append(f"播放達訂閱數 {_cn_ratio(t.big_ratio)} 倍")
    average = channel.average_views
    if (
        average
        and average > 0
        and video.views >= average * t.avg_ratio
        and video.views >= t.avg_floor
    ):
        reasons.append(f"播放達頻道平均 {_cn_ratio(t.avg_ratio)} 倍")
    return reasons


def composite_score(video: Video, channel: Channel, now: datetime) -> float:
    # 綜合新鮮度與觀看訂閱比，避免單純依觀看數偏向大型頻道。
    age_seconds = max(0.0, (now - video.published_at).total_seconds())
    window_seconds = SEARCH_DAYS * 86400
    recency_score = max(0.0, 1.0 - min(age_seconds, window_seconds) / window_seconds)
    ratio = subscriber_ratio(video, channel)
    if ratio is None:
        ratio_score = 0.0
    elif math.isinf(ratio):
        ratio_score = 1.0
    else:
        ratio_score = ratio / (ratio + 5.0)
    return 0.5 * recency_score + 0.5 * ratio_score


def _sort_key(item: tuple[Video, Channel, list[str]], now: datetime):
    video, channel, _ = item
    return (
        composite_score(video, channel, now),
        video.published_at.timestamp(),
        subscriber_ratio(video, channel) or -1,
    )


def report_row(
    video: Video,
    channel: Channel,
    now: datetime,
    reasons: Optional[list[str]] = None,
) -> str:
    ratio = subscriber_ratio(video, channel)
    if ratio is None:
        ratio_text = "訂閱數隱藏"
    elif math.isinf(ratio):
        ratio_text = "∞"
    else:
        ratio_text = f"{ratio:.2f}x"
    url = f"https://www.youtube.com/watch?v={video.video_id}"
    cells = [
        f"[{markdown_text(video.title)}]({url})",
        markdown_text(channel.title or video.channel_title),
        format_number(channel.subscribers),
        f"{video.views:,}",
        ratio_text,
        f"{video.published_at:%Y-%m-%d}",
        format_duration(video.duration_seconds),
    ]
    if reasons is not None:
        cells.append(markdown_text("；".join(reasons)) or "—")
    return "| " + " | ".join(cells) + " |"


def append_video_table(
    lines: list[str],
    entries: list[tuple[Video, Channel, list[str]]],
    now: datetime,
    include_reasons: bool = True,
) -> None:
    headers = ["影片標題", "頻道名稱", "訂閱數", "播放量", "播放/訂閱比", "發布日期", "時長"]
    aligns = ["---", "---", "---:", "---:", "---:", "---", "---:"]
    if include_reasons:
        headers.append("入選原因")
        aligns.append("---")
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(aligns) + " |")
    for video, channel, reasons in entries:
        lines.append(
            report_row(video, channel, now, reasons if include_reasons else None)
        )


# 趨勢摘要用的停用詞（不具辨識度的常見字），萃取高頻詞時排除。
_TREND_STOPWORDS = {
    "the", "a", "an", "to", "of", "in", "for", "with", "and", "or", "on", "is", "are",
    "be", "my", "you", "your", "this", "that", "it", "its", "how", "what", "why", "when",
    "i", "me", "we", "us", "vs", "new", "best", "top", "use", "using", "make", "made",
    "get", "got", "do", "does", "did", "can", "will", "just", "now", "not", "from",
    "ai", "video", "videos", "youtube", "tutorial", "review", "guide", "ep",
    "教學", "教程", "介紹", "完整", "最新", "如何", "這個", "影片", "頻道", "訂閱",
}


def _trend_tokens(text: str) -> set[str]:
    # 從標題抽出可當「主題詞」的 token：英數詞（≥2 字）與短中文詞（2–4 字）。
    lower = text.lower()
    english = re.findall(r"[a-z][a-z0-9+#.]+", lower)
    chinese = [run for run in re.findall(r"[㐀-鿿]+", text) if 2 <= len(run) <= 4]
    tokens: set[str] = set()
    for token in english + chinese:
        token = token.strip(".")
        if len(token) >= 2 and not token.isdigit():
            tokens.add(token)
    return tokens


def _common_terms(titles: list[str], keyword: str, limit: int = 3) -> list[str]:
    # 以「出現於幾支影片」（document frequency）找反覆出現的主題詞。
    keyword_tokens = _trend_tokens(keyword)
    frequency: dict[str, int] = {}
    for title in titles:
        for token in _trend_tokens(title):
            if token in _TREND_STOPWORDS or token in keyword_tokens:
                continue
            frequency[token] = frequency.get(token, 0) + 1
    common = sorted(
        ((term, count) for term, count in frequency.items() if count >= 2),
        key=lambda item: (-item[1], item[0]),
    )
    return [term for term, _ in common[:limit]]


def _readable_views(value: int) -> str:
    return f"{value / 10_000:.1f} 萬" if value >= 10_000 else f"{value:,}"


def _keyword_trend_comment(
    keyword: str,
    breakout_entries: list[tuple[Video, Channel, list[str]]],
    candidate_videos: list[Video],
    now: datetime,
) -> str:
    # 依當週數據自動歸納單一領域的趨勢（不使用 AI）。
    breakout_videos = [video for video, _, _ in breakout_entries]
    material = breakout_videos or candidate_videos
    if not material:
        return "本週無相關影片資料。"

    parts: list[str] = []
    if breakout_videos:
        parts.append(f"{len(breakout_videos)} 支爆款")
    else:
        parts.append(f"無爆款（{len(candidate_videos)} 支候選）")

    top = max(material, key=lambda video: video.views)
    top_title = top.title.strip()
    if len(top_title) > 30:
        top_title = top_title[:30] + "…"
    parts.append(f"熱度最高〈{markdown_text(top_title)}〉約 {_readable_views(top.views)}次")

    terms = _common_terms([video.title for video in material], keyword)
    if terms:
        parts.append("熱門詞：" + "、".join(terms))

    reason_frequency: dict[str, int] = {}
    for _, _, reasons in breakout_entries:
        for reason in reasons:
            reason_frequency[reason] = reason_frequency.get(reason, 0) + 1
    if reason_frequency:
        dominant = max(reason_frequency.items(), key=lambda item: item[1])[0]
        parts.append(f"多因「{dominant}」")

    return "；".join(parts) + "。"


def generate_report(
    keywords: list[str],
    keyword_to_ids: dict[str, list[str]],
    videos: dict[str, Video],
    channels: dict[str, Channel],
    now: datetime,
    quota_used: int,
    loose_keywords: Iterable[str] = (),
) -> str:
    loose = {value.casefold() for value in loose_keywords}

    def thresholds_for(keyword: str) -> Thresholds:
        return LOOSE_THRESHOLDS if keyword.casefold() in loose else STRICT_THRESHOLDS

    # 各關鍵詞用自己的門檻判定；同一支影片只要在任一關鍵詞達標即列入 TOP 10。
    # 長影片與 Shorts 分開累計，各自產出一份全域 TOP 10。
    keyword_breakouts: dict[str, list[tuple[Video, Channel, list[str]]]] = {}
    keyword_candidates: dict[str, list[Video]] = {}
    keyword_long_counts: dict[str, int] = {}
    keyword_shorts_counts: dict[str, int] = {}
    global_qualifying: dict[str, tuple[Video, Channel, list[str]]] = {}
    global_shorts_qualifying: dict[str, tuple[Video, Channel, list[str]]] = {}

    for keyword in keywords:
        thresholds = thresholds_for(keyword)
        entries: list[tuple[Video, Channel, list[str]]] = []
        candidates: list[Video] = []
        long_count = 0
        shorts_count = 0
        for video_id in dict.fromkeys(keyword_to_ids.get(keyword, [])):
            video = videos.get(video_id)
            if not video:
                continue
            channel = channels.get(video.channel_id)
            if not channel:
                continue
            candidates.append(video)
            reasons = breakout_reasons(video, channel, thresholds)
            if is_short_form(video):
                shorts_count += 1
                if reasons:
                    previous = global_shorts_qualifying.get(video_id)
                    if previous is None or len(reasons) > len(previous[2]):
                        global_shorts_qualifying[video_id] = (video, channel, reasons)
                continue
            long_count += 1
            if reasons:
                entries.append((video, channel, reasons))
                previous = global_qualifying.get(video_id)
                if previous is None or len(reasons) > len(previous[2]):
                    global_qualifying[video_id] = (video, channel, reasons)
        entries.sort(key=lambda item: _sort_key(item, now), reverse=True)
        keyword_breakouts[keyword] = entries
        keyword_candidates[keyword] = candidates
        keyword_long_counts[keyword] = long_count
        keyword_shorts_counts[keyword] = shorts_count

    ranked = sorted(
        global_qualifying.values(), key=lambda item: _sort_key(item, now), reverse=True
    )
    shorts_ranked = sorted(
        global_shorts_qualifying.values(), key=lambda item: _sort_key(item, now), reverse=True
    )
    loose_display = [keyword for keyword in keywords if keyword.casefold() in loose]

    lines = [
        f"# YouTube 爆款雷達週報 - {now:%Y-%m-%d}",
        "",
        f"- 搜尋期間：{(now - timedelta(days=SEARCH_DAYS)):%Y-%m-%d} 至 {now:%Y-%m-%d}（UTC）",
        "- 搜尋範圍：全球（未限制地區與語言）",
        f"- 關鍵詞數：{len(keywords)}",
        f"- 長影片候選（去重）：{sum(1 for v in videos.values() if not is_short_form(v))}",
        f"- Shorts 候選（去重）：{sum(1 for v in videos.values() if is_short_form(v))}",
        f"- 長影片爆款：{len(global_qualifying)}",
        f"- Shorts 爆款：{len(global_shorts_qualifying)}",
        f"- 本次 API 配額估算：{quota_used:,} 點",
    ]
    if loose_display:
        lines.append(
            "- 寬鬆門檻關鍵詞：" + "、".join(markdown_text(k) for k in loose_display)
        )

    lines.extend(["", "## 本週各領域趨勢發現", ""])
    for keyword in keywords:
        comment = _keyword_trend_comment(
            keyword,
            keyword_breakouts.get(keyword, []),
            keyword_candidates.get(keyword, []),
            now,
        )
        lines.append(f"- **{markdown_text(keyword)}**：{comment}")

    lines.extend(["", "## 本週爆款 TOP 10", ""])
    if ranked:
        append_video_table(lines, ranked[:10], now)
    else:
        lines.append("本週沒有找到符合條件的影片。")

    lines.extend(["", "## YouTube Shorts 本週爆款 TOP 10", ""])
    if shorts_ranked:
        append_video_table(lines, shorts_ranked[:10], now)
    else:
        lines.append("本週沒有找到符合條件的 Shorts。")

    for keyword in keywords:
        suffix = "（寬鬆門檻）" if keyword.casefold() in loose else ""
        lines.extend(["", f"## {markdown_text(keyword)}{suffix}", ""])
        searched = len(dict.fromkeys(keyword_to_ids.get(keyword, [])))
        entries = keyword_breakouts.get(keyword, [])
        lines.append(
            f"- 診斷：搜尋 {searched} 支 → 長影片 "
            f"{keyword_long_counts.get(keyword, 0)} 支（Shorts "
            f"{keyword_shorts_counts.get(keyword, 0)} 支另計入 Shorts TOP 10）→ "
            f"爆款 {len(entries)} 支"
        )
        lines.append("")
        if entries:
            append_video_table(lines, entries, now)
        else:
            lines.append("本關鍵詞沒有符合條件的影片。")

    lines.extend(
        [
            "",
            "---",
            "",
            "排序分數：近 7 天發布新鮮度與播放/訂閱比各占 50%；訂閱數隱藏時比例分數記為 0。",
            "各領域趨勢發現：依當週影片標題高頻詞、最高熱度影片與主要入選原因自動歸納（純數據，未使用 AI）。",
            "診斷：搜尋＝該關鍵詞搜回的影片數；長影片＝排除 Shorts 後可評分的數量；爆款＝達標數。",
            "時長 < 120 秒、或標題標註 #shorts 者視為 Shorts，不計入長影片榜，"
            "改用同一套門檻另外計入「YouTube Shorts 本週爆款 TOP 10」。",
            "嚴格門檻（符合任一）：小於 1 千訂閱播放達 1 萬、小於 1 萬訂閱播放達 10 萬、"
            "播放達訂閱數 5 倍，或播放達頻道平均 5 倍（且播放 ≥ 5 萬）。",
            "寬鬆門檻（標示「寬鬆門檻」的關鍵詞）：小於 1 千訂閱播放達 5 千、"
            "小於 1 萬訂閱播放達 5 萬、播放達訂閱數 3 倍，或播放達頻道平均 3 倍（且播放 ≥ 2 萬）。",
            "訂閱數隱藏的頻道仍可憑頻道平均門檻入選。",
            "",
        ]
    )
    return "\n".join(lines)


def run_report(
    api: YouTubeAPI,
    keywords: list[str],
    now: datetime,
    loose_keywords: Iterable[str] = (),
) -> int:
    # 執行搜尋、補抓、篩選與輸出；回傳行程結束碼。配額帳本由呼叫端統一記錄。
    published_after = now - timedelta(days=SEARCH_DAYS)
    keyword_to_ids: dict[str, list[str]] = {}
    all_video_ids: list[str] = []
    successful_searches = 0
    quota_exhausted = False
    stop_search = False

    # 部分搜尋失敗時保留成功結果，只有致命錯誤才提前中止。
    for index, keyword in enumerate(keywords, start=1):
        log(f"搜尋關鍵詞 {index}/{len(keywords)}：{keyword}（全球）")
        try:
            ids = api.search(keyword, published_after)
        except APIError as exc:
            if exc.is_quota_error:
                quota_exhausted = True
                log("警告：YouTube API 今日配額已用完。搜尋立即停止，且不會覆寫既有週報。")
            log(f"關鍵詞「{keyword}」搜尋失敗，已略過：{exc}")
            if exc.should_stop_run:
                stop_search = True
                break
            keyword_to_ids[keyword] = []
            continue
        keyword_to_ids[keyword] = list(dict.fromkeys(ids))
        all_video_ids.extend(keyword_to_ids[keyword])
        successful_searches += 1

    if quota_exhausted or stop_search:
        if not quota_exhausted:
            log("搜尋因致命錯誤中止，不會產生或覆寫週報。")
        return 2

    if successful_searches == 0:
        log("錯誤：所有 YouTube 搜尋都失敗，未產生或覆寫週報。")
        return 1

    unique_video_ids = list(dict.fromkeys(all_video_ids))
    videos: dict[str, Video] = {}
    channels: dict[str, Channel] = {}
    try:
        videos = api.get_videos(unique_video_ids)
        # 保留所有影片（含 Shorts），讓 generate_report 分別產出長影片與 Shorts 排行。
        for keyword, ids in keyword_to_ids.items():
            for video_id in ids:
                if video_id in videos:
                    videos[video_id].keywords.add(keyword)
        channel_ids = list(dict.fromkeys(video.channel_id for video in videos.values()))
        channels = api.get_channels(channel_ids)
    except APIError as exc:
        if exc.is_quota_error:
            log("警告：YouTube API 今日配額已用完。資料補抓未完成，不會產生或覆寫週報。")
            return 2
        log(f"API 資料補抓提前停止：{exc}")

    report = generate_report(
        keywords, keyword_to_ids, videos, channels, now, api.quota_used, loose_keywords
    )
    output_path = BASE_DIR / f"週報-{now:%Y-%m-%d}.md"
    try:
        output_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        log(f"無法寫入週報：{exc}")
        return 1

    print(f"週報已產生：{output_path}")
    return 0


def main() -> int:
    # 主流程：載入設定、確認當日配額、執行報表，並於結束時更新配額帳本。
    now = datetime.now(timezone.utc)
    load_dotenv(BASE_DIR / ".env")
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        log("錯誤：請在同目錄 .env 設定 YOUTUBE_API_KEY。")
        return 1

    try:
        keywords = load_keywords(BASE_DIR / "keywords.txt")
        daily_quota_limit = load_daily_quota_limit()
    except (OSError, ValueError) as exc:
        log(f"錯誤：{exc}")
        return 1

    loose_keywords = load_loose_keywords()
    active_loose = [k for k in keywords if k.casefold() in loose_keywords]
    if active_loose:
        print("寬鬆門檻關鍵詞：" + "、".join(active_loose))

    # 先確認當日（太平洋時間）已消耗的配額，並換算本次執行後的預估累計。
    estimated_search_quota = estimate_search_quota(len(keywords))
    already_used_today = load_quota_usage(now)
    projected_total = already_used_today + estimated_search_quota
    print(f"今日已消耗配額（太平洋時間，本機帳本估算）：{already_used_today:,} 點")
    print(
        f"本次搜尋預估配額：{estimated_search_quota:,} 點，"
        f"預估執行後當日累計：{projected_total:,} / 每日上限 {daily_quota_limit:,} 點"
    )
    if projected_total > daily_quota_limit:
        log(
            "警告：預估執行後當日累計配額已超過每日上限！"
            "執行期間可能因 YouTube 配額耗盡而提前停止。"
        )
    elif projected_total >= daily_quota_limit * 0.8:
        log("警告：預估執行後當日累計配額已達每日上限的 80% 以上，請避免今日再次執行。")

    api = YouTubeAPI(api_key)
    try:
        return run_report(api, keywords, now, loose_keywords)
    finally:
        # 不論成功或中途中止，皆把本次實際消耗計入當日帳本。
        daily_total = record_quota_usage(now, api.quota_used)
        print(f"本次實際消耗 API 配額（估算）：{api.quota_used:,} 點")
        print(
            f"今日累計實際消耗（本機帳本估算）：{daily_total:,} 點 / "
            f"每日上限 {daily_quota_limit:,} 點"
        )


if __name__ == "__main__":
    raise SystemExit(main())
