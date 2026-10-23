"""Google for chat reviews: new messages read from each Chat app's conversations with your own read-only authorization,
replies posted as the app that was mentioned, and the access tokens of each.

The chat review serves one Chat app per reviewing desk (config.CHAT_REVIEW_APPS), each in its own Google Cloud project,
and every function here that touches a credential is told which app it is for. Nothing here listens on the network.
The chat-review job (fleet/chat_review.py) calls out, over HTTPS, to two Google hosts, with two kinds of authorization
per app that never stand in for each other or for another app's:
- Reading is yours, read only. For each app, a Desktop OAuth client of its project, its id and secret and the refresh
  token scripts/chat-review-authorize.py got for exactly READ_SCOPES (chat.messages.readonly and
  chat.memberships.readonly) sit in your login Keychain as one generic password (service the app's read Keychain
  service, account USER_TOKEN_ACCOUNT). read_token trades them at the OAuth token endpoint for a short-lived access
  token and refuses one whose granted scopes are anything but those two. It is per app because Chat answers
  spaces/<id>/members/app with the membership of the app of the project the OAuth client belongs to. With it this
  module only reads Chat: spaces.members.get (that app's own membership, and an allowlisted sender's, by email),
  spaces.messages.list, and media.download of an image attached to a message the app takes (download_media), which
  chat.messages.readonly covers.
- Sending is the app's. Its worker service account's one JSON key is a generic password too (service the app's key
  Keychain service, account the service account's email), stored there by scripts/chat-review-store-key.py as the
  base64 of its PKCS#8 body. access_token signs a JWT bearer grant (RS256) for the chat.bot scope alone with it, and
  with that token this module only posts spaces.messages.create in a request's thread, as that app.
send refuses a Chat read with anything but a read token it minted, and a Chat post with anything but an app token it
minted, so a read token never sends and an app's never reads; create_message takes only the named app's own token.
run_desk.keychain_output reads every Keychain item with /usr/bin/security, and run_desk.openssl_sign_output signs with
/usr/bin/openssl, which gets the key on its stdin.
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
import http.client
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
STORE_KEY_HELPER = "scripts/chat-review-store-key.py"
# The app a helper stores for when it is not told (--app), and the settings each app of CHAT_REVIEW_APPS names.
DEFAULT_APP = "hermione"
APP_FIELDS = ("name", "read_keychain", "service_account", "keychain")
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
# An attached file's media, as media.download names it: its attachmentDataRef.resourceName.
MEDIA_RESOURCE = re.compile(r"[A-Za-z0-9._~+/=-]{1,1000}")
# The attachments of a message one list call gives back, at most.
ATTACHMENTS_MAX = 10
# Messages one list call asks for; the caller caps the pages.
PAGE_SIZE = 100
PAGE_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{1,2048}")
# Messages one read of a thread's recent messages asks for, at most.
HISTORY_PAGE_SIZE = 25
NAME_MAX = 200
TEXT_MAX = 4096

# Tokens in this process's memory only: the app tokens by (app, scope), and the read tokens by app.
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


# Apps


def app_setting(app: str, field: str) -> str:
    """The name of the config setting that holds field (one of APP_FIELDS) of app, from CHAT_REVIEW_APPS."""
    names = config.CHAT_REVIEW_APPS.get(app) if isinstance(config.CHAT_REVIEW_APPS, dict) else None
    name = names.get(field) if isinstance(names, dict) and field in APP_FIELDS else None
    if not isinstance(name, str) or re.fullmatch(r"CHAT_REVIEW_[A-Z_]{1,60}", name) is None:
        raise GoogleError(f"{app} is not a chat review app in CHAT_REVIEW_APPS")
    return name


def app_value(app: str, field: str) -> object:
    """The value of field of app, read from config now."""
    return getattr(config, app_setting(app, field), None)


def helper_command(helper: str, app: str, argument: str) -> str:
    """How to run a helper for app, as a message names it: --app only for an app other than DEFAULT_APP."""
    return f"{helper} {argument}" if app == DEFAULT_APP else f"{helper} --app {app} {argument}"


# Tokens


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _keychain_service(app: str, field: str) -> str:
    name, service = app_setting(app, field), app_value(app, field)
    if not isinstance(service, str) or KEYCHAIN_SERVICE.fullmatch(service) is None:
        raise GoogleError(f"{name} is not a Keychain service name")
    return service


def _identity(app: str) -> tuple:
    service, account = _keychain_service(app, "keychain"), app_value(app, "service_account")
    if not isinstance(account, str) or SERVICE_ACCOUNT.fullmatch(account) is None:
        raise GoogleError(f"{app_setting(app, 'service_account')} is not a service account email")
    return service, account


def _key_pem(service: str, account: str, app: str) -> bytes:
    """The service account's private key as PEM, read from your login Keychain."""
    code, out = run_desk.keychain_output(service, account)
    body = out.strip()
    if code != 0 or KEY_BODY.fullmatch(body) is None:
        # Nothing security printed is kept.
        raise GoogleError(f"no usable key for {account} is in your login Keychain under service {service} (exit"
                          f" {code}): store one with {helper_command(STORE_KEY_HELPER, app, '<key.json>')}")
    lines = [body[index:index + 64] for index in range(0, len(body), 64)]
    return b"-----BEGIN PRIVATE KEY-----\n" + b"\n".join(lines) + b"\n-----END PRIVATE KEY-----\n"


