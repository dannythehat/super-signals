"""Official Federal Reserve RSS capture for Claudy Phase 0-lite.

Only fixed federalreserve.gov HTTPS feeds are fetched. Parsed observations are
append-only market evidence; this module has no trading or Telegram capability.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
from uuid import UUID
from xml.etree import ElementTree

import httpx

from app.claudy_market_repository import ClaudyMarketRepository

logger = logging.getLogger(__name__)

FED_RSS_FEEDS = {
    "fed_press_monetary": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "fed_speeches": "https://www.federalreserve.gov/feeds/speeches.xml",
    "fed_testimony": "https://www.federalreserve.gov/feeds/testimony.xml",
}
_MAX_FEED_BYTES = 2_000_000


class FedRssError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FedRssCaptureResult:
    feeds_checked: int
    feeds_failed: int
    observations_seen: int
    observations_added: int
    broker_trade_action_created: bool = False


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Fed RSS timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(element: ElementTree.Element, *names: str) -> str | None:
    wanted = set(names)
    for child in element.iter():
        if _local_name(child.tag) in wanted and child.text and child.text.strip():
            return child.text.strip()
    return None


def _atom_link(element: ElementTree.Element) -> str | None:
    for child in element.iter():
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href and href.strip():
            return href.strip()
        if child.text and child.text.strip():
            return child.text.strip()
    return None


def _published_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    value = raw.strip()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def parse_fed_rss(xml_text: str, *, feed_key: str) -> list[dict[str, object]]:
    """Parse RSS 2.0 or Atom into a small deterministic evidence shape."""
    encoded = xml_text.encode("utf-8")
    if len(encoded) > _MAX_FEED_BYTES:
        raise FedRssError("fed_rss_feed_too_large")
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise FedRssError("fed_rss_invalid_xml") from exc

    items = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]
    observations: list[dict[str, object]] = []
    for item in items:
        title = _first_text(item, "title")
        link = _first_text(item, "link") or _atom_link(item)
        external_id = _first_text(item, "guid", "id") or link
        published_raw = _first_text(item, "pubDate", "published", "updated")
        description = _first_text(item, "description", "summary", "content")
        if not external_id:
            seed = "|".join((feed_key, title or "", published_raw or ""))
            external_id = sha256(seed.encode("utf-8")).hexdigest()

        raw_payload = {
            "feed_key": feed_key,
            "external_id": external_id,
            "title": title,
            "link": link,
            "published": published_raw,
            "description": description,
        }
        payload_json = json.dumps(raw_payload, sort_keys=True, separators=(",", ":"))
        observations.append(
            {
                "source": "federal_reserve_rss",
                "external_id": f"{feed_key}:{external_id}",
                "event_type": feed_key,
                "published_at": _published_at(published_raw),
                "headline": title,
                "structured_data_json": json.dumps(
                    {"feed_key": feed_key, "link": link},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "raw_payload_json": payload_json,
                "payload_digest": sha256(payload_json.encode("utf-8")).hexdigest(),
            }
        )
    return observations


class FedRssGateway:
    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self._timeout = httpx.Timeout(timeout_seconds)

    async def fetch(self, *, feed_key: str, url: str) -> str:
        expected = FED_RSS_FEEDS.get(feed_key)
        if expected is None or url != expected:
            raise FedRssError("fed_rss_feed_not_allowed")
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                response = await client.get(
                    url,
                    headers={
                        "Accept": "application/rss+xml, application/xml, text/xml",
                        "User-Agent": "SuperSignals-ClaudyRecorder/1.0",
                    },
                )
        except httpx.TimeoutException as exc:
            raise FedRssError("fed_rss_timeout") from exc
        except httpx.HTTPError as exc:
            raise FedRssError("fed_rss_unreachable") from exc
        if response.status_code != 200:
            raise FedRssError(f"fed_rss_http_{response.status_code}")
        if len(response.content) > _MAX_FEED_BYTES:
            raise FedRssError("fed_rss_feed_too_large")
        return response.text


class ClaudyFedRssRecorderService:
    def __init__(
        self,
        *,
        repository: ClaudyMarketRepository,
        gateway: FedRssGateway,
    ) -> None:
        self._repository = repository
        self._gateway = gateway

    async def capture_once(self, *, now: datetime | None = None) -> FedRssCaptureResult:
        observed_at = _utc(now or datetime.now(UTC))
        failures = 0
        seen = 0
        added = 0
        for feed_key, url in FED_RSS_FEEDS.items():
            try:
                xml_text = await self._gateway.fetch(feed_key=feed_key, url=url)
                observations = parse_fed_rss(xml_text, feed_key=feed_key)
            except FedRssError as exc:
                failures += 1
                logger.warning("Claudy Fed RSS capture failed feed=%s code=%s", feed_key, exc)
                continue
            seen += len(observations)
            for observation in observations:
                _, _, created = self._repository.store_event_observation(
                    source=str(observation["source"]),
                    external_id=str(observation["external_id"]),
                    event_type=str(observation["event_type"]),
                    published_at=observation["published_at"],
                    first_observed_at=observed_at,
                    headline=(
                        str(observation["headline"])
                        if observation["headline"] is not None
                        else None
                    ),
                    structured_data_json=str(observation["structured_data_json"]),
                    raw_payload_json=str(observation["raw_payload_json"]),
                    payload_digest=str(observation["payload_digest"]),
                )
                added += int(created)
        return FedRssCaptureResult(
            feeds_checked=len(FED_RSS_FEEDS),
            feeds_failed=failures,
            observations_seen=seen,
            observations_added=added,
        )


class ClaudyFedRssRecorderManager:
    """Independent 120-second official-event loop; failures never stop market capture."""

    def __init__(
        self,
        service: ClaudyFedRssRecorderService,
        *,
        poll_seconds: float = 120.0,
        sleep=asyncio.sleep,
    ) -> None:
        self._service = service
        self._poll_seconds = max(float(poll_seconds), 30.0)
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="claudy-phase0-fed-rss-recorder",
            )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            try:
                await self._service.capture_once(now=datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Claudy Phase 0-lite Fed RSS cycle failed safely")
            await self._sleep(self._poll_seconds)


def build_claudy_fed_rss_recorder_manager(*, session_factory) -> ClaudyFedRssRecorderManager | None:
    enabled = os.getenv("SUPER_SIGNALS_CLAUDY_CAPTURE_ENABLED", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    reference_raw = os.getenv("SUPER_SIGNALS_CLAUDY_REFERENCE_USER_ID", "").strip()
    try:
        UUID(reference_raw)
    except ValueError:
        logger.error(
            "Claudy Fed RSS recorder disabled: reference demo user id is missing or invalid"
        )
        return None
    raw_poll = os.getenv("SUPER_SIGNALS_CLAUDY_FED_RSS_POLL_SECONDS", "120").strip()
    try:
        poll_seconds = float(raw_poll)
    except ValueError:
        logger.error("Claudy Fed RSS recorder disabled: poll interval is invalid")
        return None
    if poll_seconds <= 0:
        logger.error("Claudy Fed RSS recorder disabled: poll interval must be positive")
        return None
    repository = ClaudyMarketRepository(session_factory)
    service = ClaudyFedRssRecorderService(repository=repository, gateway=FedRssGateway())
    return ClaudyFedRssRecorderManager(service, poll_seconds=poll_seconds)
