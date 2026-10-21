"""Optional HTTP Basic authentication for the whole application.

Off unless both ``DASHBOARD_USER`` and ``DASHBOARD_PASSWORD`` are set, so
local development and the simulator stay frictionless.

This is a stopgap for a single-operator dashboard, not a user management
system: one shared credential, no sessions, no per-user audit trail, and
no way to revoke one person's access. It exists so a test deployment does
not sit open to the internet. Before this holds real client data, replace
it with proper application authentication.
"""

from __future__ import annotations

import base64
import binascii
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from .config import settings


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request lacking valid Basic credentials."""

    async def dispatch(self, request, call_next):
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
                user, _, password = decoded.partition(":")
            except (binascii.Error, UnicodeDecodeError):
                user = password = ""

            # compare_digest avoids leaking length or prefix through timing.
            if secrets.compare_digest(user, settings.dashboard_user) and secrets.compare_digest(
                password, settings.dashboard_password
            ):
                return await call_next(request)

        return PlainTextResponse(
            "Authentication required.\n",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="GuardPoint", charset="UTF-8"'},
        )
