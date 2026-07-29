"""Secure managed educational-image import and manifest authorisation."""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import sqlite3
import stat
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

PIL = pytest.importorskip("PIL")
from PIL import Image

from learning_studio import assets, service, storage, tools
from learning_studio.config import LearningStudioConfig
from learning_studio.schemas import IMPORT_ASSET_SCHEMA
from learning_studio.validation import SchemaViolation, validate
from tests.component_examples import example, manifest


def _source_root(hermes_home: Path) -> Path:
    root = hermes_home / "cache" / "images"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _image(path: Path, fmt: str = "PNG", size: tuple[int, int] = (3, 2)) -> bytes:
    image = Image.new("RGB", size, (21, 84, 126))
    image.save(path, format=fmt, quality=90, lossless=True)
    return path.read_bytes()


def _import(principal, source: Path, **overrides):
    values = {
        "principal": principal,
        "source_path": str(source),
        "title": "Cell structure diagram",
        "alt_text": "A labelled diagram showing the major parts of a cell.",
        "provenance": "host_image_generation",
    }
    values.update(overrides)
    return service.import_asset(**values)


def _visual_manifest(asset_id: str, alt_text: str):
    component = example("image_observation")
    component["content"]["image"] = {"asset_ref": asset_id, "alt_text": alt_text}
    return manifest([component])


def test_import_schema_advertises_the_alt_text_decorative_contract():
    validator = Draft202012Validator(IMPORT_ASSET_SCHEMA["parameters"])
    common = {
        "source_path": "/profile/cache/images/example.png",
        "title": "Example",
        "provenance": "operator_selected",
    }

    assert list(validator.iter_errors({**common, "alt_text": "Useful description"})) == []
    assert list(validator.iter_errors({**common, "decorative": True})) == []
    assert list(validator.iter_errors(common))
    assert list(validator.iter_errors({**common, "decorative": True, "alt_text": "Must be absent"}))
    assert list(validator.iter_errors({**common, "title": "   ", "alt_text": "Useful"}))

    validate({**common, "alt_text": "Useful description"}, IMPORT_ASSET_SCHEMA["parameters"])
    validate({**common, "decorative": True}, IMPORT_ASSET_SCHEMA["parameters"])
    with pytest.raises(SchemaViolation):
        validate(common, IMPORT_ASSET_SCHEMA["parameters"])
    with pytest.raises(SchemaViolation):
        validate(
            {**common, "decorative": True, "alt_text": "Must be absent"},
            IMPORT_ASSET_SCHEMA["parameters"],
        )


@pytest.mark.parametrize(
    ("fmt", "suffix", "mime"),
    [("PNG", ".png", "image/png"), ("JPEG", ".jpg", "image/jpeg"), ("WEBP", ".webp", "image/webp")],
)
def test_imports_supported_images_from_the_profile_cache(hermes_home, principal, fmt, suffix, mime):
    source = _source_root(hermes_home) / f"generated{suffix}"
    data = _image(source, fmt)

    result = _import(principal, source, generation_prompt="SECRET GENERATION PROMPT")

    assert result["ok"] is True
    assert result["asset_id"].isalnum() and len(result["asset_id"]) == 32
    assert result["mime_type"] == mime
    assert result["width"] == 3 and result["height"] == 2
    assert result["byte_size"] == len(data)
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    blob = json.dumps(result)
    assert str(source) not in blob
    assert "SECRET GENERATION PROMPT" not in blob


def test_detects_mime_from_bytes_not_the_filename(hermes_home, principal):
    source = _source_root(hermes_home) / "pretends-to-be-jpeg.jpg"
    _image(source, "PNG")

    result = _import(principal, source)

    assert result["mime_type"] == "image/png"


@pytest.mark.parametrize("kind", ["corrupt", "unsupported"])
def test_rejects_corrupt_and_unsupported_files(hermes_home, principal, kind):
    source = _source_root(hermes_home) / "bad.png"
    if kind == "corrupt":
        source.write_bytes(b"\x89PNG\r\n\x1a\nnot-an-image")
    else:
        _image(source, "BMP")

    with pytest.raises(service.ValidationError, match="corrupt|supported|PNG|JPEG|WebP"):
        _import(principal, source)

    assert not (hermes_home / "workspace" / "learning-studio" / "assets").exists()


def test_rejects_oversized_file_before_decoding(hermes_home, principal):
    source = _source_root(hermes_home) / "large.png"
    _image(source)
    source.write_bytes(source.read_bytes() + b"padding")
    config = replace(LearningStudioConfig(), max_asset_bytes=16)

    with pytest.raises(service.ValidationError, match="byte|size|large"):
        _import(principal, source, config=config)


