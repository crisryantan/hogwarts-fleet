"""Google for chat reviews: the request Sheet the Chat app's Apps Script fills, replies posted as the app, and the
worker service account's access tokens.

Nothing here listens on the network. The chat-review job (fleet/chat_review.py) calls out, over HTTPS, to three Google
endpoints, all as the worker service account (CHAT_REVIEW_SERVICE_ACCOUNT):
- the OAuth token endpoint, with a JWT bearer grant this module signs (RS256) for exactly one of two scopes,
  spreadsheets or chat.bot, and gets back a short-lived access token;
- Sheets REST, values.get and values.batchUpdate on the one request Sheet (CHAT_REVIEW_SHEET_ID), shared as editor
  with this service account and with the writer service account chat-app/Code.gs appends rows as, and no one else;
- Chat REST, spaces.messages.create in the request's thread, as the app, with the chat.bot scope.
The worker service account's one JSON key is never a file here: its private key sits in your login Keychain as a generic
password (service CHAT_REVIEW_KEYCHAIN_SERVICE, account the service account's email), stored there by
scripts/chat-review-store-key.py as the base64 of its PKCS#8 body. run_desk.keychain_output reads it with
/usr/bin/security, and run_desk.openssl_sign_output signs with /usr/bin/openssl, which gets the key on its stdin. The
key and every token live only in this process's memory: never in argv, a log, a file or an error, and no error text a
server or a tool printed is kept. Requests go straight to the Google hosts, with no proxy, certificates checked and no
redirect followed, so a bearer token never goes to any other host.

Each Chat reply carries a request id the caller derives from its own record, so sending one again (after a kill, or a
failed answer) gives back the message Chat already made instead of a second one.

It starts no process itself: run_desk runs security and openssl for it.
"""
from __future__ import annotations

import base64
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from fleet import common, config, run_desk
from fleet.safefs import FleetError

TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"
CHAT_ROOT = "https://chat.googleapis.com/v1"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
SCOPES = (SHEETS_SCOPE, CHAT_BOT_SCOPE)
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
# The signed grant is good for this long; the token Google gives back is kept no longer than that, in memory only.
GRANT_SECONDS = 600
TOKEN_MARGIN_SECONDS = 60
REPLY_OPTION = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{20,4096}")
# What the Keychain item holds: the base64 of the private key's PKCS#8 body, one line.
KEY_BODY = re.compile(rb"[A-Za-z0-9+/]{400,8000}={0,2}")
HTTP_TIMEOUT_SECONDS = 30
RESPONSE_MAX_BYTES = 8 * 1024 * 1024
# What Chat takes as a message's text is 4096 characters.
MESSAGE_MAX_CHARS = 4000
SERVICE_ACCOUNT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com")
KEYCHAIN_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
SHEET_ID = re.compile(r"[A-Za-z0-9_-]{20,100}")
SPACE = re.compile(r"spaces/[A-Za-z0-9_-]{1,100}")
THREAD = re.compile(r"spaces/[A-Za-z0-9_-]{1,100}/threads/[A-Za-z0-9_.-]{1,200}")
REQUEST_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
# The request Sheet: one tab, one row per request, written by chat-app/Code.gs in these columns, A to I.
SHEET_TAB = "requests"
COLUMNS = ("message", "thread", "space", "sender_email", "sender_name", "pr_url", "created", "status", "run")
STATUS_COLUMN, RUN_COLUMN = "H", "I"
CELL_MAX = 400
# The statuses this side writes; only Code.gs writes "queued".
WRITTEN_STATUSES = ("taken", "done", "failed")
RUN_MARKER = re.compile(r"[0-9a-f]{16}")

_TOKENS: dict = {}


class GoogleError(FleetError):
    """A Google call that failed. Its text never holds a key, a token or anything a server or a tool printed."""


