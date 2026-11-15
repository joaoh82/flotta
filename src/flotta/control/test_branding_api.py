"""An agency's branding (M16.2): its name, accent and logo, for its clients.

Read by anyone in the org, since a client's people are who it is for; set by
an owner. A logo is an image by its bytes, never SVG, and at most 256 KB.
"""

from __future__ import annotations

import base64

import pytest

from flotta.auth import ROLES, SCOPE_FLEET_READ, scopes_for_role
from flotta.branding import MAX_LOGO_BYTES, BrandingRefused, check_accent, sniff_logo
from flotta.control import test_tenancy
from flotta.control.test_tenancy import _bearer

fleet = test_tenancy.fleet
api = test_tenancy.api

OWNER = _bearer(*ROLES["owner"], org="agency-x")
MEMBER = _bearer(*ROLES["member"], org="agency-x")
VIEWER = _bearer(*scopes_for_role("client_viewer", client="acme"), org="agency-x", client="acme")
STRANGER = _bearer(*ROLES["owner"], org="agency-y")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
PATH = "/api/orgs/agency-x/branding"


def _b64(data):
    return base64.b64encode(data).decode()


def _put(api, body, headers=OWNER):
    return api.put(PATH, json=body, headers=headers)


# -- the checks -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "kind"), [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")]
)
def test_a_logo_is_known_by_its_bytes(data, kind):
    assert sniff_logo(data) == kind


@pytest.mark.parametrize("data", [SVG, b"GIF89a" + b"\x00" * 10, b"", b"%PDF-1.7"])
def test_anything_else_is_refused(data):
    with pytest.raises(BrandingRefused):
        sniff_logo(data)


@pytest.mark.parametrize("raw", ["#0F766E", "  #0f766e "])
def test_an_accent_is_a_hex_colour(raw):
    assert check_accent(raw) == "#0f766e"


@pytest.mark.parametrize("raw", ["teal", "#fff", "#0f766e;background:url(x)", 7, None, "#0f766g"])
def test_an_accent_that_is_not_one_is_refused(raw):
    with pytest.raises(BrandingRefused):
        check_accent(raw)


# -- the API ----------------------------------------------------------------------


def test_an_org_with_no_branding_is_flottas_own_look(api):
    answer = api.get(PATH, headers=VIEWER)

    assert answer.status_code == 200
    assert answer.json() == {"org": "agency-x", "name": "agency-x", "accent": None, "logo": None}
    assert api.get(f"{PATH}/logo", headers=VIEWER).status_code == 404


def test_an_owner_sets_name_accent_and_logo_and_a_client_reads_them(api):
    set_ = _put(api, {"name": "Harbor Automation", "accent": "#0F766E", "logo": _b64(PNG)})

    assert set_.status_code == 200, set_.text
    seen = api.get(PATH, headers=VIEWER).json()
    assert seen["name"] == "Harbor Automation"
    assert seen["accent"] == "#0f766e"
    assert seen["logo"]["type"] == "image/png"
    logo = api.get(f"{PATH}/logo", headers=VIEWER)
    assert logo.status_code == 200
    assert logo.content == PNG
    assert logo.headers["content-type"] == "image/png"
    assert logo.headers["x-content-type-options"] == "nosniff"


def test_a_data_url_is_judged_by_its_bytes_not_its_header(api):
    claims_png = "data:image/png;base64," + _b64(SVG)

    assert _put(api, {"logo": claims_png}).status_code == 422
    assert _put(api, {"logo": "data:image/png;base64," + _b64(WEBP)}).status_code == 200
    assert api.get(f"{PATH}/logo", headers=OWNER).headers["content-type"] == "image/webp"


def test_svg_is_refused(api):
    answer = _put(api, {"logo": _b64(SVG)})

    assert answer.status_code == 422
    assert "SVG" in answer.json()["detail"]


def test_a_logo_over_the_limit_is_refused(api):
    big = PNG + b"\x00" * MAX_LOGO_BYTES

    assert _put(api, {"logo": _b64(big)}).status_code == 422
    assert _put(api, {"logo": "not base64 at all!"}).status_code == 422


def test_what_the_body_leaves_out_is_kept(api):
    _put(api, {"accent": "#112233", "logo": _b64(JPEG)})

    _put(api, {"accent": "#445566"})
    after_accent = api.get(PATH, headers=OWNER).json()
    _put(api, {"logo": None})
    after_logo = api.get(PATH, headers=OWNER).json()

    assert after_accent["logo"]["type"] == "image/jpeg", "changing the accent keeps the logo"
    assert after_logo == {**after_accent, "logo": None}
    assert api.get(f"{PATH}/logo", headers=OWNER).status_code == 404


def test_a_name_is_one_line(api):
    for name in ("", "Harbor\r\nBcc: everyone@example.com", "x" * 500, "Tab\there"):
        assert _put(api, {"name": name}).status_code == 422, repr(name)
    assert api.get(PATH, headers=OWNER).json()["name"] == "agency-x"


def test_an_unknown_field_is_refused(api):
    assert _put(api, {"colour": "#112233"}).status_code == 422


def test_only_an_owner_sets_branding(api):
    for who in (MEMBER, VIEWER):
        assert _put(api, {"accent": "#112233"}, headers=who).status_code == 403
    assert _put(api, {"accent": "#112233"}, headers=STRANGER).status_code == 404
    assert api.get(PATH, headers=OWNER).json()["accent"] is None


def test_another_org_reads_none_of_it(api):
    _put(api, {"logo": _b64(PNG)})
    reader = _bearer(SCOPE_FLEET_READ, org="agency-y")

    assert api.get(PATH, headers=reader).status_code == 404
    assert api.get(f"{PATH}/logo", headers=reader).status_code == 404


def test_a_logo_one_byte_over_is_refused_after_decoding():
    """Its base64 fits the length checked before decoding; its bytes do not."""
    from flotta.branding import decode_logo

    over = PNG + b"\x00" * (MAX_LOGO_BYTES + 1 - len(PNG))
    at = over[:-1]

    with pytest.raises(BrandingRefused, match="256 KB"):
        decode_logo(_b64(over))
    assert decode_logo(_b64(at)) == (at, "image/png")