def test_rejects_excessive_dimensions(hermes_home, principal):
    source = _source_root(hermes_home) / "wide.png"
    _image(source, size=(5, 2))
    config = replace(LearningStudioConfig(), max_asset_width=4)

    with pytest.raises(service.ValidationError, match="dimension|width"):
        _import(principal, source, config=config)


@pytest.mark.parametrize("source_kind", ["relative", "traversal", "outside"])
def test_rejects_untrusted_and_traversing_paths(hermes_home, principal, source_kind):
    root = _source_root(hermes_home)
    outside = hermes_home / "outside.png"
    _image(outside)
    if source_kind == "relative":
        source = Path("cache/images/picture.png")
    elif source_kind == "traversal":
        source = root / ".." / "images" / "picture.png"
    else:
        source = outside

    with pytest.raises(service.ValidationError, match="trusted|absolute|traversal"):
        _import(principal, source)


def test_rejects_a_symlink_that_escapes_the_trusted_root(hermes_home, principal):
    root = _source_root(hermes_home)
    outside = hermes_home / "outside.png"
    _image(outside)
    link = root / "escape.png"
    link.symlink_to(outside)

    with pytest.raises(service.ValidationError, match="trusted|symlink"):
        _import(principal, link)


def test_rejects_when_the_trusted_cache_root_itself_escapes(hermes_home, principal):
    cache = hermes_home / "cache"
    cache.mkdir()
    outside = hermes_home.parent / "outside-image-cache"
    outside.mkdir()
    source = outside / "escape.png"
    _image(source)
    (cache / "images").symlink_to(outside, target_is_directory=True)

    with pytest.raises(service.ValidationError, match="trusted|profile|root"):
        _import(principal, source)


@pytest.mark.parametrize(("alt_text", "decorative"), [(None, False), ("", False), ("   ", False)])
def test_meaningful_images_require_useful_alt_text(hermes_home, principal, alt_text, decorative):
    source = _source_root(hermes_home) / "image.png"
    _image(source)

    with pytest.raises(service.ValidationError, match="alt text"):
        _import(principal, source, alt_text=alt_text, decorative=decorative)


@pytest.mark.parametrize(
    "unsafe_alt",
    [
        "See https://example.com/image",
        "Read /etc/passwd",
        "<strong>diagram</strong>",
        "hidden\u202e text",
        "first line\nsecond line",
    ],
)
def test_import_alt_text_uses_the_manifest_safety_contract(hermes_home, principal, unsafe_alt):
    source = _source_root(hermes_home) / "unsafe-alt.png"
    _image(source)

    with pytest.raises(service.ValidationError):
        _import(principal, source, alt_text=unsafe_alt)


def test_import_normalises_alt_text_once_and_returns_the_stored_form(hermes_home, principal):
    source = _source_root(hermes_home) / "normalised.png"
    _image(source)
    decomposed = "Cafe\u0301 diagram"

    imported = _import(principal, source, alt_text=decomposed)

    assert imported["alt_text"] == unicodedata.normalize("NFC", decomposed)
    assert (
        service.prepare_experience(
            principal=principal,
            manifest=_visual_manifest(imported["asset_id"], imported["alt_text"]),
        )["stored"]
        is True
    )


def test_decorative_images_must_be_explicit_and_have_no_alt_text(hermes_home, principal):
    source = _source_root(hermes_home) / "divider.png"
    _image(source)

    result = _import(principal, source, alt_text=None, decorative=True)

    assert result["decorative"] is True
    assert "alt_text" not in result


def test_hash_dedup_is_scoped_to_the_learner(hermes_home, principal, other_principal):
    source = _source_root(hermes_home) / "same.png"
    _image(source)

    first = _import(principal, source)
    duplicate = _import(principal, source, title="A different title")
    other = _import(other_principal, source)

    assert duplicate["asset_id"] == first["asset_id"]
    assert duplicate["deduplicated"] is True
    assert duplicate["title"] == first["title"]
    assert duplicate["deduplication_policy"] == "first_import_metadata_is_immutable"
    assert duplicate["metadata_conflicts"] == ["title"]
    assert other["asset_id"] != first["asset_id"]
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM managed_assets").fetchone()["n"] == 2


def test_concurrent_same_scope_imports_publish_one_asset(hermes_home, principal):
    source = _source_root(hermes_home) / "concurrent.png"
    _image(source)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _import(principal, source), range(2)))

    assert len({result["asset_id"] for result in results}) == 1
    assert sorted(result["deduplicated"] for result in results) == [False, True]
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM managed_assets").fetchone()["n"] == 1


