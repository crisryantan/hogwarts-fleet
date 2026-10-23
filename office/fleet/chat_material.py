"""What a teammate attaches to a chat turn, read by the chat-review job and never by the model: the images attached to
the Chat message, the GitHub gists it links and the public web pages it links. Each goes to the turn (a review, a
question about a review, or a mention answer) as untrusted data, between marker lines carrying the turn's nonce, and
nothing a turn writes is checked against less: an answer or finding that repeats a long run of it is withheld.

- Images: at most IMAGE_MAX of the message's uploaded attachments, each downloaded with the app's read token through
  Chat's media download (google_chat.download_media; chat.messages.readonly covers it), at most IMAGE_MAX_BYTES, and
  kept only when its first bytes are a PNG, JPEG, GIF or WebP, whatever its name or claimed type says. They go in the
  turn's work folder under names of the job's own. config.CHAT_REVIEW_IMAGES off downloads none.
- Gists: at most GIST_MAX links to gist.github.com or gist.githubusercontent.com, each read with gh api gists/<id>
  (gitops.check_read_argv's gist shape), only their text files, at most GIST_TEXT_MAX characters in all.
- Web pages: at most LINK_MAX other links, never a GitHub PR link, fetched with GET only: https on port 443 to a host
  name (never an address), no cookies, credentials or proxy, a timeout and PAGE_MAX_BYTES read, only text-like
  content, HTML reduced to its text. The host is resolved here and refused when any address it has is loopback,
  private, link-local, shared (CGNAT), multicast, reserved or otherwise not public, IPv4 or IPv6; the connection goes
  to the address that was checked, with the certificate checked for the host name, and at most REDIRECT_MAX redirects
  are followed, each hop checked by the same rules.
A read that fails is a note in the input ("could not read that link"), never the end of the turn, and only counts and
outcomes are ever logged: never a link, its query string, a page's text or an image.
"""
from __future__ import annotations

import contextlib
import html.parser
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.parse
from typing import NamedTuple, Optional

from hogwarts.errors import StoreError

from fleet import common, config, gitops, google_chat
from fleet.safefs import FleetError

IMAGE_MAX = 3
IMAGE_MAX_BYTES = 5 * 1024 * 1024
GIST_MAX = 3
GIST_TEXT_MAX = 100_000
LINK_MAX = 3
PAGE_MAX_BYTES = 300 * 1024
# What one page's text may hold once its HTML is reduced, and all the material's text together.
PAGE_TEXT_MAX = 30_000
TEXT_MAX = 120_000
REDIRECT_MAX = 3
FETCH_TIMEOUT_SECONDS = 10
# The whole of one link's fetch, every hop's name lookup, connection, headers and body together, ends by this: a server
# that answers a byte at a time never holds the job past it.
LINK_DEADLINE_SECONDS = 20
URL_MAX = 2000
# The kinds of image kept, by their first bytes, and the name each gets in the work folder.
IMAGE_KINDS = (("png", (b"\x89PNG\r\n\x1a\n",)), ("jpg", (b"\xff\xd8\xff",)), ("gif", (b"GIF87a", b"GIF89a")))
IMAGE_NAME = re.compile(r"image-[1-9]\.(?:png|jpg|gif|webp)")
# A host name a link may name: lowercase labels with a dot, the last starting with a letter, so no address in any
# spelling (1.2.3.4, 2130706433, 0x7f.1) and no single-label name (localhost) passes.
HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
GIST_ID = gitops.GIST_ID
GIST_PAGE = re.compile(r"https://gist\.github\.com/(?:[A-Za-z0-9-]{1,39}/)?([0-9a-f]{1,64})(?:[/#?]\S*)?",
                       re.IGNORECASE)
GIST_RAW = re.compile(r"https://gist\.githubusercontent\.com/[A-Za-z0-9-]{1,39}/([0-9a-f]{1,64})(?:/\S*)?",
                      re.IGNORECASE)
GIST_HOSTS = ("gist.github.com", "gist.githubusercontent.com")
# A PR link is a review request, never a page to read.
PR_LINK = re.compile(r"https?://(?:www\.)?github\.com/[^/\s]+/[^/\s]+/pull/", re.IGNORECASE)
ADDRESS = re.compile(r"https?://[^\s<>|]+", re.IGNORECASE)
MARKUP = re.compile(r"<(https?://[^\s<>|]*)(?:\|[^<>\n]*)?>", re.IGNORECASE)
WORD = re.compile(MARKUP.pattern + "|" + ADDRESS.pattern, re.IGNORECASE)
# A host written with no scheme, "docs.example.com" or "example.org/guide", read as an https link. Only common web
# endings count, so a file name such as "build.py" or "node.js" is never taken for one.
BARE_TLDS = ("com", "org", "net", "io", "dev", "app", "ai", "co", "edu", "gov", "info", "me", "page", "site", "tech",
             "us", "uk", "au", "ca", "de", "fr", "jp", "nz", "sg", "in")
