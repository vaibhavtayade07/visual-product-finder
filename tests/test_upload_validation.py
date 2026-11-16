"""E12/US-2: upload validation, HEIC recognize, consent gate, per-IP rate limit."""
import io

import pytest
from PIL import Image

from conftest import (corrupt_jpeg, error_code, heic_magic_bytes, make_jpeg, make_png,
                      new_session, oversized_jpeg, search, too_small_jpeg)


def basic_search(client, image, tenant=None, consent="test-v1", content_type="image/jpeg"):
    return search(client, image, tenant=tenant, session_id=new_session("up"),
                  consent=consent, content_type=content_type)


def test_unsupported_type_rejected(client, seeded):
    resp = basic_search(client, b"this is definitely not an image", content_type="text/plain")
    assert resp.status_code == 415
    assert error_code(resp) == "UNSUPPORTED_TYPE"


def test_oversized_upload_rejected(client, seeded):
    resp = basic_search(client, oversized_jpeg())
    assert resp.status_code == 413
    assert error_code(resp) == "IMAGE_TOO_LARGE"


def test_too_small_image_rejected(client, seeded):
    resp = basic_search(client, too_small_jpeg())
    assert resp.status_code == 400
    assert error_code(resp) == "IMAGE_TOO_SMALL"
    assert resp.json()["error"]["details"]["min_side"] == 200


def test_corrupt_jpeg_rejected_cleanly(client, seeded):
    resp = basic_search(client, corrupt_jpeg())
    assert resp.status_code == 400
    assert error_code(resp) == "DECODE_FAILED"


def test_heic_garbage_sniffed_then_rejected_cleanly(client, seeded):
    # magic bytes say HEIC: the sniff must recognize it (not UNSUPPORTED_TYPE) and the
    # decode failure must be a clean 400, never a 500.
    resp = basic_search(client, heic_magic_bytes(), content_type="image/heic")
    assert resp.status_code == 400
    assert error_code(resp) == "DECODE_FAILED"


def test_consent_required_before_upload(client, seeded):
    resp = basic_search(client, make_jpeg(), consent="")
    assert resp.status_code == 400
    assert error_code(resp) == "CONSENT_REQUIRED"


def test_valid_jpeg_and_png_accepted(client, seeded):
    jpeg = basic_search(client, make_jpeg(seed=5))
    assert jpeg.status_code == 202, jpeg.text
    png = basic_search(client, make_png(), content_type="image/png")
    assert png.status_code == 202, png.text


def test_rate_limit_per_ip(client, make_tenant):
    slug = make_tenant({"rate_limit": {"ip_per_min": 3}})
    statuses = [basic_search(client, make_jpeg(seed=i), tenant=slug).status_code for i in range(3)]
    assert statuses == [202, 202, 202]
    blocked = basic_search(client, make_jpeg(seed=99), tenant=slug)
    assert blocked.status_code == 429
    assert error_code(blocked) == "RATE_LIMITED"


def test_rate_limit_is_per_tenant_not_global(client, make_tenant):
    slug_a = make_tenant({"rate_limit": {"ip_per_min": 1}})
    slug_b = make_tenant({"rate_limit": {"ip_per_min": 1}})
    assert basic_search(client, make_jpeg(seed=1), tenant=slug_a).status_code == 202
    # tenant A is now exhausted for this IP...
    assert basic_search(client, make_jpeg(seed=2), tenant=slug_a).status_code == 429
    # ...but tenant B has its own window
    assert basic_search(client, make_jpeg(seed=3), tenant=slug_b).status_code == 202


def test_heic_roundtrip_when_encoder_available(client, seeded):
    import pillow_heif

    try:
        img = Image.new("RGB", (320, 320), (200, 190, 180))
        buf = io.BytesIO()
        img.save(buf, format="HEIF")
        heic_bytes = buf.getvalue()
    except Exception as exc:  # noqa: BLE001 - encoder optional on some platforms
        pytest.skip(f"pillow_heif encoder unavailable: {exc}")

    resp = basic_search(client, heic_bytes, content_type="image/heic")
    assert resp.status_code == 202, resp.text
