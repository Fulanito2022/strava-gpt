from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dateutil import parser as dtp
from datetime import timedelta

from .config import ADMIN_TOKEN, BASE_URL, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET
from . import auth as oauth
from .stats import summarize_runs, compare_runs
from .strava import exchange_code_for_token, ensure_fresh_token, list_activities, get_activity
from .storage import upsert_token, get_token, save_or_update_activity, query_runs

# ---- helper para recuperar athlete tras reinicio ----
try:
    from .storage import get_any_athlete_id as storage_get_any_athlete_id
except Exception:
    storage_get_any_athlete_id = None
    from sqlalchemy import select
    from .storage import SessionLocal
    from .models import Token
    def _fallback_get_any_athlete_id() -> int | None:
        with SessionLocal() as s:
            t = s.execute(select(Token)).scalars().first()
            return t.athlete_id if t else None

app = FastAPI(title="Strava GPT Backend", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# OAuth router
app.include_router(oauth.router)

ATHLETE_ID_SINGLETON: int | None = None

def resolve_athlete_id() -> int | None:
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

from datetime import datetime, timezone
from fastapi import HTTPException
import httpx
import os

STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")

@app.get("/oauth/callback")
async def oauth_callback(code: str | None = None, error: str | None = None):
    # 1) Validaciones básicas
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing 'code'")

    # 2) Intercambiar el code por tokens en Strava
    token_url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": int(STRAVA_CLIENT_ID),
        "client_secret": STRAVA_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(token_url, data=payload)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Esto evita que el 400 de Strava termine como 500 opaco
            raise HTTPException(status_code=400, detail=f"Strava token error: {e.response.text}") from e

        data = resp.json()

    # 3) Extraer campos necesarios
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_at_unix = data.get("expires_at")
    athlete = data.get("athlete") or {}
    athlete_id = athlete.get("id")
    scope = data.get("scope")  # opcional

    if not all([access_token, refresh_token, expires_at_unix, athlete_id]):
        raise HTTPException(status_code=500, detail=f"Missing fields in Strava response: {data}")

    # 4) Convertir expires_at UNIX -> datetime con TZ
    expires_at = datetime.fromtimestamp(int(expires_at_unix), tz=timezone.utc)

    # 5) Guardar/actualizar token en BD
    # asegúrate de que la firma de storage.upsert_token acepte scope opcional
    upsert_token(
        athlete_id=athlete_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scope=scope,
    )

    return {"detail": "Autorización correcta. Ya puedes usar el GPT."}

# --- Admin: suscripción webhooks ---
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

from datetime import datetime, timezone
import httpx

@app.post("/admin/initial-import")
async def initial_import(days: int = 365, authorization: str = Header(None)):
    """
    Importa actividades históricas desde Strava de los últimos 'days' días y las guarda en DB.
    Solo RUN.
    """
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(401, "No autorizado")

    aid = resolve_athlete_id()
    if not aid:
        raise HTTPException(400, "Falta autorizar OAuth primero")

    # Token fresco
    tok = get_token(aid)
    if not tok:
        raise HTTPException(400, "Sin token para este atleta")
    tok = await ensure_fresh_token(tok, upsert_token)

    after_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    saved = 0
    page = 1
    async with httpx.AsyncClient(timeout=30.0) as c:
        while True:
            r = await c.get(
                "https://www.strava.com/api/v3/athlete/activities",
                headers={"Authorization": f"Bearer {tok.access_token}"},
                params={"after": after_ts, "per_page": 200, "page": page},
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            for act in batch:
                if act.get("type") == "Run":
                    # Si quieres mejores parciales, aquí podrías pedir el detalle completo
                    # det = await c.get(f"https://www.strava.com/api/v3/activities/{act['id']}",
                    #                  headers={"Authorization": f"Bearer {tok.access_token}"})
                    # det.raise_for_status()
                    # save_or_update_activity(det.json(), athlete_id=aid)
                    save_or_update_activity(act, athlete_id=aid)
                    saved += 1
            page += 1

    return {"imported": True, "count": saved, "days": days}


@app.get("/strava/webhook")
async def strava_verify(request: Request):
    qp = request.query_params
    verify_token = qp.get("hub.verify_token") or qp.get("token")
    challenge = qp.get("hub.challenge") or qp.get("challenge")
    if verify_token != "verify":
        raise HTTPException(403, "verify_token incorrecto")
    return {"hub.challenge": challenge}

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
    if act.get("type") == "Run":
        save_or_update_activity(act, athlete_id=ev.owner_id)
    return {"ok": True}

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
        "avg_pace": None,
    } for r in runs]

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

# --- NUEVO: Comparativa ---
@app.get("/stats/compare")
async def stats_compare(start: str, end: str, prev_weeks: int | None = 4):
    """Compara [start,end] con un periodo previo:
    - prev_weeks=N => N*7 días antes de 'start'
    - si prev_weeks es None/0 => mismo tamaño de ventana justo anterior
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
