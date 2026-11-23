"""Constants for ariston_schedule."""
from datetime import timedelta

DOMAIN = "ariston_schedule"
API_URL = "https://www.ariston-net.remotethermo.com/api/v2/"
USER_AGENT = "RestSharp/106.11.7.0"

CONF_GW = "gateway_id"
CONF_CHEAP_START = "cheap_start_hour"
CONF_CHEAP_END = "cheap_end_hour"
DEFAULT_CHEAP_START = 0  # jeftina tarifa od 00:00
DEFAULT_CHEAP_END = 6    # do 06:00
CONF_HEATING_ENTITY = "heating_entity"
DEFAULT_HEATING_ENTITY = "binary_sensor.bojler_greje"  # fustom integracija: bojler trenutno greje
CONF_PRICE_CHEAP = "price_cheap_entity"
DEFAULT_PRICE_CHEAP = "input_number.struja_cena_jeftina"
CONF_PRICE_EXPENSIVE = "price_expensive_entity"
DEFAULT_PRICE_EXPENSIVE = "input_number.struja_cena_skupa"

SCAN_INTERVAL = timedelta(minutes=10)
BACKUP_DIR_NAME = "ariston_schedule_backups"
MAX_BACKUPS = 50

SERVICE_SET_DAYS = "set_days"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_RESTORE_BACKUP = "restore_backup"
SERVICE_REFRESH = "refresh"
SERVICE_EXPORT_XLSX = "export_xlsx"