def test_atomic_copy_has_private_modes_and_no_temporary_files(hermes_home, principal):
    source = _source_root(hermes_home) / "atomic.png"
    expected = _image(source)

    result = _import(principal, source)
    assets = hermes_home / "workspace" / "learning-studio" / "assets"
    files = list(assets.iterdir())

    assert len(files) == 1
    assert files[0].read_bytes() == expected
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(assets.stat().st_mode) == 0o700
    assert not any(path.name.startswith(".tmp-") for path in files)
    assert result["asset_id"] in files[0].name


def test_successful_import_releases_the_publication_descriptor(hermes_home, principal, monkeypatch):
    source = _source_root(hermes_home) / "released-handle.png"
    _image(source)
    real_copy = assets.copy_atomic
    captured = None

    def capture_publication(image, asset_id, *, ownership_sink=None):
        nonlocal captured
        captured = real_copy(image, asset_id, ownership_sink=ownership_sink)
        return captured

    monkeypatch.setattr(assets, "copy_atomic", capture_publication)

    _import(principal, source)

    assert captured is not None
    assert captured.file_descriptor is None
    assert captured.directory_handle is None


def test_released_publication_cannot_close_a_reused_descriptor(hermes_home):
    image = assets.InspectedImage(b"published", "1" * 64, "image/png", "png", 1, 1)
    published = assets.copy_atomic(image, "released-descriptor-reuse")
    released_fd = published.file_descriptor
    assert released_fd is not None
    assets.release_managed_asset(published)

    unrelated = storage.storage_root() / "unrelated-descriptor.txt"
    unrelated.write_bytes(b"unrelated")
    source_fd = assets.os.open(unrelated, assets.os.O_RDONLY)
    try:
        if source_fd != released_fd:
            assets.os.dup2(source_fd, released_fd)

        assets.retire_managed_asset(published)

        assert assets.os.fstat(released_fd).st_size == len(b"unrelated")
    finally:
        with contextlib.suppress(OSError):
            assets.os.close(released_fd)
        if source_fd != released_fd:
            assets.os.close(source_fd)


def test_publication_context_interruption_retires_and_closes_the_exact_inode(
    hermes_home, monkeypatch
):
    image = assets.InspectedImage(b"published", "1" * 64, "image/png", "png", 1, 1)
    real_context = assets._open_managed_assets_directory
    real_retire = assets._retire_file_handle
    retired_fd = None

    @contextlib.contextmanager
    def interrupt_during_context_teardown():
        with real_context() as pinned:
            yield pinned
        raise KeyboardInterrupt

    def capture_retired_descriptor(handle):
        nonlocal retired_fd
        retired_fd = handle.fileno()
        real_retire(handle)

    monkeypatch.setattr(assets, "_open_managed_assets_directory", interrupt_during_context_teardown)
    monkeypatch.setattr(assets, "_retire_file_handle", capture_retired_descriptor)

    with pytest.raises(KeyboardInterrupt):
        assets.copy_atomic(image, "post-publish-interrupt")

    assert retired_fd is not None
    with pytest.raises(OSError):
        assets.os.fstat(retired_fd)
    retired = storage.storage_root() / "assets" / "post-publish-interrupt.png"
    assert retired.stat().st_size == 0
    assert stat.S_IMODE(retired.stat().st_mode) == 0


def test_interruption_inside_directory_close_leaks_no_descriptors(hermes_home):
    image = assets.InspectedImage(b"published", "1" * 64, "image/png", "png", 1, 1)
    source_lines, first_line = inspect.getsourcelines(assets._close_exact_descriptor)
    target_line = first_line + next(
        index for index, line in enumerate(source_lines) if line.strip() == "os.close(fd)"
    )
    interrupted = False
    fd_count_before = len(assets.os.listdir("/proc/self/fd"))

    def interrupt_before_close(frame, event, _arg):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is assets._close_exact_descriptor.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt
        return interrupt_before_close

    sys.settrace(interrupt_before_close)
    try:
        with pytest.raises(KeyboardInterrupt):
            assets.copy_atomic(image, "directory-close-interrupt")
    finally:
        sys.settrace(None)

    assert interrupted
    assert len(assets.os.listdir("/proc/self/fd")) == fd_count_before
    retired = storage.storage_root() / "assets" / "directory-close-interrupt.png"
    assert retired.stat().st_size == 0
    assert stat.S_IMODE(retired.stat().st_mode) == 0


