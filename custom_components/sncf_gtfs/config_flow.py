"""Config flow for SNCF GTFS-RT Tracker integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, SOURCE_IMPORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ADD_ROUNDTRIP,
    CONF_DESTINATION_UIC,
    CONF_NAME,
    CONF_ORIGIN_UIC,
    CONF_TRACK_TYPE,
    CONF_TRAIN_NUMBER,
    DOMAIN,
    STATIONS_URL,
    TRACK_TYPE_ROUTE,
    TRACK_TYPE_TRAIN,
)

_LOGGER = logging.getLogger(__name__)
STATIONS_TIMEOUT = aiohttp.ClientTimeout(total=15)

FALLBACK_STATIONS: dict[str, str] = {
    "87686006": "Paris Gare de Lyon",
    "87271007": "Paris Gare du Nord",
    "87391003": "Paris Montparnasse",
    "87723197": "Lyon Part Dieu",
    "87751008": "Marseille Saint-Charles",
    "87581009": "Bordeaux Saint-Jean",
    "87286005": "Lille Flandres",
    "87212027": "Strasbourg",
    "87481002": "Nantes",
    "87611004": "Toulouse Matabiau",
    "87747006": "Grenoble",
}


class SncfGtfsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SNCF GTFS-RT Tracker."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._stations_map: dict[str, str] = {}

    async def _async_load_stations(self) -> list[SelectOptionDict]:
        """Download and format the official SNCF passenger stations list."""
        if not self._stations_map:
            session = async_get_clientsession(self.hass)
            try:
                async with session.get(STATIONS_URL, timeout=STATIONS_TIMEOUT) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

                for item in data:
                    name = item.get("nom")
                    raw_uic = str(item.get("codes_uic") or "")
                    uic = raw_uic.split(";")[0].strip()[:8]
                    if name and len(uic) == 8 and uic.isdigit():
                        self._stations_map[uic] = name
            except Exception as err:
                _LOGGER.warning("Unable to load SNCF stations list: %s", err)
                self._stations_map = dict(FALLBACK_STATIONS)

        sorted_stations = sorted(self._stations_map.items(), key=lambda x: x[1])
        return [
            SelectOptionDict(value=uic, label=f"{name} ({uic})")
            for uic, name in sorted_stations
        ]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initial step: choose between tracking a specific train or a route."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["track_train", "track_route"],
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle automatic creation of the return route entry."""
        origin = import_data[CONF_ORIGIN_UIC]
        destination = import_data[CONF_DESTINATION_UIC]
        name = import_data[CONF_NAME]

        await self.async_set_unique_id(f"route_{origin}_{destination}")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(title=name, data=import_data)

    async def async_step_track_train(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure tracking for a specific train number."""
        errors: dict[str, str] = {}
        station_options = await self._async_load_stations()

        if user_input is not None:
            train_num = (user_input.get(CONF_TRAIN_NUMBER) or "").strip()
            origin_uic = (user_input.get(CONF_ORIGIN_UIC) or "").strip()

            if not train_num:
                errors[CONF_TRAIN_NUMBER] = "invalid_train_number"
            else:
                origin_name = self._stations_map.get(origin_uic, origin_uic)
                default_title = (
                    f"Train {train_num} ({origin_name})"
                    if origin_name
                    else f"Train SNCF {train_num}"
                )
                name = (user_input.get(CONF_NAME) or "").strip() or default_title

                unique_id = (
                    f"train_{train_num}_{origin_uic}"
                    if origin_uic
                    else f"train_{train_num}"
                )
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_TRACK_TYPE: TRACK_TYPE_TRAIN,
                        CONF_NAME: name,
                        CONF_TRAIN_NUMBER: train_num,
                        CONF_ORIGIN_UIC: origin_uic,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_TRAIN_NUMBER): str,
                vol.Optional(CONF_ORIGIN_UIC): SelectSelector(
                    SelectSelectorConfig(
                        options=station_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_NAME, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="track_train", data_schema=schema, errors=errors
        )

    async def async_step_track_route(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure tracking for an origin -> destination route."""
        errors: dict[str, str] = {}
        station_options = await self._async_load_stations()

        if user_input is not None:
            origin = (user_input.get(CONF_ORIGIN_UIC) or "").strip()
            destination = (user_input.get(CONF_DESTINATION_UIC) or "").strip()
            add_roundtrip = bool(user_input.get(CONF_ADD_ROUNDTRIP, False))

            origin_name = self._stations_map.get(origin, origin)
            dest_name = self._stations_map.get(destination, destination)
            default_title = f"{origin_name} -> {dest_name}"
            name = (user_input.get(CONF_NAME) or "").strip() or default_title

            if origin == destination:
                errors["base"] = "same_station"
            else:
                unique_id = f"route_{origin}_{destination}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                # Trigger creation of the return route in the background if checked
                if add_roundtrip:
                    return_title = f"{dest_name} -> {origin_name}"
                    self.hass.async_create_task(
                        self.hass.config_entries.flow.async_init(
                            DOMAIN,
                            context={"source": SOURCE_IMPORT},
                            data={
                                CONF_TRACK_TYPE: TRACK_TYPE_ROUTE,
                                CONF_NAME: return_title,
                                CONF_ORIGIN_UIC: destination,
                                CONF_DESTINATION_UIC: origin,
                            },
                        )
                    )

                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_TRACK_TYPE: TRACK_TYPE_ROUTE,
                        CONF_NAME: name,
                        CONF_ORIGIN_UIC: origin,
                        CONF_DESTINATION_UIC: destination,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ORIGIN_UIC): SelectSelector(
                    SelectSelectorConfig(
                        options=station_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_DESTINATION_UIC): SelectSelector(
                    SelectSelectorConfig(
                        options=station_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_ADD_ROUNDTRIP, default=False): bool,
                vol.Optional(CONF_NAME, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="track_route", data_schema=schema, errors=errors
        )