# The whole host must end there (never "example.com.internal" read as "example.com"), and a port, path, query or
# fragment right after it is kept, so the guard checks the link as written.
BARE_HOST = re.compile(r"(?<![\w@./:-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:" + "|".join(BARE_TLDS)
                       + r"))(?![\w-]|\.[a-z0-9])((?::[0-9]+)?(?:[/?#][^\s<>|]*)?)", re.IGNORECASE)
RESOURCE = re.compile(r"[A-Za-z0-9._~+/=-]{1,1000}")
# A gist file of one of these types is no text, whatever its content says.
NOT_TEXT = ("image/", "audio/", "video/", "font/", "application/octet-stream", "application/pdf", "application/zip",
            "application/gzip", "application/x-tar")
# Content a page may be read as: text, and the structured text formats.
TEXT_TYPES = ("application/json", "application/xml", "application/xhtml+xml", "application/javascript",
              "application/x-yaml", "application/yaml", "application/toml")
REDIRECTS = (301, 302, 303, 307, 308)
COULD_NOT_LINK = "could not read that link"
COULD_NOT_GIST = "could not read that gist"
COULD_NOT_IMAGE = "could not read that image"
USER_AGENT = "chat-review-reader/1"


class Refused(FleetError):
    """A link, a hop or an answer the reader will not take. Its text names the rule, never the link."""


class Gathered(NamedTuple):
    """What a turn gets: the lines of its fenced material block ([] when nothing was attached), the images for its
    work folder by name, the fetched text the echo guard reads, and the counts and outcomes for the log."""
    lines: list
    images: dict
    untrusted: str
    counts: dict


NOTHING = Gathered([], {}, "", {})


# What a message attached


def plan(text: object, attachments: object = ()) -> dict:
    """The material a message attaches, for its record until its turn runs: the resource names of at most IMAGE_MAX
    uploaded images, at most GIST_MAX gist ids and at most LINK_MAX other links, each once, in the order given, and
    how many more of each there were. A GitHub PR link is no material. {} when there is none."""
    images = [name for name in (attachments if isinstance(attachments, (list, tuple)) else ())
              if isinstance(name, str) and RESOURCE.fullmatch(name) and ".." not in name]
    gists, links = [], []
    for url in _addresses(text if isinstance(text, str) else ""):
        if PR_LINK.match(url):
            continue
        gist = _gist_id(url)
        if gist is not None:
            if gist not in gists:
                gists.append(gist)
        elif _host_of(url) in GIST_HOSTS:
            continue  # a gist host with no gist in it: nothing to read there
        elif url not in links:
            links.append(url)
    found = {"images": _unique(images)[:IMAGE_MAX], "gists": gists[:GIST_MAX], "links": links[:LINK_MAX]}
    more = {"images": len(_unique(images)) - IMAGE_MAX, "gists": len(gists) - GIST_MAX, "links": len(links) - LINK_MAX}
    found["more"] = {kind: count for kind, count in more.items() if count > 0}
    return found if any(found[kind] for kind in ("images", "gists", "links")) or found["more"] else {}


def _unique(items: list) -> list:
    seen: list = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _addresses(text: str) -> list:
    """Each address in text, in its order, Chat's <url|label> markup taken as its url, with the marks around a word
    let go."""
    found = [(match.start(), match.group(1) or match.group(0)) for match in WORD.finditer(text)]
    # An http link is read over https, the only scheme the reader fetches; a bare host the same way, in its place in
    # the message, with every written link blanked first so none is read twice.
    found = [(at, "https://" + url[len("http://"):] if url.lower().startswith("http://") else url) for at, url in found]
    bare = WORD.sub(lambda match: " " * len(match.group(0)), text)
    found += [(match.start(), "https://" + match.group(1) + match.group(2)) for match in BARE_HOST.finditer(bare)]
    urls = [url.rstrip(")]>\"'.,;:!?") for _, url in sorted(found, key=lambda pair: pair[0])]
    return [url for url in urls if url]


def _host_of(url: str) -> Optional[str]:
    try:
        return urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None


