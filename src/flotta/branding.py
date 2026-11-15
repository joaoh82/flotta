"""How an agency looks to its clients (M16.2): an accent colour and a logo.

The hosted product's portal and report wear them; the runtime only keeps them,
checks them and hands them back. Nothing here renders anything.

**A logo is an image the browser draws, never a document it runs.** So only
PNG, JPEG and WebP, told apart by their first bytes rather than by what the
uploader said they were, and never SVG, which is a document that can carry
script. It is served with its sniffed type and `nosniff`, so a file that is
not what it claims is not reinterpreted on the way out either.
"""

from __future__ import annotations

import base64
import binascii
import re

#: The most a logo may weigh. A portal's header and a report's first page
#: draw it small; anything bigger is a photograph, and slows every page.
MAX_LOGO_BYTES = 256 * 1024

#: `#rrggbb`, which is what every renderer of it (CSS, a PDF, an email) reads.
_ACCENT = re.compile(r"^#[0-9a-fA-F]{6}$")


class BrandingRefused(ValueError):
    """What was sent is not an accent or a logo we keep, and why."""


def check_accent(raw: object) -> str:
    """`#rrggbb`, lowercased, or a refusal."""
    if not isinstance(raw, str) or not _ACCENT.match(raw.strip()):
        raise BrandingRefused('an accent is a colour as "#rrggbb", like "#0f766e"')
    return raw.strip().lower()


def sniff_logo(data: bytes) -> str:
    """The image type `data` is, by its first bytes, or a refusal."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise BrandingRefused("a logo is a PNG, JPEG or WebP image; SVG and other files are refused")


def decode_logo(raw: object) -> tuple[bytes, str]:
    """A logo sent as base64, decoded and checked: `(bytes, type)`."""
    if not isinstance(raw, str) or not raw.strip():
        raise BrandingRefused("send the logo as base64, or null to remove it")
    # A data URL's header says what the sender thinks it is; the bytes decide.
    text = raw.strip().split(",", 1)[1] if raw.strip().startswith("data:") else raw.strip()
    if len(text) > (MAX_LOGO_BYTES * 4) // 3 + 4:
        raise BrandingRefused(f"a logo is at most {MAX_LOGO_BYTES // 1024} KB")
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BrandingRefused("the logo is not valid base64") from exc
    if len(data) > MAX_LOGO_BYTES:
        raise BrandingRefused(f"a logo is at most {MAX_LOGO_BYTES // 1024} KB")
    return data, sniff_logo(data)
