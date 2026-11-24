"""Sensor platform for SNCF GTFS-RT Tracker."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DESTINATION_UIC,
    CONF_NAME,
    CONF_ORIGIN_UIC,
    CONF_TRACK_TYPE,
    CONF_TRAIN_NUMBER,
    DEPARTURE_MARGIN_SEC,
    DOMAIN,
    MAX_STORED_TRAINS,
    TRACK_TYPE_ROUTE,
    TRACK_TYPE_TRAIN,
)


def _format_local_hhmm(ts: float | int | None) -> str | None:
    """Format a POSIX timestamp as HH:MM in Home Assistant's local timezone."""
    if ts is None:
        return None
    return dt_util.as_local(dt_util.utc_from_timestamp(ts)).strftime("%H:%M")


def _alert_matches_stop_only(alert: dict, target_uics: tuple[str, ...]) -> bool:
    """Match stop-level alerts only when they are not scoped to specific trips or routes."""
    if alert.get("trip_ids") or alert.get("route_ids"):
        return False
    valid_uics = [u for u in target_uics if u]
    if not valid_uics:
        return False
    return any(
        s.endswith(uic)
        for s in alert.get("stop_ids", [])
        for uic in valid_uics
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SNCF sensors from a config entry."""
    coordinator = hass.data[DOMAIN]["coordinator"]
    track_type = entry.data[CONF_TRACK_TYPE]

    entities: list[SensorEntity] = []

    if track_type == TRACK_TYPE_TRAIN:
        entities.append(SncfSpecificTrainSensor(coordinator, entry))
    elif track_type == TRACK_TYPE_ROUTE:
        entities.append(SncfRouteSensor(coordinator, entry))

    if not hass.data[DOMAIN].get("timestamp_sensor_entry_id"):
        entities.append(SncfLastDownloadSensor(coordinator))
        hass.data[DOMAIN]["timestamp_sensor_entry_id"] = entry.entry_id

    async_add_entities(entities)


class SncfSpecificTrainSensor(CoordinatorEntity, SensorEntity):
    """Sensor tracking a single train number."""

    _unrecorded_attributes = frozenset({"alerts"})

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._train_number = entry.data[CONF_TRAIN_NUMBER]
        self._origin_uic = entry.data.get(CONF_ORIGIN_UIC, "")
        self._attr_name = entry.data[CONF_NAME]
        self._attr_unique_id = f"{entry.entry_id}_train"
        self._attr_icon = "mdi:train-variant"
        self._cached_train: dict | None = None
        self._cache_populated = False

    def _compute_train(self) -> dict | None:
        data = self.coordinator.data
        if not data:
            return None

        now_ts = data["now_ts"]
        candidates = []

        for trip in data["trips"]:
            if trip["train_number"] != self._train_number:
                continue

            target_stop = None
            if self._origin_uic:
                target_stop = next(
                    (s for s in trip["stops"] if s["stop_id"].endswith(self._origin_uic)),
                    None,
                )
                if not target_stop:
                    continue
            elif trip["stops"]:
                target_stop = trip["stops"][0]

            dep_ts = target_stop["dep_ts"] if target_stop else None
            if not dep_ts or dep_ts < (now_ts - DEPARTURE_MARGIN_SEC):
                continue

            candidates.append((dep_ts, trip, target_stop))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0])
        dep_ts, trip, target_stop = candidates[0]

        is_realtime = bool(
            trip.get("is_realtime") or (target_stop and target_stop.get("is_realtime"))
        )
        is_canceled = trip["canceled"] or (target_stop["skipped"] if target_stop else False)
        dep_delay_sec = target_stop["dep_delay"] if target_stop else 0
        delay_min = int(dep_delay_sec / 60)

        if is_canceled:
            status = "Canceled"
        elif delay_min > 0:
            status = f"Delayed (+{delay_min} min)"
        elif is_realtime:
            status = "On time"
        else:
            status = "Scheduled"

        est_dep = _format_local_hhmm(dep_ts)
        sched_dep_ts = (
            target_stop.get("sched_dep_ts")
            if target_stop and target_stop.get("sched_dep_ts") is not None
            else (dep_ts - dep_delay_sec)
        )
        sched_dep = _format_local_hhmm(sched_dep_ts)

        seen_titles = set()
        relevant_alerts = []
        for a in data["alerts"]:
            matches_trip = trip["trip_id"] in a["trip_ids"]
            matches_route = bool(trip.get("route_id")) and trip["route_id"] in a.get("route_ids", [])
            matches_stop = _alert_matches_stop_only(a, (self._origin_uic,))
            if (matches_trip or matches_route or matches_stop) and a["title"] not in seen_titles:
                seen_titles.add(a["title"])
                relevant_alerts.append({
                    "title": a["title"],
                    "description": a["description"],
                })

        return {
            "status": status,
            "train_number": self._train_number,
            "trip_id": trip["trip_id"],
            "departure_ts": dep_ts,
            "scheduled_departure": sched_dep,
            "estimated_departure": est_dep,
            "delay_minutes": delay_min,
            "is_canceled": is_canceled,
            "is_realtime": is_realtime,
            "alerts": relevant_alerts,
        }

    def _get_train(self) -> dict | None:
        if not self._cache_populated:
            self._cached_train = self._compute_train()
            self._cache_populated = True
        return self._cached_train

    @callback
    def _handle_coordinator_update(self) -> None:
        self._cached_train = self._compute_train()
        self._cache_populated = True
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        train = self._get_train()
        if not train:
            return "Inactive / Outside window"
        return train["status"]

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        train = self._get_train() or {}
        return {
            "train_number": self._train_number,
            "origin_uic": self._origin_uic or None,
            "departure_ts": train.get("departure_ts"),
            "scheduled_departure": train.get("scheduled_departure"),
            "estimated_departure": train.get("estimated_departure"),
            "delay_minutes": train.get("delay_minutes", 0),
            "is_canceled": train.get("is_canceled", False),
            "is_realtime": train.get("is_realtime", False),
            "alerts": train.get("alerts", []),
            "downloaded_at": data.get("downloaded_at"),
            "feed_timestamp": data.get("feed_timestamp"),
        }


class SncfRouteSensor(CoordinatorEntity, SensorEntity):
    """Sensor displaying the next train departure time and listing upcoming trains on a route."""

    _unrecorded_attributes = frozenset({"trains", "alerts"})

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._origin_uic = entry.data[CONF_ORIGIN_UIC]
        self._destination_uic = entry.data[CONF_DESTINATION_UIC]
        self._attr_name = entry.data[CONF_NAME]
        self._attr_unique_id = f"{entry.entry_id}_route"
        self._attr_icon = "mdi:train"
        self._cached_route_data: tuple[list[dict], list[dict]] = ([], [])
        self._cache_populated = False

    def _compute_route_data(self) -> tuple[list[dict], list[dict]]:
        data = self.coordinator.data
        if not data:
            return [], []

        now_ts = data["now_ts"]
        matched_trains = []
        matched_trip_ids = set()
        matched_route_ids = set()

        for trip in data["trips"]:
            idx_dep = None
            stop_dep = None
            stop_arr = None

            for idx, s in enumerate(trip["stops"]):
                if s["stop_id"].endswith(self._origin_uic):
                    idx_dep = idx
                    stop_dep = s
                elif s["stop_id"].endswith(self._destination_uic) and idx_dep is not None:
                    stop_arr = s
                    break

            if stop_dep is None or stop_arr is None:
                continue

            dep_ts = stop_dep["dep_ts"]
            if not dep_ts or dep_ts < (now_ts - DEPARTURE_MARGIN_SEC):
                continue

            is_realtime = bool(
                trip.get("is_realtime")
                or stop_dep.get("is_realtime")
                or stop_arr.get("is_realtime")
            )
            is_canceled = trip["canceled"] or stop_dep["skipped"] or stop_arr["skipped"]
            dep_delay_sec = stop_dep["dep_delay"]
            delay_min = int(dep_delay_sec / 60)

            if is_canceled:
                status = "Canceled"
            elif delay_min > 0:
                status = f"Delayed (+{delay_min} min)"
            elif is_realtime:
                status = "On time"
            else:
                status = "Scheduled"

            est_dep = _format_local_hhmm(dep_ts)
            sched_dep_ts = (
                stop_dep.get("sched_dep_ts")
                if stop_dep.get("sched_dep_ts") is not None
                else (dep_ts - dep_delay_sec)
            )
            sched_dep = _format_local_hhmm(sched_dep_ts)

            arr_ts = stop_arr["arr_ts"] or stop_arr["dep_ts"]
            arr_delay = stop_arr.get("arr_delay", dep_delay_sec)
            est_arr = _format_local_hhmm(arr_ts) if arr_ts else None
            if stop_arr.get("sched_arr_ts") is not None:
                sched_arr_ts = stop_arr["sched_arr_ts"]
            elif stop_arr.get("sched_dep_ts") is not None:
                sched_arr_ts = stop_arr["sched_dep_ts"]
            elif arr_ts is not None:
                sched_arr_ts = arr_ts - arr_delay
            else:
                sched_arr_ts = None
            sched_arr = _format_local_hhmm(sched_arr_ts) if sched_arr_ts else est_arr

            matched_trip_ids.add(trip["trip_id"])
            if trip.get("route_id"):
                matched_route_ids.add(trip["route_id"])

            matched_trains.append({
                "train_number": trip["train_number"],
                "trip_id": trip["trip_id"],
                "departure_ts": dep_ts,
                "scheduled_departure": sched_dep,
                "scheduled_arrival": sched_arr,
                "estimated_departure": est_dep,
                "estimated_arrival": est_arr,
                "delay_minutes": delay_min,
                "is_canceled": is_canceled,
                "is_realtime": is_realtime,
                "status": status,
            })

        matched_trains.sort(key=lambda t: t["departure_ts"] or float("inf"))

        seen_titles = set()
        relevant_alerts = []
        for a in data["alerts"]:
            matches_trip = any(tid in matched_trip_ids for tid in a["trip_ids"])
            matches_route = any(rid in matched_route_ids for rid in a.get("route_ids", []))
            matches_stop = _alert_matches_stop_only(
                a, (self._origin_uic, self._destination_uic)
            )
            if (matches_trip or matches_route or matches_stop) and a["title"] not in seen_titles:
                seen_titles.add(a["title"])
                relevant_alerts.append({
                    "title": a["title"],
                    "description": a["description"],
                })

        return matched_trains, relevant_alerts

    def _get_route_data(self) -> tuple[list[dict], list[dict]]:
        if not self._cache_populated:
            self._cached_route_data = self._compute_route_data()
            self._cache_populated = True
        return self._cached_route_data

    @callback
    def _handle_coordinator_update(self) -> None:
        self._cached_route_data = self._compute_route_data()
        self._cache_populated = True
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Returns the estimated departure time of the next train."""
        trains, _ = self._get_route_data()
        if not trains:
            return "No upcoming train"
        next_train = trains[0]
        if next_train.get("is_canceled"):
            return "Canceled"
        return next_train.get("estimated_departure") or "Unknown"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        trains, alerts = self._get_route_data()
        next_train = trains[0] if trains else {}
        return {
            "train_count": len(trains),
            "origin_uic": self._origin_uic,
            "destination_uic": self._destination_uic,
            "next_train_number": next_train.get("train_number"),
            "next_scheduled_departure": next_train.get("scheduled_departure"),
            "next_scheduled_arrival": next_train.get("scheduled_arrival"),
            "next_departure": next_train.get("estimated_departure"),
            "next_arrival": next_train.get("estimated_arrival"),
            "next_status": next_train.get("status"),
            "trains": trains[:MAX_STORED_TRAINS],
            "alerts": alerts,
            "downloaded_at": data.get("downloaded_at"),
            "feed_timestamp": data.get("feed_timestamp"),
        }


class SncfLastDownloadSensor(CoordinatorEntity, SensorEntity):
    """Global timestamp sensor showing when the GTFS-RT feed was last downloaded."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_name = "SNCF Last Download"
    _attr_unique_id = "sncf_gtfs_last_download"
    _attr_icon = "mdi:clock-check-outline"

    @property
    def native_value(self) -> datetime | None:
        data = self.coordinator.data or {}
        iso_str = data.get("downloaded_at")
        return dt_util.parse_datetime(iso_str) if iso_str else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "feed_timestamp": data.get("feed_timestamp"),
        }
