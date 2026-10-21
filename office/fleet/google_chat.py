"""Google for chat reviews: new messages read from the Chat app's conversations with your own read-only authorization,
replies posted as the app, and the access tokens of each.

Nothing here listens on the network. The chat-review job (fleet/chat_review.py) calls out, over HTTPS, to two Google
hosts, with two separate authorizations that never stand in for each other:
- Reading is yours, read only. A Desktop OAuth client's id and secret and the refresh token
  scripts/chat-review-authorize.py got for exactly READ_SCOPES (chat.messages.readonly and chat.memberships.readonly)
  sit in your login Keychain as one generic password (service CHAT_REVIEW_USER_KEYCHAIN_SERVICE, account
  USER_TOKEN_ACCOUNT). read_token trades them at the OAuth token endpoint for a short-lived access token and refuses one
  whose granted scopes are anything but those two. With it this module only reads Chat: spaces.members.get (the app's
  own membership, spaces/<id>/members/app, and an allowlisted sender's, by email) and spaces.messages.list.
- Sending is the app's. The worker service account's one JSON key (CHAT_REVIEW_SERVICE_ACCOUNT) is a generic password
  too (service CHAT_REVIEW_KEYCHAIN_SERVICE, account the service account's email), stored there by
  scripts/chat-review-store-key.py as the base64 of its PKCS#8 body. access_token signs a JWT bearer grant (RS256)
  for the chat.bot scope alone with it, and with that token this module only posts spaces.messages.create in a
  request's thread.
send refuses a Chat read with anything but the read token it minted, and a Chat post with anything but an app token
it minted, so the read token never sends and the app's never reads. run_desk.keychain_output reads both Keychain items
with /usr/bin/security, and run_desk.openssl_sign_output signs with /usr/bin/openssl, which gets the key on its stdin.
Keys, the refresh token, the client secret and every access token live only in this process's memory: never in argv, a
log, a file or an error, and no error text a server or a tool printed is kept. Requests go straight to the Google
hosts, with no proxy, certificates checked and no redirect followed, so a bearer token never goes to any other host.

Each Chat reply carries a request id the caller derives from its own record, so sending one again (after a kill, or a
failed answer) gives back the message Chat already made instead of a second one.

It starts no process itself: run_desk runs security and openssl for it.
"""
from __future__ import annotations

import base64
import calendar
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
CHAT_ROOT = "https://chat.googleapis.com/v1"
CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
# The app's token is for chat.bot alone, and it only sends.
SCOPES = (CHAT_BOT_SCOPE,)
# Your read token is for exactly these two, and it only reads.
READ_SCOPES = ("https://www.googleapis.com/auth/chat.messages.readonly",
               "https://www.googleapis.com/auth/chat.memberships.readonly")
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
# The account of the read authorization's Keychain item, as scripts/chat-review-authorize.py stores it.
USER_TOKEN_ACCOUNT = "chat-review-read-only"
AUTHORIZE_HELPER = "scripts/chat-review-authorize.py"
# The signed grant is good for this long; the token Google gives back is kept no longer than that, in memory only.
GRANT_SECONDS = 600
TOKEN_MARGIN_SECONDS = 60
REPLY_OPTION = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{20,4096}")
# What the worker key's Keychain item holds: the base64 of the private key's PKCS#8 body, one line.
KEY_BODY = re.compile(rb"[A-Za-z0-9+/]{400,8000}={0,2}")
# What the read authorization's item holds: the base64 of one JSON object, its client id, client secret and refresh
# token.
USER_BLOB = re.compile(rb"[A-Za-z0-9+/]{40,8000}={0,2}")
CLIENT_ID = re.compile(r"[0-9]{1,30}-[a-z0-9]{1,64}\.apps\.googleusercontent\.com")
CLIENT_SECRET = re.compile(r"[A-Za-z0-9_-]{10,100}")
REFRESH_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{20,2048}")
HTTP_TIMEOUT_SECONDS = 30
RESPONSE_MAX_BYTES = 8 * 1024 * 1024
# What Chat takes as a message's text is 4096 characters.
MESSAGE_MAX_CHARS = 4000
SERVICE_ACCOUNT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com")
KEYCHAIN_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
SPACE = re.compile(r"spaces/[A-Za-z0-9_-]{1,100}")
THREAD = re.compile(r"spaces/[A-Za-z0-9_-]{1,100}/threads/[A-Za-z0-9_.-]{1,200}")
MESSAGE = re.compile(r"(spaces/[A-Za-z0-9_-]{1,100})/messages/[A-Za-z0-9_.-]{1,200}")
USER = re.compile(r"users/[A-Za-z0-9_-]{1,100}")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")
REQUEST_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
# A message's createTime, in UTC as Chat gives it, read to the microsecond.
CREATE_TIME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?Z")
# Messages one list call asks for; the caller caps the pages.
PAGE_SIZE = 100
PAGE_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{1,2048}")
NAME_MAX = 200
TEXT_MAX = 4096

