import jwt
from fastapi import Request
from jwt import PyJWKClient

from .config import Settings
from .domain import Problem

_keys = PyJWKClient("https://www.gstatic.com/iap/verify/public_key-jwk", cache_keys=True)


def authenticate(request: Request, settings: Settings):
    if settings.mode == "demo":
        host = request.headers.get("host", "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "testserver"):
            raise Problem("The demo is available on localhost only.", 403, "local_only")
        return "local-demo"
    assertion = request.headers.get("x-goog-iap-jwt-assertion", "")
    if not assertion:
        raise Problem("Sign in through the protected application URL.", 401, "unauthorized")
    try:
        key = _keys.get_signing_key_from_jwt(assertion)
        claims = jwt.decode(
            assertion,
            key.key,
            algorithms=["ES256"],
            audience=settings.iap_audience,
            issuer="https://cloud.google.com/iap",
            options={"require": ["exp", "iat", "sub", "email"]},
            leeway=30,
        )
        return claims["email"]
    except jwt.PyJWTError:
        raise Problem("Your sign-in could not be verified. Sign in again.", 401, "unauthorized") from None


def protect_mutation(request: Request, settings: Settings):
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.headers.get("x-salestrackr-request") != "1":
        raise Problem("Missing request protection header.", 403, "csrf")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise Problem("Cross-site writes are not allowed.", 403, "csrf")
    origin = request.headers.get("origin")
    allowed = (
        {settings.public_origin}
        if settings.mode == "production"
        else {
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        }
    )
    if settings.mode == "demo" and settings.public_origin:
        allowed.add(settings.public_origin)
    if origin and origin not in allowed:
        raise Problem("This page is not an allowed application origin.", 403, "csrf")
    archive_upload = (
        request.url.path in ("/api/backups/inspect", "/api/backups/restore")
        and request.headers.get("content-type", "").split(";")[0] == "application/zip"
    )
    if not archive_upload and "application/json" not in request.headers.get("content-type", ""):
        raise Problem("Send application/json.", 415, "content_type")