def test_interruption_between_copy_return_and_assignment_recovers_ownership(
    hermes_home, principal, monkeypatch
):
    source = _source_root(hermes_home) / "return-assignment-interrupt.png"
    _image(source)
    real_copy = assets.copy_atomic
    captured = None

    def interrupt_after_copy(image, asset_id, *, ownership_sink=None):
        nonlocal captured
        captured = real_copy(image, asset_id, ownership_sink=ownership_sink)
        raise KeyboardInterrupt

    monkeypatch.setattr(assets, "copy_atomic", interrupt_after_copy)

    with pytest.raises(KeyboardInterrupt):
        _import(principal, source)

    assert captured is not None
    assert captured.file_descriptor is None
    assert captured.directory_handle is None
    retired = assets.managed_assets_root() / captured.storage_name
    assert retired.stat().st_size == 0
    assert stat.S_IMODE(retired.stat().st_mode) == 0
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM managed_assets").fetchone()["n"] == 0


def test_managed_import_fails_closed_without_secure_directory_operations(
    hermes_home, principal, monkeypatch
):
    source = _source_root(hermes_home) / "unsupported-platform.png"
    _image(source)
    monkeypatch.setattr(assets, "_supports_secure_directory_operations", lambda: False)

    with pytest.raises(service.ValidationError, match="descriptor-relative|platform"):
        _import(principal, source)

    assert not (hermes_home / "workspace" / "learning-studio" / "assets").exists()


def test_managed_root_resolution_failure_is_safe(hermes_home, monkeypatch):
    missing = hermes_home / "private-path-canary" / "missing"
    monkeypatch.setattr(assets, "ensure_storage_root", lambda: missing)

    with pytest.raises(assets.AssetError, match="unavailable") as refusal:
        assets.managed_assets_root()

    assert str(missing) not in str(refusal.value)


def test_atomic_copy_never_overwrites_an_existing_asset_name(hermes_home):
    first = assets.InspectedImage(b"first", "1" * 64, "image/png", "png", 1, 1)
    second = assets.InspectedImage(b"second", "2" * 64, "image/png", "png", 1, 1)
    published = assets.copy_atomic(first, "fixed-id")
    destination = assets.managed_assets_root() / published.storage_name

    with pytest.raises(assets.AssetError, match="exists|collision"):
        assets.copy_atomic(second, "fixed-id")

    assert destination.read_bytes() == b"first"
    assets.release_managed_asset(published)


def test_atomic_copy_is_pinned_against_managed_directory_swap(hermes_home, monkeypatch):
    image = assets.InspectedImage(b"pinned", "1" * 64, "image/png", "png", 1, 1)
    assets.managed_assets_root()
    outside = hermes_home.parent / "outside-swap"
    outside.mkdir()
    pinned = storage.storage_root() / "assets-pinned"
    original = assets._open_managed_assets_directory

    @contextlib.contextmanager
    def swap_after_open():
        with original() as (root, directory_fd):
            root.rename(pinned)
            root.symlink_to(outside, target_is_directory=True)
            yield root, directory_fd

    monkeypatch.setattr(assets, "_open_managed_assets_directory", swap_after_open)
    published = assets.copy_atomic(image, "pinned-id")

    assert list(outside.iterdir()) == []
    assert (pinned / published.storage_name).read_bytes() == b"pinned"
    assets.release_managed_asset(published)


def test_cleanup_retires_the_original_inode_after_directory_replacement(hermes_home):
    image = assets.InspectedImage(b"published", "1" * 64, "image/png", "png", 1, 1)
    published = assets.copy_atomic(image, "cleanup-id")
    root = assets.managed_assets_root()
    pinned = storage.storage_root() / "assets-pinned"
    root.rename(pinned)
    root.mkdir(mode=0o700)
    unrelated = root / published.storage_name
    unrelated.write_bytes(b"unrelated")

    assets.retire_managed_asset(published)

    assert unrelated.read_bytes() == b"unrelated"
    retired = pinned / published.storage_name
    assert retired.stat().st_size == 0
    assert stat.S_IMODE(retired.stat().st_mode) == 0


def test_cleanup_never_unlinks_a_same_name_replacement(hermes_home):
    image = assets.InspectedImage(b"published", "1" * 64, "image/png", "png", 1, 1)
    published = assets.copy_atomic(image, "cleanup-entry-race")
    root = assets.managed_assets_root()
    preserved_name = "preserved-original.png"
    (root / published.storage_name).rename(root / preserved_name)
    replacement = root / published.storage_name
    replacement.write_bytes(b"unrelated")
    replacement.chmod(0o600)

    assets.retire_managed_asset(published)

    assert replacement.read_bytes() == b"unrelated"
    preserved = root / preserved_name
    assert preserved.stat().st_size == 0
    assert stat.S_IMODE(preserved.stat().st_mode) == 0


