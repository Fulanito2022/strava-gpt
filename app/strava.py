import httpx
from .config import STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET

STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_AUTH = "https://www.strava.com/oauth"

async def exchange_code_for_token(code: str):
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{STRAVA_AUTH}/token", data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        })
        r.raise_for_status()
        return r.json()

async def refresh_access_token(refresh_token: str):
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{STRAVA_AUTH}/token", data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        r.raise_for_status()
        return r.json()

async def get_authenticated_athlete(access_token: str):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {access_token}"}) as c:
        r = await c.get(f"{STRAVA_API}/athlete")
        r.raise_for_status()
        return r.json()

async def list_activities(access_token: str, after: int | None = None, before: int | None = None, page: int = 1, per_page: int = 100):
    params = {"page": page, "per_page": per_page}
    if after:
        params["after"] = after
    if before:
        params["before"] = before
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {access_token}"}) as c:
        r = await c.get(f"{STRAVA_API}/athlete/activities", params=params)
        r.raise_for_status()
        return r.json()

async def get_activity(access_token: str, activity_id: int):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {access_token}"}) as c:
        r = await c.get(f"{STRAVA_API}/activities/{activity_id}")
        r.raise_for_status()
        return r.json()

async def ensure_fresh_token(token_row, storage_updater):
    from time import time
    now = int(time())
    if token_row.expires_at - 60 < now:
        data = await refresh_access_token(token_row.refresh_token)
        storage_updater(
            athlete_id=token_row.athlete_id,
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=int(data["expires_at"]),
        )
        token_row.access_token = data["access_token"]
        token_row.refresh_token = data["refresh_token"]
        token_row.expires_at = int(data["expires_at"])
    return token_row