def _gist_id(url: str) -> Optional[str]:
    for pattern in (GIST_RAW, GIST_PAGE):
        match = pattern.fullmatch(url)
        if match is not None:
            return match.group(1).lower()
    return None


def image_kind(raw: bytes) -> Optional[str]:
    """png, jpg, gif or webp by the file's first bytes, else None. Its name and claimed type are never read."""
    for kind, magics in IMAGE_KINDS:
        if any(raw.startswith(magic) for magic in magics):
            return kind
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


# Reading it


def gather(material: object, app: str, nonce: str) -> Gathered:
    """Read what material (plan) names, for app's turn: its images with app's read token, its gists with gh and its
    links with the guarded fetch. A read that fails is noted, never raised."""
    if not isinstance(material, dict) or not material:
        return NOTHING
    notes, texts, images, counts = [], [], {}, {}
    _images(material.get("images"), app, images, notes, counts)
    for number, gist in enumerate(_listed(material.get("gists"), GIST_ID)[:GIST_MAX], 1):
        try:
            texts.append((f"GIST {number}", _gist_text(gist)))
            counts["gists"] = counts.get("gists", 0) + 1
        except (FleetError, StoreError, ValueError, RecursionError):
            notes.append(f"GIST {number}: {COULD_NOT_GIST}")
            counts["gists_failed"] = counts.get("gists_failed", 0) + 1
    links = [url for url in (material.get("links") if isinstance(material.get("links"), list) else [])
             if isinstance(url, str)][:LINK_MAX]
    for number, url in enumerate(links, 1):
        try:
            host, text = fetch_page(url)
            texts.append((f"LINK {number} ({host})", text))
            counts["links"] = counts.get("links", 0) + 1
        except (FleetError, OSError, ValueError, http.client.HTTPException):
            notes.append(f"LINK {number}: {COULD_NOT_LINK}")
            counts["links_failed"] = counts.get("links_failed", 0) + 1
    more = material.get("more") if isinstance(material.get("more"), dict) else {}
    for kind in ("images", "gists", "links"):
        if type(more.get(kind)) is int and more[kind] > 0:
            notes.append(f"{more[kind]} more {kind} not read: at most {LIMITS[kind]} are")
    return _gathered(texts, images, notes, counts, nonce)


LIMITS = {"images": IMAGE_MAX, "gists": GIST_MAX, "links": LINK_MAX}


def _listed(items: object, pattern: re.Pattern) -> list:
    return [item for item in (items if isinstance(items, list) else []) if isinstance(item, str)
            and pattern.fullmatch(item)]


def _images(resources: object, app: str, images: dict, notes: list, counts: dict) -> None:
    names = [item for item in (resources if isinstance(resources, list) else []) if isinstance(item, str)
             and RESOURCE.fullmatch(item) and ".." not in item][:IMAGE_MAX]
    if not names:
        return
    if config.CHAT_REVIEW_IMAGES is not True:
        notes.append(f"{len(names)} attached image(s) not read: image reading is switched off")
        counts["images_off"] = len(names)
        return
    for number, resource in enumerate(names, 1):
        try:
            raw = google_chat.download_media(resource, google_chat.read_token(app), IMAGE_MAX_BYTES)
            kind = image_kind(raw)
            if kind is None:
                raise Refused("not a png, jpeg, gif or webp image")
            images[f"image-{number}.{kind}"] = raw
            counts["images"] = counts.get("images", 0) + 1
        except (FleetError, ValueError):
            notes.append(f"IMAGE {number}: {COULD_NOT_IMAGE}")
            counts["images_failed"] = counts.get("images_failed", 0) + 1


def fences(nonce: str) -> tuple:
    return f"<<<UNTRUSTED ATTACHED MATERIAL {nonce}>>>", f"<<<END OF UNTRUSTED ATTACHED MATERIAL {nonce}>>>"


def _gathered(texts: list, images: dict, notes: list, counts: dict, nonce: str) -> Gathered:
    """The fenced block: the images by name, then each text with its label, then the notes, the texts normalized and
    scrubbed (common.untrusted_text) and held to TEXT_MAX in all."""
    opening, closing = fences(nonce)
    lines, kept, room = [opening], [], TEXT_MAX
    if images:
        lines.append("IMAGES attached to the message, in your working directory: " + ", ".join(sorted(images)))
    for label, text in texts:
        cleaned = common.untrusted_text(text)
        cut = cleaned[:room]
        room -= len(cut)
        lines += [label, cut + ("\n(cut here: the material is longer than a chat turn reads)" if len(cut) < len(cleaned)
                                else "")]
        kept.append(cut)
    lines += [f"NOTE {note}" for note in notes]
    lines.append(closing)
    return Gathered(lines, images, "\n".join(kept), counts)