# Tokens


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _identity() -> tuple:
    service, account = config.CHAT_REVIEW_KEYCHAIN_SERVICE, config.CHAT_REVIEW_SERVICE_ACCOUNT
    if not isinstance(account, str) or SERVICE_ACCOUNT.fullmatch(account) is None:
        raise GoogleError("CHAT_REVIEW_SERVICE_ACCOUNT is not a service account email")
    if not isinstance(service, str) or KEYCHAIN_SERVICE.fullmatch(service) is None:
        raise GoogleError("CHAT_REVIEW_KEYCHAIN_SERVICE is not a Keychain service name")
    return service, account


def _key_pem(service: str, account: str) -> bytes:
    """The service account's private key as PEM, read from your login Keychain."""
    code, out = run_desk.keychain_output(service, account)
    body = out.strip()
    if code != 0 or KEY_BODY.fullmatch(body) is None:
        # Nothing security printed is kept.
        raise GoogleError(f"no usable key for {account} is in your login Keychain under service {service} (exit"
                          f" {code}): store one with scripts/chat-review-store-key.py")
    lines = [body[index:index + 64] for index in range(0, len(body), 64)]
    return b"-----BEGIN PRIVATE KEY-----\n" + b"\n".join(lines) + b"\n-----END PRIVATE KEY-----\n"


def grant(scope: str, now: int) -> str:
    """A JWT bearer grant for scope, signed RS256 with the service account's key by openssl."""
    if scope not in SCOPES:
        raise GoogleError("a chat review token is only for the spreadsheets or the chat.bot scope")
    service, account = _identity()
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iss": account, "scope": scope, "aud": TOKEN_URL, "iat": now, "exp": now + GRANT_SECONDS}
    signing_input = ".".join(_b64url(json.dumps(part, separators=(",", ":"), sort_keys=True).encode("ascii"))
                             for part in (header, claims))
    code, signature = run_desk.openssl_sign_output(_key_pem(service, account), signing_input.encode("ascii"))
    if code != 0 or not 128 <= len(signature) <= 1024:
        raise GoogleError(f"openssl could not sign the token grant (exit {code})")
    return f"{signing_input}.{_b64url(signature)}"


def access_token(scope: str) -> str:
    """A short-lived access token for scope, minted from a signed grant, kept in this process's memory only."""
    now = int(time.time())
    held = _TOKENS.get(scope)
    if held is not None and held[1] - TOKEN_MARGIN_SECONDS > now:
        return held[0]
    answer = send(TOKEN_URL, None, {"grant_type": JWT_BEARER, "assertion": grant(scope, now)})
    token, kind, lasts = answer.get("access_token"), answer.get("token_type"), answer.get("expires_in")
    if not isinstance(token, str) or TOKEN.fullmatch(token) is None or not isinstance(kind, str) \
            or kind.lower() != "bearer" or type(lasts) is not int or lasts <= TOKEN_MARGIN_SECONDS:
        raise GoogleError("Google's token answer is not an access token")
    _TOKENS[scope] = (token, now + min(lasts, GRANT_SECONDS))
    return token


def forget_tokens() -> None:
    _TOKENS.clear()


# HTTPS


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every redirect is refused (urllib then raises it as an HTTPError), since one would carry the bearer token on."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib's own signature
        return None


