from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Tuple

from ..output import atomic_write_bytes


SUPPORTED_DIRECT_SUFFIXES = {".jpg", ".png", ".gif", ".webp"}


def unwrap_apple_event_data(data: bytes) -> bytes:
    """Unwrap Music/JXA raw-data descriptors such as ``'tdta'($89504E47...$)``.

    Music.app can expose custom/local artwork through JXA as a textual
    Apple-event ``typeData`` descriptor instead of as the image bytes
    themselves.  The payload inside ``$...$`` is hexadecimal.
    """
    marker = b"'tdta'($"
    start = data.find(marker)
    if start < 0:
        return data

    payload_start = start + len(marker)
    payload_end = data.find(b"$)", payload_start)
    if payload_end < 0:
        return data

    encoded = re.sub(rb"\s+", b"", data[payload_start:payload_end])
    if not encoded or len(encoded) % 2 or re.search(rb"[^0-9A-Fa-f]", encoded):
        return data

    try:
        decoded = bytes.fromhex(encoded.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return data

    # Only replace the descriptor when its payload looks like an image.
    return decoded if detect_image_suffix(decoded) else data


def detect_image_suffix(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith((b"MM\x00*", b"II*\x00")):
        return ".tiff"
    return ""


def write_runtime_image(data: bytes, runtime_dir: Path, stem: str) -> Tuple[str, str]:
    if not data:
        return "", "empty_data"

    suffix = detect_image_suffix(data)
    if suffix in SUPPORTED_DIRECT_SUFFIXES:
        out = runtime_dir / f"{stem}{suffix}"
        atomic_write_bytes(out, data)
        return out.name, ""

    return "", f"unknown_image_format:first_bytes={data[:16].hex()}"


def convert_artwork_file(raw_path: Path, runtime_dir: Path, stem: str = "cover_direct") -> Tuple[str, str]:
    original_data = raw_path.read_bytes()
    data = unwrap_apple_event_data(original_data)
    if not data:
        raw_path.unlink(missing_ok=True)
        return "", "direct:empty_file"

    if data != original_data:
        # sips must receive the unwrapped binary payload too (for TIFF/other
        # formats that are not served directly by the browser overlay).
        raw_path.write_bytes(data)

    artwork_file, error = write_runtime_image(data, runtime_dir, stem)
    if artwork_file:
        raw_path.unlink(missing_ok=True)
        return artwork_file, ""

    out = runtime_dir / f"{stem}.png"
    try:
        result = subprocess.run(
            ["sips", "-s", "format", "png", str(raw_path), "--out", str(out)],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        raw_path.unlink(missing_ok=True)
        return "", f"direct:{error}:sips=not_found"
    except subprocess.TimeoutExpired:
        raw_path.unlink(missing_ok=True)
        return "", f"direct:{error}:sips=timeout"

    raw_path.unlink(missing_ok=True)
    if result.returncode == 0 and out.exists() and out.stat().st_size > 0:
        return out.name, ""

    detail = (result.stderr or result.stdout).strip()
    return "", f"direct:{error}:sips={detail}"
