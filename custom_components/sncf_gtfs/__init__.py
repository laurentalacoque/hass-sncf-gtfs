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
from zoneinfo import ZoneInfo

import aiohttp
from google.transit import gtfs_realtime_pb2

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CANCELED,
    CONF_DESTINATION_UIC,
    CONF_ORIGIN_UIC,
    CONF_TRAIN_NUMBER,
    DOMAIN,
    GTFS_RT_ALERTS_URL,
    GTFS_RT_URL,
    GTFS_STATIC_URL,
    SKIPPED,
    SNCF_TIMEZONE,
    STATIC_CACHE_HOURS,
    STATIC_LOOKAHEAD_HOURS,
    UPDATE_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR]
FRONTEND_SCRIPT_BASE_URL = "/sncf_gtfs_static/sncf-cards.js"
SNCF_TZ = ZoneInfo(SNCF_TIMEZONE)
WEEKDAY_COLUMNS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
STATIC_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)
RT_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=15)


def _has_field(msg, field_name: str) -> bool:
    """Safely check protobuf field presence."""
    has_field = getattr(msg, "HasField", None)
    if callable(has_field):
        try:
            return bool(has_field(field_name))
        except ValueError:
            return False
    return getattr(msg, field_name, None) is not None


def _service_origin_timestamp(date_str: str) -> int:
    """Return GTFS service origin timestamp (noon minus 12h in Europe/Paris).

    Per the GTFS specification, HH:MM:SS stop times are relative to noon minus
    12 hours in the agency timezone so DST transitions do not shift times by ±1h.
    """
    noon_dt = datetime.strptime(date_str, "%Y%m%d").replace(hour=12, tzinfo=SNCF_TZ)
    return int(noon_dt.timestamp()) - 12 * 3600