def test_integrity_check_is_pinned_against_managed_directory_swap(
    hermes_home, principal, monkeypatch
):
    source = _source_root(hermes_home) / "verify-swap.png"
    _image(source)
    imported = _import(principal, source)
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT * FROM managed_assets WHERE id = ?", (imported["asset_id"],)
        ).fetchone()

    outside = hermes_home.parent / "outside-verify-swap"
    outside.mkdir()
    pinned = storage.storage_root() / "assets-pinned"
    original = assets._open_managed_assets_directory

    @contextlib.contextmanager
    def swap_after_open():
        with original() as (root, directory_fd):
            root.rename(pinned)
            root.symlink_to(outside, target_is_directory=True)
            (outside / row["storage_name"]).write_bytes(b"attacker-controlled")
            yield root, directory_fd

    monkeypatch.setattr(assets, "_open_managed_assets_directory", swap_after_open)

    assets.verify_managed_asset(row)
    assert (outside / row["storage_name"]).read_bytes() == b"attacker-controlled"


def test_integrity_refuses_a_path_replaced_after_open(hermes_home, principal, monkeypatch):
    source = _source_root(hermes_home) / "integrity-entry-race.png"
    _image(source)
    imported = _import(principal, source)
    root = assets.managed_assets_root()
    managed = next(root.iterdir())
    preserved = root / "preserved-integrity-original.png"
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT * FROM managed_assets WHERE id = ?", (imported["asset_id"],)
        ).fetchone()
    real_sha256 = assets.hashlib.sha256
    swapped = False

    def swap_while_hashing(data=b""):
        nonlocal swapped
        if not swapped:
            swapped = True
            managed.rename(preserved)
            managed.write_bytes(b"X" * len(data))
            managed.chmod(0o600)
        return real_sha256(data)

    monkeypatch.setattr(assets.hashlib, "sha256", swap_while_hashing)

    with pytest.raises(assets.AssetError, match="integrity"):
        assets.verify_managed_asset(row)

    assert swapped is True
    assert preserved.exists()


def test_rejects_an_escaping_managed_assets_directory(hermes_home, principal):
    source = _source_root(hermes_home) / "safe.png"
    _image(source)
    storage.initialize()
    outside = hermes_home.parent / "outside-managed-assets"
    outside.mkdir()
    (storage.storage_root() / "assets").symlink_to(outside, target_is_directory=True)

    with pytest.raises(service.ValidationError, match="managed asset|storage|profile"):
        _import(principal, source)

    assert list(outside.iterdir()) == []


def test_rejects_animated_webp(hermes_home, principal):
    source = _source_root(hermes_home) / "animated.webp"
    frames = [Image.new("RGB", (2, 2), colour) for colour in ("red", "blue")]
    frames[0].save(
        source,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )

    with pytest.raises(service.ValidationError, match="animated|single-frame"):
        _import(principal, source)


def test_rejects_images_with_private_embedded_metadata(hermes_home, principal):
    source = _source_root(hermes_home) / "private-metadata.jpg"
    image = Image.new("RGB", (2, 2), "blue")
    exif = Image.Exif()
    exif[0x010E] = "private-exif-canary"
    image.save(source, format="JPEG", exif=exif)

    with pytest.raises(service.ValidationError, match="metadata|clean copy"):
        _import(principal, source)


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_accepts_non_sensitive_dpi_metadata(hermes_home, principal, fmt):
    source = _source_root(hermes_home) / f"print-resolution.{fmt.lower()}"
    Image.new("RGB", (2, 2), "blue").save(source, format=fmt, dpi=(300, 300))

    result = _import(principal, source)

    assert result["ok"] is True


def test_rejects_opaque_icc_profile_payloads(hermes_home, principal):
    source = _source_root(hermes_home) / "private-colour-profile.jpg"
    Image.new("RGB", (2, 2), "blue").save(
        source, format="JPEG", icc_profile=b"PRIVATE-ICC-PROFILE-CANARY"
    )

    with pytest.raises(service.ValidationError, match="metadata|clean copy"):
        _import(principal, source)


def test_rejects_uncommon_jpeg_application_metadata(hermes_home, principal):
    source = _source_root(hermes_home) / "photoshop-metadata.jpg"
    _image(source, "JPEG")
    original = source.read_bytes()
    payload = b"Photoshop 3.0\x00private-location-and-author-canary"
    app13 = b"\xff\xed" + (len(payload) + 2).to_bytes(2, "big") + payload
    source.write_bytes(original[:2] + app13 + original[2:])

    with pytest.raises(service.ValidationError, match="metadata|clean copy"):
        _import(principal, source)


def test_rejects_unrecognized_jpeg_application_segments(hermes_home, principal):
    source = _source_root(hermes_home) / "unknown-app13.jpg"
    _image(source, "JPEG")
    original = source.read_bytes()
    payload = b"PRIVATE-APP13-CANARY"
    app13 = b"\xff\xed" + (len(payload) + 2).to_bytes(2, "big") + payload
    source.write_bytes(original[:2] + app13 + original[2:])

    with pytest.raises(service.ValidationError, match="metadata|clean copy"):
        _import(principal, source)


