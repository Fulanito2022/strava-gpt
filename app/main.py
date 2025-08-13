import os
import math
from datetime import datetime, timedelta, date, timezone
from typing import Optional, Dict, Any, List

import httpx
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.responses import RedirectResponse, JSONResponse

from .storage import (
    upsert_token,
    list_tokens,
    query_runs,
    save_or_update_activity,
    Token,
)

APP_TITLE = "Strava GPT Backend"
APP_VERSION = "1.0.0"

app = FastAPI(title=APP_TITLE, version=APP_VERSION)

# === ENV VARS ===
BASE_URL = os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8000"
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")  # para proteger /admin/*


# === HELPERS ===
def require_admin(authorization: Optional[str]) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(500, detail="Falta ADMIN_TOKEN en variables de entorno")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail="Authorization Bearer requerido")
    token = authorization.split(" ", 1)[1].strip()
    if token != ADMIN_TOKEN:
        raise HTTPException(403, detail="ADMIN_TOKEN inválido")


def parse_ymd(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(400, detail=f"Fecha inválida: {s}. Usa YYYY-MM-DD")


def format_pace(moving_time_s: int, distance_m: int) -> Optional[str]:
    if not distance_m or distance_m <= 0:
        return None
    sec_per_km = moving_time_s / (distance_m / 1000.0)
    if not math.isfinite(sec_per_km) or sec_per_km <= 0:
        return None
    m = int(sec_per_km // 60)
    s = int(round(sec_per_km % 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d} min/km"


async def ensure_fresh_access_token(tok: Token) -> Token:
    """Refresca el access_token si expiró. tok.expires_at es epoch (int)."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if tok.expires_at and tok.expires_at > (now_ts + 60):
        return tok

    if not STRAVA_CLIENT_ID or not STRAVA_CLIENT_SECRET:
        raise HTTPException(500, detail="Faltan STRAVA_CLIENT_ID/STRAVA_CLIENT_SECRET")

    data = {
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": tok.refresh_token,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post("https://www.strava.com/api/v3/oauth/token", data=data)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, detail=f"Strava refresh failed: {resp.text}")

    payload = resp.json()
    upsert_token(
        athlete_id=tok.athlete_id,
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        expires_at=payload["expires_at"],  # epoch int
        scope=payload.get("scope"),
    )
    tok.access_token = payload["access_token"]
    tok.refresh_token = payload["refresh_token"]
    tok.expires_at = int(payload["expires_at"])
    tok.scope = payload.get("scope")
    return tok


async def fetch_activities_since(tok: Token, after_epoch: int) -> int:
    """Descarga y guarda actividades del atleta desde 'after_epoch' (segundos)."""
    total = 0
    page = 1
    per_page = 200

    tok = await ensure_fresh_access_token(tok)

    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            params = {"after": after_epoch, "page": page, "per_page": per_page}
            headers = {"Authorization": f"Bearer {tok.access_token}"}
            resp = await client.get(
                "https://www.strava.com/api/v3/athlete/activities",
                params=params,
                headers=headers,
            )
            if resp.status_code >= 400:
                raise HTTPException(resp.status_code, detail=f"Strava activities error: {resp.text}")

            items = resp.json()
            if not items:
                break

            for act in items:
                save_or_update_activity(act)
                total += 1

            if len(items) < per_page:
                break
            page += 1

    return total


# === ENDPOINTS ===

@app.get("/health", summary="Health")
def health():
    return {"ok": True, "version": APP_VERSION}


# -------- OAuth --------

@app.get("/oauth/start", tags=["oauth"], summary="Oauth Start")
def oauth_start():
    if not STRAVA_CLIENT_ID:
        raise HTTPException(500, "Falta STRAVA_CLIENT_ID en variables de entorno")

    redirect_uri = f"{BASE_URL}/oauth/callback"

    params = {
        "client_id": STRAVA_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "approval_prompt": "auto",
        "scope": "read,activity:read_all,profile:read_all",
    }
    url = "https://www.strava.com/oauth/authorize?" + httpx.QueryParams(params).to_str()
    return RedirectResponse(url)


@app.get("/oauth/callback", summary="Oauth Callback")
async def oauth_callback(
    code: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
):
    if error:
        raise HTTPException(400, detail=error)
    if not code:
        raise HTTPException(400, detail="Falta 'code' de Strava")
    if not STRAVA_CLIENT_ID or not STRAVA_CLIENT_SECRET:
        raise HTTPException(500, detail="Faltan STRAVA_CLIENT_ID/STRAVA_CLIENT_SECRET")

    data = {
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post("https://www.strava.com/api/v3/oauth/token", data=data)

    if resp.status_code >= 400:
        raise HTTPException(502, detail=f"Strava token exchange failed: {resp.text}")

    payload = resp.json()
    athlete_id = payload.get("athlete", {}).get("id")
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_at = payload.get("expires_at")  # epoch (int)
    scope = payload.get("scope", "")

    if not (athlete_id and access_token and refresh_token and expires_at):
        raise HTTPException(502, detail=f"Respuesta inválida de Strava: {payload}")

    upsert_token(
        athlete_id=athlete_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scope=scope,
    )

    return JSONResponse({"detail": "Autorización correcta. Ya puedes usar el GPT.", "athlete_id": athlete_id})


# -------- Admin --------

@app.post("/admin/initial-import", summary="Initial Import")
async def initial_import(
    days: int = Query(default=365, ge=1, le=2000, description="Días atrás a importar"),
    authorization: Optional[str] = Header(default=None),
):
    require_admin(authorization)

    toks = list_tokens()
    if not toks:
        raise HTTPException(400, detail="Falta autorizar OAuth primero")

    after_epoch = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    results: Dict[int, int] = {}
    for tok in toks:
        count = await fetch_activities_since(tok, after_epoch)
        results[tok.athlete_id] = count

    return {"imported": results, "since": after_epoch}


@app.post("/admin/subscribe", summary="Admin Subscribe")
def admin_subscribe(authorization: Optional[str] = Header(default=None)):
    require_admin(authorization)
    return {"detail": "OK (placeholder). Webhook no necesario para este flujo."}


# -------- Webhook (opcional, placeholder) --------

@app.get("/strava/webhook", summary="Strava Verify")
def strava_verify(
    hub_mode: Optional[str] = Query(alias="hub.mode", default=None),
    hub_challenge: Optional[str] = Query(alias="hub.challenge", default=None),
    hub_verify: Optional[str] = Query(alias="hub.verify_token", default=None),
):
    if hub_challenge:
        return {"hub.challenge": hub_challenge}
    return {"detail": "ok"}


@app.post("/strava/webhook", summary="Strava Event")
def strava_event(event: Dict[str, Any]):
    return {"received": True}


# -------- Datos de usuario --------

@app.get(
    "/activities",
    summary="Activities",
    description="Lista actividades de running entre start y end (ISO YYYY-MM-DD).",
)
def activities(
    start: str = Query(...),
    end: str = Query(...),
):
    d1 = parse_ymd(start)
    d2 = parse_ymd(end)
    if d2 < d1:
        raise HTTPException(400, detail="end debe ser >= start")

    runs = query_runs(d1, d2)
    out: List[Dict[str, Any]] = []
    for r in runs:
        out.append(
            {
                "id": r.id,
                "date": r.start_date.date().isoformat(),
                "name": r.name,
                "distance_km": round(r.distance_m / 1000.0, 2),
                "moving_time_min": round(r.moving_time_s / 60.0, 1),
                "avg_hr": round(r.average_heartrate, 0) if r.average_heartrate else None,
                "elev_gain_m": r.total_elevation_gain_m,
                "avg_pace": format_pace(r.moving_time_s, r.distance_m),
            }
        )
    return out


@app.get("/stats/summary", summary="Stats Summary")
def stats_summary(
    start: str = Query(...),
    end: str = Query(...),
):
    d1 = parse_ymd(start)
    d2 = parse_ymd(end)
    if d2 < d1:
        raise HTTPException(400, detail="end debe ser >= start")

    runs = query_runs(d1, d2)
    sessions = len(runs)
    total_dist_m = sum(r.distance_m for r in runs)
    total_time_s = sum(r.moving_time_s for r in runs)
    total_elev = sum(r.total_elevation_gain_m for r in runs)
    avg_hr_vals = [r.average_heartrate for r in runs if r.average_heartrate]

    avg_pace = format_pace(total_time_s, total_dist_m) if total_dist_m > 0 else None
    avg_hr = round(sum(avg_hr_vals) / len(avg_hr_vals)) if avg_hr_vals else None

    def best_time_for_dist(min_dist_m: int) -> Optional[str]:
        eligible = [r for r in runs if r.distance_m >= min_dist_m and r.moving_time_s > 0]
        if not eligible:
            return None
        best = min(eligible, key=lambda r: r.moving_time_s / (r.distance_m / min_dist_m))
        ratio = min_dist_m / best.distance_m
        secs = int(best.moving_time_s * ratio)
        m, s = secs // 60, secs % 60
        return f"{m}:{s:02d}"

    best = {"5k": best_time_for_dist(5000), "10k": best_time_for_dist(10000), "21k": best_time_for_dist(21097)}

    return {
        "sessions": sessions,
        "distance_km": round(total_dist_m / 1000.0, 2),
        "moving_time_h": round(total_time_s / 3600.0, 2),
        "elev_gain_m": total_elev,
        "avg_pace": avg_pace,
        "avg_hr": avg_hr,
        "best_efforts": best,
    }


@app.get(
    "/stats/compare",
    summary="Stats Compare",
    description=(
        "Compara [start,end] con un periodo previo:\n"
        "- prev_weeks=N => N*7 días antes de 'start'\n"
        "- si prev_weeks es None/0 => mismo tamaño de ventana justo anterior"
    ),
)
def stats_compare(
    start: str = Query(...),
    end: str = Query(...),
    prev_weeks: Optional[int] = Query(default=4),
):
    d1 = parse_ymd(start)
    d2 = parse_ymd(end)
    if d2 < d1:
        raise HTTPException(400, detail="end debe ser >= start")

    def summarize(d1: date, d2: date):
        runs = query_runs(d1, d2)
        sessions = len(runs)
        total_dist_m = sum(r.distance_m for r in runs)
        total_time_s = sum(r.moving_time_s for r in runs)
        total_elev = sum(r.total_elevation_gain_m for r in runs)
        avg_hr_vals = [r.average_heartrate for r in runs if r.average_heartrate]
        return {
            "sessions": sessions,
            "distance_km": round(total_dist_m / 1000.0, 2),
            "moving_time_h": round(total_time_s / 3600.0, 2),
            "elev_gain_m": total_elev,
            "avg_pace": format_pace(total_time_s, total_dist_m) if total_dist_m > 0 else None,
            "avg_hr": round(sum(avg_hr_vals) / len(avg_hr_vals)) if avg_hr_vals else None,
        }

    if prev_weeks and prev_weeks > 0:
        days = prev_weeks * 7
        prev_end = d1 - timedelta(days=1)
        prev_start = d1 - timedelta(days=days)
    else:
        window = (d2 - d1).days + 1
        prev_end = d1 - timedelta(days=1)
        prev_start = prev_end - timedelta(days=window - 1)

    current = summarize(d1, d2)
    previous = summarize(prev_start, prev_end)

    return {
        "current": current,
        "previous": previous,
        "previous_range": {"start": prev_start.isoformat(), "end": prev_end.isoformat()},
    }
