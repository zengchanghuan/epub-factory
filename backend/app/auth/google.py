"""Google OAuth 2.0 授权码流程。"""
import os
from typing import Optional
from urllib.parse import urlencode

import httpx

_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def get_authorize_url(state: Optional[str] = None) -> str:
    params = {
        "client_id": _CLIENT_ID,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
    }
    if state:
        params["state"] = state
    return f"{_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> Optional[dict]:
    """用授权码换取 userinfo，返回 {google_id, email, name, picture}；失败返回 None。"""
    try:
        with httpx.Client(timeout=10) as client:
            token_resp = client.post(
                _TOKEN_URL,
                data={
                    "code": code,
                    "client_id": _CLIENT_ID,
                    "client_secret": _CLIENT_SECRET,
                    "redirect_uri": _REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
            token_resp.raise_for_status()
            access_token = token_resp.json().get("access_token")
            if not access_token:
                return None

            userinfo_resp = client.get(
                _USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            userinfo_resp.raise_for_status()
            data = userinfo_resp.json()
            return {
                "google_id": data.get("sub"),
                "email": data.get("email"),
                "name": data.get("name"),
                "picture": data.get("picture"),
            }
    except Exception as e:
        import logging
        logging.getLogger("epub_factory").error(f"[Google OAuth] exchange_code failed: {e}", exc_info=True)
        return None