def grant(scope: str, now: int, app: str) -> str:
    """A JWT bearer grant for scope, signed RS256 with app's service account's key by openssl."""
    if scope not in SCOPES:
        raise GoogleError("the app's token is only for the chat.bot scope")
    service, account = _identity(app)
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iss": account, "scope": scope, "aud": TOKEN_URL, "iat": now, "exp": now + GRANT_SECONDS}
    signing_input = ".".join(_b64url(json.dumps(part, separators=(",", ":"), sort_keys=True).encode("ascii"))
                             for part in (header, claims))
    code, signature = run_desk.openssl_sign_output(_key_pem(service, account, app), signing_input.encode("ascii"))
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


def access_token(scope: str, app: str) -> str:
    """app's short-lived access token for scope, minted from a signed grant, kept in this process's memory only. It
    only sends, as that app."""
    now = int(time.time())
    held = _TOKENS.get((app, scope))
    if held is not None and held[1] - TOKEN_MARGIN_SECONDS > now:
        return held[0]
    token, lasts = _bearer(send(TOKEN_URL, None, {"grant_type": JWT_BEARER, "assertion": grant(scope, now, app)}))
    if _is_read_token(token) or any(token == found[0] for key, found in _TOKENS.items() if key[0] != app):
        raise GoogleError("Google gave the app a read token or another app's token")
    _TOKENS[(app, scope)] = (token, now + min(lasts, GRANT_SECONDS))
    return token


def _read_authorization(app: str) -> dict:
    """Your read authorization for app's project: its client id, client secret and refresh token, read from your login
    Keychain."""
    service = _keychain_service(app, "read_keychain")
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
                            f" (exit {code}): run {authorize_command(app)}")
    return stored


def authorize_command(app: str) -> str:
    return helper_command(AUTHORIZE_HELPER, app, "<client.json>")


def read_token(app: str, timeout: Optional[float] = None) -> str:
    """Your short-lived read-only access token for app's project, minted from its stored refresh token, kept in this
    process's memory only. ReadAuthError when the authorization is missing, refused, or granted any scope but
    READ_SCOPES; GoogleError when Google could not be asked (within timeout, when given: send). It only reads."""
    now, began = int(time.time()), time.monotonic()
    held = _READ.get(app)
    if held is not None and held["until"] - TOKEN_MARGIN_SECONDS > now:
        return held["token"]
    stored = _read_authorization(app)
    left = None if timeout is None else timeout - (time.monotonic() - began)  # what the Keychain read left of it
    try:
        answer = send(TOKEN_URL, None, {"grant_type": "refresh_token", "client_id": stored["client_id"],
                                        "client_secret": stored["client_secret"],
                                        "refresh_token": stored["refresh_token"]}, timeout=left)
    except GoogleError as exc:
        if exc.status in (400, 401, 403):  # invalid_grant (revoked or expired) or invalid_client
            raise ReadAuthError(f"Google refused the read-only Chat authorization (HTTP {exc.status}): run"
                                f" {authorize_command(app)} again") from None
        raise
    granted = answer.get("scope")
    if not isinstance(granted, str) or sorted(set(granted.split())) != sorted(READ_SCOPES):
        raise ReadAuthError("the read-only Chat authorization is not for exactly the two read scopes: run"
                            f" {authorize_command(app)} again")
    token, lasts = _bearer(answer)
    if _is_app_token(token) or any(token == found["token"] for key, found in _READ.items() if key != app):
        raise GoogleError("Google gave the read authorization an app's token or another app's read token")
    _READ[app] = {"token": token, "until": now + min(lasts, 3600)}
    return token


