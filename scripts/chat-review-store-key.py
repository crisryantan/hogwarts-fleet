"""Store the chat review worker's service account key in your login Keychain, or replace it when you rotate it.

Run it yourself in Terminal, from your clone of the kit, right after you download the worker service account's JSON key
from Google Cloud:
  /usr/bin/python3 -I -B scripts/chat-review-store-key.py <downloaded key.json>

It reads CHAT_REVIEW_SERVICE_ACCOUNT and CHAT_REVIEW_KEYCHAIN_SERVICE from the installed office's fleet/config.py
(~/.hogwarts), without running it, and refuses a key whose client_email is not that service account, so no other
account's key can land here by mistake. It stores the private key's PKCS#8 body (base64) as a generic
password in your login Keychain, service CHAT_REVIEW_KEYCHAIN_SERVICE and account the service account's email, by
driving /usr/bin/security -i over its stdin: the key is never in any process's argv, a file this writes, or its
output. An item already there is replaced (-U), which is how a rotated key goes in. It then reads the item back to
check it, and tells you to delete the downloaded file. It prints OK or FAILED and exits 1 on any failure.
"""
import ast
import base64
import json
import os
import re
import shlex
import subprocess
import sys

SECURITY = "/usr/bin/security"
KEY_MAX_BYTES = 16384
TIMEOUT_SECONDS = 30
SERVICE_ACCOUNT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com")
KEYCHAIN_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
PEM = re.compile(r"-----BEGIN PRIVATE KEY-----\n?([A-Za-z0-9+/=\n]+?)\n?-----END PRIVATE KEY-----\n?")
BODY = re.compile(rb"[A-Za-z0-9+/]{400,8000}={0,2}")


class Failed(Exception):
    pass


def office_settings(office: str) -> tuple:
    """(Keychain service, service account email) as the installed office's fleet/config.py sets them, read as text."""
    path = os.path.join(office, "fleet", "config.py")
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError, ValueError):
        raise Failed(f"could not read {path}. Install the office from this clone first") from None
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ("CHAT_REVIEW_KEYCHAIN_SERVICE", "CHAT_REVIEW_SERVICE_ACCOUNT"):
                try:
                    found[node.targets[0].id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    service, account = found.get("CHAT_REVIEW_KEYCHAIN_SERVICE"), found.get("CHAT_REVIEW_SERVICE_ACCOUNT")
    if not isinstance(service, str) or KEYCHAIN_SERVICE.fullmatch(service) is None:
        raise Failed(f"CHAT_REVIEW_KEYCHAIN_SERVICE in {path} is not filled in yet")
    if not isinstance(account, str) or SERVICE_ACCOUNT.fullmatch(account) is None:
        raise Failed(f"CHAT_REVIEW_SERVICE_ACCOUNT in {path} is not filled in yet")
    return service, account


def key_body(raw: bytes, account: str) -> bytes:
    """The base64 PKCS#8 body of the key file's private key, once the file is that service account's JSON key."""
    try:
        key = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Failed("that file is not a JSON key") from None
    if not isinstance(key, dict) or key.get("type") != "service_account":
        raise Failed("that file is not a service account key")
    if key.get("client_email") != account:
        raise Failed("that key is not for CHAT_REVIEW_SERVICE_ACCOUNT, the worker service account")
    match = PEM.fullmatch(key.get("private_key", "")) if isinstance(key.get("private_key"), str) else None
    body = match.group(1).replace("\n", "").encode("ascii") if match else b""
    try:
        base64.b64decode(body, validate=True)
    except ValueError:
        body = b""
    if BODY.fullmatch(body) is None:
        raise Failed("that key's private_key is not a PKCS#8 RSA key")
    return body


def store(service: str, account: str, body: bytes, home: str, run=subprocess.run) -> None:
    """Add or replace the Keychain item through security -i on stdin, then read it back."""
    env = {"HOME": home, "PATH": "/usr/bin:/bin"}
    line = b" ".join([b"add-generic-password", b"-U", b"-s", service.encode("ascii"), b"-a", account.encode("ascii"),
                      b"-w", body]) + b"\n"
    try:
        added = run([SECURITY, "-i"], input=line, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                    timeout=TIMEOUT_SECONDS, check=False)
        if added.returncode != 0:
            raise Failed(f"security could not store the key (exit {added.returncode}). Is your login Keychain unlocked?")
        read = run([SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"], stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Failed(f"{SECURITY} did not run") from None
    if read.returncode != 0 or read.stdout.strip() != body:
        raise Failed("the key did not read back from your login Keychain as it was stored")


def main(argv: list) -> int:
    if len(argv) != 1:
        print("usage: /usr/bin/python3 -I -B scripts/chat-review-store-key.py <downloaded key.json>")
        return 2
    path = argv[0]
    home = os.path.expanduser("~")
    try:
        service, account = office_settings(os.path.join(home, ".hogwarts"))
        print(f"   OK      the worker service account is {account}, stored under Keychain service {service}")
        try:
            with open(path, "rb") as handle:
                raw = handle.read(KEY_MAX_BYTES + 1)
        except OSError:
            raise Failed(f"could not read {path}") from None
        if len(raw) > KEY_MAX_BYTES:
            raise Failed("that file is too large to be a service account key")
        store(service, account, key_body(raw, account), home)
    except Failed as exc:
        print(f"   FAILED  {exc}")
        return 1
    print("   OK      the key is in your login Keychain (an older one there was replaced)")
    print(f"Now delete the downloaded key file yourself: rm {shlex.quote(path)}")
    print("If this was a rotation, delete the old key in Google Cloud too (IAM, Service Accounts, the account, Keys).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
