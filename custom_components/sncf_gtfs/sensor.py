"""Sensor platform for SNCF GTFS-RT Tracker."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DESTINATION_UIC,
    CONF_NAME,
    CONF_ORIGIN_UIC,
    CONF_TRACK_TYPE,
    CONF_TRAIN_NUMBER,
    DEPARTURE_MARGIN_SEC,
    DOMAIN,
    TRACK_TYPE_ROUTE,
    TRACK_TYPE_TRAIN,
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

    # Add the global download timestamp sensor once
    if not hass.data[DOMAIN].get("timestamp_sensor_added"):
        entities.append(SncfLastDownloadSensor(coordinator))
        hass.data[DOMAIN]["timestamp_sensor_added"] = True

    async_add_entities(entities)


class SncfSpecificTrainSensor(CoordinatorEntity, SensorEntity):
    """Sensor tracking a single train number."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._train_number = entry.data[CONF_TRAIN_NUMBER]
        self._origin_uic = entry.data.get(CONF_ORIGIN_UIC, "")
        self._attr_name = entry.data[CONF_NAME]
        self._attr_unique_id = f"{entry.entry_id}_train"
        self._attr_icon = "mdi:train-variant"

    def _find_train(self) -> dict | None:
        data = self.coordinator.data
        if not data:
            return None

        now_ts = data["now_ts"]

        for trip in data["trips"]:
            if trip["train_number"] != self._train_number:
                continue

            # Find target stop (either specified origin_uic or first stop of the trip)
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
            if dep_ts and dep_ts < (now_ts - DEPARTURE_MARGIN_SEC):
                continue

            is_canceled = trip["canceled"] or (target_stop["skipped"] if target_stop else False)
            delay_min = int((target_stop["dep_delay"] if target_stop else 0) // 60)

            if is_canceled:
                status = "Canceled"
            elif delay_min > 0:
                status = f"Delayed (+{delay_min} min)"
            else:
                status = "On time"

            est_dep = datetime.fromtimestamp(dep_ts).strftime("%H:%M") if dep_ts else None
            sched_dep_ts = (dep_ts - target_stop["dep_delay"]) if (dep_ts and target_stop) else None
            sched_dep = datetime.fromtimestamp(sched_dep_ts).strftime("%H:%M") if sched_dep_ts else est_dep

            # Find matching alerts (trip, route or origin stop)
            seen_titles = set()
            relevant_alerts = []
            for a in data["alerts"]:
                matches_trip = trip["trip_id"] in a["trip_ids"]
                matches_route = bool(trip.get("route_id")) and trip["route_id"] in a.get("route_ids", [])
                matches_stop = bool(self._origin_uic) and any(
                    s.endswith(self._origin_uic) for s in a["stop_ids"]
                )
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
                "scheduled_departure": sched_dep,
                "estimated_departure": est_dep,
                "delay_minutes": delay_min,
                "is_canceled": is_canceled,
                "alerts": relevant_alerts,
            }

        return None

    @property
    def native_value(self) -> str:
        train = self._find_train()
        if not train:
            return "Inactive / Outside window"
        return train["status"]

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        train = self._find_train() or {}
        return {
            "train_number": self._train_number,
            "origin_uic": self._origin_uic or None,
            "scheduled_departure": train.get("scheduled_departure"),
            "estimated_departure": train.get("estimated_departure"),
            "delay_minutes": train.get("delay_minutes", 0),
            "is_canceled": train.get("is_canceled", False),
            "alerts": train.get("alerts", []),
            "downloaded_at": data.get("downloaded_at"),
            "feed_timestamp": data.get("feed_timestamp"),
        }


class SncfRouteSensor(CoordinatorEntity, SensorEntity):
    """Sensor displaying the next train departure time and listing all upcoming trains on a route."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._origin_uic = entry.data[CONF_ORIGIN_UIC]
        self._destination_uic = entry.data[CONF_DESTINATION_UIC]
        self._attr_name = entry.data[CONF_NAME]
        self._attr_unique_id = f"{entry.entry_id}_route"
        self._attr_icon = "mdi:train"

    def _get_route_data(self) -> tuple[list[dict], list[dict]]:
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
            if dep_ts and dep_ts < (now_ts - DEPARTURE_MARGIN_SEC):
                continue

            is_canceled = trip["canceled"] or stop_dep["skipped"] or stop_arr["skipped"]
            delay_min = int(stop_dep["dep_delay"] // 60)

            if is_canceled:
                status = "Canceled"
            elif delay_min > 0:
                status = f"Delayed (+{delay_min} min)"
            else:
                status = "On time"

            est_dep = datetime.fromtimestamp(dep_ts).strftime("%H:%M") if dep_ts else None
            sched_dep_ts = (dep_ts - stop_dep["dep_delay"]) if dep_ts else None
            sched_dep = datetime.fromtimestamp(sched_dep_ts).strftime("%H:%M") if sched_dep_ts else est_dep

            arr_ts = stop_arr["arr_ts"] or stop_arr["dep_ts"]
            arr_delay = stop_arr.get("arr_delay", stop_dep["dep_delay"])
            est_arr = datetime.fromtimestamp(arr_ts).strftime("%H:%M") if arr_ts else None
            sched_arr_ts = (arr_ts - arr_delay) if arr_ts else None
            sched_arr = datetime.fromtimestamp(sched_arr_ts).strftime("%H:%M") if sched_arr_ts else est_arr

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
                "status": status,
            })

        matched_trains.sort(key=lambda t: t["departure_ts"] or float("inf"))

        seen_titles = set()
        relevant_alerts = []
        for a in data["alerts"]:
            matches_trip = any(tid in matched_trip_ids for tid in a["trip_ids"])
            matches_route = any(rid in matched_route_ids for rid in a.get("route_ids", []))
            matches_stop = any(
                s.endswith(self._origin_uic) or s.endswith(self._destination_uic)
                for s in a["stop_ids"]
            )
            if (matches_trip or matches_route or matches_stop) and a["title"] not in seen_titles:
                seen_titles.add(a["title"])
                relevant_alerts.append({
                    "title": a["title"],
                    "description": a["description"],
                })

        return matched_trains, relevant_alerts

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
            "trains": trains,
            "alerts": alerts,
            "downloaded_at": data.get("downloaded_at"),
            "feed_timestamp": data.get("feed_timestamp"),
        }


class SncfLastDownloadSensor(CoordinatorEntity, SensorEntity):
    """Global timestamp sensor showing when the GTFS-RT feed was last downloaded."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_name = "SNCF Dernier Téléchargement"
    _attr_unique_id = "sncf_gtfs_last_download"
    _attr_icon = "mdi:clock-check-outline"

    @property
    def native_value(self) -> datetime | None:
        data = self.coordinator.data or {}
        iso_str = data.get("downloaded_at")
        return datetime.fromisoformat(iso_str) if iso_str else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "feed_timestamp": data.get("feed_timestamp"),
        }