"""The SNCF GTFS-RT integration."""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timedelta
import io
import json
import logging
from pathlib import Path
import re
import time
import zipfile

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
    CONF_DESTINATION_UIC,
    CONF_ORIGIN_UIC,
    DOMAIN,
    GTFS_RT_ALERTS_URL,
    GTFS_RT_URL,
    GTFS_STATIC_URL,
    SKIPPED,
    STATIC_CACHE_HOURS,
    STATIC_LOOKAHEAD_HOURS,
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


def parse_gtfs_time_to_seconds(time_str: str) -> int | None:
    """Converts GTFS HH:MM:SS (including >= 24:00:00) into seconds since midnight."""
    if not time_str:
        return None
    parts = time_str.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2]) if len(parts) > 2 else 0
        return hours * 3600 + minutes * 60 + seconds
    except ValueError:
        return None


class SncfGtfsCoordinator(DataUpdateCoordinator):
    """Shared coordinator merging SNCF static GTFS schedules and live GTFS-RT feeds."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.session = async_get_clientsession(hass)
        self.cache_file = Path(hass.config.path(".storage")) / "sncf_gtfs_static_cache.json"
        self._static_data: dict | None = None
        self._static_lock = asyncio.Lock()

    def _get_configured_uics(self) -> set[str]:
        """Collect all 8-digit station UIC codes configured across entries."""
        uics: set[str] = set()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            for key in (CONF_ORIGIN_UIC, CONF_DESTINATION_UIC):
                val = (entry.data.get(key) or "").strip()
                if val:
                    uics.add(val)
        return uics

    def _load_cache_from_disk(self) -> dict | None:
        if not self.cache_file.exists():
            return None
        try:
            with self.cache_file.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as err:
            _LOGGER.warning("Failed to read SNCF static cache: %s", err)
            return None

    def _save_cache_to_disk(self, payload: dict) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_file.open("w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception as err:
            _LOGGER.warning("Failed to save SNCF static cache: %s", err)

    def _parse_static_zip(self, zip_bytes: bytes, target_uics: set[str]) -> dict:
        """Stream-filter static GTFS ZIP for active services and configured station UICs."""
        now_dt = datetime.now().astimezone()
        valid_dates = {
            (now_dt + timedelta(days=offset)).strftime("%Y%m%d")
            for offset in (-1, 0, 1, 2)
        }

        active_services_by_date: dict[str, set[str]] = {}
        active_service_ids: set[str] = set()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # 1. calendar_dates.txt -> service_id circulant autour d'aujourd'hui
            with zf.open("calendar_dates.txt") as raw_cd:
                reader = csv.DictReader(io.TextIOWrapper(raw_cd, encoding="utf-8-sig"))
                for row in reader:
                    if row.get("exception_type") != "1":
                        continue
                    d_str = (row.get("date") or "").strip()
                    s_id = (row.get("service_id") or "").strip()
                    if d_str in valid_dates and s_id:
                        active_services_by_date.setdefault(d_str, set()).add(s_id)
                        active_service_ids.add(s_id)

            # 2. trips.txt -> trip_id actifs uniquement
            trips_meta: dict[str, dict] = {}
            with zf.open("trips.txt") as raw_tr:
                reader = csv.DictReader(io.TextIOWrapper(raw_tr, encoding="utf-8-sig"))
                for row in reader:
                    s_id = (row.get("service_id") or "").strip()
                    if s_id not in active_service_ids:
                        continue
                    t_id = (row.get("trip_id") or "").strip()
                    if t_id:
                        trips_meta[t_id] = {
                            "service_id": s_id,
                            "route_id": (row.get("route_id") or "").strip(),
                            "train_number": (row.get("trip_headsign") or "").strip()
                            or extract_train_number(t_id),
                        }

            # 3. stop_times.txt -> filtrage sur les gares UIC surveillées (ou tous si tracking par numéro seul)
            trip_stops: dict[str, list[dict]] = {}
            filter_by_uic = bool(target_uics)
            with zf.open("stop_times.txt") as raw_st:
                reader = csv.DictReader(io.TextIOWrapper(raw_st, encoding="utf-8-sig"))
                for row in reader:
                    t_id = (row.get("trip_id") or "").strip()
                    if t_id not in trips_meta:
                        continue
                    stop_id = (row.get("stop_id") or "").strip()
                    if filter_by_uic and not any(stop_id.endswith(uic) for uic in target_uics):
                        continue
                    seq = int(row.get("stop_sequence") or 0)
                    arr_sec = parse_gtfs_time_to_seconds(row.get("arrival_time") or "")
                    dep_sec = parse_gtfs_time_to_seconds(row.get("departure_time") or "")
                    if arr_sec is None and dep_sec is None:
                        continue
                    trip_stops.setdefault(t_id, []).append({
                        "stop_id": stop_id,
                        "seq": seq,
                        "arr_sec": arr_sec if arr_sec is not None else dep_sec,
                        "dep_sec": dep_sec if dep_sec is not None else arr_sec,
                    })

        # Construit la liste compacte des circulations datées
        dated_trips: list[dict] = []
        for t_id, stops in trip_stops.items():
            meta = trips_meta[t_id]
            s_id = meta["service_id"]
            stops.sort(key=lambda s: s["seq"])
            for d_str, s_ids in active_services_by_date.items():
                if s_id not in s_ids:
                    continue
                day_dt = datetime.strptime(d_str, "%Y%m%d").astimezone()
                day_midnight_ts = int(day_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
                dated_trips.append({
                    "trip_id": t_id,
                    "route_id": meta["route_id"],
                    "train_number": meta["train_number"],
                    "stops": [
                        {
                            "stop_id": st["stop_id"],
                            "arr_ts": day_midnight_ts + st["arr_sec"],
                            "dep_ts": day_midnight_ts + st["dep_sec"],
                        }
                        for st in stops
                    ],
                })

        payload = {
            "fetched_at": time.time(),
            "uics": sorted(target_uics),
            "trips": dated_trips,
        }
        self._save_cache_to_disk(payload)
        return payload

    async def _ensure_static_schedule(self) -> list[dict]:
        """Ensure static GTFS schedules are cached and cover all configured UICs."""
        target_uics = self._get_configured_uics()
        now_ts = time.time()
        max_age = STATIC_CACHE_HOURS * 3600

        async with self._static_lock:
            if self._static_data is None:
                self._static_data = await self.hass.async_add_executor_job(
                    self._load_cache_from_disk
                )

            needs_refresh = True
            if self._static_data:
                age = now_ts - self._static_data.get("fetched_at", 0)
                cached_uics = set(self._static_data.get("uics", []))
                if age < max_age and target_uics.issubset(cached_uics):
                    needs_refresh = False

            if needs_refresh:
                try:
                    _LOGGER.info("Downloading SNCF static GTFS archive for UICs: %s", target_uics)
                    async with self.session.get(GTFS_STATIC_URL, timeout=120) as resp:
                        resp.raise_for_status()
                        zip_bytes = await resp.read()
                    self._static_data = await self.hass.async_add_executor_job(
                        self._parse_static_zip, zip_bytes, target_uics
                    )
                except Exception as err:
                    _LOGGER.warning("Could not refresh SNCF static GTFS: %s", err)

        return (self._static_data or {}).get("trips", [])

    async def _fetch_bytes(self, url: str) -> bytes:
        async with self.session.get(url, timeout=15) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _async_update_data(self) -> dict:
        static_trips = await self._ensure_static_schedule()

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
            self._merge_static_and_rt, static_trips, trips_bytes, alerts_bytes
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

    def _merge_static_and_rt(
        self, static_trips: list[dict], trips_bytes: bytes, alerts_bytes: bytes | None
    ) -> dict:
        now_ts = time.time()
        horizon_ts = now_ts + STATIC_LOOKAHEAD_HOURS * 3600
        alerts: list[dict] = []

        # 1. Indexation du flux temps réel GTFS-RT par trip_id
        trips_feed = gtfs_realtime_pb2.FeedMessage()
        trips_feed.ParseFromString(trips_bytes)

        rt_by_trip_id: dict[str, dict] = {}
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
                    "is_realtime": True,
                })

            rt_by_trip_id[trip_id] = {
                "trip_id": trip_id,
                "route_id": route_id,
                "train_number": train_number,
                "canceled": trip_canceled,
                "is_realtime": True,
                "stops": stops,
            }

        # 2. Fusion des circulations théoriques (24h) avec les mises à jour temps réel
        merged_trips: list[dict] = []
        consumed_rt_ids: set[str] = set()

        for st_trip in static_trips:
            st_stops = st_trip["stops"]
            if not st_stops:
                continue
            first_dep = st_stops[0]["dep_ts"]
            last_arr = st_stops[-1]["arr_ts"]
            if last_arr < (now_ts - 3600) or first_dep > horizon_ts:
                continue

            t_id = st_trip["trip_id"]
            rt_trip = rt_by_trip_id.get(t_id)

            if rt_trip:
                consumed_rt_ids.add(t_id)
                rt_stops_by_id = {s["stop_id"]: s for s in rt_trip["stops"]}
                merged_stops = []
                for st_s in st_stops:
                    rt_s = rt_stops_by_id.get(st_s["stop_id"])
                    if not rt_s:
                        # Fallback par code UIC (8 derniers chiffres)
                        uic_suffix = st_s["stop_id"][-8:]
                        rt_s = next(
                            (cand for cand in rt_trip["stops"] if cand["stop_id"].endswith(uic_suffix)),
                            None,
                        )
                    if rt_s:
                        merged_stops.append({
                            "stop_id": st_s["stop_id"],
                            "skipped": rt_s["skipped"],
                            "dep_ts": rt_s["dep_ts"] or st_s["dep_ts"],
                            "arr_ts": rt_s["arr_ts"] or st_s["arr_ts"],
                            "dep_delay": rt_s["dep_delay"],
                            "arr_delay": rt_s["arr_delay"],
                            "is_realtime": True,
                        })
                    else:
                        merged_stops.append({
                            "stop_id": st_s["stop_id"],
                            "skipped": False,
                            "dep_ts": st_s["dep_ts"],
                            "arr_ts": st_s["arr_ts"],
                            "dep_delay": 0,
                            "arr_delay": 0,
                            "is_realtime": False,
                        })

                merged_trips.append({
                    "trip_id": t_id,
                    "route_id": rt_trip["route_id"] or st_trip["route_id"],
                    "train_number": st_trip["train_number"],
                    "canceled": rt_trip["canceled"],
                    "is_realtime": True,
                    "stops": merged_stops,
                })
            else:
                merged_trips.append({
                    "trip_id": t_id,
                    "route_id": st_trip["route_id"],
                    "train_number": st_trip["train_number"],
                    "canceled": False,
                    "is_realtime": False,
                    "stops": [
                        {
                            "stop_id": st_s["stop_id"],
                            "skipped": False,
                            "dep_ts": st_s["dep_ts"],
                            "arr_ts": st_s["arr_ts"],
                            "dep_delay": 0,
                            "arr_delay": 0,
                            "is_realtime": False,
                        }
                        for st_s in st_stops
                    ],
                })

        # 3. Ajoute les éventuels trains temps réel absents du GTFS statique (trains spéciaux / variantes)
        for t_id, rt_trip in rt_by_trip_id.items():
            if t_id not in consumed_rt_ids:
                merged_trips.append(rt_trip)

        # 4. Lecture du flux ServiceAlerts
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
            "trips": merged_trips,
            "alerts": alerts,
            "now_ts": now_ts,
            "downloaded_at": downloaded_at,
            "feed_timestamp": feed_timestamp,
        }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SNCF GTFS-RT Tracker from a config entry."""
    hass.data.setdefault(DOMAIN, {})

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

    if "coordinator" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["coordinator"] = SncfGtfsCoordinator(hass)
        hass.data[DOMAIN]["init_lock"] = asyncio.Lock()

    coordinator: SncfGtfsCoordinator = hass.data[DOMAIN]["coordinator"]

    async with hass.data[DOMAIN]["init_lock"]:
        if coordinator.data is None:
            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                raise ConfigEntryNotReady(f"Initial fetch failed: {coordinator.last_exception}")
        else:
            # Si une nouvelle gare UIC vient d'être ajoutée, rafraîchit le cache statique
            await coordinator.async_refresh()

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
        hass.data[DOMAIN].pop("timestamp_sensor_added", None)

    return unload_ok