def _get_frontend_mtime(js_path: Path) -> int | None:
    """Return frontend script mtime if it exists (safe to run in executor)."""
    if not js_path.exists():
        return None
    return int(js_path.stat().st_mtime)


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
    """Converts GTFS HH:MM:SS (including >= 24:00:00) into seconds since service origin."""
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

    def _get_configured_targets(self) -> tuple[set[str], set[str]]:
        """Collect all configured 8-digit station UIC codes and train numbers."""
        uics: set[str] = set()
        train_numbers: set[str] = set()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            for key in (CONF_ORIGIN_UIC, CONF_DESTINATION_UIC):
                val = (entry.data.get(key) or "").strip()
                if val:
                    uics.add(val)
            train_num = (entry.data.get(CONF_TRAIN_NUMBER) or "").strip()
            if train_num:
                train_numbers.add(train_num)
        return uics, train_numbers

    def _is_static_cache_valid(
        self,
        now_ts: float,
        target_uics: set[str],
        target_train_numbers: set[str],
    ) -> bool:
        """Return True if the in-memory static cache is fresh and covers all targets."""
        if not self._static_data:
            return False
        age = now_ts - self._static_data.get("fetched_at", 0)
        if age >= STATIC_CACHE_HOURS * 3600:
            return False
        cached_uics = set(self._static_data.get("uics", []))
        cached_trains = set(self._static_data.get("train_numbers", []))
        return (
            target_uics.issubset(cached_uics)
            and target_train_numbers.issubset(cached_trains)
        )

    def needs_refresh_for_current_targets(self) -> bool:
        """Return True if newly configured entries require refreshing the static schedule."""
        target_uics, target_train_numbers = self._get_configured_targets()
        return not self._is_static_cache_valid(
            time.time(), target_uics, target_train_numbers
        )

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

    def _parse_static_zip(
        self,
        zip_bytes: bytes,
        target_uics: set[str],
        target_train_numbers: set[str] | None = None,
    ) -> dict:
        """Stream-filter static GTFS ZIP for active services and configured targets."""
        target_train_numbers = target_train_numbers or set()
        now_dt = datetime.now(SNCF_TZ)
        valid_dates = {
            (now_dt + timedelta(days=offset)).strftime("%Y%m%d")
            for offset in (-1, 0, 1, 2)
        }
        valid_date_objs = {
            d_str: datetime.strptime(d_str, "%Y%m%d").date()
            for d_str in valid_dates
        }

        active_services_by_date: dict[str, set[str]] = {d: set() for d in valid_dates}

        if not target_uics and not target_train_numbers:
            payload = {
                "fetched_at": time.time(),
                "uics": [],
                "train_numbers": [],
                "trips": [],
            }
            self._save_cache_to_disk(payload)
            return payload

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            namelist = set(zf.namelist())

            # 1a. calendar.txt -> regular weekly service patterns (if present)
            if "calendar.txt" in namelist:
                with zf.open("calendar.txt") as raw_cal:
                    reader = csv.DictReader(io.TextIOWrapper(raw_cal, encoding="utf-8-sig"))
                    for row in reader:
                        s_id = (row.get("service_id") or "").strip()
                        start_d = (row.get("start_date") or "").strip()
                        end_d = (row.get("end_date") or "").strip()
                        if not s_id or not start_d or not end_d:
                            continue
                        for d_str, d_obj in valid_date_objs.items():
                            if start_d <= d_str <= end_d:
                                weekday_col = WEEKDAY_COLUMNS[d_obj.weekday()]
                                if (row.get(weekday_col) or "").strip() == "1":
                                    active_services_by_date[d_str].add(s_id)

            # 1b. calendar_dates.txt -> additions (1) and removals (2)
            if "calendar_dates.txt" in namelist:
                with zf.open("calendar_dates.txt") as raw_cd:
                    reader = csv.DictReader(io.TextIOWrapper(raw_cd, encoding="utf-8-sig"))
                    for row in reader:
                        d_str = (row.get("date") or "").strip()
                        if d_str not in active_services_by_date:
                            continue
                        s_id = (row.get("service_id") or "").strip()
                        if not s_id:
                            continue
                        exc_type = (row.get("exception_type") or "").strip()
                        if exc_type == "1":
                            active_services_by_date[d_str].add(s_id)
                        elif exc_type == "2":
                            active_services_by_date[d_str].discard(s_id)

            active_service_ids: set[str] = set().union(*active_services_by_date.values())

            # 2. trips.txt -> active trips only
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

            # 3. stop_times.txt -> filter by configured station UICs OR tracked train numbers
            trip_stops: dict[str, list[dict]] = {}
            with zf.open("stop_times.txt") as raw_st:
                reader = csv.DictReader(io.TextIOWrapper(raw_st, encoding="utf-8-sig"))
                for row in reader:
                    t_id = (row.get("trip_id") or "").strip()
                    meta = trips_meta.get(t_id)
                    if meta is None:
                        continue
                    stop_id = (row.get("stop_id") or "").strip()
                    matches_train = meta["train_number"] in target_train_numbers
                    matches_uic = bool(target_uics) and any(
                        stop_id.endswith(uic) for uic in target_uics
                    )
                    if not (matches_train or matches_uic):
                        continue
                    try:
                        seq = int(row.get("stop_sequence") or 0)
                    except ValueError:
                        seq = 0
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

        # Build compact dated trip list using DST-safe GTFS service origin timestamps
        service_origins = {
            d_str: _service_origin_timestamp(d_str)
            for d_str in active_services_by_date
        }
        dated_trips: list[dict] = []
        for t_id, stops in trip_stops.items():
            meta = trips_meta[t_id]
            s_id = meta["service_id"]
            stops.sort(key=lambda s: s["seq"])
            for d_str, s_ids in active_services_by_date.items():
                if s_id not in s_ids:
                    continue
                origin_ts = service_origins[d_str]
                dated_trips.append({
                    "trip_id": t_id,
                    "service_date": d_str,
                    "route_id": meta["route_id"],
                    "train_number": meta["train_number"],
                    "stops": [
                        {
                            "stop_id": st["stop_id"],
                            "arr_ts": origin_ts + st["arr_sec"],
                            "dep_ts": origin_ts + st["dep_sec"],
                        }
                        for st in stops
                    ],
                })

        payload = {
            "fetched_at": time.time(),
            "uics": sorted(target_uics),
            "train_numbers": sorted(target_train_numbers),
            "trips": dated_trips,
        }
        self._save_cache_to_disk(payload)
        return payload

    async def _ensure_static_schedule(self) -> list[dict]:
        """Ensure static GTFS schedules are cached and cover all configured targets."""
        target_uics, target_train_numbers = self._get_configured_targets()
        now_ts = time.time()

        async with self._static_lock:
            if self._static_data is None:
                self._static_data = await self.hass.async_add_executor_job(
                    self._load_cache_from_disk
                )

            if not self._is_static_cache_valid(now_ts, target_uics, target_train_numbers):
                try:
                    _LOGGER.info(
                        "Downloading SNCF static GTFS archive for UICs=%s trains=%s",
                        target_uics,
                        target_train_numbers,
                    )
                    async with self.session.get(
                        GTFS_STATIC_URL, timeout=STATIC_DOWNLOAD_TIMEOUT
                    ) as resp:
                        resp.raise_for_status()
                        zip_bytes = await resp.read()
                    self._static_data = await self.hass.async_add_executor_job(
                        self._parse_static_zip,
                        zip_bytes,
                        target_uics,
                        target_train_numbers,
                    )
                except Exception as err:
                    _LOGGER.warning("Could not refresh SNCF static GTFS: %s", err)

        return (self._static_data or {}).get("trips", [])

    async def _fetch_bytes(self, url: str) -> bytes:
        async with self.session.get(url, timeout=RT_DOWNLOAD_TIMEOUT) as resp:
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
            if _has_field(ie, "trip") and ie.trip.trip_id:
                trip_targets.append(ie.trip.trip_id)
            if _has_field(ie, "route_id") and ie.route_id:
                route_targets.append(ie.route_id)
            if _has_field(ie, "stop_id") and ie.stop_id:
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

    def _match_rt_trip(
        self,
        t_id: str,
        service_date: str,
        first_dep: int,
        today_paris: str,
        rt_by_key: dict[tuple[str, str], dict],
    ) -> tuple[tuple[str, str], dict] | tuple[None, None]:
        """Match a dated static trip to its corresponding GTFS-RT trip update."""
        if service_date and (t_id, service_date) in rt_by_key:
            key = (t_id, service_date)
            return key, rt_by_key[key]

        nodate_key = (t_id, "")
        rt_trip = rt_by_key.get(nodate_key)
        if rt_trip is None:
            return None, None

        # Prevent a dateless RT update from attaching to both today's and tomorrow's runs
        rt_ref_ts = next(
            (s["dep_ts"] or s["arr_ts"] for s in rt_trip["stops"] if s["dep_ts"] or s["arr_ts"]),
            None,
        )
        if rt_ref_ts is not None:
            if abs(rt_ref_ts - first_dep) < 12 * 3600:
                return nodate_key, rt_trip
            return None, None

        if not service_date or service_date == today_paris:
            return nodate_key, rt_trip
        return None, None

    def _merge_static_and_rt(
        self, static_trips: list[dict], trips_bytes: bytes, alerts_bytes: bytes | None
    ) -> dict:
        now_ts = time.time()
        horizon_ts = now_ts + STATIC_LOOKAHEAD_HOURS * 3600
        today_paris = datetime.fromtimestamp(now_ts, tz=SNCF_TZ).strftime("%Y%m%d")
        alerts: list[dict] = []

        # 1. Index GTFS-RT trip updates by (trip_id, start_date)
        trips_feed = gtfs_realtime_pb2.FeedMessage()
        trips_feed.ParseFromString(trips_bytes)

        rt_by_key: dict[tuple[str, str], dict] = {}
        for entity in trips_feed.entity:
            if _has_field(entity, "alert"):
                self._extract_alert(entity.alert, now_ts, alerts)
                continue
            if not _has_field(entity, "trip_update"):
                continue

            tu = entity.trip_update
            trip_id = tu.trip.trip_id
            start_date = (getattr(tu.trip, "start_date", "") or "").strip()
            route_id = tu.trip.route_id
            train_number = extract_train_number(trip_id)
            trip_canceled = tu.trip.schedule_relationship == CANCELED

            stops = []
            for stu in tu.stop_time_update:
                has_dep = _has_field(stu, "departure")
                has_arr = _has_field(stu, "arrival")
                dep_ts = stu.departure.time if (has_dep and stu.departure.time) else None
                arr_ts = stu.arrival.time if (has_arr and stu.arrival.time) else None
                has_dep_delay = has_dep and _has_field(stu.departure, "delay")
                has_arr_delay = has_arr and _has_field(stu.arrival, "delay")
                dep_delay = stu.departure.delay if has_dep_delay else 0
                arr_delay = stu.arrival.delay if has_arr_delay else dep_delay

                stops.append({
                    "stop_id": stu.stop_id,
                    "skipped": stu.schedule_relationship == SKIPPED,
                    "dep_ts": dep_ts,
                    "arr_ts": arr_ts,
                    "sched_dep_ts": (dep_ts - dep_delay) if dep_ts is not None else None,
                    "sched_arr_ts": (arr_ts - arr_delay) if arr_ts is not None else None,
                    "dep_delay": dep_delay,
                    "arr_delay": arr_delay,
                    "_has_dep_delay": has_dep_delay,
                    "_has_arr_delay": has_arr_delay,
                    "is_realtime": True,
                })

            rt_by_key[(trip_id, start_date)] = {
                "trip_id": trip_id,
                "service_date": start_date,
                "route_id": route_id,
                "train_number": train_number,
                "canceled": trip_canceled,
                "is_realtime": True,
                "stops": stops,
            }

        # 2. Merge static schedules (24h window) with live RT updates
        merged_trips: list[dict] = []
        consumed_rt_keys: set[tuple[str, str]] = set()

        for st_trip in static_trips:
            st_stops = st_trip["stops"]
            if not st_stops:
                continue
            first_dep = st_stops[0]["dep_ts"]
            last_arr = st_stops[-1]["arr_ts"]
            if last_arr < (now_ts - 3600) or first_dep > horizon_ts:
                continue

            t_id = st_trip["trip_id"]
            service_date = st_trip.get("service_date") or ""
            rt_key, rt_trip = self._match_rt_trip(
                t_id, service_date, first_dep, today_paris, rt_by_key
            )

            if rt_trip and rt_key is not None:
                consumed_rt_keys.add(rt_key)
                rt_stops_by_id = {s["stop_id"]: s for s in rt_trip["stops"]}
                merged_stops = []
                for st_s in st_stops:
                    rt_s = rt_stops_by_id.get(st_s["stop_id"])
                    if not rt_s:
                        uic_suffix = st_s["stop_id"][-8:]
                        rt_s = next(
                            (cand for cand in rt_trip["stops"] if cand["stop_id"].endswith(uic_suffix)),
                            None,
                        )
                    if rt_s:
                        st_dep = st_s["dep_ts"]
                        st_arr = st_s["arr_ts"]

                        if rt_s["dep_ts"] is not None:
                            est_dep = rt_s["dep_ts"]
                            dep_delay = (
                                rt_s["dep_delay"]
                                if rt_s["_has_dep_delay"]
                                else (est_dep - st_dep)
                            )
                        else:
                            dep_delay = rt_s["dep_delay"] if rt_s["_has_dep_delay"] else 0
                            est_dep = st_dep + dep_delay

                        if rt_s["arr_ts"] is not None:
                            est_arr = rt_s["arr_ts"]
                            arr_delay = (
                                rt_s["arr_delay"]
                                if rt_s["_has_arr_delay"]
                                else (est_arr - st_arr)
                            )
                        else:
                            arr_delay = (
                                rt_s["arr_delay"]
                                if rt_s["_has_arr_delay"]
                                else dep_delay
                            )
                            est_arr = st_arr + arr_delay

                        merged_stops.append({
                            "stop_id": st_s["stop_id"],
                            "skipped": rt_s["skipped"],
                            "dep_ts": est_dep,
                            "arr_ts": est_arr,
                            "sched_dep_ts": st_dep,
                            "sched_arr_ts": st_arr,
                            "dep_delay": dep_delay,
                            "arr_delay": arr_delay,
                            "is_realtime": True,
                        })
                    else:
                        merged_stops.append({
                            "stop_id": st_s["stop_id"],
                            "skipped": False,
                            "dep_ts": st_s["dep_ts"],
                            "arr_ts": st_s["arr_ts"],
                            "sched_dep_ts": st_s["dep_ts"],
                            "sched_arr_ts": st_s["arr_ts"],
                            "dep_delay": 0,
                            "arr_delay": 0,
                            "is_realtime": False,
                        })

                merged_trips.append({
                    "trip_id": t_id,
                    "service_date": service_date,
                    "route_id": rt_trip["route_id"] or st_trip["route_id"],
                    "train_number": st_trip["train_number"],
                    "canceled": rt_trip["canceled"],
                    "is_realtime": True,
                    "stops": merged_stops,
                })
            else:
                merged_trips.append({
                    "trip_id": t_id,
                    "service_date": service_date,
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
                            "sched_dep_ts": st_s["dep_ts"],
                            "sched_arr_ts": st_s["arr_ts"],
                            "dep_delay": 0,
                            "arr_delay": 0,
                            "is_realtime": False,
                        }
                        for st_s in st_stops
                    ],
                })

        # 3. Append any RT-only trips absent from static GTFS (special/variant trains)
        for rt_key, rt_trip in rt_by_key.items():
            if rt_key not in consumed_rt_keys:
                cleaned_stops = [
                    {k: v for k, v in st.items() if not k.startswith("_")}
                    for st in rt_trip["stops"]
                ]
                merged_trips.append({
                    **rt_trip,
                    "stops": cleaned_stops,
                })

        # 4. Parse ServiceAlerts feed
        if alerts_bytes:
            alerts_feed = gtfs_realtime_pb2.FeedMessage()
            alerts_feed.ParseFromString(alerts_bytes)
            for entity in alerts_feed.entity:
                if _has_field(entity, "alert"):
                    self._extract_alert(entity.alert, now_ts, alerts)

        downloaded_at = dt_util.now().isoformat()
        feed_timestamp = (
            dt_util.as_local(
                dt_util.utc_from_timestamp(trips_feed.header.timestamp)
            ).isoformat()
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
        version = await hass.async_add_executor_job(_get_frontend_mtime, js_path)
        if version is not None:
            try:
                await hass.http.async_register_static_paths([
                    StaticPathConfig(
                        FRONTEND_SCRIPT_BASE_URL, str(js_path), cache_headers=False
                    )
                ])
            except RuntimeError:
                pass
            add_extra_js_url(hass, f"{FRONTEND_SCRIPT_BASE_URL}?v={version}")

    if "coordinator" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["coordinator"] = SncfGtfsCoordinator(hass)
        hass.data[DOMAIN]["init_lock"] = asyncio.Lock()

    coordinator: SncfGtfsCoordinator = hass.data[DOMAIN]["coordinator"]

    async with hass.data[DOMAIN]["init_lock"]:
        if coordinator.data is None:
            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                raise ConfigEntryNotReady(
                    f"Initial fetch failed: {coordinator.last_exception}"
                )
        elif coordinator.needs_refresh_for_current_targets():
            # Refresh static schedule only when a newly added entry introduces new targets
            await coordinator.async_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    domain_data = hass.data.get(DOMAIN, {})
    remaining_loaded = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and e.state is ConfigEntryState.LOADED
    ]

    if not remaining_loaded:
        coordinator: SncfGtfsCoordinator | None = domain_data.pop("coordinator", None)
        if coordinator is not None:
            await coordinator.async_shutdown()
        domain_data.pop("init_lock", None)
        domain_data.pop("timestamp_sensor_entry_id", None)
    elif domain_data.get("timestamp_sensor_entry_id") == entry.entry_id:
        # Re-home the global timestamp sensor onto another active entry
        domain_data.pop("timestamp_sensor_entry_id", None)
        hass.async_create_task(
            hass.config_entries.async_reload(remaining_loaded[0].entry_id)
        )

    return True
