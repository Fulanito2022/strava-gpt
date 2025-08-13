import os
import math
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse

from .storage import (
    get_db, get_token, upsert_token, save_or_update_activity,
    get_any_athlete_id
)

app = FastAPI()

STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")
PUBLIC_URL = os.environ.get("PUBLIC_URL")  # p.ej. https://strava-gpt-xxxx.onrender.com

if not (STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET and ADMIN_TOKEN):
    raise RuntimeError("Faltan STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET o ADMIN_TOKEN en variables de entorno")

def _base_url(request: Request) -> str:
    if PUBLIC_URL:
        return PUBLIC_URL.rstrip("/")
    # fallback por si no configuraste PUBLIC_URL (Render reescribe host)
    return f"{request.url.scheme}://{request.headers.get('host')}".rstrip("/")


@app.get("/oauth/start")
def oauth_start(request: Request):
    redirect_uri = _base_url(request) + "/oauth/callback"
    params = {
        "client_id": STRAVA_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "read,activity:read_all,profile:read_all",
    }
    # httpx.QueryParams(...): usa str(...) en vez de .to_str()
    url = "https://www.strava.com/oauth/authorize?" + urlencode(params, doseq=True)
    return RedirectResponse(url, status_code=307)


@app.get("/oauth/callback")
def oauth_callback(request: Request, code: Optional[str] = None, error: Optional[str] = None):
    if error:
        raise HTTPException(status_code=400, detail=f"Strava devolvió error: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Falta 'code' en el callback")

    redirect_uri = _base_url(request) + "/oauth/callback"
    token_url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }

    with httpx.Client(timeout=30) as client:
        res = client.post(token_url, data=payload)
        res.raise_for_status()
        data = res.json()

    athlete_id = int(data["athlete"]["id"])
    access_token = data["access_token"]
    refresh_token = data["refresh_token"]
    # Strava da 'expires_at' en epoch (segundos)
    expires_at = int(data["expires_at"])
    scope = ",".join(data.get("scope", [])) if isinstance(data.get("scope"), list) else (data.get("scope") or "")

    upsert_token(
        athlete_id=athlete_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,  # lo convertimos a datetime en storage.upsert_token
        scope=scope,
    )

    return JSONResponse({"detail": "Autorización correcta. Ya puedes usar el GPT.", "athlete_id": athlete_id})


def _auth_admin_or_403(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="Falta Authorization Bearer")
    token = auth.split(" ", 1)[1].strip()
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")


def _get_strava_client(access_token: str) -> httpx.Client:
    headers = {"Authorization": f"Bearer {access_token}"}
    return httpx.Client(base_url="https://www.strava.com/api/v3", headers=headers, timeout=30)


def _get_access_token_for(athlete_id: int) -> str:
    tok = get_token(athlete_id)
    if not tok:
        raise HTTPException(status_code=404, detail=f"No hay token para athlete_id={athlete_id}")
    return tok.access_token


def _epoch_n_days_ago(days: int) -> int:
    dt = datetime.now(tz=timezone.utc) - timedelta(days=days)
    return int(dt.timestamp())


def _fetch_activities_since(access_token: str, after_epoch: int) -> int:
    """
    Descarga actividades desde 'after_epoch' y las guarda.
    Devuelve cuántas se guardaron/actualizaron.
    """
    saved = 0
    with _get_strava_client(access_token) as client:
        page = 1
        per_page = 200
        while True:
            resp = client.get("/athlete/activities", params={"after": after_epoch, "page": page, "per_page": per_page})
            resp.raise_for_status()
            items: List[Dict[str, Any]] = resp.json()
            if not items:
                break
            for act in items:
                save_or_update_activity(act)
                saved += 1
            if len(items) < per_page:
                break
            page += 1
    return saved


@app.post("/admin/initial-import")
async def initial_import(request: Request, days: int = 365, athlete_id: Optional[int] = None):
    _auth_admin_or_403(request)

    # Elige athlete_id si no viene
    if not athlete_id:
        candidate = get_any_athlete_id()
        if not candidate:
            raise HTTPException(status_code=404, detail="No hay ningún atleta autorizado todavía")
        athlete_id = candidate

    access_token = _get_access_token_for(athlete_id)
    after_epoch = _epoch_n_days_ago(days)
    count = _fetch_activities_since(access_token, after_epoch)
    return {"imported": count, "athlete_id": athlete_id, "since_epoch": after_epoch}


@app.get("/activities")
def list_activities(start: str, end: str):
    """
    start/end: YYYY-MM-DD (UTC)
    """
    from sqlalchemy import text
    db = get_db()
    try:
        q = text("""
            SELECT id, athlete_id, type, name, start_date, distance_m, moving_time_s, elapsed_time_s,
                   total_elevation_gain_m, average_heartrate, max_heartrate
            FROM activities
            WHERE start_date >= :start::timestamptz AND start_date < (:end::date + INTERVAL '1 day')
            ORDER BY start_date DESC
        """)
        rows = db.execute(q, {"start": start, "end": end}).mappings().all()
        return {"count": len(rows), "activities": list(rows)}
    finally:
        db.close()


@app.get("/stats/summary")
def stats_summary(start: str, end: str):
    from sqlalchemy import text
    db = get_db()
    try:
        q = text("""
            SELECT
              COUNT(*) AS n,
              COALESCE(SUM(distance_m),0) AS dist_m,
              COALESCE(SUM(moving_time_s),0) AS time_s,
              COALESCE(SUM(total_elevation_gain_m),0) AS elev_m
            FROM activities
            WHERE start_date >= :start::timestamptz AND start_date < (:end::date + INTERVAL '1 day')
        """)
        row = db.execute(q, {"start": start, "end": end}).mappings().first()
        return row
    finally:
        db.close()