_TOKENS: dict = {}
_READ: dict = {}


class GoogleError(FleetError):
    """A Google call that failed. Its text never holds a key, a token or anything a server or a tool printed; status
    is the HTTP status Google answered, when it answered one."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class ReadAuthError(GoogleError):
    """Your read-only authorization is missing, refused (revoked or expired) or broader than read only. Reading stops
    until you run scripts/chat-review-authorize.py again; nothing broader is ever tried instead."""


# Tokens


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _keychain_service(name: str) -> str:
    service = getattr(config, name)
    if not isinstance(service, str) or KEYCHAIN_SERVICE.fullmatch(service) is None:
        raise GoogleError(f"{name} is not a Keychain service name")
    return service


def _identity() -> tuple:
    service, account = _keychain_service("CHAT_REVIEW_KEYCHAIN_SERVICE"), config.CHAT_REVIEW_SERVICE_ACCOUNT
    if not isinstance(account, str) or SERVICE_ACCOUNT.fullmatch(account) is None:
        raise GoogleError("CHAT_REVIEW_SERVICE_ACCOUNT is not a service account email")
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
        raise GoogleError("the app's token is only for the chat.bot scope")
    service, account = _identity()
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iss": account, "scope": scope, "aud": TOKEN_URL, "iat": now, "exp": now + GRANT_SECONDS}
    signing_input = ".".join(_b64url(json.dumps(part, separators=(",", ":"), sort_keys=True).encode("ascii"))
                             for part in (header, claims))
    code, signature = run_desk.openssl_sign_output(_key_pem(service, account), signing_input.encode("ascii"))
    if code != 0 or not 128 <= len(signature) <= 1024:
        raise GoogleError(f"openssl could not sign the token grant (exit {code})")
    return f"{signing_input}.{_b64url(signature)}"


def _bearer(answer: dict) -> tuple:
    """(access token, its lifetime) from the token endpoint's answer, else GoogleError."""
    token, kind, lasts = answer.get("access_token"), answer.get("token_type"), answer.get("expires_in")
    if not isinstance(token, str) or TOKEN.fullmatch(token) is None or not isinstance(kind, str) \
            or kind.lower() != "bearer" or type(lasts) is not int or lasts <= TOKEN_MARGIN_SECONDS:
        raise GoogleError("Google's token answer is not an access token")
    return token, lasts


def access_token(scope: str) -> str:
    """The app's short-lived access token for scope, minted from a signed grant, kept in this process's memory only.
    It only sends."""
    now = int(time.time())
    held = _TOKENS.get(scope)
    if held is not None and held[1] - TOKEN_MARGIN_SECONDS > now:
        return held[0]
    token, lasts = _bearer(send(TOKEN_URL, None, {"grant_type": JWT_BEARER, "assertion": grant(scope, now)}))
    if token == _READ.get("token"):
        raise GoogleError("Google gave the app the read token")
    _TOKENS[scope] = (token, now + min(lasts, GRANT_SECONDS))
    return token


def _read_authorization() -> dict:
    """Your read authorization's client id, client secret and refresh token, read from your login Keychain."""
    service = _keychain_service("CHAT_REVIEW_USER_KEYCHAIN_SERVICE")
    code, out = run_desk.keychain_output(service, USER_TOKEN_ACCOUNT)
    blob = out.strip()
    stored = None
    if code == 0 and USER_BLOB.fullmatch(blob) is not None:
        try:
            stored = common.strict_json(base64.b64decode(blob, validate=True))
        except (ValueError, UnicodeDecodeError, RecursionError):
            stored = None
    if not isinstance(stored, dict) or not all(isinstance(stored.get(field), str) and pattern.fullmatch(stored[field])
                                               for field, pattern in (("client_id", CLIENT_ID),
                                                                      ("client_secret", CLIENT_SECRET),
                                                                      ("refresh_token", REFRESH_TOKEN))):
        # Nothing security printed is kept.
        raise ReadAuthError(f"no usable read-only Chat authorization is in your login Keychain under service {service}"
                            f" (exit {code}): run {AUTHORIZE_HELPER} <client.json>")
    return stored


