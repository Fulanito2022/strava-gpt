from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dateutil import parser as dtp

from .config import ADMIN_TOKEN, BASE_URL, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET
from . import auth as oauth
from .stats import summarize_runs, compare_runs
from .strava import exchange_code_for_token, ensure_fresh_token, list_activities, get_activity
from .storage import upsert_token, get_token, save_or_update_activity, query_runs
from datetime import timedelta  


# ---- intentamos importar helper opcional; si no existe, definimos fallback ----
try:
    from .storage import get_any_athlete_id as storage_get_any_athlete_id
except Exception:
    storage_get_any_athlete_id = None
    # Fallback leyendo directamente la tabla de tokens
    from sqlalchemy import select
    from .storage import SessionLocal  # existe en storage.py
    from .models import Token

    def _fallback_get_any_athlete_id() -> int | None:
        with SessionLocal() as s:
            t = s.execute(select(Token)).scalars().first()
            return t.athlete_id if t else None

# -----------------------------------------------------------------------------

app = FastAPI(title="Strava GPT Backend", version="1.0.0")

# CORS para poder llamar desde herramientas en el navegador (Hoppscotch, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# OAuth router
app.include_router(oauth.router)

# Guardamos en memoria si está disponible; si Render se reinicia, lo resolvemos desde DB
ATHLETE_ID_SINGLETON: int | None = None


def resolve_athlete_id() -> int | None:
    """Devuelve el athlete_id activo. Si la app se ha reiniciado,
    lo recupera de la base de datos sin necesidad de reautorizar."""
    global ATHLETE_ID_SINGLETON
    if ATHLETE_ID_SINGLETON:
        return ATHLETE_ID_SINGLETON

    aid = None
    if storage_get_any_athlete_id:
        try:
            aid = storage_get_any_athlete_id()
        except Exception:
            aid = None
    else:
        # Fallback si no existe el helper en storage.py
        try:
            aid = _fallback_get_any_athlete_id()
        except Exception:
            aid = None

    if aid:
        ATHLETE_ID_SINGLETON = aid
        return aid
    return None


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/oauth/callback")
async def oauth_callback(code: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(400, f"OAuth error: {error}")
    if not code:
        raise HTTPException(400, "Falta code")
    data = await exchange_code_for_token(code)
    access_token = data["access_token"]
    refresh_token = data["refresh_token"]
    expires_at = int(data["expires_at"])
    athlete_id = data["athlete"]["id"]

    upsert_token(athlete_id, access_token, refresh_token, expires_at)

    global ATHLETE_ID_SINGLETON
    ATHLETE_ID_SINGLETON = athlete_id

    return PlainTextResponse("Autorización correcta. Ya puedes usar el GPT.")


# --- Admin: crear suscripción webhook (llámalo una vez) ---
class SubReq(BaseModel):
    verify_token: str = "verify"


@app.post("/admin/subscribe")
async def admin_subscribe(authorization: str = Header(None)):
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(401, "No autorizado")
    import httpx
    async with httpx.AsyncClient() as c:
        r = await c.post("https://www.strava.com/api/v3/push_subscriptions", data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "callback_url": f"{BASE_URL}/strava/webhook",
            "verify_token": "verify",
        })
        r.raise_for_status()
        return r.json()


# --- Webhook verification ---
@app.get("/strava/webhook")
async def strava_verify(request: Request):
    # Strava puede usar 'hub.*' o parámetros simples
    qp = request.query_params
    verify_token = qp.get("hub.verify_token") or qp.get("token")
    challenge = qp.get("hub.challenge") or qp.get("challenge")
    if verify_token != "verify":
        raise HTTPException(403, "verify_token incorrecto")
    return {"hub.challenge": challenge}


# --- Webhook events ---
class Event(BaseModel):
    object_type: str
    object_id: int
    aspect_type: str
    owner_id: int
    subscription_id: int | None = None
    event_time: int | None = None
    updates: dict | None = None


