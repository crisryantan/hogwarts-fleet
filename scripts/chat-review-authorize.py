"""Give the chat review its read-only Chat authorization, or replace it, by signing in as yourself once.

Run it yourself in Terminal, from your clone of the kit, after you create a Desktop OAuth client in the Google Cloud
project and download its client JSON:
  /usr/bin/python3 -I -B scripts/chat-review-authorize.py <downloaded client.json>

It reads CHAT_REVIEW_USER_KEYCHAIN_SERVICE (and CHAT_REVIEW_KEYCHAIN_SERVICE, which must differ) from the installed
office's fleet/config.py (~/.hogwarts), without running it, and takes only a Desktop ("installed") client. It runs the
installed-app loopback flow: it listens on 127.0.0.1 alone, on a port the system picks, prints the Google sign-in link
to open in your browser, and asks for exactly two scopes, chat.messages.readonly and chat.memberships.readonly, with
PKCE (S256) and a random state. Once you allow them, Google sends your browser back to that port with a code, which it
trades, with the PKCE verifier only this process holds, for a refresh token. A grant for any other set of scopes is
revoked and nothing is stored. It stores the client id, the client secret and the refresh token, as the base64 of one
JSON object, as a generic password in your login Keychain (service CHAT_REVIEW_USER_KEYCHAIN_SERVICE, account
chat-review-read-only) by driving /usr/bin/security -i over its stdin: none of them is ever in any process's argv, a
file this writes, or its output, and the access token Google gives back is dropped. An item already there is
replaced (-U), which is how you re-authorize after a revoke or an expiry. It reads the item back to check it, and
prints OK or FAILED and exits 1 on any failure. Requests go only to Google's token endpoint, with no proxy and no
redirect followed.
"""
import ast
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SECURITY = "/usr/bin/security"
CLIENT_MAX_BYTES = 16384
TIMEOUT_SECONDS = 30
WAIT_SECONDS = 300
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPES = ("https://www.googleapis.com/auth/chat.messages.readonly",
          "https://www.googleapis.com/auth/chat.memberships.readonly")
ACCOUNT = "chat-review-read-only"
KEYCHAIN_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
CLIENT_ID = re.compile(r"[0-9]{1,30}-[a-z0-9]{1,64}\.apps\.googleusercontent\.com")
CLIENT_SECRET = re.compile(r"[A-Za-z0-9_-]{10,100}")
REFRESH_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{20,2048}")
CODE = re.compile(r"[A-Za-z0-9._~/+=-]{10,2048}")
RESPONSE_MAX_BYTES = 65536
DONE_PAGE = b"The chat review's read-only authorization came back to the terminal. You can close this tab.\n"


class Failed(Exception):
    pass


