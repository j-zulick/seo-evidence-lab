"""Bounded HTTPS transport. Connections use vetted addresses with normal TLS verification."""
import http.client
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


class RetrievalError(Exception):
    def __init__(self, reason, response=None):
        super().__init__(reason)
        self.response = response


@dataclass(frozen=True)
class Response:
    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""

    def values(self, name):
        return [value for key, value in self.headers if key.lower() == name]

    def header(self, name):
        values = self.values(name)
        if len(values) > 1:
            raise RetrievalError("ambiguous_" + name, self)
        return values[0] if values else None


def approved_host(url, hosts):
    if any(ord(char) < 33 or ord(char) > 126 for char in url) or any(c in url for c in "\\#"):
        raise RetrievalError("unsafe_url")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise RetrievalError("unsafe_url") from None
    host = parts.hostname
    if (parts.scheme != "https" or host not in hosts or parts.netloc != host
            or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host or "")):
        raise RetrievalError("unapproved_destination")
    return host


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError()
    return seconds


# Timed-out DNS calls cannot multiply without bound, even across ingestors.
_dns_slots = threading.BoundedSemaphore(8)


def resolve(host, deadline):
    if not _dns_slots.acquire(timeout=remaining(deadline)):
        raise TimeoutError()
    result = queue.Queue(maxsize=1)

    def lookup():
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError as exc:
            result.put(exc)
        finally:
            _dns_slots.release()

    threading.Thread(target=lookup, daemon=True).start()
    try:
        addresses = result.get(timeout=remaining(deadline))
    except queue.Empty:
        raise TimeoutError() from None
    if isinstance(addresses, OSError):
        raise addresses
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo
                                        or ip in ipaddress.ip_network("64:ff9b::/96")
                                        or ip in ipaddress.ip_network("64:ff9b:1::/48")))):
            raise RetrievalError("unsafe_address")
    if not addresses:
        raise RetrievalError("dns_empty")
    return addresses


def check_body(response, max_bytes):
    encoding = response.header("content-encoding")
    if encoding is not None and encoding.strip().lower() != "identity":
        raise RetrievalError("unsupported_content_encoding", response)
    length = response.header("content-length")
    transfer = response.header("transfer-encoding")
    if transfer is not None and (transfer.strip().lower() != "chunked" or length is not None):
        raise RetrievalError("invalid_framing", response)
    if length is not None:
        if not re.fullmatch(r"[0-9]{1,18}", length.strip()):
            raise RetrievalError("invalid_content_length", response)
        if int(length) > max_bytes:
            raise RetrievalError("body_too_large", response)
    if response.header("content-range") is not None:
        raise RetrievalError("partial_content", response)
    return int(length) if length is not None else None


def read_chunked(stream, limit, metadata):
    body = bytearray()
    for _ in range(65536):
        line = stream.readline(4097)
        size = re.fullmatch(rb"([0-9a-fA-F]{1,16})(?:;[^\r\n]*)?\r\n", line)
        if not size or len(line) > 4096:
            raise RetrievalError("invalid_chunk_framing", metadata)
        count = int(size[1], 16)
        if count > limit - len(body):
            raise RetrievalError("body_too_large", metadata)
        if count == 0:
            for _ in range(101):
                trailer = stream.readline(4097)
                if trailer == b"\r\n":
                    return bytes(body)
                if len(trailer) > 4096 or not trailer.endswith(b"\r\n"):
                    break
            raise RetrievalError("invalid_chunk_trailer", metadata)
        data = stream.read(count)
        if len(data) != count or stream.read(2) != b"\r\n":
            raise RetrievalError("truncated_body", metadata)
        body.extend(data)
    raise RetrievalError("too_many_chunks", metadata)


def https_get(url, headers, deadline, max_bytes):
    parts = urlsplit(url)
    context = ssl.create_default_context()
    # Pin the socket to a vetted address without losing SNI or hostname validation.
    family, kind, proto, _, address = resolve(parts.hostname, deadline)[0]
    metadata = None
    with socket.socket(family, kind, proto) as raw:
        raw.settimeout(remaining(deadline))
        raw.connect(address)
        with context.wrap_socket(raw, server_hostname=parts.hostname,
                                 do_handshake_on_connect=False) as tls:
            def abort():
                try:
                    tls.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = threading.Timer(remaining(deadline), abort)
            timer.daemon = True
            timer.start()
            connection = http.client.HTTPConnection(parts.hostname)
            connection.sock = tls
            try:
                tls.do_handshake()
                target = parts.path or "/"
                if parts.query:
                    target += "?" + parts.query
                connection.request("GET", target, headers=headers)
                with connection.getresponse() as reply:
                    metadata = Response(reply.status, tuple(reply.getheaders()))
                    remaining(deadline)
                    if reply.status != 200:
                        return metadata
                    length = check_body(metadata, max_bytes)
                    if length is None and metadata.header("transfer-encoding") is None:
                        raise RetrievalError("unframed_body", metadata)
                    body = (read_chunked(reply.fp, max_bytes, metadata) if length is None
                            else reply.read(max_bytes + 1))
                    remaining(deadline)
                    if len(body) > max_bytes:
                        raise RetrievalError("body_too_large", metadata)
                    if length is not None and len(body) != length:
                        raise RetrievalError("truncated_body", metadata)
                    return Response(metadata.status, metadata.headers, body)
            except http.client.IncompleteRead:
                reason = "timeout" if time.monotonic() >= deadline else "truncated_body"
                raise RetrievalError(reason, metadata) from None
            except (OSError, http.client.HTTPException) as exc:
                if isinstance(exc, TimeoutError) or time.monotonic() >= deadline:
                    reason = "timeout"
                else:
                    reason = "transport_error"
                raise RetrievalError(reason, metadata) from None
            finally:
                timer.cancel()
                connection.close()
