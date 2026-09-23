"""Who can open the app: a family login, configured without touching code.

Set APP_USERS on the server as comma-separated name:password pairs:

    APP_USERS="Mariano:some-long-password,Maura:another-long-password"

Add a person or change a password by editing that one variable. Names are
case-insensitive at login and shown as written (the Step 3 assistant uses
them to know who's asking).

This uses HTTP Basic auth: the browser shows its own login box once and
remembers it. It's only safe over HTTPS, which Render provides. If
APP_USERS is unset (local development), the app is open.
"""

import base64
import binascii
import os
import secrets

from fastapi import Request
from fastapi.responses import Response

REALM = "Family Budget"


def load_users() -> dict[str, tuple[str, str]]:
    """{lowercase name: (display name, password)} from APP_USERS."""
    users = {}
    for entry in os.environ.get("APP_USERS", "").split(","):
        name, sep, password = entry.strip().partition(":")
        if sep and name.strip() and password:
            users[name.strip().lower()] = (name.strip(), password)
    return users


def check(header: str | None, users: dict[str, tuple[str, str]]) -> str | None:
    """Return the display name if the Authorization header is valid, else None."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    name, _, password = decoded.partition(":")
    display, expected = users.get(name.strip().lower(), (None, ""))
    # compare_digest takes the same time whether or not the guess is close,
    # so response timing can't leak the password letter by letter.
    ok = secrets.compare_digest(password.encode(), expected.encode())
    return display if display and ok else None


async def require_login(request: Request, call_next):
    """FastAPI middleware: runs before every request, including /docs."""
    users = load_users()
    if not users:
        request.state.user = None
        return await call_next(request)
    user = check(request.headers.get("Authorization"), users)
    if user is None:
        return Response(
            "Login required",
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'},
        )
    request.state.user = user
    return await call_next(request)