def _gist_text(gist: str) -> str:
    """The text files of one gist, read with gh api gists/<id>, each under its name, at most GIST_TEXT_MAX in all."""
    data = common.strict_json(gitops.run_gh_read(gitops.gist_read_argv(gist)))
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(data.get("id"), str) or data["id"].lower() != gist or not isinstance(files, dict):
        raise Refused("GitHub's answer is not that gist")
    parts, size = [], 0
    for name in sorted(files):
        item = files[name]
        content = item.get("content") if isinstance(item, dict) else None
        kind = item.get("type") if isinstance(item, dict) else None
        if not isinstance(content, str) or item.get("truncated") is True or "\x00" in content \
                or not isinstance(kind, str) or kind.lower().startswith(NOT_TEXT):
            parts.append(f"FILE {common.one_line(name, 100)}: not read (not a whole text file)")
            continue
        piece = content[:max(GIST_TEXT_MAX - size, 0)]
        size += len(piece)
        parts += [f"FILE {common.one_line(name, 100)}", piece]
        if size >= GIST_TEXT_MAX:
            parts.append("(cut here: the gist is longer than a chat turn reads)")
            break
    if not parts:
        raise Refused("the gist has no files")
    return "\n".join(parts)


def _text_type(value: object) -> bool:
    if not isinstance(value, str):
        return False
    kind = value.split(";", 1)[0].strip().lower()
    return kind.startswith("text/") or kind in TEXT_TYPES or kind.endswith(("+json", "+xml"))


# The guarded fetch