def forget_tokens() -> None:
    _TOKENS.clear()
    _READ.clear()


def _is_read_token(token: object) -> bool:
    return any(token == found["token"] for found in _READ.values())


def _is_app_token(token: object, app: Optional[str] = None) -> bool:
    """Whether token is an app token this process minted: any app's, or with app, that app's own."""
    return any(token == found[0] for key, found in _TOKENS.items() if app is None or key[0] == app)


# HTTPS


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every redirect is refused (urllib then raises it as an HTTPError), since one would carry the bearer token on."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib's own signature
        return None


def _opener() -> urllib.request.OpenerDirector:
    """No proxy from anywhere, no redirect followed, and certificates checked against the system's roots."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                       urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def send(url: str, token: Optional[str], payload: Optional[dict], timeout: Optional[float] = None) -> dict:
    """One request and the JSON object answered. The token endpoint gets payload as a form and no token. Chat gets
    payload as JSON with the app's token as its bearer (a post), or no payload with your read token (a read): a read
    with the app's token, or a post with the read token, is refused before anything is sent. Only those two Google
    hosts are reached, each call within HTTP_TIMEOUT_SECONDS, or timeout when it is less (none left: refused before
    anything is sent). Tests replace this."""
    seconds = HTTP_TIMEOUT_SECONDS if timeout is None else min(HTTP_TIMEOUT_SECONDS, timeout)
    if not seconds > 0:
        raise GoogleError("no time is left for the call")
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
        with _opener().open(request, timeout=seconds) as answer:
            raw = answer.read(RESPONSE_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise GoogleError(f"Google answered HTTP {exc.code}", status=exc.code) from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
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


def _read(url: str, token: str, timeout: Optional[float] = None) -> dict:
    """One Chat read with a read token, within timeout when given (send). Chat refusing the token itself (HTTP 401) is
    a ReadAuthError, so reading stops as it does when the refresh is refused."""
    if not _is_read_token(token):
        raise GoogleError("Chat is read only with the read-only authorization")
    try:
        return send(url, token, None, timeout=timeout)
    except ReadAuthError:
        raise
    except GoogleError as exc:
        if exc.status == 401:
            app = next((key for key, found in _READ.items() if found["token"] == token), DEFAULT_APP)
            raise ReadAuthError(f"Chat refused the read-only authorization (HTTP 401): run {authorize_command(app)}"
                                " again", status=401) from None
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


def app_member(space: str, token: str, timeout: Optional[float] = None) -> Optional[tuple]:
    """(the app's user name in space, the microsecond its membership began or None) when the app is a joined member
    of it, else None: spaces.members.get of spaces/<id>/members/app, with your read token for that app's project,
    which Chat answers with that project's own app."""
    try:
        answer = _read(f"{CHAT_ROOT}/{_space(space)}/members/app", token, timeout)
    except GoogleError as exc:
        if exc.status in (403, 404):  # not a member, or a conversation you are not in
            return None
        raise
    name = _joined(answer, space, "BOT")
    return None if name is None else (name, time_us(answer.get("createTime")))


def member_user(space: str, email: str, token: str, timeout: Optional[float] = None) -> Optional[str]:
    """The Chat user name of the person with that email when they are a joined human member of space, else None."""
    if not isinstance(email, str) or EMAIL.fullmatch(email) is None:
        raise GoogleError("that is not an email")
    try:
        answer = _read(f"{CHAT_ROOT}/{_space(space)}/members/{urllib.parse.quote(email, safe='@')}", token, timeout)
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


def _mentions_anyone(message: dict) -> bool:
    """Whether the message mentions anyone at all, a person or an app."""
    annotations = message.get("annotations")
    return any(isinstance(item, dict) and item.get("type") == "USER_MENTION"
               for item in (annotations if isinstance(annotations, list) else []))


def _text(value: object, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def list_messages(space: str, after_us: int, app_user: str, token: str, page: Optional[str] = None,
                  threads: frozenset = frozenset()) -> tuple:
    """One page of space's messages created after after_us, oldest first, with your read token: (messages, the next
    page's token or None). A message that does not mention the app (app_user) is given back as its time alone, so
    nothing else of it ever leaves here; one that does is its name, time, thread, sender and the text Chat gives with
    the app mentions taken out (argumentText) and the resource names of its uploaded images (_images). The one
    exception, for a thread follow-up: a message that mentions no one at all, in one of threads (those that were the
    app's alone for a while), is given back the same way,
    marked listening, with its whole text."""
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
        thread = item.get("thread") if isinstance(item.get("thread"), dict) else {}
        mentions = _mentions(item, app_user)
        listening = not mentions and thread.get("name") in threads and not _mentions_anyone(item)
        if not mentions and not listening:
            messages.append({"created_us": at, "mentions": False})
            continue
        sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
        argument = item.get("argumentText") if mentions else item.get("text")
        if not isinstance(argument, str) or len(argument) > TEXT_MAX:
            argument = ""  # text longer than Chat sends is read as none, never as a cut prefix
        messages.append({"created_us": at, "mentions": mentions, "listening": listening,
                         "attachments": _images(item.get("attachment")),
                         "name": _text(item.get("name"), NAME_MAX * 2),
                         "thread": _text(thread.get("name"), NAME_MAX * 2),
                         "sender": _text(sender.get("name"), NAME_MAX), "sender_type": _text(sender.get("type"), 20),
                         "sender_name": _text(sender.get("displayName"), NAME_MAX),
                         "argument": argument})
    return messages, following


def thread_messages(space: str, thread: str, before_us: int, app_user: str, users: frozenset, token: str,
                    page: Optional[str] = None, size: int = HISTORY_PAGE_SIZE,
                    timeout: Optional[float] = None) -> tuple:
    """One page of thread's messages created before before_us, newest first, with your read token: (messages, the next
    page's token or None), for an answer's context. Each is its time and whether the app (app_user) sent it (own); only
    one the app sent, or a person whose Chat user name is in users sent, is also its sender, the sender's display name
    and its whole text, so nothing else of any other message ever leaves here. Within timeout when given (send)."""
    if not isinstance(app_user, str) or USER.fullmatch(app_user) is None:
        raise GoogleError("the app's user name is not a Chat user name")
    if not isinstance(thread, str) or THREAD.fullmatch(thread) is None or not thread.startswith(_space(space) + "/"):
        raise GoogleError("the thread is not a thread of that space")
    if type(size) is not int or not 1 <= size <= HISTORY_PAGE_SIZE:
        raise GoogleError("a thread read asks for a page of at most a few messages")
    query = {"pageSize": size, "filter": f'createTime < "{rfc3339(before_us)}" AND thread.name = {thread}',
             "orderBy": "createTime DESC"}
    if page is not None:
        if not isinstance(page, str) or PAGE_TOKEN.fullmatch(page) is None:
            raise GoogleError("Chat's page token is not one")
        query["pageToken"] = page
    answer = _read(f"{CHAT_ROOT}/{space}/messages?{urllib.parse.urlencode(query)}", token, timeout)
    found = answer.get("messages", [])
    following = answer.get("nextPageToken") or None
    if not isinstance(found, list) or (following is not None and (not isinstance(following, str)
                                                                    or PAGE_TOKEN.fullmatch(following) is None)):
        raise GoogleError("Chat's answer has no messages")
    messages = []
    for item in found[:size]:
        item = item if isinstance(item, dict) else {}
        at = time_us(item.get("createTime"))
        thread_of = item.get("thread") if isinstance(item.get("thread"), dict) else {}
        if at is None or at >= before_us or thread_of.get("name") != thread:
            continue
        sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
        own = sender.get("type") == "BOT" and sender.get("name") == app_user
        if not own and (sender.get("type") != "HUMAN" or sender.get("name") not in users):
            messages.append({"created_us": at, "own": False})
            continue
        text, shown = item.get("text"), sender.get("displayName")
        if not isinstance(text, str) or len(text) > TEXT_MAX:
            text = ""  # text longer than Chat sends is read as none, never as a cut prefix
        if not isinstance(shown, str) or len(shown) > NAME_MAX:
            shown = ""  # a name is whole or none, so no cut leaves part of a credential for the scrub to miss
        messages.append({"created_us": at, "own": own, "sender": _text(sender.get("name"), NAME_MAX),
                         "sender_name": shown, "text": text})
    return messages, following


def _images(found: object) -> list:
    """The media resource name of each attachment of a message that was uploaded to Chat and says it is an image.
    What it says is not trusted: chat_material keeps only a file whose first bytes are an image."""
    names = []
    for item in (found if isinstance(found, list) else [])[:ATTACHMENTS_MAX]:
        if not isinstance(item, dict):
            continue
        ref = item.get("attachmentDataRef")
        name, kind = ref.get("resourceName") if isinstance(ref, dict) else None, item.get("contentType")
        if item.get("source") == "UPLOADED_CONTENT" and isinstance(kind, str) and kind.lower().startswith("image/") \
                and isinstance(name, str) and MEDIA_RESOURCE.fullmatch(name) and ".." not in name:
            names.append(name)
    return names


def download_media(resource: str, token: str, limit: int) -> bytes:
    """The bytes of one attached file, by its media resource name, with your read token (media.download, ?alt=media),
    at most limit of them: more is a GoogleError, never a cut file."""
    if not isinstance(resource, str) or MEDIA_RESOURCE.fullmatch(resource) is None or ".." in resource \
            or resource.startswith("/"):
        raise GoogleError("that is not a media resource name")
    if not _is_read_token(token) or _is_app_token(token):
        raise GoogleError("Chat is read only with the read-only authorization")
    return fetch_media(f"{CHAT_ROOT}/media/{urllib.parse.quote(resource, safe='/')}?alt=media", token, limit)


def fetch_media(url: str, token: str, limit: int) -> bytes:
    """One media download from Chat with a read token, at most limit bytes, no redirect followed, so the token goes to
    no other host. Tests replace this."""
    if not url.startswith(CHAT_ROOT + "/media/") or not _is_read_token(token) or _is_app_token(token):
        raise GoogleError("a media download goes only to Chat, with the read-only authorization")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with _opener().open(request, timeout=HTTP_TIMEOUT_SECONDS) as answer:
            raw = answer.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise GoogleError(f"Google answered HTTP {exc.code}", status=exc.code) from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        raise GoogleError("Google could not be reached") from None
    if len(raw) > limit:
        raise GoogleError("the attachment is larger than the fleet reads")
    return raw


# Sending in Chat, as the app


def create_message(space: str, thread: str, text: str, request_id: str, token: str, app: str) -> dict:
    """Post text as app in thread (a new thread if Chat cannot reply there), once per request_id, with app's own
    token. The answer's message name, checked to be in that space."""
    _space(space)
    if not isinstance(thread, str) or THREAD.fullmatch(thread) is None or not thread.startswith(space + "/"):
        raise GoogleError("the thread is not a thread of that space")
    if not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None:
        raise GoogleError("the request id is not one the fleet made")
    if not isinstance(text, str) or not text.strip() or len(text) > MESSAGE_MAX_CHARS:
        raise GoogleError(f"a Chat reply is text of at most {MESSAGE_MAX_CHARS} characters")
    if not _is_app_token(token, app) or _is_read_token(token):
        raise GoogleError("Chat is written only as the app that was asked, with its own token")
    query = urllib.parse.urlencode({"messageReplyOption": REPLY_OPTION, "requestId": request_id})
    answer = send(f"{CHAT_ROOT}/{space}/messages?{query}", token, {"text": text, "thread": {"name": thread}})
    name = answer.get("name")
    if not isinstance(name, str) or not name.startswith(space + "/messages/"):
        raise GoogleError("Chat's answer names no message in that space")
    return {"name": common.one_line(name, 300)}
