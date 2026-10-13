"""Google Chat for chat reviews: the Chat app's Pub/Sub subscription, pulled, and replies posted as the app.

Nothing here listens on the network. The chat-review job (fleet/chat_review.py) calls out, over HTTPS, to two Google
APIs, with access tokens gcloud prints and nothing else:
- Pub/Sub REST, projects/<p>/subscriptions/<s>:pull and :acknowledge, with your own gcloud login
  (gcloud auth print-access-token);
- Chat REST, spaces.messages.create in the request's thread, as the Chat app, by impersonating the app's service
  account for the chat.bot scope (gcloud auth print-access-token --impersonate-service-account <sa> --scopes <scope>).
  The service account has no key: you hold Token Creator on it, so only your own login can mint its tokens.
gcloud runs by its absolute path (config.GCLOUD_BIN) with an environment of only HOME and PATH. A token lives only in
this process's memory, for one call: it is never in argv, a log, a file or an error, and no error text a server or
gcloud printed is kept. Requests go straight to the two Google hosts, with no proxy and certificates checked.

Each Chat reply carries a request id the caller derives from its own record, so sending one again (after a kill, or a
failed answer) gives back the message Chat already made instead of a second one.

It starts no process itself: run_desk.gcloud_token_output runs gcloud for it.
"""
from __future__ import annotations

import base64
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from fleet import common, config, run_desk
from fleet.safefs import FleetError

PUBSUB_ROOT = "https://pubsub.googleapis.com/v1"
CHAT_ROOT = "https://chat.googleapis.com/v1"
CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
REPLY_OPTION = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{20,4096}")
HTTP_TIMEOUT_SECONDS = 30
RESPONSE_MAX_BYTES = 8 * 1024 * 1024
# One Pub/Sub message's data, decoded: a Chat event is a few KB.
EVENT_MAX_BYTES = 65536
# What Chat takes as a message's text is 4096 characters.
MESSAGE_MAX_CHARS = 4000
PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
SUBSCRIPTION = re.compile(r"[A-Za-z][A-Za-z0-9._~+-]{2,254}")
SERVICE_ACCOUNT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com")
SPACE = re.compile(r"spaces/[A-Za-z0-9_-]{1,100}")
THREAD = re.compile(r"spaces/[A-Za-z0-9_-]{1,100}/threads/[A-Za-z0-9_.-]{1,200}")
REQUEST_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
ACK_ID = re.compile(r"[A-Za-z0-9_./+=-]{1,2048}")


class GoogleError(FleetError):
    """A Google call that failed. Its text never holds a token or anything a server or gcloud printed."""


# Tokens


def pull_token_argv() -> list:
    return [config.GCLOUD_BIN, "auth", "print-access-token"]


def chat_token_argv(service_account: str) -> list:
    if not isinstance(service_account, str) or SERVICE_ACCOUNT.fullmatch(service_account) is None:
        raise GoogleError("the Chat app's service account is not a service account email")
    return [config.GCLOUD_BIN, "auth", "print-access-token", "--impersonate-service-account", service_account,
            "--scopes", CHAT_BOT_SCOPE]


def run_gcloud(argv: list) -> str:
    """The access token gcloud prints for argv, one of the two shapes above. Tests replace this."""
    if argv != pull_token_argv() and (len(argv) != 7 or argv != chat_token_argv(argv[4])):
        raise GoogleError("gcloud runs only to print an access token")
    try:
        code, out = run_desk.gcloud_token_output(argv)
    except FleetError as exc:
        raise GoogleError(str(exc)) from None
    token = out.decode("ascii", "replace").strip()
    if code != 0 or TOKEN.fullmatch(token) is None:
        # Nothing gcloud printed is kept: it can name an account, a project or a token.
        raise GoogleError(f"gcloud could not print an access token (exit {code}): run gcloud auth login, and check"
                          " you hold Service Account Token Creator on the Chat app's service account")
    return token


def pull_token() -> str:
    return run_gcloud(pull_token_argv())


def chat_token(service_account: str) -> str:
    return run_gcloud(chat_token_argv(service_account))


# HTTPS


def _opener() -> urllib.request.OpenerDirector:
    """No proxy from anywhere, and certificates checked against the system's roots."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def send(url: str, token: str, payload: dict) -> dict:
    """One JSON request with payload as its body (so urllib sends it as a POST) and the token as its bearer, and the
    JSON object answered. Only the two Google hosts are reached. Tests replace this."""
    if not (url.startswith(PUBSUB_ROOT + "/") or url.startswith(CHAT_ROOT + "/")):
        raise GoogleError("the chat review calls only Pub/Sub and Chat")
    body = json.dumps(payload, ensure_ascii=True).encode("ascii")
    request = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {token}",
                                                              "Content-Type": "application/json"})
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


# Pub/Sub


def subscription_path(project: str, subscription: str) -> str:
    if not isinstance(project, str) or PROJECT.fullmatch(project) is None:
        raise GoogleError("the project is not a Google Cloud project id")
    if not isinstance(subscription, str) or SUBSCRIPTION.fullmatch(subscription) is None \
            or subscription.startswith("goog"):
        raise GoogleError("the subscription is not a Pub/Sub subscription id")
    return f"projects/{project}/subscriptions/{subscription}"


def pull(project: str, subscription: str, token: str, most: int) -> list:
    """Up to most messages, each {"ack_id", "message_id", "data"} with data as bytes, or None for a message that does
    not decode; any received message's ack id is kept, so even a bad one is acknowledged."""
    url = f"{PUBSUB_ROOT}/{subscription_path(project, subscription)}:pull"
    answer = send(url, token, {"maxMessages": int(most), "returnImmediately": True})
    received = answer.get("receivedMessages", [])
    if not isinstance(received, list):
        raise GoogleError("Pub/Sub's pull answer has no message list")
    found = []
    for item in received[: max(int(most), 0)]:
        ack_id = item.get("ackId") if isinstance(item, dict) else None
        if not isinstance(ack_id, str) or ACK_ID.fullmatch(ack_id) is None:
            continue  # nothing to acknowledge it with; its deadline lapses and the subscription's retention ends it
        message = item.get("message") if isinstance(item.get("message"), dict) else {}
        message_id = message.get("messageId") if isinstance(message.get("messageId"), str) else ""
        data = message.get("data")
        decoded: Optional[bytes] = None
        if isinstance(data, str) and len(data) <= EVENT_MAX_BYTES * 2:
            try:
                decoded = base64.b64decode(data, validate=True)
            except ValueError:
                decoded = None
            if decoded is not None and len(decoded) > EVENT_MAX_BYTES:
                decoded = None
        found.append({"ack_id": ack_id, "message_id": common.one_line(message_id, 64), "data": decoded})
    return found


def acknowledge(project: str, subscription: str, token: str, ack_ids: list) -> None:
    if not ack_ids:
        return
    url = f"{PUBSUB_ROOT}/{subscription_path(project, subscription)}:acknowledge"
    send(url, token, {"ackIds": list(ack_ids)})


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
