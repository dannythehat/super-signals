import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.claudy_fed_rss import (
    FED_RSS_FEEDS,
    ClaudyFedRssRecorderManager,
    ClaudyFedRssRecorderService,
    FedRssError,
    FedRssGateway,
    parse_fed_rss,
)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Fed</title>
<item><guid>https://www.federalreserve.gov/newsevents/a.htm</guid>
<title>Federal Reserve issues FOMC statement</title>
<link>https://www.federalreserve.gov/newsevents/a.htm</link>
<pubDate>Wed, 29 Jul 2026 14:00:00 -0400</pubDate>
<description>Statement text.</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>tag:fed:test-1</id><title>Economic outlook</title>
<link href="https://www.federalreserve.gov/newsevents/speech/test.htm" />
<updated>2026-07-16T16:00:00Z</updated><summary>Speech.</summary></entry>
</feed>"""


def test_rss_parser_preserves_point_in_time_identity_and_utc_timestamp() -> None:
    rows = parse_fed_rss(RSS, feed_key="fed_press_monetary")
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "federal_reserve_rss"
    assert str(row["external_id"]).startswith("fed_press_monetary:")
    assert row["headline"] == "Federal Reserve issues FOMC statement"
    assert row["published_at"] == datetime(2026, 7, 29, 18, 0, tzinfo=UTC)
    assert len(str(row["payload_digest"])) == 64


def test_atom_parser_is_supported_without_changing_evidence_shape() -> None:
    rows = parse_fed_rss(ATOM, feed_key="fed_speeches")
    assert len(rows) == 1
    assert rows[0]["external_id"] == "fed_speeches:tag:fed:test-1"
    assert rows[0]["published_at"] == datetime(2026, 7, 16, 16, 0, tzinfo=UTC)


def test_gateway_allowlist_contains_only_fixed_official_https_feeds() -> None:
    assert set(FED_RSS_FEEDS) == {"fed_press_monetary", "fed_speeches", "fed_testimony"}
    assert all(
        url.startswith("https://www.federalreserve.gov/feeds/")
        for url in FED_RSS_FEEDS.values()
    )


def test_gateway_rejects_non_allowlisted_url_before_network() -> None:
    gateway = FedRssGateway()
    with pytest.raises(FedRssError, match="not_allowed"):
        asyncio.run(
            gateway.fetch(
                feed_key="fed_speeches",
                url="https://example.com/feed.xml",
            )
        )


class FakeRepository:
    def __init__(self, *, has_demo_account: bool = True) -> None:
        self.has_demo_account = has_demo_account
        self.rows: dict[tuple[str, str, str], int] = {}

    def load_reference_demo_account(self, owner_user_id):
        return {"id": "demo"} if self.has_demo_account else None

    def store_event_observation(self, **kwargs):
        key = (kwargs["source"], kwargs["external_id"], kwargs["payload_digest"])
        if key in self.rows:
            return "same-id", self.rows[key], False
        revision = 1 + max(
            [
                value
                for (source, external_id, _), value in self.rows.items()
                if source == kwargs["source"] and external_id == kwargs["external_id"]
            ],
            default=0,
        )
        self.rows[key] = revision
        return f"id-{len(self.rows)}", revision, True


class ThreeFeedGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, *, feed_key: str, url: str) -> str:
        self.calls += 1
        assert url == FED_RSS_FEEDS[feed_key]
        return RSS


def _service(repository: FakeRepository, gateway) -> ClaudyFedRssRecorderService:
    return ClaudyFedRssRecorderService(
        reference_user_id=uuid4(),
        repository=repository,  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )


def test_recorder_is_idempotent_across_repeated_feed_polls() -> None:
    repository = FakeRepository()
    gateway = ThreeFeedGateway()
    service = _service(repository, gateway)
    now = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)

    first = asyncio.run(service.capture_once(now=now))
    second = asyncio.run(service.capture_once(now=now))

    assert first.feeds_checked == 3
    assert first.feeds_failed == 0
    assert first.observations_added == 3
    assert second.observations_added == 0


def test_missing_reference_demo_account_blocks_external_feed_requests() -> None:
    repository = FakeRepository(has_demo_account=False)
    gateway = ThreeFeedGateway()
    service = _service(repository, gateway)

    result = asyncio.run(
        service.capture_once(now=datetime(2026, 8, 15, 5, 30, tzinfo=UTC))
    )

    assert result.feeds_checked == 0
    assert result.observations_added == 0
    assert gateway.calls == 0


class OneFailingFeedGateway:
    async def fetch(self, *, feed_key: str, url: str) -> str:
        if feed_key == "fed_speeches":
            raise FedRssError("fed_rss_timeout")
        return RSS


def test_one_feed_failure_does_not_erase_other_official_evidence() -> None:
    repository = FakeRepository()
    service = _service(repository, OneFailingFeedGateway())
    result = asyncio.run(
        service.capture_once(now=datetime(2026, 8, 15, 5, 30, tzinfo=UTC))
    )

    assert result.feeds_failed == 1
    assert result.observations_added == 2


class FailingFeedService:
    async def capture_once(self, **kwargs):
        raise RuntimeError("rss failure")


def test_feed_manager_failure_is_isolated_and_keeps_two_minute_cadence() -> None:
    delays: list[float] = []

    async def stop_after_sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    manager = ClaudyFedRssRecorderManager(
        FailingFeedService(),  # type: ignore[arg-type]
        poll_seconds=120,
        sleep=stop_after_sleep,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager._run())
    assert delays == [120]


NAIVE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Fed</title>
<item><guid>naive-1</guid><title>No offset stated</title>
<pubDate>Wed, 29 Jul 2026 14:00:00</pubDate></item>
</channel></rss>"""

BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
]>
<rss version="2.0"><channel><item><title>&lol2;</title></item></channel></rss>"""

FEED_KEY = "fed_press_monetary"
FEED_URL = FED_RSS_FEEDS[FEED_KEY]


def test_naive_publication_time_is_unknown_rather_than_assumed_utc() -> None:
    rows = parse_fed_rss(NAIVE_RSS, feed_key=FEED_KEY)

    assert len(rows) == 1
    # The Fed publishes in Eastern Time. Assuming UTC would mis-stamp this by four hours,
    # so an offset-less timestamp must be recorded as unknown instead of guessed.
    assert rows[0]["published_at"] is None
    assert "14:00:00" in str(rows[0]["raw_payload_json"])


def test_entity_bearing_xml_is_refused_before_parsing() -> None:
    with pytest.raises(FedRssError, match="doctype_not_allowed"):
        parse_fed_rss(BILLION_LAUGHS, feed_key=FEED_KEY)


def test_unchanged_feed_is_revalidated_and_never_reparsed() -> None:
    seen_requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(dict(request.headers))
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304, headers={"ETag": '"v1"'})
        return httpx.Response(200, text=RSS, headers={"ETag": '"v1"'})

    gateway = FedRssGateway(transport=httpx.MockTransport(handler))

    first = asyncio.run(gateway.fetch(feed_key=FEED_KEY, url=FEED_URL))
    second = asyncio.run(gateway.fetch(feed_key=FEED_KEY, url=FEED_URL))

    assert first is not None
    assert second is None
    assert "If-None-Match" not in seen_requests[0]
    assert seen_requests[1]["if-none-match"] == '"v1"'


def test_unchanged_feed_costs_no_parsing_and_no_dedupe_queries() -> None:
    class ConditionalGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch(self, *, feed_key: str, url: str) -> str | None:
            self.calls += 1
            return RSS if self.calls <= len(FED_RSS_FEEDS) else None

    repository = FakeRepository()
    gateway = ConditionalGateway()
    service = _service(repository, gateway)
    now = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)

    first = asyncio.run(service.capture_once(now=now))
    stored_after_first = len(repository.rows)
    second = asyncio.run(service.capture_once(now=now))

    assert first.observations_added == 3
    assert first.feeds_unchanged == 0
    assert second.feeds_unchanged == 3
    assert second.observations_seen == 0
    assert len(repository.rows) == stored_after_first


def test_declared_oversize_feed_is_refused_without_reading_the_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<rss/>",
            headers={"Content-Length": "9000000"},
        )

    gateway = FedRssGateway(transport=httpx.MockTransport(handler))

    with pytest.raises(FedRssError, match="too_large"):
        asyncio.run(gateway.fetch(feed_key=FEED_KEY, url=FEED_URL))


def test_streamed_body_stops_at_the_two_megabyte_ceiling() -> None:
    async def oversized():
        for _ in range(4):
            yield b"x" * 600_000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized())

    gateway = FedRssGateway(transport=httpx.MockTransport(handler))

    with pytest.raises(FedRssError, match="too_large"):
        asyncio.run(gateway.fetch(feed_key=FEED_KEY, url=FEED_URL))
