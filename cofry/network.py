import ipaddress
import re
import socket
import socketserver
import select
import threading
from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import urllib3

from .config import Problem


def validate(url):
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)
                or any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url):
            raise ValueError
        return parts
    except ValueError:
        raise Problem("Informe um link HTTPS público válido.") from None


def public_ip(host):
    addresses = [row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
    def supported(value):
        ip = ipaddress.ip_address(value)
        return ip.is_global and not ip.is_multicast and (
            ip.version == 4 or ip in ipaddress.ip_network("2000::/3")
            and ip.sixtofour is None and ip.teredo is None
        )

    if not addresses or not all(map(supported, addresses)):
        raise Problem("O link deve apontar para um endereço público.")
    return addresses[0]


@contextmanager
def request(url, method="GET", body=None, headers=None, redirects=0, read_timeout=25):
    headers = headers or {}
    for step in range(redirects + 1):
        parts = validate(url)
        host = parts.hostname.encode("idna").decode("ascii")
        ip = public_ip(host)
        pool = urllib3.HTTPSConnectionPool(
            ip, port=443, assert_hostname=host, server_hostname=host,
            timeout=urllib3.Timeout(connect=10, read=read_timeout), retries=False,
        )
        response = None
        try:
            path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
            response = pool.urlopen(method, path, body=body, headers={**headers, "Host": host},
                                    redirect=False, preload_content=False)
            if response.status in {301, 302, 303, 307, 308} and step < redirects:
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            yield response
            return
        finally:
            if response is not None:
                response.close()
            pool.close()


def read_limited(response, limit):
    raw = response.read(limit + 1, decode_content=False)
    if len(raw) > limit:
        raise Problem("A resposta do serviço excedeu o limite.")
    return raw


def normalize(url):
    parts = validate(url)
    query = parse_qs(parts.query)
    if parts.hostname == "drive.google.com":
        path = parts.path.split("/")
        file_id = path[3] if path[:3] == ["", "file", "d"] and len(path) > 3 else query.get("id", [""])[0]
        if not file_id or not file_id.replace("-", "").replace("_", "").isalnum():
            raise Problem("Link do Google Drive inválido.")
        return "https://drive.usercontent.google.com/download?" + urlencode(
            {"id": file_id, "export": "download", "confirm": "t"}
        )
    if parts.hostname in {"dropbox.com", "www.dropbox.com"}:
        query.pop("raw", None)
        query["dl"] = ["1"]
        return parts._replace(query=urlencode(query, doseq=True), fragment="").geturl()
    return url


class Tunnel(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(8)
        try:
            method, authority, _ = self.rfile.readline(4096).decode("ascii").strip().split()
            host, port = authority.rsplit(":", 1)
            host = host.lower()
            allowed = ("youtube.com", "googlevideo.com", "youtubei.googleapis.com", "gstatic.com")
            if method != "CONNECT" or port != "443" or not any(
                host == suffix or host.endswith("." + suffix) for suffix in allowed
            ):
                raise ValueError
            for _ in range(40):
                line = self.rfile.readline(4096)
                if line == b"\r\n":
                    break
            else:
                raise ValueError
            ip = public_ip(host)
            upstream = socket.create_connection((ip, 443), timeout=10)
        except (ValueError, UnicodeError, OSError, Problem):
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return
        with upstream:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            try:
                sockets = (self.connection, upstream)
                while ready := select.select(sockets, [], [], 25)[0]:
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        destination = upstream if source is self.connection else self.connection
                        destination.sendall(data)
            except OSError:
                return


@contextmanager
def youtube_proxy():
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Tunnel) as server:
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            thread.join()


def youtube_url(url):
    parts = validate(url)
    segments = parts.path.strip("/").split("/")
    if parts.hostname == "youtu.be":
        video_id = segments[0]
    elif segments[0] in {"shorts", "live", "embed"} and len(segments) == 2:
        video_id = segments[1]
    elif segments == ["watch"]:
        video_id = parse_qs(parts.query).get("v", [""])[0]
    else:
        video_id = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise Problem("Não foi possível baixar este link. Envie o arquivo.")
    return "https://www.youtube.com/watch?v=" + video_id