@pytest.mark.parametrize("mutation", ["delete", "tamper", "permissions"])
def test_dedup_never_reports_success_for_broken_managed_bytes(hermes_home, principal, mutation):
    source = _source_root(hermes_home) / f"broken-{mutation}.png"
    _image(source)
    imported = _import(principal, source)
    managed = next((hermes_home / "workspace" / "learning-studio" / "assets").iterdir())
    if mutation == "delete":
        managed.unlink()
    elif mutation == "tamper":
        managed.write_bytes(b"tampered")
    else:
        managed.chmod(0o644)

    with pytest.raises(service.ValidationError, match="integrity"):
        _import(principal, source)

    assert imported["asset_id"]


def test_database_insert_failure_retires_final_bytes_and_removes_temporary_files(
    hermes_home, principal
):
    source = _source_root(hermes_home) / "rollback.png"
    _image(source)
    storage.initialize()
    with storage.connect() as conn:
        conn.execute(
            "CREATE TRIGGER abort_asset_insert BEFORE INSERT ON managed_assets "
            "BEGIN SELECT RAISE(ABORT, 'asset insert refused'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        _import(principal, source)

    root = hermes_home / "workspace" / "learning-studio" / "assets"
    files = list(root.iterdir())
    assert len(files) == 1
    assert files[0].stat().st_size == 0
    assert stat.S_IMODE(files[0].stat().st_mode) == 0
    assert not any(path.name.startswith(".tmp-") for path in files)
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM managed_assets").fetchone()["n"] == 0


def test_database_commit_failure_retires_and_closes_the_exact_publication(
    hermes_home, principal, monkeypatch
):
    source = _source_root(hermes_home) / "commit-rollback.png"
    _image(source)
    real_copy = assets.copy_atomic
    captured = None

    def capture_publication(image, asset_id, *, ownership_sink=None):
        nonlocal captured
        captured = real_copy(image, asset_id, ownership_sink=ownership_sink)
        return captured

    @contextlib.contextmanager
    def fail_at_commit(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.rollback()
            raise sqlite3.OperationalError("forced commit failure")

    monkeypatch.setattr(assets, "copy_atomic", capture_publication)
    monkeypatch.setattr(storage, "transaction", fail_at_commit)

    with pytest.raises(sqlite3.OperationalError, match="commit failure"):
        _import(principal, source)

    assert captured is not None
    assert captured.file_descriptor is None
    assert captured.directory_handle is None
    retired = assets.managed_assets_root() / captured.storage_name
    assert retired.stat().st_size == 0
    assert stat.S_IMODE(retired.stat().st_mode) == 0
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM managed_assets").fetchone()["n"] == 0


def test_post_commit_interruption_preserves_the_committed_asset(
    hermes_home, principal, monkeypatch
):
    source = _source_root(hermes_home) / "committed-interruption.png"
    expected = _image(source)
    real_copy = assets.copy_atomic
    captured = None

    def capture_publication(image, asset_id, *, ownership_sink=None):
        nonlocal captured
        captured = real_copy(image, asset_id, ownership_sink=ownership_sink)
        return captured

    @contextlib.contextmanager
    def interrupt_after_commit(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            raise KeyboardInterrupt

    monkeypatch.setattr(assets, "copy_atomic", capture_publication)
    monkeypatch.setattr(storage, "transaction", interrupt_after_commit)

    with pytest.raises(KeyboardInterrupt):
        _import(principal, source)

    assert captured is not None
    assert captured.file_descriptor is None
    assert captured.directory_handle is None
    managed = assets.managed_assets_root() / captured.storage_name
    assert managed.read_bytes() == expected
    assert stat.S_IMODE(managed.stat().st_mode) == 0o600
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM managed_assets WHERE id = ?",
                (captured.storage_name.split(".")[0],),
            ).fetchone()["n"]
            == 1
        )


def test_normal_success_interruption_still_releases_publication(
    hermes_home, principal, monkeypatch
):
    source = _source_root(hermes_home) / "normal-success-interruption.png"
    expected = _image(source)
    real_copy = assets.copy_atomic
    captured = None

    def capture_publication(image, asset_id, *, ownership_sink=None):
        nonlocal captured
        captured = real_copy(image, asset_id, ownership_sink=ownership_sink)
        return captured

    source_lines, first_line = inspect.getsourcelines(service.import_asset)
    target_line = first_line + next(
        index for index, line in enumerate(source_lines) if line == "        if result is None:\n"
    )
    interrupted = False

    def interrupt_before_normal_release(frame, event, _arg):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is service.import_asset.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt
        return interrupt_before_normal_release

    monkeypatch.setattr(assets, "copy_atomic", capture_publication)
    sys.settrace(interrupt_before_normal_release)
    try:
        with pytest.raises(KeyboardInterrupt):
            _import(principal, source)
    finally:
        sys.settrace(None)

    assert interrupted
    assert captured is not None
    assert captured.file_descriptor is None
    assert captured.directory_handle is None
    managed = assets.managed_assets_root() / captured.storage_name
    assert managed.read_bytes() == expected
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM managed_assets").fetchone()["n"] == 1


def test_interruption_inside_release_leaves_no_unreachable_descriptors(
    hermes_home, principal, monkeypatch
):
    source = _source_root(hermes_home) / "release-interruption.png"
    _image(source)
    real_copy = assets.copy_atomic
    captured = None
    captured_fd = None

    def capture_publication(image, asset_id, *, ownership_sink=None):
        nonlocal captured, captured_fd
        captured = real_copy(image, asset_id, ownership_sink=ownership_sink)
        captured_fd = captured.file_descriptor
        return captured

    source_lines, first_line = inspect.getsourcelines(assets.release_managed_asset)
    target_line = first_line + next(
        index for index, line in enumerate(source_lines) if line.strip() == "file_handle.close()"
    )
    interrupted = False
    fd_count_before = len(assets.os.listdir("/proc/self/fd"))

    def interrupt_during_release(frame, event, _arg):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is assets.release_managed_asset.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt
        return interrupt_during_release

    monkeypatch.setattr(assets, "copy_atomic", capture_publication)
    sys.settrace(interrupt_during_release)
    try:
        with pytest.raises(KeyboardInterrupt):
            _import(principal, source)
    finally:
        sys.settrace(None)

    assert interrupted
    assert captured is not None
    assert captured.file_handle is None
    assert captured.directory_handle is None
    assert captured_fd is not None
    with pytest.raises(OSError):
        assets.os.fstat(captured_fd)
    assert len(assets.os.listdir("/proc/self/fd")) == fd_count_before
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM managed_assets").fetchone()["n"] == 1


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("sha256", "not-a-sha"),
        ("provenance", "model_claimed_magic"),
        ("storage_name", "../outside.png"),
        ("title", "   "),
    ],
)
def test_asset_table_enforces_integrity_for_direct_sql(hermes_home, principal, column, value):
    source = _source_root(hermes_home) / f"constraint-{column}.png"
    _image(source)
    imported = _import(principal, source)

    with storage.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"UPDATE managed_assets SET {column} = ? WHERE id = ?",
            (value, imported["asset_id"]),
        )

    with storage.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_asset_table_rejects_a_foreign_owned_track_even_via_direct_sql(
    hermes_home, principal, other_principal
):
    source = _source_root(hermes_home) / "foreign-track-sql.png"
    _image(source)
    imported = _import(principal, source)
    foreign_track = service.save_context(
        principal=other_principal,
        track={"name": "Foreign SQL track", "confirmed": True},
    )["outcome"]["track"]["track_id"]

    with storage.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE managed_assets SET track_id = ?, scope_key = ? WHERE id = ?",
            (foreign_track, foreign_track, imported["asset_id"]),
        )