def read_token() -> str:
    """Your short-lived read-only access token, minted from the stored refresh token, kept in this process's memory
    only. ReadAuthError when the authorization is missing, refused, or granted any scope but READ_SCOPES; GoogleError
    when Google could not be asked. It only reads."""
    now = int(time.time())
    if _READ and _READ["until"] - TOKEN_MARGIN_SECONDS > now:
        return _READ["token"]
    stored = _read_authorization()
    try:
        answer = send(TOKEN_URL, None, {"grant_type": "refresh_token", "client_id": stored["client_id"],
                                        "client_secret": stored["client_secret"],
                                        "refresh_token": stored["refresh_token"]})
    except GoogleError as exc:
        if exc.status in (400, 401, 403):  # invalid_grant (revoked or expired) or invalid_client
            raise ReadAuthError(f"Google refused the read-only Chat authorization (HTTP {exc.status}): run"
                                f" {AUTHORIZE_HELPER} <client.json> again") from None
        raise
    granted = answer.get("scope")
    if not isinstance(granted, str) or sorted(set(granted.split())) != sorted(READ_SCOPES):
        raise ReadAuthError("the read-only Chat authorization is not for exactly the two read scopes: run"
                            f" {AUTHORIZE_HELPER} <client.json> again")
    token, lasts = _bearer(answer)
    if any(token == held[0] for held in _TOKENS.values()):
        raise GoogleError("Google gave the read authorization the app's token")
    _READ.update({"token": token, "until": now + min(lasts, 3600)})
    return token


def forget_tokens() -> None:
    _TOKENS.clear()
    _READ.clear()


def _is_read_token(token: object) -> bool:
    return bool(_READ) and token == _READ.get("token")


