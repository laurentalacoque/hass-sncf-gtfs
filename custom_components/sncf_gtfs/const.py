"""Constants for the SNCF GTFS-RT integration."""

DOMAIN = "sncf_gtfs"

GTFS_RT_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"
STATIONS_URL = "https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/gares-de-voyageurs/exports/json"

FRONTEND_SCRIPT_URL = "/sncf_gtfs_static/sncf-cards.js"

CONF_TRACK_TYPE = "track_type"
CONF_NAME = "name"
CONF_TRAIN_NUMBER = "train_number"
CONF_ORIGIN_UIC = "origin_uic"
CONF_DESTINATION_UIC = "destination_uic"
CONF_ADD_ROUNDTRIP = "also_add_roundtrip"

TRACK_TYPE_TRAIN = "train"
TRACK_TYPE_ROUTE = "route"

DEPARTURE_MARGIN_SEC = 60
UPDATE_INTERVAL_MINUTES = 3

# Protobuf ScheduleRelationship enums
SKIPPED = 1
CANCELED = 3