def check_url(url: object) -> tuple:
    """(host, request target) of a link the reader may fetch: https, port 443 or none, no user or password, a host name
    (HOST) and never an address, printable ASCII only. Refused for anything else."""
    if not isinstance(url, str) or not url or len(url) > URL_MAX or not url.isascii() \
            or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise Refused("not a plain link")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https":
        raise Refused("only https is read")
    try:
        port = parts.port
    except ValueError:
        raise Refused("not a port") from None
    if port not in (None, 443):
        raise Refused("only port 443 is read")
    host = (parts.hostname or "").lower()
    if parts.username is not None or parts.password is not None \
            or parts.netloc.lower() not in (host, f"{host}:443"):
        raise Refused("a link with credentials or an odd host part")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise Refused("an address, not a host name")
    if HOST.fullmatch(host) is None:
        raise Refused("not a host name")
    return host, (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def public(address: str) -> bool:
    """Whether an address a host resolved to is a public one: not loopback, private, link-local, shared (CGNAT),
    multicast, reserved, unspecified or otherwise not global, nor an IPv6 address that carries an IPv4 one (mapped,
    6to4, Teredo, NAT64), which could carry any of those."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved \
            or ip.is_unspecified or not ip.is_global:
        return False
    if ip.version == 4:
        return ip not in ipaddress.ip_network("100.64.0.0/10")
    carrying = ("::ffff:0:0/96", "2002::/16", "2001::/32", "64:ff9b::/96", "64:ff9b:1::/48", "fec0::/10")
    return not any(ip in ipaddress.ip_network(net) for net in carrying)


def vetted(host: str, deadline: float) -> str:
    """The address to connect to for host: the first it resolves to, when every one it resolves to is public, looked up
    by the deadline (time.monotonic)."""
    addresses = _resolve(host, deadline)
    if not addresses or not all(public(address) for address in addresses):
        raise Refused("the host resolves to an address that is not public")
    return addresses[0]


def _resolve(host: str, deadline: float) -> list:
    """Every address host resolves to, for TCP on 443, looked up in a thread of its own so a slow resolver never holds
    the job past the deadline. Tests replace this."""
    found: list = []

    def look() -> None:
        try:
            found.append(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP))
        except (OSError, UnicodeError):
            pass

    worker = threading.Thread(target=look, daemon=True)
    worker.start()
    worker.join(max(deadline - time.monotonic(), 0))
    if not found:
        raise Refused("the host does not resolve in time")
    return _unique([item[4][0] for item in found[0] if item[4]])


class _Pinned(http.client.HTTPSConnection):
    """An HTTPS connection to one checked address, its certificate checked for the host name. http.client reads no
    proxy setting and sends no cookie. It keeps its own socket (raw) for _cut, since http.client lets go of sock once an
    answer says Connection: close, while that answer still reads from it."""

    def __init__(self, host: str, address: str, timeout: float = FETCH_TIMEOUT_SECONDS) -> None:
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._address = address
        self.raw: Optional[socket.socket] = None

    def connect(self) -> None:
        self.sock = self.raw = self._open()

    def _open(self) -> socket.socket:
        self.raw = socket.create_connection((self._address, 443), self.timeout)
        return self._context.wrap_socket(self.raw, server_hostname=self.host)


def _cut(connection: _Pinned) -> None:
    """End a connection that ran past its deadline, through the socket it keeps, so a blocked connect, handshake,
    header, body or trailer read on it returns at once."""
    sock = getattr(connection, "raw", None) or connection.sock
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


def _get(host: str, address: str, target: str, deadline: float) -> tuple:
    """(status, headers with lowercase names, body of at most PAGE_MAX_BYTES + 1 bytes) of one GET of target from host
    at address, with no body read for a redirect, all of it by the deadline (time.monotonic): a timer cuts the
    connection then, however the server drips its headers or body. Tests replace this."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise Refused("the link took too long")
    connection = _Pinned(host, address, min(FETCH_TIMEOUT_SECONDS, left))
    timer = threading.Timer(left, _cut, (connection,))
    timer.daemon = True
    timer.start()
    try:
        connection.request("GET", target, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                                                   "Accept": "text/html, text/plain, application/json;q=0.9",
                                                   "Connection": "close"})
        answer = connection.getresponse()
        headers = {name.lower(): value for name, value in answer.getheaders()}
        if answer.status in REDIRECTS:
            return answer.status, headers, b""
        chunks, total = [], 0
        while total <= PAGE_MAX_BYTES:
            chunk = answer.read1(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if time.monotonic() >= deadline:  # cut by the timer: what came is not the whole page
            raise Refused("the link took too long")
        return answer.status, headers, b"".join(chunks)
    finally:
        timer.cancel()
        connection.close()


def fetch_page(url: str) -> tuple:
    """(host, text) of one public web page: the guarded GET (check_url, vetted), following at most REDIRECT_MAX
    redirects with each hop checked the same way and none visited twice, a 200 answer of a text-like type only, read
    to PAGE_MAX_BYTES, its HTML reduced to text, all of it within LINK_DEADLINE_SECONDS. Refused for anything else."""
    seen, first = [], check_url(url)[0]
    deadline = time.monotonic() + LINK_DEADLINE_SECONDS
    for _ in range(REDIRECT_MAX + 1):
        host, target = check_url(url)
        if url in seen:
            raise Refused("a redirect loop")
        seen.append(url)
        status, headers, body = _get(host, vetted(host, deadline), target, deadline)
        if status in REDIRECTS:
            location = headers.get("location")
            if not isinstance(location, str) or not location:
                raise Refused("a redirect to nowhere")
            url = urllib.parse.urljoin(url, location.strip()).split("#", 1)[0]
            continue
        if status != 200:
            raise Refused(f"the page answered {status}")
        kind = headers.get("content-type", "")
        if not _text_type(kind):
            raise Refused("not text")
        if headers.get("content-encoding", "identity").strip().lower() != "identity":
            raise Refused("an encoded answer, though none was asked for")
        return first, _page_text(body[:PAGE_MAX_BYTES], kind)
    raise Refused("too many redirects")


def _page_text(body: bytes, kind: str) -> str:
    charset = re.search(r"charset=([A-Za-z0-9._-]{1,40})", kind, re.IGNORECASE)
    try:
        text = body.decode(charset.group(1) if charset else "utf-8", "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    if kind.split(";", 1)[0].strip().lower() in ("text/html", "application/xhtml+xml"):
        reader = _Text()
        try:
            reader.feed(text)
            reader.close()
        except (AssertionError, AttributeError, ValueError):  # markup the parser gives up on
            raise Refused("the page's markup could not be read") from None
        text = reader.text()
    return text.replace("\x00", "")[:PAGE_TEXT_MAX]


class _Text(html.parser.HTMLParser):
    """A page's visible text: everything outside script, style and the like, its whitespace collapsed by line."""

    SKIPPED = {"script", "style", "noscript", "template", "svg", "iframe", "object", "head"}
    BREAKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "section", "article", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self.skipping = 0
        self.title: list = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "title":
            self.in_title = True
        elif tag in self.SKIPPED:
            self.skipping += 1
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag in self.SKIPPED and self.skipping:
            self.skipping -= 1
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
        elif not self.skipping:
            self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).split("\n")]
        body = "\n".join(line for line in lines if line)
        title = " ".join("".join(self.title).split())
        return f"TITLE {title}\n{body}" if title else body
