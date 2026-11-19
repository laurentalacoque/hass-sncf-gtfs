# SNCF GTFS-RT for Home Assistant (`hass-sncf-gtfs`)

Custom Home Assistant integration to track real-time SNCF train schedules (TER, TGV, Intercités) using official Open Data GTFS and GTFS-RT feeds, complete with custom Lovelace cards.

## Features

- **Real-Time Schedules & Disruptions (GTFS-RT)**: Tracks delays, cancellations, and updated departure/arrival times using a single shared coordinator (polled every 3 minutes without redundant API calls).
- **UI Configuration (Config Flow)**: Searchable departure and arrival station selection with an optional automatic round-trip setup.
- **Built-in Lovelace Cards**:
  - `custom:sncf-route-card`: Displays upcoming departures and arrivals for a selected route.
  - `custom:sncf-specific-train-card`: Dedicated live tracking for a specific train number.

## Installation

1. In Home Assistant, go to **Settings > Marketplace**.
2. Open the menu in the top right corner (**⋮**) and select **Custom repositories** (or add `https://github.com/laurentalacoque/hass-sncf-gtfs` as an **Integration**).
3. Search for **SNCF GTFS-RT**, click **Download / Install**, and restart Home Assistant.

## Configuration

1. Navigate to **Settings > Devices & services > Add integration**.
2. Search for **SNCF GTFS-RT**.
3. Select your departure and arrival stations (or enter a specific train number) and submit.
4. Add the **SNCF Route Card** or **SNCF Specific Train Card** directly from your dashboard's visual card picker.