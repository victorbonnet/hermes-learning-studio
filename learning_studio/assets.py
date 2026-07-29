"""Secure import of local image results into profile-managed storage.

Pillow is deliberately imported only inside :func:`inspect_image`. Hermes can
register and use the base plugin without installing the ``media`` extra.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import secrets
import stat
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import LearningStudioConfig
from .paths import DIRECTORY_MODE, FILE_MODE, ensure_storage_root, hermes_home

ASSET_DIRECTORY = "assets"
PROVENANCES = ("host_image_generation", "learner_provided", "operator_selected")
_FORMATS = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
}
# Pillow exposes format bookkeeping and arbitrary embedded application data
# through the same ``Image.info`` mapping.  Use an allowlist rather than a
# denylist: otherwise an uncommon JPEG APP marker (for example Photoshop
# IRB/IPTC data) can survive validation and later leak when the managed bytes
# are served.  These keys describe the JPEG container itself and carry no
# caller-authored payload.
_SAFE_IMAGE_INFO_KEYS = {
    "adobe",
    "adobe_transform",
    "background",
    "duration",
    "jfif",
    "jfif_density",
    "jfif_unit",
    "jfif_version",
    "loop",
    "timestamp",
}


def _supports_secure_directory_operations() -> bool:
    """Whether this runtime can pin every managed-storage operation to a dirfd."""
    required_dir_fd = (os.open, os.mkdir, os.stat, os.unlink, os.link)
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and all(function in os.supports_dir_fd for function in required_dir_fd)
        and os.stat in os.supports_follow_symlinks
        and os.link in os.supports_follow_symlinks
    )


class AssetError(ValueError):
    """An asset was unsafe or invalid. Messages contain no source paths."""


class MediaDependencyError(AssetError):
    """The optional image-validation dependency is unavailable."""


@dataclass(frozen=True)
class InspectedImage:
    data: bytes
    sha256: str
    mime_type: str
    extension: str
    width: int
    height: int

    @property
    def byte_size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class TrustedSource:
    """A resolved source plus the inode approved during containment checks."""

    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class PublishedAsset:
    """A publication handle bound to the directory and file inodes created."""

    storage_name: str
    directory_device: int
    directory_inode: int
    file_device: int
    file_inode: int


def trusted_source_roots() -> tuple[Path, ...]:
    """Roots written by Hermes' image tool in the active profile.

    The host's image-generation providers materialise generated and downloaded
    images under ``$HERMES_HOME/cache/images``. No workspace-wide or arbitrary
    existing path is trusted merely because the process can read it.
    """
    try:
        home = hermes_home().resolve(strict=True)
        root = (home / "cache" / "images").resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AssetError("The trusted profile image root is unavailable") from exc
    if root != home and home not in root.parents:
        raise AssetError("The trusted image root escapes the active profile")
    if not root.is_dir():
        raise AssetError("The trusted profile image root is not a directory")
    return (root,)


def _trusted_source(raw: str) -> TrustedSource:
    if not isinstance(raw, str) or not raw.strip():
        raise AssetError("source_path must be a non-empty absolute path")
    source = Path(raw)
    if not source.is_absolute():
        raise AssetError("source_path must be an absolute path under a trusted image root")
    if ".." in source.parts:
        raise AssetError("source_path contains traversal and was refused")
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AssetError("The source image could not be opened from a trusted image root") from exc

    for root in trusted_source_roots():
        try:
            resolved_root = root.resolve(strict=True)
        except OSError:
            continue
        if resolved == resolved_root or resolved_root in resolved.parents:
            try:
                info = resolved.stat()
            except OSError as exc:
                raise AssetError("The source image could not be opened safely") from exc
            return TrustedSource(resolved, info.st_dev, info.st_ino)
    raise AssetError("The source image is outside the trusted profile image roots")


def _read_bounded(source: TrustedSource, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(source.path, flags)
    except OSError as exc:
        raise AssetError("The source image could not be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AssetError("The source image must be a regular file")
        if (info.st_dev, info.st_ino) != (source.device, source.inode):
            raise AssetError("The source image changed during safety validation")
        if info.st_size > maximum:
            raise AssetError(f"The source image is above the configured {maximum}-byte limit")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise AssetError(f"The source image is above the configured {maximum}-byte limit")
        return data
    finally:
        os.close(fd)


def inspect_image(source_path: str, config: LearningStudioConfig) -> InspectedImage:
    """Resolve, byte-limit, identify, verify, and fully decode an image."""
    source = _trusted_source(source_path)
    data = _read_bounded(source, config.max_asset_bytes)
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise MediaDependencyError(
            "Managed image import requires the optional media dependency. "
            "Install hermes-learning-studio[media] and retry."
        ) from exc

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                if image_format not in _FORMATS:
                    raise AssetError("Only PNG, JPEG, and WebP images are supported")
                if (
                    bool(getattr(image, "is_animated", False))
                    or int(getattr(image, "n_frames", 1)) != 1
                ):
                    raise AssetError("Animated images are not supported; use a single-frame image")
                if width > config.max_asset_width or height > config.max_asset_height:
                    raise AssetError(
                        "The source image dimensions exceed the configured width or height limit"
                    )
                if width * height > config.max_asset_pixels:
                    raise AssetError(
                        "The source image dimensions exceed the configured pixel limit"
                    )
                image.verify()
            # verify() checks structure without decoding pixels. Reopen and load
            # so truncated streams and decompression failures are refused too.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                unsafe_jpeg_segment = False
                if image_format == "JPEG":
                    for marker, payload in getattr(image, "applist", ()):
                        safe_jfif = (
                            marker == "APP0"
                            and len(payload) == 14
                            and payload.startswith(b"JFIF\x00")
                        )
                        safe_adobe = (
                            marker == "APP14"
                            and len(payload) == 12
                            and payload.startswith(b"Adobe")
                        )
                        if not (safe_jfif or safe_adobe):
                            unsafe_jpeg_segment = True
                            break
                if (
                    set(image.info).difference(_SAFE_IMAGE_INFO_KEYS)
                    or bool(getattr(image, "text", {}))
                    or unsafe_jpeg_segment
                ):
                    raise AssetError(
                        "Images with embedded metadata are not supported; export a clean copy"
                    )
    except AssetError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise AssetError(
            "The source image is corrupt or is not a supported PNG, JPEG, or WebP"
        ) from exc
    except Image.DecompressionBombError as exc:
        raise AssetError("The source image dimensions are unsafe to decode") from exc
    except Image.DecompressionBombWarning as exc:
        raise AssetError("The source image dimensions are unsafe to decode") from exc

    mime_type, extension = _FORMATS[image_format]
    return InspectedImage(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        mime_type=mime_type,
        extension=extension,
        width=width,
        height=height,
    )


@contextlib.contextmanager
def _open_managed_assets_directory():
    """Yield the validated asset root and an inode-pinned directory descriptor.

    Every file operation must be relative to this descriptor.  Returning only
    a checked pathname would leave a window in which another process could
    replace ``assets/`` with a symlink before a later create or open.
    """
    if not _supports_secure_directory_operations():
        raise AssetError(
            "Managed image import requires secure descriptor-relative filesystem operations "
            "that are unavailable on this platform"
        )
    base = ensure_storage_root().resolve(strict=True)
    root = base / ASSET_DIRECTORY
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    base_fd = directory_fd = None
    try:
        expected_base = base.stat(follow_symlinks=False)
        base_fd = os.open(base, directory_flags)
        opened_base = os.fstat(base_fd)
        if (opened_base.st_dev, opened_base.st_ino) != (
            expected_base.st_dev,
            expected_base.st_ino,
        ):
            raise AssetError("The managed asset storage directory changed during validation")
        with contextlib.suppress(FileExistsError):
            os.mkdir(ASSET_DIRECTORY, DIRECTORY_MODE, dir_fd=base_fd)
        directory_fd = os.open(ASSET_DIRECTORY, directory_flags, dir_fd=base_fd)
        opened = os.fstat(directory_fd)
        linked = os.stat(ASSET_DIRECTORY, dir_fd=base_fd, follow_symlinks=False)
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            linked.st_dev,
            linked.st_ino,
        ):
            raise AssetError("The managed asset storage directory changed during validation")
        with contextlib.suppress(OSError, NotImplementedError):
            os.fchmod(directory_fd, DIRECTORY_MODE)
        yield root, directory_fd
    except AssetError:
        raise
    except (OSError, RuntimeError) as exc:
        raise AssetError("The managed asset storage directory is unavailable") from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        if base_fd is not None:
            os.close(base_fd)


def managed_assets_root() -> Path:
    """Create and validate the managed root, returning it for diagnostics."""
    with _open_managed_assets_directory() as (root, _directory_fd):
        return root


def copy_atomic(image: InspectedImage, asset_id: str) -> PublishedAsset:
    """Write validated bytes privately, then atomically publish the final name."""
    storage_name = f"{asset_id}.{image.extension}"
    with _open_managed_assets_directory() as (_root, directory_fd):
        temporary_name = ""
        fd = None
        published = False
        try:
            create_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            for _attempt in range(10):
                temporary_name = f".tmp-{secrets.token_hex(16)}"
                try:
                    fd = os.open(temporary_name, create_flags, FILE_MODE, dir_fd=directory_fd)
                    break
                except FileExistsError:
                    continue
            if fd is None:
                raise AssetError("A private temporary asset file could not be allocated")
            os.fchmod(fd, FILE_MODE)
            with os.fdopen(fd, "wb") as handle:
                fd = None
                handle.write(image.data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Publish within the same pinned directory inode, refusing to
                # replace an existing asset on an id collision.
                os.link(
                    temporary_name,
                    storage_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                published = True
            except FileExistsError as exc:
                raise AssetError("A managed asset id collision already exists") from exc
            os.unlink(temporary_name, dir_fd=directory_fd)
            temporary_name = ""
            os.fsync(directory_fd)
            directory_info = os.fstat(directory_fd)
            published_info = os.stat(storage_name, dir_fd=directory_fd, follow_symlinks=False)
        except BaseException:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if temporary_name:
                with contextlib.suppress(OSError):
                    os.unlink(temporary_name, dir_fd=directory_fd)
            if published:
                with contextlib.suppress(OSError):
                    os.unlink(storage_name, dir_fd=directory_fd)
            raise
    return PublishedAsset(
        storage_name=storage_name,
        directory_device=directory_info.st_dev,
        directory_inode=directory_info.st_ino,
        file_device=published_info.st_dev,
        file_inode=published_info.st_ino,
    )


def remove_managed_asset(published: PublishedAsset) -> None:
    """Remove a just-published asset relative to the pinned managed directory."""
    storage_name = published.storage_name
    if not storage_name or Path(storage_name).name != storage_name:
        raise AssetError("Managed asset integrity validation failed")
    with _open_managed_assets_directory() as (_root, directory_fd):
        directory_info = os.fstat(directory_fd)
        if (directory_info.st_dev, directory_info.st_ino) != (
            published.directory_device,
            published.directory_inode,
        ):
            raise AssetError("Managed asset cleanup refused a changed storage directory")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(storage_name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return
        try:
            file_info = os.fstat(fd)
            linked_info = os.stat(storage_name, dir_fd=directory_fd, follow_symlinks=False)
            expected = (published.file_device, published.file_inode)
            if (file_info.st_dev, file_info.st_ino) != expected or (
                linked_info.st_dev,
                linked_info.st_ino,
            ) != expected:
                raise AssetError("Managed asset cleanup refused a changed asset file")
            os.unlink(storage_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(fd)


def verify_managed_asset(row: Any) -> None:
    """Fail closed unless a row's managed bytes remain private and authentic."""
    name = str(row["storage_name"])
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise AssetError("Managed asset integrity validation failed")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    with _open_managed_assets_directory() as (_root, directory_fd):
        try:
            fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise AssetError("Managed asset integrity validation failed") from exc
        try:
            info = os.fstat(fd)
            expected_size = int(row["byte_size"])
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_size != expected_size
            ):
                raise AssetError("Managed asset integrity validation failed")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(expected_size + 1)
            if len(data) != expected_size or hashlib.sha256(data).hexdigest() != str(row["sha256"]):
                raise AssetError("Managed asset integrity validation failed")
        finally:
            os.close(fd)


def safe_metadata(
    row: Any, *, deduplicated: bool, metadata_conflicts: list[str] | None = None
) -> dict[str, Any]:
    """Tool-safe projection: no storage name, prompt, source path, or owner ids."""
    result: dict[str, Any] = {
        "ok": True,
        "asset_id": str(row["id"]),
        "title": str(row["title"]),
        "decorative": bool(row["decorative"]),
        "provenance": str(row["provenance"]),
        "mime_type": str(row["mime_type"]),
        "byte_size": int(row["byte_size"]),
        "width": int(row["width"]),
        "height": int(row["height"]),
        "sha256": str(row["sha256"]),
        "track_id": row["track_id"],
        "created_at": str(row["created_at"]),
        "deduplicated": deduplicated,
    }
    if deduplicated:
        result["deduplication_policy"] = "first_import_metadata_is_immutable"
    if metadata_conflicts:
        result["metadata_conflicts"] = sorted(metadata_conflicts)
    if row["alt_text"] is not None:
        result["alt_text"] = str(row["alt_text"])
    return result
