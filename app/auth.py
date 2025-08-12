from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode
from .config import STRAVA_CLIENT_ID, STRAVA_SCOPES, BASE_URL

router = APIRouter(prefix="/oauth", tags=["oauth"])

@router.get("/start")
def oauth_start():
    if not STRAVA_CLIENT_ID:
        raise HTTPException(500, "Falta STRAVA_CLIENT_ID")
    params = {
        "client_id": STRAVA_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": f"{BASE_URL}/oauth/callback",
        "approval_prompt": "auto",
        "scope": ",".join(STRAVA_SCOPES),
    }
    url = f"https://www.strava.com/oauth/authorize?{urlencode(params)}"
    return RedirectResponse(url)
