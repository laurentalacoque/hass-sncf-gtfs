"""Unit tests for the SNCF GTFS-RT Home Assistant integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock
import zipfile
from zoneinfo import ZoneInfo

PARIS_TZ = ZoneInfo("Europe/Paris")


def _install_stubs() -> None:
    """Install lightweight Home Assistant and protobuf stubs when running standalone."""
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    sys.modules["homeassistant"] = ha

    # aiohttp stub (only if not installed)
    if "aiohttp" not in sys.modules:
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            aiohttp_mod = types.ModuleType("aiohttp")

            class ClientTimeout:
                def __init__(self, total=None):
                    self.total = total

            aiohttp_mod.ClientTimeout = ClientTimeout
            sys.modules["aiohttp"] = aiohttp_mod

    # voluptuous stub (only if not installed)
    if "voluptuous" not in sys.modules:
        try:
            import voluptuous  # noqa: F401
        except ImportError:
            vol_mod = types.ModuleType("voluptuous")
            vol_mod.Schema = lambda x: x
            vol_mod.Required = lambda key, default=None: key
            vol_mod.Optional = lambda key, default=None: key
            sys.modules["voluptuous"] = vol_mod

    # google.transit.gtfs_realtime_pb2 stub
    if "google.transit.gtfs_realtime_pb2" not in sys.modules:
        google_mod = types.ModuleType("google")
        transit_mod = types.ModuleType("google.transit")
        pb2_mod = types.ModuleType("google.transit.gtfs_realtime_pb2")

        class _StubMessage:
            def __init__(self, **kwargs):
                self._present = set()
                for k, v in kwargs.items():
                    setattr(self, k, v)
                    if v is not None:
                        self._present.add(k)

            def HasField(self, name: str) -> bool:
                return name in self._present

        class FeedMessage:
            _next_entities: list = []
            _next_timestamp: int = 0

            def __init__(self):
                self.entity = []
                self.header = types.SimpleNamespace(timestamp=0)

            def ParseFromString(self, data: bytes) -> None:
                payload = json.loads(data.decode("utf-8"))
                self.header = types.SimpleNamespace(timestamp=payload.get("timestamp", 0))
                self.entity = []
                for ent in payload.get("entities", []):
                    self.entity.append(_deserialize_entity(ent))

        def _deserialize_entity(ent: dict) -> _StubMessage:
            kwargs: dict = {}
            if "trip_update" in ent:
                tu = ent["trip_update"]
                trip_raw = tu["trip"]
                trip = _StubMessage(
                    trip_id=trip_raw.get("trip_id", ""),
                    route_id=trip_raw.get("route_id", ""),
                    start_date=trip_raw.get("start_date", ""),
                    schedule_relationship=trip_raw.get("schedule_relationship", 0),
                )
                stus = []
                for stu_raw in tu.get("stop_time_update", []):
                    stu_kwargs = {
                        "stop_id": stu_raw.get("stop_id", ""),
                        "schedule_relationship": stu_raw.get("schedule_relationship", 0),
                    }
                    if "departure" in stu_raw:
                        dep = stu_raw["departure"]
                        stu_kwargs["departure"] = _StubMessage(
                            time=dep.get("time"),
                            delay=dep.get("delay"),
                        )
                    if "arrival" in stu_raw:
                        arr = stu_raw["arrival"]
                        stu_kwargs["arrival"] = _StubMessage(
                            time=arr.get("time"),
                            delay=arr.get("delay"),
                        )
                    stus.append(_StubMessage(**stu_kwargs))
                kwargs["trip_update"] = _StubMessage(trip=trip, stop_time_update=stus)

            if "alert" in ent:
                al = ent["alert"]
                periods = [
                    types.SimpleNamespace(start=p.get("start", 0), end=p.get("end", 0))
                    for p in al.get("active_period", [])
                ]
                informed = []
                for ie in al.get("informed_entity", []):
                    ie_kwargs = {}
                    if "trip_id" in ie:
                        ie_kwargs["trip"] = _StubMessage(trip_id=ie["trip_id"])
                    if "route_id" in ie:
                        ie_kwargs["route_id"] = ie["route_id"]
                    if "stop_id" in ie:
                        ie_kwargs["stop_id"] = ie["stop_id"]
                    informed.append(_StubMessage(**ie_kwargs))
                header = types.SimpleNamespace(
                    translation=[
                        types.SimpleNamespace(language=t.get("language", "fr"), text=t["text"])
                        for t in al.get("header_text", [])
                    ]
                )
                desc = types.SimpleNamespace(
                    translation=[
                        types.SimpleNamespace(language=t.get("language", "fr"), text=t["text"])
                        for t in al.get("description_text", [])
                    ]
                )
                kwargs["alert"] = _StubMessage(
                    active_period=periods,
                    informed_entity=informed,
                    header_text=header,
                    description_text=desc,
                )

            return _StubMessage(**kwargs)

        pb2_mod.FeedMessage = FeedMessage
        transit_mod.gtfs_realtime_pb2 = pb2_mod
        google_mod.transit = transit_mod
        sys.modules["google"] = google_mod
        sys.modules["google.transit"] = transit_mod
        sys.modules["google.transit.gtfs_realtime_pb2"] = pb2_mod

    # homeassistant submodules
    for mod_name in (
        "homeassistant.components",
        "homeassistant.components.frontend",
        "homeassistant.components.http",
        "homeassistant.components.sensor",
        "homeassistant.config_entries",
        "homeassistant.const",
        "homeassistant.core",
        "homeassistant.exceptions",
        "homeassistant.helpers",
        "homeassistant.helpers.aiohttp_client",
        "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.selector",
        "homeassistant.helpers.update_coordinator",
        "homeassistant.util",
        "homeassistant.util.dt",
    ):
        sys.modules[mod_name] = types.ModuleType(mod_name)

    sys.modules["homeassistant.components.frontend"].add_extra_js_url = MagicMock()

    class StaticPathConfig:
        def __init__(self, url_path, path, cache_headers=False):
            self.url_path = url_path
            self.path = path
            self.cache_headers = cache_headers

    sys.modules["homeassistant.components.http"].StaticPathConfig = StaticPathConfig

    class SensorDeviceClass:
        TIMESTAMP = "timestamp"

    class SensorEntity:
        def async_write_ha_state(self):
            pass

    sys.modules["homeassistant.components.sensor"].SensorDeviceClass = SensorDeviceClass
    sys.modules["homeassistant.components.sensor"].SensorEntity = SensorEntity

    class ConfigEntryState:
        LOADED = "loaded"
        NOT_LOADED = "not_loaded"

    class ConfigEntry:
        pass

    class ConfigFlow:
        def __init_subclass__(cls, domain=None, **kwargs):
            super().__init_subclass__(**kwargs)

        def async_show_form(self, step_id, data_schema=None, errors=None):
            return {"type": "form", "step_id": step_id, "errors": errors or {}}

        def async_show_menu(self, step_id, menu_options):
            return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

        def async_create_entry(self, title, data):
            return {"type": "create_entry", "title": title, "data": data}

        async def async_set_unique_id(self, unique_id):
            self._unique_id = unique_id

        def _abort_if_unique_id_configured(self):
            pass

    sys.modules["homeassistant.config_entries"].ConfigEntry = ConfigEntry
    sys.modules["homeassistant.config_entries"].ConfigEntryState = ConfigEntryState
    sys.modules["homeassistant.config_entries"].ConfigFlow = ConfigFlow
    sys.modules["homeassistant.config_entries"].ConfigFlowResult = dict
    sys.modules["homeassistant.config_entries"].SOURCE_IMPORT = "import"

    class Platform:
        SENSOR = "sensor"

    sys.modules["homeassistant.const"].Platform = Platform
    sys.modules["homeassistant.core"].HomeAssistant = object
    sys.modules["homeassistant.core"].callback = lambda fn: fn

    class ConfigEntryNotReady(Exception):
        pass

    sys.modules["homeassistant.exceptions"].ConfigEntryNotReady = ConfigEntryNotReady
    sys.modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = lambda hass: MagicMock()
    sys.modules["homeassistant.helpers.entity_platform"].AddEntitiesCallback = object

    class SelectOptionDict(dict):
        pass

    class SelectSelector:
        def __init__(self, config):
            self.config = config

    class SelectSelectorConfig:
        def __init__(self, options, mode):
            self.options = options
            self.mode = mode

    class SelectSelectorMode:
        DROPDOWN = "dropdown"

    sel = sys.modules["homeassistant.helpers.selector"]
    sel.SelectOptionDict = SelectOptionDict
    sel.SelectSelector = SelectSelector
    sel.SelectSelectorConfig = SelectSelectorConfig
    sel.SelectSelectorMode = SelectSelectorMode

    class UpdateFailed(Exception):
        pass

    class DataUpdateCoordinator:
        def __init__(self, hass, logger, name, update_interval):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data = None
            self.last_update_success = True
            self.last_exception = None
            self.shutdown_called = False

        async def async_shutdown(self):
            self.shutdown_called = True

    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

    uc = sys.modules["homeassistant.helpers.update_coordinator"]
    uc.DataUpdateCoordinator = DataUpdateCoordinator
    uc.UpdateFailed = UpdateFailed
    uc.CoordinatorEntity = CoordinatorEntity

    dt_mod = sys.modules["homeassistant.util.dt"]
    dt_mod.now = lambda: datetime.now(PARIS_TZ)
    dt_mod.utc_from_timestamp = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc)
    dt_mod.as_local = lambda dt_obj: dt_obj.astimezone(PARIS_TZ)
    dt_mod.parse_datetime = lambda s: datetime.fromisoformat(s)
    sys.modules["homeassistant.util"].dt = dt_mod


_install_stubs()

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from custom_components.sncf_gtfs import (  # noqa: E402
    SncfGtfsCoordinator,
    _service_origin_timestamp,
    async_unload_entry,
    parse_gtfs_time_to_seconds,
)
from custom_components.sncf_gtfs.config_flow import SncfGtfsConfigFlow  # noqa: E402
from custom_components.sncf_gtfs.const import (  # noqa: E402
    CANCELED,
    CONF_DESTINATION_UIC,
    CONF_NAME,
    CONF_ORIGIN_UIC,
    CONF_TRACK_TYPE,
    CONF_TRAIN_NUMBER,
    DOMAIN,
    MAX_STORED_TRAINS,
    TRACK_TYPE_ROUTE,
    TRACK_TYPE_TRAIN,
)
from custom_components.sncf_gtfs.sensor import (  # noqa: E402
    SncfRouteSensor,
    SncfSpecificTrainSensor,
)


def _make_static_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _make_fake_hass(tmpdir: str, entries: list | None = None):
    hass = MagicMock()
    hass.data = {}
    hass.config.path = lambda *parts: str(Path(tmpdir, *parts))
    hass.config_entries.async_entries = MagicMock(return_value=entries or [])
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock()
    hass.async_create_task = lambda coro: asyncio.create_task(coro)
    return hass


class TestTimeAndDst(unittest.TestCase):
    def test_parse_gtfs_time_to_seconds(self):
        self.assertEqual(parse_gtfs_time_to_seconds("08:30:00"), 8 * 3600 + 30 * 60)
        self.assertEqual(parse_gtfs_time_to_seconds("25:15:30"), 25 * 3600 + 15 * 60 + 30)
        self.assertIsNone(parse_gtfs_time_to_seconds(""))
        self.assertIsNone(parse_gtfs_time_to_seconds("invalid"))

    def test_service_origin_timestamp_dst_spring_forward(self):
        # 2026-03-29 is Europe/Paris spring forward (02:00 CET -> 03:00 CEST)
        origin = _service_origin_timestamp("20260329")
        dep_0800 = datetime.fromtimestamp(origin + 8 * 3600, tz=PARIS_TZ)
        self.assertEqual(dep_0800.strftime("%Y-%m-%d %H:%M"), "2026-03-29 08:00")

    def test_service_origin_timestamp_dst_fall_back(self):
        # 2026-10-25 is Europe/Paris fall back (03:00 CEST -> 02:00 CET)
        origin = _service_origin_timestamp("20261025")
        dep_0800 = datetime.fromtimestamp(origin + 8 * 3600, tz=PARIS_TZ)
        self.assertEqual(dep_0800.strftime("%Y-%m-%d %H:%M"), "2026-10-25 08:00")


class TestStaticAndRtMerge(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.hass = _make_fake_hass(self.tmpdir.name)
        self.coord = SncfGtfsCoordinator(self.hass)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_static_zip_calendar_and_train_number_filter(self):
        today = datetime.now(PARIS_TZ).strftime("%Y%m%d")
        weekday_idx = datetime.now(PARIS_TZ).weekday()
        flags = ["1" if i == weekday_idx else "0" for i in range(7)]

        zip_bytes = _make_static_zip({
            "calendar.txt": (
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                f"SVC_WEEKLY,{','.join(flags)},20200101,20991231\n"
                f"SVC_REMOVED,{','.join(flags)},20200101,20991231\n"
            ),
            "calendar_dates.txt": (
                "service_id,date,exception_type\n"
                f"SVC_REMOVED,{today},2\n"
                f"SVC_ADDED,{today},1\n"
            ),
            "trips.txt": (
                "route_id,service_id,trip_id,trip_headsign\n"
                "R1,SVC_WEEKLY,OCESN17616F1,17616\n"
                "R2,SVC_REMOVED,OCESN99999F1,99999\n"
                "R3,SVC_ADDED,OCESN88888F1,88888\n"
            ),
            "stop_times.txt": (
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "OCESN17616F1,08:00:00,08:00:00,StopPoint:OCE-87747006,1\n"
                "OCESN17616F1,09:00:00,09:00:00,StopPoint:OCE-87723197,2\n"
                "OCESN99999F1,10:00:00,10:00:00,StopPoint:OCE-87747006,1\n"
                "OCESN88888F1,11:00:00,11:00:00,StopPoint:OCE-87686006,1\n"
            ),
        })

        # Track route on 87747006/87723197 AND specific train 88888 without origin_uic
        payload = self.coord._parse_static_zip(
            zip_bytes,
            target_uics={"87747006", "87723197"},
            target_train_numbers={"88888"},
        )
        today_trips = [t for t in payload["trips"] if t["service_date"] == today]
        train_nums = {t["train_number"] for t in today_trips}

        self.assertIn("17616", train_nums)
        self.assertIn("88888", train_nums)
        self.assertNotIn("99999", train_nums)

    def test_multiday_trip_id_does_not_leak_rt_delay_to_tomorrow(self):
        now_ts = int(datetime.now(PARIS_TZ).timestamp())
        today_str = datetime.fromtimestamp(now_ts, tz=PARIS_TZ).strftime("%Y%m%d")
        tomorrow_str = datetime.fromtimestamp(now_ts + 86400, tz=PARIS_TZ).strftime("%Y%m%d")

        dep_today = now_ts + 1800
        dep_tomorrow = dep_today + 72000  # 20h ahead, still within 24h horizon

        static_trips = [
            {
                "trip_id": "OCESN17616F1",
                "service_date": today_str,
                "route_id": "R1",
                "train_number": "17616",
                "stops": [
                    {"stop_id": "StopPoint:OCE-87747006", "arr_ts": dep_today, "dep_ts": dep_today},
                    {"stop_id": "StopPoint:OCE-87723197", "arr_ts": dep_today + 3600, "dep_ts": dep_today + 3600},
                ],
            },
            {
                "trip_id": "OCESN17616F1",
                "service_date": tomorrow_str,
                "route_id": "R1",
                "train_number": "17616",
                "stops": [
                    {"stop_id": "StopPoint:OCE-87747006", "arr_ts": dep_tomorrow, "dep_ts": dep_tomorrow},
                    {"stop_id": "StopPoint:OCE-87723197", "arr_ts": dep_tomorrow + 3600, "dep_ts": dep_tomorrow + 3600},
                ],
            },
        ]

        # RT cancels today's run only
        rt_payload = json.dumps({
            "timestamp": now_ts,
            "entities": [
                {
                    "trip_update": {
                        "trip": {
                            "trip_id": "OCESN17616F1",
                            "start_date": today_str,
                            "route_id": "R1",
                            "schedule_relationship": CANCELED,
                        },
                        "stop_time_update": [],
                    }
                }
            ],
        }).encode("utf-8")

        merged = self.coord._merge_static_and_rt(static_trips, rt_payload, None)
        by_date = {t["service_date"]: t for t in merged["trips"]}

        self.assertTrue(by_date[today_str]["canceled"])
        self.assertTrue(by_date[today_str]["is_realtime"])
        self.assertFalse(by_date[tomorrow_str]["canceled"])
        self.assertFalse(by_date[tomorrow_str]["is_realtime"])

    def test_rt_delay_only_and_time_only_reconciliation(self):
        now_ts = int(datetime.now(PARIS_TZ).timestamp())
        today_str = datetime.fromtimestamp(now_ts, tz=PARIS_TZ).strftime("%Y%m%d")
        st_dep = now_ts + 1800
        st_arr = st_dep + 3600

        static_trips = [
            {
                "trip_id": "OCESN10001",
                "service_date": today_str,
                "route_id": "R1",
                "train_number": "10001",
                "stops": [
                    {"stop_id": "StopPoint:OCE-87747006", "arr_ts": st_dep, "dep_ts": st_dep},
                    {"stop_id": "StopPoint:OCE-87723197", "arr_ts": st_arr, "dep_ts": st_arr},
                ],
            },
            {
                "trip_id": "OCESN10002",
                "service_date": today_str,
                "route_id": "R1",
                "train_number": "10002",
                "stops": [
                    {"stop_id": "StopPoint:OCE-87747006", "arr_ts": st_dep, "dep_ts": st_dep},
                    {"stop_id": "StopPoint:OCE-87723197", "arr_ts": st_arr, "dep_ts": st_arr},
                ],
            },
        ]

        rt_payload = json.dumps({
            "timestamp": now_ts,
            "entities": [
                # Case 1: delay provided, time omitted
                {
                    "trip_update": {
                        "trip": {"trip_id": "OCESN10001", "start_date": today_str},
                        "stop_time_update": [
                            {"stop_id": "StopPoint:OCE-87747006", "departure": {"delay": 600}},
                            {"stop_id": "StopPoint:OCE-87723197", "arrival": {"delay": 600}},
                        ],
                    }
                },
                # Case 2: time provided (+900s), delay omitted
                {
                    "trip_update": {
                        "trip": {"trip_id": "OCESN10002", "start_date": today_str},
                        "stop_time_update": [
                            {"stop_id": "StopPoint:OCE-87747006", "departure": {"time": st_dep + 900}},
                            {"stop_id": "StopPoint:OCE-87723197", "arrival": {"time": st_arr + 900}},
                        ],
                    }
                },
            ],
        }).encode("utf-8")

        merged = self.coord._merge_static_and_rt(static_trips, rt_payload, None)
        by_id = {t["trip_id"]: t for t in merged["trips"]}

        s1 = by_id["OCESN10001"]["stops"][0]
        self.assertEqual(s1["sched_dep_ts"], st_dep)
        self.assertEqual(s1["dep_ts"], st_dep + 600)
        self.assertEqual(s1["dep_delay"], 600)

        s2 = by_id["OCESN10002"]["stops"][0]
        self.assertEqual(s2["sched_dep_ts"], st_dep)
        self.assertEqual(s2["dep_ts"], st_dep + 900)
        self.assertEqual(s2["dep_delay"], 900)


class TestSensorsAndLifecycle(unittest.TestCase):
    def test_route_sensor_alert_scoping_and_train_cap(self):
        now_ts = int(datetime(2026, 6, 1, 8, 0, tzinfo=PARIS_TZ).timestamp())
        trips = []
        for i in range(MAX_STORED_TRAINS + 5):
            dep = now_ts + (i + 1) * 600
            trips.append({
                "trip_id": f"TRIP_{i}",
                "route_id": "ROUTE_A",
                "train_number": str(10000 + i),
                "canceled": False,
                "is_realtime": True,
                "stops": [
                    {
                        "stop_id": "StopPoint:OCE-87747006",
                        "skipped": False,
                        "dep_ts": dep + 300 if i == 0 else dep,
                        "arr_ts": dep + 300 if i == 0 else dep,
                        "sched_dep_ts": dep,
                        "sched_arr_ts": dep,
                        "dep_delay": 300 if i == 0 else 0,
                        "arr_delay": 300 if i == 0 else 0,
                        "is_realtime": True,
                    },
                    {
                        "stop_id": "StopPoint:OCE-87723197",
                        "skipped": False,
                        "dep_ts": dep + 3900 if i == 0 else dep + 3600,
                        "arr_ts": dep + 3900 if i == 0 else dep + 3600,
                        "sched_dep_ts": dep + 3600,
                        "sched_arr_ts": dep + 3600,
                        "dep_delay": 300 if i == 0 else 0,
                        "arr_delay": 300 if i == 0 else 0,
                        "is_realtime": True,
                    },
                ],
            })

        alerts = [
            # Matches our route
            {"trip_ids": [], "route_ids": ["ROUTE_A"], "stop_ids": [], "title": "Route A Alert", "description": ""},
            # Belongs to another route that happens to stop at Lyon Part-Dieu -> must NOT match
            {
                "trip_ids": [],
                "route_ids": ["ROUTE_OTHER"],
                "stop_ids": ["StopPoint:OCE-87723197"],
                "title": "Unrelated TGV Alert",
                "description": "",
            },
            # Pure station-wide alert (no trip/route filter) -> MUST match
            {
                "trip_ids": [],
                "route_ids": [],
                "stop_ids": ["StopPoint:OCE-87747006"],
                "title": "Station Power Outage",
                "description": "",
            },
        ]

        coordinator = MagicMock()
        coordinator.data = {
            "trips": trips,
            "alerts": alerts,
            "now_ts": now_ts,
            "downloaded_at": "2026-06-01T08:00:00+02:00",
            "feed_timestamp": "2026-06-01T07:59:00+02:00",
        }
        entry = types.SimpleNamespace(
            entry_id="e1",
            data={
                CONF_TRACK_TYPE: TRACK_TYPE_ROUTE,
                CONF_ORIGIN_UIC: "87747006",
                CONF_DESTINATION_UIC: "87723197",
                CONF_NAME: "Grenoble -> Lyon",
            },
        )
        sensor = SncfRouteSensor(coordinator, entry)

        self.assertEqual(sensor.native_value, "08:15")
        attrs = sensor.extra_state_attributes
        self.assertEqual(attrs["train_count"], MAX_STORED_TRAINS + 5)
        self.assertEqual(len(attrs["trains"]), MAX_STORED_TRAINS)
        self.assertEqual(attrs["next_scheduled_departure"], "08:10")
        self.assertEqual(attrs["next_departure"], "08:15")
        self.assertEqual(attrs["next_status"], "Delayed (+5 min)")
        alert_titles = [a["title"] for a in attrs["alerts"]]
        self.assertEqual(alert_titles, ["Route A Alert", "Station Power Outage"])

    def test_specific_train_sensor_caches_until_update(self):
        now_ts = int(datetime(2026, 6, 1, 8, 0, tzinfo=PARIS_TZ).timestamp())
        coordinator = MagicMock()
        coordinator.data = {
            "trips": [
                {
                    "trip_id": "OCESN17616",
                    "route_id": "R1",
                    "train_number": "17616",
                    "canceled": False,
                    "is_realtime": True,
                    "stops": [
                        {
                            "stop_id": "StopPoint:OCE-87747006",
                            "skipped": False,
                            "dep_ts": now_ts + 1200,
                            "arr_ts": now_ts + 1200,
                            "sched_dep_ts": now_ts + 1200,
                            "sched_arr_ts": now_ts + 1200,
                            "dep_delay": 0,
                            "arr_delay": 0,
                            "is_realtime": True,
                        }
                    ],
                }
            ],
            "alerts": [],
            "now_ts": now_ts,
            "downloaded_at": "2026-06-01T08:00:00+02:00",
            "feed_timestamp": None,
        }
        entry = types.SimpleNamespace(
            entry_id="e2",
            data={
                CONF_TRACK_TYPE: TRACK_TYPE_TRAIN,
                CONF_TRAIN_NUMBER: "17616",
                CONF_ORIGIN_UIC: "87747006",
                CONF_NAME: "Train 17616",
            },
        )
        sensor = SncfSpecificTrainSensor(coordinator, entry)
        self.assertEqual(sensor.native_value, "On time")
        self.assertEqual(sensor.extra_state_attributes["scheduled_departure"], "08:20")

        # Update coordinator data and trigger callback
        coordinator.data["trips"][0]["canceled"] = True
        sensor._handle_coordinator_update()
        self.assertEqual(sensor.native_value, "Canceled")

    def test_unload_entry_shuts_down_coordinator_and_rehomes_timestamp_sensor(self):
        async def _run():
            with tempfile.TemporaryDirectory() as tmpdir:
                e1 = types.SimpleNamespace(entry_id="e1", state=ConfigEntryState.LOADED)
                e2 = types.SimpleNamespace(entry_id="e2", state=ConfigEntryState.LOADED)

                # Case 1: Unload owner of timestamp sensor while e2 remains loaded
                hass = _make_fake_hass(tmpdir, entries=[e1, e2])
                coord = SncfGtfsCoordinator(hass)
                hass.data[DOMAIN] = {
                    "coordinator": coord,
                    "init_lock": asyncio.Lock(),
                    "timestamp_sensor_entry_id": "e1",
                }
                ok = await async_unload_entry(hass, e1)
                self.assertTrue(ok)
                self.assertFalse(coord.shutdown_called)
                self.assertNotIn("timestamp_sensor_entry_id", hass.data[DOMAIN])
                await asyncio.sleep(0)
                hass.config_entries.async_reload.assert_called_once_with("e2")

                # Case 2: Unload last loaded entry -> shuts down coordinator
                hass.config_entries.async_entries.return_value = [e2]
                ok = await async_unload_entry(hass, e2)
                self.assertTrue(ok)
                self.assertTrue(coord.shutdown_called)
                self.assertNotIn("coordinator", hass.data[DOMAIN])

        asyncio.run(_run())


class TestConfigFlow(unittest.TestCase):
    def test_track_train_handles_none_optionals_and_blank_train_number(self):
        async def _run():
            flow = SncfGtfsConfigFlow()
            flow.hass = MagicMock()
            flow._stations_map = {"87747006": "Grenoble"}

            # Blank train number -> form error
            res_err = await flow.async_step_track_train(
                {CONF_TRAIN_NUMBER: "   ", CONF_ORIGIN_UIC: None, CONF_NAME: None}
            )
            self.assertEqual(res_err["type"], "form")
            self.assertEqual(res_err["errors"][CONF_TRAIN_NUMBER], "invalid_train_number")

            # Valid train number with None optional fields
            res_ok = await flow.async_step_track_train(
                {CONF_TRAIN_NUMBER: " 17616 ", CONF_ORIGIN_UIC: None, CONF_NAME: None}
            )
            self.assertEqual(res_ok["type"], "create_entry")
            self.assertEqual(res_ok["data"][CONF_TRAIN_NUMBER], "17616")
            self.assertEqual(res_ok["data"][CONF_ORIGIN_UIC], "")
            self.assertEqual(res_ok["title"], "Train SNCF 17616")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