@app.post("/strava/webhook")
async def strava_event(ev: Event):
    if ev.object_type != "activity":
        return {"ignored": True}

    token_row = get_token(ev.owner_id)
    if not token_row:
        return {"error": "Sin token para este atleta"}

    token_row = await ensure_fresh_token(token_row, upsert_token)
    act = await get_activity(token_row.access_token, ev.object_id)
    # Guardar solo si es running
    if act.get("type") == "Run":
        save_or_update_activity(act, athlete_id=ev.owner_id)
    return {"ok": True}


# --- Endpoint para el GPT: actividades por fechas ---
@app.get("/activities")
async def activities(start: str, end: str):
    """Lista actividades de running entre start y end (ISO YYYY-MM-DD)."""
    aid = resolve_athlete_id()
    if not aid:
        raise HTTPException(400, "Falta autorizar OAuth primero")

    s = dtp.isoparse(start).replace(tzinfo=None)
    e = dtp.isoparse(end).replace(tzinfo=None)
    runs = query_runs(aid, s.isoformat(), e.isoformat())
    return [{
        "id": r.id,
        "date": r.start_date.isoformat(),
        "name": r.name,
        "distance_km": round(r.distance_m / 1000, 2),
        "moving_time_min": round(r.moving_time_s / 60, 1),
        "avg_hr": r.average_heartrate,
        "elev_gain_m": r.total_elevation_gain_m,
        "avg_pace": None,  # calculable client-side si se desea
    } for r in runs]


# --- Resumen por fechas (para respuestas rápidas del GPT) ---
@app.get("/stats/summary")
async def stats_summary(start: str, end: str):
    aid = resolve_athlete_id()
    if not aid:
        raise HTTPException(400, "Falta autorizar OAuth primero")

    s = dtp.isoparse(start).replace(tzinfo=None)
    e = dtp.isoparse(end).replace(tzinfo=None)
    runs = query_runs(aid, s.isoformat(), e.isoformat())
    summary = summarize_runs(runs)
    return summary

@app.get("/stats/compare")
async def stats_compare(start: str, end: str, prev_weeks: int | None = 4):
    """Compara el rango [start,end] con un rango previo.
    - Si prev_weeks está definido: compara con las prev_weeks*7 días justo antes de 'start'.
    - Si no: compara con un rango de la MISMA longitud justo anterior.
    """
    aid = resolve_athlete_id()
    if not aid:
        raise HTTPException(400, "Falta autorizar OAuth primero")

    s = dtp.isoparse(start).replace(tzinfo=None)
    e = dtp.isoparse(end).replace(tzinfo=None)

    if prev_weeks and prev_weeks > 0:
        prev_start = s - timedelta(days=prev_weeks * 7)
        prev_end = s - timedelta(seconds=1)
    else:
        span = e - s
        prev_start = s - span
        prev_end = s - timedelta(seconds=1)

    curr_runs = query_runs(aid, s.isoformat(), e.isoformat())
    prev_runs = query_runs(aid, prev_start.isoformat(), prev_end.isoformat())

    result = compare_runs(curr_runs, prev_runs)
    result["current_range"] = {"start": s.isoformat(), "end": e.isoformat()}
    result["previous_range"] = {"start": prev_start.isoformat(), "end": prev_end.isoformat()}
    return result


# --- Utilidad: import inicial (pull) para poblar la DB sin esperar webhooks ---
@app.post("/admin/initial-import")
async def initial_import(authorization: str = Header(None), days: int = 365):
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(401, "No autorizado")

    aid = resolve_athlete_id()
    if not aid:
        raise HTTPException(400, "Falta autorizar OAuth primero")

    token_row = get_token(aid)
    token_row = await ensure_fresh_token(token_row, upsert_token)

    from time import time
    after = int(time()) - days * 86400
    page = 1
    while True:
        acts = await list_activities(token_row.access_token, after=after, page=page)
        if not acts:
            break
        for a in acts:
            if a.get("type") == "Run":
                # enriquecer con best_efforts llamando al detalle
                det = await get_activity(token_row.access_token, a["id"])
                save_or_update_activity(det, athlete_id=aid)
        page += 1
    return {"imported": True}
