import os
from dotenv import load_dotenv

load_dotenv()

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "")
# La URL pública de tu backend, sin trailing slash, p. ej. https://tuapp.onrender.com
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

# Scopes mínimos para leer todas tus actividades (incluidas privadas)
STRAVA_SCOPES = ["read", "activity:read_all"]

# Dónde guardamos la DB SQLite
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data.db")

# Clave opcional simple para proteger el endpoint admin de suscripción webhook
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
