# SNCF GTFS-RT for Home Assistant (`hass-sncf-gtfs`)

[![Version](https://img.shields.io/github/v/tag/laurentalacoque/hass-sncf-gtfs?label=Version&style=for-the-badge&color=03a9f4)](https://github.com/laurentalacoque/hass-sncf-gtfs/tags)
[![Validate](https://img.shields.io/github/actions/workflow/status/laurentalacoque/hass-sncf-gtfs/validate.yaml?branch=main&label=Validate&logo=githubactions&logoColor=white&style=for-the-badge)](https://github.com/laurentalacoque/hass-sncf-gtfs/actions/workflows/validate.yaml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?logo=homeassistantcommunitystore&logoColor=white&style=for-the-badge)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Integration-41BDF5.svg?logo=homeassistant&logoColor=white&style=for-the-badge)](https://www.home-assistant.io/)
[![License](https://img.shields.io/github/license/laurentalacoque/hass-sncf-gtfs?style=for-the-badge&color=4caf50)](LICENSE)

Custom Home Assistant integration to track real-time SNCF train schedules (TER, TGV, Intercités) using official Open Data GTFS and GTFS-RT feeds, complete with custom Lovelace cards.

## Features

- **Real-Time Schedules & Disruptions (GTFS-RT)**: Tracks delays, cancellations, and updated departure/arrival times using a single shared coordinator (polled every 3 minutes without redundant API calls).
- **UI Configuration (Config Flow)**: Searchable departure and arrival station selection with an optional automatic round-trip setup.
- **Built-in Lovelace Cards**:
  - `custom:sncf-route-card`: Displays upcoming departures and arrivals for a selected route.
  - `custom:sncf-specific-train-card`: Dedicated live tracking for a specific train number.

## Installation

### One-click via HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=laurentalacoque&repository=hass-sncf-gtfs&category=integration)

### Manual via HACS

1. In Home Assistant, open **HACS**.
2. Open the menu in the top right corner (**⋮**) and select **Custom repositories**, then add `https://github.com/laurentalacoque/hass-sncf-gtfs` as an **Integration**.
3. Search for **SNCF GTFS-RT**, click **Download / Install**, and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=sncf_gtfs)

1. Navigate to **Settings > Devices & services > Add integration**.
2. Search for **SNCF GTFS-RT**.
3. Select your departure and arrival stations (or enter a specific train number) and submit.
4. Add the **SNCF Route Card** or **SNCF Specific Train Card** directly from your dashboard's visual card picker.