def _is_app_token(token: object) -> bool:
    return any(token == held[0] for held in _TOKENS.values())


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
    """One request and the JSON object answered. The token endpoint gets payload as a form and no token. Chat gets
    payload as JSON with the app's token as its bearer (a post), or no payload with your read token (a read): a read
    with the app's token, or a post with the read token, is refused before anything is sent. Only those two Google
    hosts are reached. Tests replace this."""
    headers = {}
    if url == TOKEN_URL:
        if token is not None or not isinstance(payload, dict):
            raise GoogleError("the token endpoint takes a grant and no token")
        body = urllib.parse.urlencode(payload).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif url.startswith(CHAT_ROOT + "/"):
        if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
            raise GoogleError("a Chat call needs an access token")
        if payload is None and (not _is_read_token(token) or _is_app_token(token)):
            raise GoogleError("Chat is read only with the read-only authorization")
        if payload is not None and (not _is_app_token(token) or _is_read_token(token)):
            raise GoogleError("Chat is written only as the app")
        headers["Authorization"] = f"Bearer {token}"
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=True).encode("ascii")
            headers["Content-Type"] = "application/json"
    else:
        raise GoogleError("the chat review calls only Google's token endpoint and Chat")
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with _opener().open(request, timeout=HTTP_TIMEOUT_SECONDS) as answer:
            raw = answer.read(RESPONSE_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise GoogleError(f"Google answered HTTP {exc.code}", status=exc.code) from None
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


# Reading Chat, with the read token


def _space(space: str) -> str:
    if not isinstance(space, str) or SPACE.fullmatch(space) is None:
        raise GoogleError("the space is not a Chat space name")
    return space


def _read(url: str, token: str) -> dict:
    """One Chat read with your read token. Chat refusing the token itself (HTTP 401) is a ReadAuthError, so reading
    stops as it does when the refresh is refused."""
    if not _is_read_token(token):
        raise GoogleError("Chat is read only with the read-only authorization")
    try:
        return send(url, token, None)
    except ReadAuthError:
        raise
    except GoogleError as exc:
        if exc.status == 401:
            raise ReadAuthError(f"Chat refused the read-only authorization (HTTP 401): run {AUTHORIZE_HELPER}"
                                " <client.json> again", status=401) from None
        raise


def _joined(answer: dict, space: str, kind: str) -> Optional[str]:
    """The member's user name when answer is a joined membership of that space of that kind (HUMAN or BOT)."""
    member = answer.get("member") if isinstance(answer.get("member"), dict) else {}
    name = member.get("name")
    if not isinstance(answer.get("name"), str) or not answer["name"].startswith(space + "/members/") \
            or answer.get("state") != "JOINED" or member.get("type") != kind \
            or not isinstance(name, str) or USER.fullmatch(name) is None:
        return None
    return name


def app_member(space: str, token: str) -> Optional[tuple]:
    """(the app's user name in space, the microsecond its membership began or None) when the app is a joined member
    of it, else None: spaces.members.get of spaces/<id>/members/app, with your read token."""
    try:
        answer = _read(f"{CHAT_ROOT}/{_space(space)}/members/app", token)
    except GoogleError as exc:
        if exc.status in (403, 404):  # not a member, or a conversation you are not in
            return None
        raise
    name = _joined(answer, space, "BOT")
    return None if name is None else (name, time_us(answer.get("createTime")))


def member_user(space: str, email: str, token: str) -> Optional[str]:
    """The Chat user name of the person with that email when they are a joined human member of space, else None."""
    if not isinstance(email, str) or EMAIL.fullmatch(email) is None:
        raise GoogleError("that is not an email")
    try:
        answer = _read(f"{CHAT_ROOT}/{_space(space)}/members/{urllib.parse.quote(email, safe='@')}", token)
    except GoogleError as exc:
        if exc.status in (403, 404):
            return None
        raise
    return _joined(answer, space, "HUMAN")


def time_us(text: object) -> Optional[int]:
    """The microsecond since the epoch of a Chat createTime, else None."""
    match = CREATE_TIME.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    try:
        seconds = calendar.timegm(time.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None
    return seconds * 1_000_000 + int((match.group(2) or "0")[:6].ljust(6, "0"))


def rfc3339(us: int) -> str:
    seconds, micros = divmod(int(us), 1_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{micros:06d}Z"


def _mentions(message: dict, app_user: str) -> bool:
    """Whether the message mentions the app itself: a user mention annotation naming the app's own user."""
    annotations = message.get("annotations")
    for item in annotations if isinstance(annotations, list) else []:
        if not isinstance(item, dict) or item.get("type") != "USER_MENTION":
            continue
        mention = item.get("userMention")
        user = mention.get("user") if isinstance(mention, dict) else None
        if isinstance(user, dict) and user.get("type") == "BOT" and user.get("name") == app_user:
            return True
    return False


def _text(value: object, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def list_messages(space: str, after_us: int, app_user: str, token: str, page: Optional[str] = None) -> tuple:
    """One page of space's messages created after after_us, oldest first, with your read token: (messages, the next
    page's token or None). A message that does not mention the app (app_user) is given back as its time alone, so
    nothing else of it ever leaves here; one that does is its name, time, thread, sender and the text Chat gives with
    the app mentions taken out (argumentText)."""
    if not isinstance(app_user, str) or USER.fullmatch(app_user) is None:
        raise GoogleError("the app's user name is not a Chat user name")
    query = {"pageSize": PAGE_SIZE, "filter": f'createTime > "{rfc3339(after_us)}"', "orderBy": "createTime ASC"}
    if page is not None:
        if not isinstance(page, str) or PAGE_TOKEN.fullmatch(page) is None:
            raise GoogleError("Chat's page token is not one")
        query["pageToken"] = page
    answer = _read(f"{CHAT_ROOT}/{_space(space)}/messages?{urllib.parse.urlencode(query)}", token)
    found = answer.get("messages", [])
    following = answer.get("nextPageToken") or None
    if not isinstance(found, list) or (following is not None and (not isinstance(following, str)
                                                                    or PAGE_TOKEN.fullmatch(following) is None)):
        raise GoogleError("Chat's answer has no messages")
    messages = []
    for item in found[:PAGE_SIZE]:
        item = item if isinstance(item, dict) else {}
        at = time_us(item.get("createTime"))
        if at is None:
            continue
        if not _mentions(item, app_user):
            messages.append({"created_us": at, "mentions": False})
            continue
        sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
        thread = item.get("thread") if isinstance(item.get("thread"), dict) else {}
        argument = item.get("argumentText")
        if not isinstance(argument, str) or len(argument) > TEXT_MAX:
            argument = ""  # text longer than Chat sends is read as none, never as a cut prefix
        messages.append({"created_us": at, "mentions": True, "name": _text(item.get("name"), NAME_MAX * 2),
                         "thread": _text(thread.get("name"), NAME_MAX * 2),
                         "sender": _text(sender.get("name"), NAME_MAX), "sender_type": _text(sender.get("type"), 20),
                         "sender_name": _text(sender.get("displayName"), NAME_MAX),
                         "argument": argument})
    return messages, following


# Sending in Chat, as the app


def create_message(space: str, thread: str, text: str, request_id: str, token: str) -> dict:
    """Post text as the app in thread (a new thread if Chat cannot reply there), once per request_id. The answer's
    message name, checked to be in that space."""
    _space(space)
    if not isinstance(thread, str) or THREAD.fullmatch(thread) is None or not thread.startswith(space + "/"):
        raise GoogleError("the thread is not a thread of that space")
    if not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None:
        raise GoogleError("the request id is not one the fleet made")
    if not isinstance(text, str) or not text.strip() or len(text) > MESSAGE_MAX_CHARS:
        raise GoogleError(f"a Chat reply is text of at most {MESSAGE_MAX_CHARS} characters")
    if not _is_app_token(token) or _is_read_token(token):
        raise GoogleError("Chat is written only as the app")
    query = urllib.parse.urlencode({"messageReplyOption": REPLY_OPTION, "requestId": request_id})
    answer = send(f"{CHAT_ROOT}/{space}/messages?{query}", token, {"text": text, "thread": {"name": thread}})
    name = answer.get("name")
    if not isinstance(name, str) or not name.startswith(space + "/messages/"):
        raise GoogleError("Chat's answer names no message in that space")
    return {"name": common.one_line(name, 300)}