def office_service(office: str) -> str:
    """CHAT_REVIEW_USER_KEYCHAIN_SERVICE as the installed office's fleet/config.py sets it, read as text."""
    path = os.path.join(office, "fleet", "config.py")
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError, ValueError):
        raise Failed(f"could not read {path}. Install the office from this clone first") from None
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ("CHAT_REVIEW_USER_KEYCHAIN_SERVICE", "CHAT_REVIEW_KEYCHAIN_SERVICE"):
                try:
                    found[node.targets[0].id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    service = found.get("CHAT_REVIEW_USER_KEYCHAIN_SERVICE")
    if not isinstance(service, str) or KEYCHAIN_SERVICE.fullmatch(service) is None:
        raise Failed(f"CHAT_REVIEW_USER_KEYCHAIN_SERVICE in {path} is not filled in yet")
    if service == found.get("CHAT_REVIEW_KEYCHAIN_SERVICE"):
        raise Failed(f"CHAT_REVIEW_USER_KEYCHAIN_SERVICE in {path} must differ from CHAT_REVIEW_KEYCHAIN_SERVICE")
    return service


def client_from(raw: bytes) -> tuple:
    """(client id, client secret) of a downloaded Desktop OAuth client file."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Failed("that file is not a client JSON") from None
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        raise Failed("that file is not a Desktop app OAuth client (it has no \"installed\" section)")
    client_id, secret = installed.get("client_id"), installed.get("client_secret")
    if not isinstance(client_id, str) or CLIENT_ID.fullmatch(client_id) is None:
        raise Failed("that client's client_id is not a Google OAuth client id")
    if not isinstance(secret, str) or CLIENT_SECRET.fullmatch(secret) is None:
        raise Failed("that client's client_secret is not one")
    return client_id, secret


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce() -> tuple:
    """(verifier, S256 challenge): the verifier never leaves this process except to the token endpoint."""
    verifier = _b64url(secrets.token_bytes(64))
    return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def auth_url(client_id: str, redirect: str, challenge: str, state: str) -> str:
    """The sign-in link: exactly the two read scopes, offline access so a refresh token comes back, and no earlier
    grant folded in (include_granted_scopes is never set)."""
    query = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
             "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
             "access_type": "offline", "prompt": "consent"}
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib's own signature
        return None


def post(url: str, form: dict) -> dict:
    """One form POST to Google's token or revoke endpoint, with no proxy and no redirect, and the JSON answered."""
    if url not in (TOKEN_URL, REVOKE_URL):
        raise Failed("this helper calls only Google's token endpoint")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                         urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode("ascii"),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as answer:
            raw = answer.read(RESPONSE_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise Failed(f"Google answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise Failed("Google could not be reached") from None
    try:
        data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (UnicodeDecodeError, ValueError):
        raise Failed("Google's answer is not JSON") from None
    if len(raw) > RESPONSE_MAX_BYTES or not isinstance(data, dict):
        raise Failed("Google's answer is not a JSON object")
    return data


def exchange(client: tuple, code: str, verifier: str, redirect: str, send=post) -> str:
    """The refresh token the code gives, once its grant is for exactly the two read scopes; any other grant is
    revoked and refused."""
    answer = send(TOKEN_URL, {"grant_type": "authorization_code", "code": code, "client_id": client[0],
                              "client_secret": client[1], "redirect_uri": redirect, "code_verifier": verifier})
    refresh, granted = answer.get("refresh_token"), answer.get("scope")
    if not isinstance(refresh, str) or REFRESH_TOKEN.fullmatch(refresh) is None:
        raise Failed("Google gave no refresh token. Remove the app's access in your Google account and run this again")
    if not isinstance(granted, str) or sorted(set(granted.split())) != sorted(SCOPES):
        try:
            send(REVOKE_URL, {"token": refresh})
        except Failed:
            pass
        raise Failed("the grant was not for exactly the two read-only Chat scopes (allow both, and check the consent"
                     " screen lists only them), so it was revoked and nothing was stored")
    return refresh


def blob(client: tuple, refresh: str) -> bytes:
    data = {"client_id": client[0], "client_secret": client[1], "refresh_token": refresh}
    return base64.b64encode(json.dumps(data, separators=(",", ":"), sort_keys=True).encode("ascii"))


def store(service: str, value: bytes, home: str, run=subprocess.run) -> None:
    """Add or replace the Keychain item through security -i on stdin, then read it back."""
    env = {"HOME": home, "PATH": "/usr/bin:/bin"}
    line = b" ".join([b"add-generic-password", b"-U", b"-s", service.encode("ascii"), b"-a", ACCOUNT.encode("ascii"),
                      b"-w", value]) + b"\n"
    try:
        added = run([SECURITY, "-i"], input=line, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                    timeout=TIMEOUT_SECONDS, check=False)
        if added.returncode != 0:
            raise Failed(f"security could not store it (exit {added.returncode}). Is your login Keychain unlocked?")
        read = run([SECURITY, "find-generic-password", "-s", service, "-a", ACCOUNT, "-w"], stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Failed(f"{SECURITY} did not run") from None
    if read.returncode != 0 or read.stdout.strip() != value:
        raise Failed("the authorization did not read back from your login Keychain as it was stored")


def listener(state: str):
    """An HTTP server on 127.0.0.1 alone, on a port the system picks, that keeps the first redirect back carrying this
    sign-in's state (any other request is answered and dropped) and logs nothing."""
    found: dict = {}

    class Back(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's own name
            if self.client_address[0] == "127.0.0.1" and not found:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                answer = {name: values[0] for name, values in query.items()
                          if name in ("code", "state", "error") and len(values) == 1}
                if secrets.compare_digest(answer.get("state", "").encode("utf-8"), state.encode("ascii")):
                    found.update(answer)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(DONE_PAGE)

        def log_message(self, *args):  # the request line holds the code, so nothing is logged
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Back)
    server.timeout = 1
    return server, found


def wait_for_code(server, found: dict, deadline: float) -> str:
    while not found and time.monotonic() < deadline:
        server.handle_request()
    if not found:
        raise Failed("no answer for this sign-in came back from the browser in time")
    code = found.get("code")
    if "error" in found or not isinstance(code, str) or CODE.fullmatch(code) is None:
        raise Failed("Google did not give a code (was the consent declined?)")
    return code


def main(argv: list) -> int:
    if len(argv) != 1:
        print("usage: /usr/bin/python3 -I -B scripts/chat-review-authorize.py <downloaded client.json>")
        return 2
    path = argv[0]
    home = os.path.expanduser("~")
    try:
        service = office_service(os.path.join(home, ".hogwarts"))
        print(f"   OK      the read-only authorization goes in Keychain service {service}")
        try:
            with open(path, "rb") as handle:
                raw = handle.read(CLIENT_MAX_BYTES + 1)
        except OSError:
            raise Failed(f"could not read {path}") from None
        if len(raw) > CLIENT_MAX_BYTES:
            raise Failed("that file is too large to be a client JSON")
        client = client_from(raw)
        verifier, challenge = pkce()
        state = secrets.token_urlsafe(32)
        server, found = listener(state)
        try:
            redirect = f"http://127.0.0.1:{server.server_address[1]}/"
            print("Open this link in your browser, sign in as yourself, and allow the two read-only Chat scopes:")
            print(auth_url(client[0], redirect, challenge, state))
            code = wait_for_code(server, found, time.monotonic() + WAIT_SECONDS)
        finally:
            server.server_close()
        store(service, blob(client, exchange(client, code, verifier, redirect)), home)
    except Failed as exc:
        print(f"   FAILED  {exc}")
        return 1
    print("   OK      the read-only authorization is in your login Keychain (an older one there was replaced)")
    print("The chat-review job uses it from its next pass. You can delete the downloaded client file.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