def test_tool_output_never_exposes_source_or_prompt(hermes_home, gateway_session):
    source = _source_root(hermes_home) / "tool.png"
    _image(source)
    secret = "generation-secret-canary"

    result = json.loads(
        tools.handle_import_asset(
            {
                "source_path": str(source),
                "title": "Generated diagram",
                "alt_text": "A diagram of three connected stages.",
                "decorative": False,
                "provenance": "host_image_generation",
                "generation_prompt": secret,
            },
            session_id="abc",
            task_id="def",
            agent=object(),
        )
    )

    assert result["ok"] is True
    blob = json.dumps(result)
    assert str(source) not in blob
    assert secret not in blob
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT generation_prompt FROM managed_assets WHERE id = ?", (result["asset_id"],)
        ).fetchone()
        columns = {
            column["name"]
            for column in conn.execute("PRAGMA table_info(managed_assets)").fetchall()
        }
    assert row["generation_prompt"] == secret
    assert "source_path" not in columns


def test_manifest_accepts_an_owned_asset(hermes_home, principal):
    source = _source_root(hermes_home) / "owned.png"
    _image(source)
    imported = _import(principal, source)

    result = service.prepare_experience(
        principal=principal,
        manifest=_visual_manifest(imported["asset_id"], imported["alt_text"]),
    )

    assert result["stored"] is True


