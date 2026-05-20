"""微信公众号网页授权 OAuth 流程（snsapi_userinfo scope）。"""
import os
from typing import Optional
from urllib.parse import urlencode, quote

import httpx

_APP_ID = os.environ.get("WECHAT_APP_ID", "")
_APP_SECRET = os.environ.get("WECHAT_APP_SECRET", "")
_REDIRECT_URI = os.environ.get("WECHAT_REDIRECT_URI", "")

_AUTH_BASE_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"
_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
_USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"


def get_authorize_url(state: str = "STATE") -> str:
    params = {
        "appid": _APP_ID,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "scope": "snsapi_userinfo",
        "state": state,
    }
    return f"{_AUTH_BASE_URL}?{urlencode(params)}#wechat_redirect"


def exchange_code(code: str) -> Optional[dict]:
    """
    用 code 换取 access_token + openid，再拉取用户 userinfo。
    返回 {openid, unionid, nickname, headimgurl}；失败返回 None。
    """
    try:
        with httpx.Client(timeout=10) as client:
            token_resp = client.get(
                _TOKEN_URL,
                params={
                    "appid": _APP_ID,
                    "secret": _APP_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                },
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()
            if "errcode" in token_data:
                import logging
                logging.getLogger("epub_factory").error(
                    f"[WeChat OAuth] token error: {token_data}"
                )
                return None

            access_token = token_data.get("access_token")
            openid = token_data.get("openid")
            if not access_token or not openid:
                return None

            userinfo_resp = client.get(
                _USERINFO_URL,
                params={
                    "access_token": access_token,
                    "openid": openid,
                    "lang": "zh_CN",
                },
            )
            userinfo_resp.raise_for_status()
            info = userinfo_resp.json()
            if "errcode" in info:
                # userinfo 拉取失败，仅返回 openid
                return {
                    "openid": openid,
                    "unionid": token_data.get("unionid"),
                    "nickname": None,
                    "headimgurl": None,
                }
            return {
                "openid": info.get("openid"),
                "unionid": info.get("unionid"),
                "nickname": info.get("nickname"),
                "headimgurl": info.get("headimgurl"),
            }
    except Exception as e:
        import logging
        logging.getLogger("epub_factory").error(f"[WeChat OAuth] exchange_code failed: {e}", exc_info=True)
        return None
