"""Bounded STEP downloads. Provider credentials never reach storage hosts."""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import cad

MAX_BYTES = 32 * 1024 * 1024


def filename(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip(".-")[:100]
    return (stem or "Onshape-export") + ".step"


def descriptors(result: dict) -> list[dict]:
    if result.get("requestState") != "DONE":
        return []
    tid = cad.cad_id(result["id"])
    return [
        {"translation_id": tid, "file_index": i,
         "filename": filename(result.get("name") or "Onshape-export"),
         "download_path": f"/api/exports/{tid}/{i}"}
        for i, _ in enumerate(result.get("resultExternalDataIds") or [])
    ]


def credential_target(url: str, base: str) -> bool:
    """Only the configured Onshape origin gets provider credentials.

    Presigned AWS storage may be followed without any provider auth. All other
    cross-origin redirects fail closed, including other Onshape subdomains.
    """
    u, b = urlsplit(url), urlsplit(base)
    if u.scheme != "https" or u.username or u.password or u.fragment or u.port not in (None, 443):
        raise ValueError("Unsafe export redirect rejected.")
    if u.hostname == b.hostname and u.port == b.port:
        return True
    if ((u.hostname or "").endswith(".amazonaws.com")
            and ("X-Amz-Signature" in parse_qs(u.query) or "Signature" in parse_qs(u.query))):
        return False
    raise ValueError(f"Export redirected to unsupported host {u.hostname}; no credentials were forwarded.")


def validate_step(data: bytes) -> dict:
    """Envelope check, not a geometric-kernel certification."""
    stripped = data.strip()
    if stripped.startswith(b"PK"):
        raise ValueError("Onshape returned a ZIP archive instead of a plain STEP file.")
    if (not stripped.startswith(b"ISO-10303-21;")
            or not stripped.endswith(b"END-ISO-10303-21;")
            or b"FILE_SCHEMA" not in stripped or b"DATA;" not in stripped):
        raise ValueError("Export was not a complete plain STEP file (ZIP/HTML/JSON are not supported).")
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def unpack_step(data: bytes) -> bytes:
    """Onshape wraps even a single STEP result in ZIP on some export paths.

    Never extract paths to disk. Accept exactly one STEP member; cap both the
    compressed download and actual decompressed bytes and let ZipFile check CRC.
    """
    if not data.startswith(b"PK"):
        validate_step(data)
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1 or not members[0].filename.lower().endswith((".step", ".stp")):
                raise ValueError("Export ZIP must contain exactly one STEP file; multi-file archives are unsupported.")
            member = members[0]
            if member.flag_bits & 1:
                raise ValueError("Encrypted export archives are unsupported.")
            if member.file_size > MAX_BYTES:
                raise ValueError("Uncompressed STEP exceeds the 32 MiB limit.")
            output = bytearray()
            with archive.open(member) as stream:
                while chunk := stream.read(65536):
                    if len(output) + len(chunk) > MAX_BYTES:
                        raise ValueError("Uncompressed STEP exceeds the 32 MiB limit.")
                    output.extend(chunk)
            result = bytes(output)
            validate_step(result)
            return result
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
        raise ValueError("Invalid or unsupported STEP export archive.") from error


async def download(base: str, path: str, access: str, secret: str, mode: str, signer) -> bytes:
    if mode not in {"hmac", "basic"}:
        raise ValueError("ONSHAPE_AUTH_MODE must be hmac or basic.")
    url = urljoin(base + "/", path)
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        for _ in range(4):
            authorized_host = credential_target(url, base)
            request = client.build_request("GET", url, headers={
                "Accept": "application/octet-stream", "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            })
            auth = None
            if authorized_host:
                if mode == "hmac":
                    signer(request, access, secret)
                else:
                    auth = httpx.BasicAuth(access, secret)
            response = await client.send(request, stream=True, auth=auth)
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("Export redirect omitted Location.")
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise RuntimeError(f"Onshape file download failed with HTTP {response.status_code}.")
                length = response.headers.get("content-length")
                if length and int(length) > MAX_BYTES:
                    raise ValueError("STEP download exceeds the 32 MiB limit.")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_BYTES:
                        raise ValueError("STEP download exceeds the 32 MiB limit.")
                    data.extend(chunk)
                return unpack_step(bytes(data))
            finally:
                await response.aclose()
    raise RuntimeError("Too many export redirects.")