def test_manifest_rejects_a_managed_asset_that_was_tampered_after_import(hermes_home, principal):
    source = _source_root(hermes_home) / "tampered-after-import.png"
    _image(source)
    imported = _import(principal, source)
    managed = next((hermes_home / "workspace" / "learning-studio" / "assets").iterdir())
    managed.write_bytes(b"tampered")

    with pytest.raises(service.ValidationError, match="integrity"):
        service.prepare_experience(
            principal=principal,
            manifest=_visual_manifest(imported["asset_id"], imported["alt_text"]),
        )


def test_manifest_rejects_an_unknown_or_other_learners_asset(
    hermes_home, principal, other_principal
):
    source = _source_root(hermes_home) / "private.png"
    _image(source)
    imported = _import(other_principal, source)

    for asset_id in (imported["asset_id"], "0" * 32):
        with pytest.raises(service.NotFoundError, match="asset"):
            service.prepare_experience(
                principal=principal,
                manifest=_visual_manifest(asset_id, imported["alt_text"]),
            )


def test_nested_image_choice_checks_every_asset_owner(hermes_home, principal, other_principal):
    first_source = _source_root(hermes_home) / "choice-a.png"
    second_source = _source_root(hermes_home) / "choice-b.png"
    _image(first_source)
    _image(second_source, size=(2, 2))
    owned = _import(principal, first_source, alt_text="Owned choice image")
    foreign = _import(other_principal, second_source, alt_text="Foreign choice image")
    component = example("image_choice")
    component["content"]["options"][0]["image"] = {
        "asset_ref": owned["asset_id"],
        "alt_text": owned["alt_text"],
    }
    component["content"]["options"][1]["image"] = {
        "asset_ref": foreign["asset_id"],
        "alt_text": foreign["alt_text"],
    }

    with pytest.raises(service.NotFoundError, match="asset"):
        service.prepare_experience(principal=principal, manifest=manifest([component]))


def test_manifest_rejects_track_scoped_asset_outside_its_track(hermes_home, principal):
    source = _source_root(hermes_home) / "track.png"
    _image(source)
    track = service.save_context(principal=principal, track={"name": "Track A", "confirmed": True})[
        "outcome"
    ]["track"]["track_id"]
    imported = _import(principal, source, track_id=track)

    with pytest.raises(service.NotFoundError, match="asset"):
        service.prepare_experience(
            principal=principal,
            manifest=_visual_manifest(imported["asset_id"], imported["alt_text"]),
        )


def test_manifest_accepts_an_asset_in_the_same_track(hermes_home, principal):
    source = _source_root(hermes_home) / "same-track.png"
    _image(source)
    track = service.save_context(principal=principal, track={"name": "Track A", "confirmed": True})[
        "outcome"
    ]["track"]["track_id"]
    imported = _import(principal, source, track_id=track)

    result = service.prepare_experience(
        principal=principal,
        track_id=track,
        manifest=_visual_manifest(imported["asset_id"], imported["alt_text"]),
    )

    assert result["stored"] is True


def test_deduplication_does_not_cross_track_scope(hermes_home, principal):
    source = _source_root(hermes_home) / "scoped.png"
    _image(source)
    track = service.save_context(principal=principal, track={"name": "Track A", "confirmed": True})[
        "outcome"
    ]["track"]["track_id"]

    unscoped = _import(principal, source)
    scoped = _import(principal, source, track_id=track)

    assert scoped["asset_id"] != unscoped["asset_id"]
    assert scoped["deduplicated"] is False


def test_import_rejects_a_track_owned_by_another_learner(hermes_home, principal, other_principal):
    source = _source_root(hermes_home) / "foreign-track.png"
    _image(source)
    foreign_track = service.save_context(
        principal=other_principal,
        track={"name": "Private track", "confirmed": True},
    )["outcome"]["track"]["track_id"]

    with pytest.raises(service.NotFoundError, match="track"):
        _import(principal, source, track_id=foreign_track)


def test_manifest_rejects_changed_alt_text_and_decorative_assets(hermes_home, principal):
    source = _source_root(hermes_home) / "alt.png"
    _image(source)
    meaningful = _import(principal, source)

    with pytest.raises(service.ValidationError, match="alt text"):
        service.prepare_experience(
            principal=principal,
            manifest=_visual_manifest(meaningful["asset_id"], "Different description"),
        )

    other_source = _source_root(hermes_home) / "decorative.png"
    _image(other_source, size=(2, 2))
    decorative = _import(principal, other_source, alt_text=None, decorative=True)
    with pytest.raises(service.ValidationError, match="decorative"):
        service.prepare_experience(
            principal=principal,
            manifest=_visual_manifest(decorative["asset_id"], "Decorative divider"),
        )