def _opener() -> urllib.request.OpenerDirector:
    """No proxy from anywhere, no redirect followed, and certificates checked against the system's roots."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                       urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def send(url: str, token: Optional[str], payload: Optional[dict]) -> dict:
    """One request and the JSON object answered. The token endpoint gets payload as a form and no token; Sheets and
    Chat get payload as JSON (a GET when it is None) with the token as its bearer. Only those Google hosts are
    reached. Tests replace this."""
    headers = {}
    if url == TOKEN_URL:
        if token is not None or not isinstance(payload, dict):
            raise GoogleError("the token endpoint takes a grant and no token")
        body = urllib.parse.urlencode(payload).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif url.startswith(SHEETS_ROOT + "/") or url.startswith(CHAT_ROOT + "/"):
        if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
            raise GoogleError("a Sheets or Chat call needs an access token")
        headers["Authorization"] = f"Bearer {token}"
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=True).encode("ascii")
            headers["Content-Type"] = "application/json"
    else:
        raise GoogleError("the chat review calls only Google's token endpoint, Sheets and Chat")
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with _opener().open(request, timeout=HTTP_TIMEOUT_SECONDS) as answer:
            raw = answer.read(RESPONSE_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise GoogleError(f"Google answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise GoogleError("Google could not be reached") from None
    if len(raw) > RESPONSE_MAX_BYTES:
        raise GoogleError("Google answered more than the fleet reads")
    try:
        data = common.strict_json(raw) if raw.strip() else {}
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise GoogleError("Google's answer is not JSON") from None
    if not isinstance(data, dict):
        raise GoogleError("Google's answer is not a JSON object")
    return data


# The request Sheet


def _sheet_url(sheet_id: str) -> str:
    if not isinstance(sheet_id, str) or SHEET_ID.fullmatch(sheet_id) is None:
        raise GoogleError("the request Sheet's id is not a Sheets id")
    return f"{SHEETS_ROOT}/{sheet_id}"


def read_rows(sheet_id: str, token: str, most: int) -> list:
    """Up to most rows under the header, each (row number, its cells A to I as strings), a missing or non-string
    cell read as "" and each cut at CELL_MAX. Code.gs writes its cells as plain values (RAW), so none is a formula."""
    last = 1 + max(int(most), 0)
    target = urllib.parse.quote(f"{SHEET_TAB}!A2:{RUN_COLUMN}{last}", safe="")
    answer = send(f"{_sheet_url(sheet_id)}/values/{target}?majorDimension=ROWS&valueRenderOption=FORMATTED_VALUE",
                  token, None)
    values = answer.get("values", [])
    if not isinstance(values, list):
        raise GoogleError("the Sheet's answer has no rows")
    rows = []
    for index, row in enumerate(values[: max(int(most), 0)]):
        row = row if isinstance(row, list) else []
        cells = [cell if isinstance(cell, str) else "" for cell in row[: len(COLUMNS)]]
        cells += [""] * (len(COLUMNS) - len(cells))
        rows.append((index + 2, [cell[:CELL_MAX] for cell in cells]))
    return rows


def write_statuses(sheet_id: str, token: str, updates: list) -> None:
    """Set each (row number, status, run marker): the status and run cells only, as plain values (RAW), in one call."""
    data = []
    for row, status, marker in updates:
        if type(row) is not int or not 2 <= row <= 1_000_000 or status not in WRITTEN_STATUSES \
                or not isinstance(marker, str) or RUN_MARKER.fullmatch(marker) is None:
            raise GoogleError("a Sheet status update is not one the fleet makes")
        data.append({"range": f"{SHEET_TAB}!{STATUS_COLUMN}{row}:{RUN_COLUMN}{row}", "majorDimension": "ROWS",
                     "values": [[status, marker]]})
    if data:
        send(f"{_sheet_url(sheet_id)}/values:batchUpdate", token, {"valueInputOption": "RAW", "data": data})


# Chat


def create_message(space: str, thread: str, text: str, request_id: str, token: str) -> dict:
    """Post text as the app in thread (a new thread if Chat cannot reply there), once per request_id. The answer's
    message name, checked to be in that space."""
    if not isinstance(space, str) or SPACE.fullmatch(space) is None:
        raise GoogleError("the space is not a Chat space name")
    if not isinstance(thread, str) or THREAD.fullmatch(thread) is None or not thread.startswith(space + "/"):
        raise GoogleError("the thread is not a thread of that space")
    if not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None:
        raise GoogleError("the request id is not one the fleet made")
    if not isinstance(text, str) or not text.strip() or len(text) > MESSAGE_MAX_CHARS:
        raise GoogleError(f"a Chat reply is text of at most {MESSAGE_MAX_CHARS} characters")
    query = urllib.parse.urlencode({"messageReplyOption": REPLY_OPTION, "requestId": request_id})
    answer = send(f"{CHAT_ROOT}/{space}/messages?{query}", token, {"text": text, "thread": {"name": thread}})
    name = answer.get("name")
    if not isinstance(name, str) or not name.startswith(space + "/messages/"):
        raise GoogleError("Chat's answer names no message in that space")
    return {"name": common.one_line(name, 300)}
