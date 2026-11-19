"""The SNCF GTFS-RT integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
import time

from google.transit import gtfs_realtime_pb2

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CANCELED,
    DOMAIN,
    GTFS_RT_ALERTS_URL,
    GTFS_RT_URL,
    SKIPPED,
    UPDATE_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR]
FRONTEND_SCRIPT_BASE_URL = "/sncf_gtfs_static/sncf-cards.js"


def extract_translated_text(translated_string) -> str:
    """Extracts French text from a Protobuf TranslatedString."""
    if not translated_string.translation:
        return ""
    for t in translated_string.translation:
        if t.language in ("fr", "FR", ""):
            return t.text.strip()
    return translated_string.translation[0].text.strip()


def extract_train_number(trip_id: str) -> str:
    """Extracts the commercial train number from the SNCF trip_id."""
    match = re.search(r"OCESN(\d+)", trip_id)
    if match:
        return match.group(1)
    generic_match = re.search(r"(\d{4,6})", trip_id)
    return generic_match.group(1) if generic_match else trip_id


class SncfGtfsCoordinator(DataUpdateCoordinator):
    """Shared coordinator fetching and indexing the SNCF GTFS-RT feeds."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.session = async_get_clientsession(hass)

    async def _fetch_bytes(self, url: str) -> bytes:
        async with self.session.get(url, timeout=15) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _async_update_data(self) -> dict:
        try:
            trips_bytes, alerts_result = await asyncio.gather(
                self._fetch_bytes(GTFS_RT_URL),
                self._fetch_bytes(GTFS_RT_ALERTS_URL),
                return_exceptions=True,
            )
            if isinstance(trips_bytes, Exception):
                raise trips_bytes
            alerts_bytes = alerts_result if isinstance(alerts_result, bytes) else None
        except Exception as err:
            raise UpdateFailed(f"Error fetching SNCF GTFS-RT feed: {err}") from err

        return await self.hass.async_add_executor_job(
            self._parse_feeds, trips_bytes, alerts_bytes
        )

    def _extract_alert(self, alert, now_ts: float, alerts: list[dict]) -> None:
        """Validate active period and extract targets and text from a GTFS-RT Alert."""
        if alert.active_period:
            is_active = any(
                (not p.start or p.start <= now_ts) and (not p.end or p.end >= now_ts)
                for p in alert.active_period
            )
            if not is_active:
                return

        trip_targets = []
        route_targets = []
        stop_targets = []
        for ie in alert.informed_entity:
            if ie.HasField("trip") and ie.trip.trip_id:
                trip_targets.append(ie.trip.trip_id)
            if ie.HasField("route_id") and ie.route_id:
                route_targets.append(ie.route_id)
            if ie.HasField("stop_id") and ie.stop_id:
                stop_targets.append(ie.stop_id)

        title = extract_translated_text(alert.header_text)
        description = extract_translated_text(alert.description_text)
        if not title and not description:
            return

        alerts.append({
            "trip_ids": trip_targets,
            "route_ids": route_targets,
            "stop_ids": stop_targets,
            "title": title or description,
            "description": description,
        })

    def _parse_feeds(self, trips_bytes: bytes, alerts_bytes: bytes | None) -> dict:
        now_ts = time.time()
        trips = []
        alerts = []

        # 1. TripUpdates feed
        trips_feed = gtfs_realtime_pb2.FeedMessage()
        trips_feed.ParseFromString(trips_bytes)

        for entity in trips_feed.entity:
            if entity.HasField("alert"):
                self._extract_alert(entity.alert, now_ts, alerts)
                continue

            if not entity.HasField("trip_update"):
                continue

            tu = entity.trip_update
            trip_id = tu.trip.trip_id
            route_id = tu.trip.route_id
            train_number = extract_train_number(trip_id)
            trip_canceled = tu.trip.schedule_relationship == CANCELED

            stops = []
            for stu in tu.stop_time_update:
                dep_ts = stu.departure.time if (stu.HasField("departure") and stu.departure.time) else None
                arr_ts = stu.arrival.time if (stu.HasField("arrival") and stu.arrival.time) else None
                dep_delay = stu.departure.delay if stu.HasField("departure") else 0
                arr_delay = stu.arrival.delay if stu.HasField("arrival") else dep_delay

                stops.append({
                    "stop_id": stu.stop_id,
                    "skipped": stu.schedule_relationship == SKIPPED,
                    "dep_ts": dep_ts,
                    "arr_ts": arr_ts,
                    "dep_delay": dep_delay,
                    "arr_delay": arr_delay,
                })

            trips.append({
                "trip_id": trip_id,
                "route_id": route_id,
                "train_number": train_number,
                "canceled": trip_canceled,
                "stops": stops,
            })

        # 2. ServiceAlerts feed
        if alerts_bytes:
            alerts_feed = gtfs_realtime_pb2.FeedMessage()
            alerts_feed.ParseFromString(alerts_bytes)
            for entity in alerts_feed.entity:
                if entity.HasField("alert"):
                    self._extract_alert(entity.alert, now_ts, alerts)

        downloaded_at = datetime.now().astimezone().isoformat()
        feed_timestamp = (
            datetime.fromtimestamp(trips_feed.header.timestamp).astimezone().isoformat()
            if trips_feed.header.timestamp
            else None
        )

        return {
            "trips": trips,
            "alerts": alerts,
            "now_ts": now_ts,
            "downloaded_at": downloaded_at,
            "feed_timestamp": feed_timestamp,
        }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SNCF GTFS-RT Tracker from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # 1. Enregistrement du JS Lovelace une seule fois (verrou synchrone avant await)
    if not hass.data[DOMAIN].get("frontend_loaded"):
        hass.data[DOMAIN]["frontend_loaded"] = True
        js_path = Path(__file__).parent / "frontend" / "sncf-cards.js"
        if js_path.exists():
            try:
                await hass.http.async_register_static_paths([
                    StaticPathConfig(FRONTEND_SCRIPT_BASE_URL, str(js_path), cache_headers=False)
                ])
            except RuntimeError:
                pass
            version = int(js_path.stat().st_mtime)
            add_extra_js_url(hass, f"{FRONTEND_SCRIPT_BASE_URL}?v={version}")

    # 2. Création SYNCHRONE d'un unique coordinateur partagé (avant tout await)
    if "coordinator" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["coordinator"] = SncfGtfsCoordinator(hass)
        hass.data[DOMAIN]["init_lock"] = asyncio.Lock()

    coordinator: SncfGtfsCoordinator = hass.data[DOMAIN]["coordinator"]

    # 3. Premier rafraîchissement protégé par un verrou
    async with hass.data[DOMAIN]["init_lock"]:
        if coordinator.data is None:
            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                raise ConfigEntryNotReady(f"Initial fetch failed: {coordinator.last_exception}")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    loaded_entries = [
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and e.state.recoverable
    ]
    if unload_ok and not loaded_entries:
        hass.data[DOMAIN].pop("coordinator", None)
        hass.data[DOMAIN].pop("init_lock", None)

    return unload_ok