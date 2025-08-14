import os
from datetime import date, datetime, timezone, timedelta

def _as_utc(dt: datetime) -> datetime:
    """Devuelve dt con tz=UTC (si viene naive le pone UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _epoch_s(dt: datetime) -> int:
    """Epoch seconds de un datetime (lo fuerza a UTC)."""
    return int(_as_utc(dt).timestamp())

from typing import Optional, Dict, Any, List
from urllib.parse import urlencode

import sqlalchemy as sa
import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
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


# ---------- util ----------
def _base_url(request: Request) -> str:
    if PUBLIC_URL:
        return PUBLIC_URL.rstrip("/")
    return f"{request.url.scheme}://{request.headers.get('host')}".rstrip("/")


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


def _epoch_n_days_ago(days: int) -> int:
    dt = datetime.now(tz=timezone.utc) - timedelta(days=days)
    return int(dt.timestamp())


# ---------- OAuth ----------
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
    upsert_token(
        athlete_id=athlete_id,
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=int(data["expires_at"]),
        scope=",".join(data.get("scope", [])) if isinstance(data.get("scope"), list) else (data.get("scope") or ""),
    )

    return JSONResponse({"detail": "Autorización correcta. Ya puedes usar el GPT.", "athlete_id": athlete_id})


# ---------- Tokens ----------
def _do_refresh(athlete_id: int) -> Dict[str, Any]:
    tok = get_token(athlete_id)
    if not tok:
        raise HTTPException(status_code=404, detail=f"No hay token para athlete_id={athlete_id}")

    token_url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": tok.refresh_token,
    }
    with httpx.Client(timeout=30) as client:
        res = client.post(token_url, data=payload)
        # Si el refresh falla, Strava devuelve 400 invalid_grant
        res.raise_for_status()
        data = res.json()

    upsert_token(
        athlete_id=athlete_id,
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=int(data["expires_at"]),
        scope=",".join(data.get("scope", [])) if isinstance(data.get("scope"), list) else (data.get("scope") or getattr(tok, "scope", "")),
    )
    return data


def _ensure_valid_access_token(athlete_id: int) -> str:
    tok = get_token(athlete_id)
    if not tok:
        raise HTTPException(status_code=404, detail=f"No hay token para athlete_id={athlete_id}")

    now_s = int(datetime.now(timezone.utc).timestamp())
    expires_s = _epoch_s(tok.expires_at)  # <-- robusto a naive
    # refresca si faltan <= 60s
    if expires_s <= now_s + 60:
        _do_refresh(athlete_id)
        tok = get_token(athlete_id)
    return tok.access_token


@app.get("/admin/health")
def admin_health(db=Depends(get_db)):
    try:
        # simple ping a la DB
        list(db.execute(sa.text("SELECT 1")))
        return {"ok": True, "db": True}
    except Exception:
        return {"ok": False, "db": False}


@app.get("/admin/token-info")
def token_info(request: Request, athlete_id: Optional[int] = None):
    _auth_admin_or_403(request)
    if not athlete_id:
        athlete_id = get_any_athlete_id()
        if not athlete_id:
            raise HTTPException(status_code=404, detail="No hay ningún atleta autorizado todavía")

    tok = get_token(athlete_id)
    if not tok:
        raise HTTPException(status_code=404, detail=f"No hay token para athlete_id={athlete_id}")

    now_utc = datetime.now(timezone.utc)
    exp_utc = _as_utc(tok.expires_at)

    return {
        "athlete_id": athlete_id,
        "expires_at_epoch": int(exp_utc.timestamp()),
        "expires_at_iso": exp_utc.isoformat(),
        "seconds_left": int((exp_utc - now_utc).total_seconds()),
        "is_expired": exp_utc <= now_utc,
        "access_token_tail": tok.access_token[-6:],
        "scope": tok.scope or "",
    }



@app.post("/admin/refresh-token")
def refresh_token(request: Request, athlete_id: Optional[int] = None):
    _auth_admin_or_403(request)
    if not athlete_id:
        athlete_id = get_any_athlete_id()
        if not athlete_id:
            raise HTTPException(status_code=404, detail="No hay ningún atleta autorizado todavía")

    data = _do_refresh(athlete_id)
    return {
        "athlete_id": athlete_id,
        "refreshed": True,
        "expires_at": int(data["expires_at"]),
        "access_token_tail": data["access_token"][-6:],
    }


# ---------- Importar actividades ----------
def _fetch_activities_since(access_token: str, after_epoch: int) -> int:
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
def initial_import(request: Request, days: int = 365, athlete_id: Optional[int] = None):
    _auth_admin_or_403(request)
    if not athlete_id:
        candidate = get_any_athlete_id()
        if not candidate:
            raise HTTPException(status_code=404, detail="No hay ningún atleta autorizado todavía")
        athlete_id = candidate

    # asegura token válido
    access_token = _ensure_valid_access_token(athlete_id)
    after_epoch = _epoch_n_days_ago(days)
    count = _fetch_activities_since(access_token, after_epoch)
    return {"imported": count, "athlete_id": athlete_id, "since_epoch": after_epoch}


# ---------- Consultas de datos ----------
@app.get("/activities")
def list_activities(start: str, end: str, db=Depends(get_db)):
    start_d = date.fromisoformat(start)                 # p.ej. 2025-05-01
    end_excl = date.fromisoformat(end) + timedelta(days=1)

    q = sa.text("""
        SELECT id, athlete_id, type, name, start_date, distance_m, moving_time_s, elapsed_time_s,
               total_elevation_gain_m, average_heartrate, max_heartrate
        FROM activities
        WHERE start_date >= :start AND start_date < :end
        ORDER BY start_date DESC
    """)

    rows = db.execute(q, {"start": start_d, "end": end_excl}).mappings().all()
    return rows


@app.get("/stats/summary")
def stats_summary(start: str, end: str):
    from sqlalchemy import text
    db_gen = get_db()
    db = next(db_gen)  # obtener la sesión del generator
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
