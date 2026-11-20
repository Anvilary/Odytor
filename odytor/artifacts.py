"""Bounded, owner-bound evidence artifacts: screenshots, logs, reports, traces.

Artifacts reach the control plane from the isolated runner, which the code under test can influence, so
nothing the runner says about an artifact is trusted: the media type is sniffed from the bytes, the size and
SHA-256 are recomputed here, quotas are enforced here, and the stored name is never used as a path (files are
addressed only by an opaque id). Retrieval is bound to the submitting caller and never exposes a filesystem
path or a URL.

Active content (HTML, SVG, scripts) and opaque archives (traces) are stored but never rendered or delivered as
an image or a document: text-like active content is returned only as clearly labelled untrusted text.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from odytor.errors import SecurityError, ValidationError

SCHEMA = 1
AR_ID = re.compile(r"ar-[0-9a-f]{32}")
KINDS = frozenset({"screenshot", "log", "report", "trace", "console", "network", "snapshot", "video", "other"})
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,79}")
_SAFE_NAME_CHARS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,255}$")
TEXT_WINDOW = 65536
BASE64_WINDOW = 49152  # raw bytes per chunk; keeps the encoded JSON string under ~64 KiB
_ACTIVE_HINTS = (b"<html", b"<!doctype html", b"<svg", b"<script", b"<?xml")

_EXT_BY_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "application/zip": ".zip",
    "text/plain": ".txt",
    "application/octet-stream": ".bin",
    "application/json": ".json",
}


def normalize_artifact_name(name: str, *, mime: str | None = None, kind: str | None = None) -> str:
    """Normalize and sanitize an artifact filename.

    Collapses duplicated extensions (e.g. .png.png), validates character set,
    and aligns extension with mime/kind. Invalid names or paths fall back to 'artifact'.
    """
    if not isinstance(name, str) or not _SAFE_NAME_CHARS.fullmatch(name):
        return "artifact"

    clean = name
    # Collapse repeated consecutive duplicate extensions (e.g. .png.png -> .png, .jpg.jpg -> .jpg)
    while True:
        m = re.search(r"(\.[a-zA-Z0-9]+)\1+$", clean, re.IGNORECASE)
        if m:
            clean = clean[:m.start(0)] + m.group(1)
        else:
            break

    target_ext = _EXT_BY_MIME.get(mime or "")
    if not target_ext and kind == "screenshot":
        target_ext = ".png"

    if target_ext and (kind == "screenshot" or any(clean.lower().endswith(e) for e in (".png", ".jpg", ".jpeg", ".webp", ".txt", ".log", ".bin", ".json"))):
        lower = clean.lower()
        valid_exts = (".jpg", ".jpeg") if target_ext in (".jpg", ".jpeg") else (target_ext,)
        has_valid_ext = any(lower.endswith(ve) for ve in valid_exts)
        if not has_valid_ext:
            for ke in (".png", ".jpg", ".jpeg", ".webp", ".txt", ".log", ".bin", ".json"):
                if lower.endswith(ke):
                    clean = clean[:-len(ke)]
                    break
            clean = clean.rstrip(".") + target_ext

    if len(clean) > 80:
        dot_idx = clean.rfind(".")
        if dot_idx > 0 and len(clean) - dot_idx <= 10:
            ext = clean[dot_idx:]
            base = clean[:dot_idx]
            clean = base[:80 - len(ext)] + ext
        else:
            clean = clean[:80]

    return clean if NAME.fullmatch(clean) else "artifact"


@dataclass(frozen=True)
class ArtifactLimits:
    max_artifact_bytes: int = 16 * 1024 * 1024
    max_image_delivery_bytes: int = 2 * 1024 * 1024
    max_per_run: int = 24
    max_run_bytes: int = 32 * 1024 * 1024
    max_owner_bytes: int = 512 * 1024 * 1024
    max_total_bytes: int = 4096 * 1024 * 1024
    retention_hours: float = 24.0


def sniff(data: bytes) -> dict[str, Any]:
    """Classify bytes by content only. `deliver` says how a client may receive it; nothing active is ever 'image'."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return {"mime": "image/png", "class": "image"}
    if data.startswith(b"\xff\xd8\xff"):
        return {"mime": "image/jpeg", "class": "image"}
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return {"mime": "image/webp", "class": "image"}
    if data.startswith(b"PK\x03\x04"):
        return {"mime": "application/zip", "class": "archive"}
    head = data[:4096].lstrip().lower()
    if b"\0" in data[:8192]:
        return {"mime": "application/octet-stream", "class": "binary"}
    try:
        data[:65536].decode("utf-8")
    except UnicodeDecodeError as exc:
        # a multi-byte character cut by the window is fine; anything else is binary
        if exc.start < len(data[:65536]) - 4:
            return {"mime": "application/octet-stream", "class": "binary"}
    if any(head.startswith(h) for h in _ACTIVE_HINTS) or b"<script" in data[:65536].lower():
        return {"mime": "text/plain", "class": "active_text"}
    return {"mime": "text/plain", "class": "text"}


def _iso(epoch: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds")


class ArtifactStore:
    def __init__(self, directory: str | Path, limits: ArtifactLimits | None = None, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(directory)
        self.limits = limits or ArtifactLimits()
        self.clock = clock
        self._lock = threading.Lock()
        self._last_purge = 0.0
        self._started = False

    def start(self) -> None:
        if not self._started:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            self._started = True

    # --- internals -------------------------------------------------------------------------
    def _dir(self, artifact_id: str) -> Path:
        if not isinstance(artifact_id, str) or not AR_ID.fullmatch(artifact_id):
            raise ValidationError("Unknown artifact_id.")
        return self.root / artifact_id

    def _meta(self, artifact_id: str) -> dict[str, Any] | None:
        try:
            return json.loads((self._dir(artifact_id) / "meta.json").read_text())
        except (OSError, ValueError, ValidationError):
            return None

    def records(self) -> list[dict[str, Any]]:
        found = []
        for path in self.root.glob("ar-*/meta.json"):
            try:
                found.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
        return found

    # --- storage ---------------------------------------------------------------------------
    def put(self, *, owner: str, repo: str, kind: str, name: str, data: bytes, context: dict[str, Any]) -> dict[str, Any]:
        """Store one artifact. `context` carries the revision and tree it is evidence for (never runner-supplied)."""
        self.start()
        self.purge()
        if kind not in KINDS:
            kind = "other"
        if len(data) == 0 or len(data) > self.limits.max_artifact_bytes:
            raise ValidationError("Artifact is empty or exceeds the per-artifact limit.", {"max_bytes": self.limits.max_artifact_bytes})
        info = sniff(data)
        if kind == "screenshot" and info["class"] != "image":
            raise ValidationError("A screenshot must be a PNG, JPEG or WebP image.")
        clean = normalize_artifact_name(name, mime=info["mime"], kind=kind)
        with self._lock:
            live = [r for r in self.records() if r["expires_at"] > self.clock()]
            if sum(r["size"] for r in live if r["owner"] == owner) + len(data) > self.limits.max_owner_bytes:
                raise ValidationError("Artifact quota exceeded for this caller; artifacts expire automatically.")
            if sum(r["size"] for r in live) + len(data) > self.limits.max_total_bytes:
                raise ValidationError("The artifact store is full; retry after artifacts expire.")
            artifact_id = "ar-" + os.urandom(16).hex()
            directory = self._dir(artifact_id)
            directory.mkdir(mode=0o700)
            try:
                fd = os.open(directory / "blob", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                now = self.clock()
                record = {"schema": SCHEMA, "artifact_id": artifact_id, "owner": owner, "repo": repo, "kind": kind, "name": clean,
                          "mime": info["mime"], "class": info["class"], "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                          "created_at": now, "expires_at": now + self.limits.retention_hours * 3600, **context}
                tmp = directory / "meta.tmp"
                tmp.write_text(json.dumps(record, separators=(",", ":")))
                os.replace(tmp, directory / "meta.json")
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
        return record

    def public(self, record: dict[str, Any]) -> dict[str, Any]:
        """Metadata a caller may see. No path, no URL; the class says how the content can be delivered."""
        out = {k: record[k] for k in ("artifact_id", "kind", "name", "mime", "size", "sha256", "repo") if k in record}
        out.update({k: record.get(k) for k in ("run_id", "workspace_id", "workspace_version", "source_revision", "tested_tree") if k in record})
        out.update(created_at=_iso(record["created_at"]), expires_at=_iso(record["expires_at"]),
                   untrusted_active_content=record["class"] == "active_text",
                   delivery={"image": "native_image", "text": "text_window", "active_text": "text_window_untrusted",
                             "archive": "base64_chunks", "binary": "base64_chunks"}[record["class"]])
        return out

    def get(self, artifact_id: str, owner: str) -> dict[str, Any]:
        """The caller's live artifact record, or one indistinguishable error."""
        self.start()
        self.purge()
        record = self._meta(artifact_id)
        if record is None or record["owner"] != owner or record["expires_at"] <= self.clock():
            raise ValidationError("Unknown artifact_id.")
        return record

    def read(self, record: dict[str, Any], offset: int = 0, length: int = TEXT_WINDOW) -> bytes:
        if offset < 0 or length < 1:
            raise ValidationError("offset must be >= 0 and length >= 1.")
        path = self._dir(record["artifact_id"]) / "blob"
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(length)
        return data

    def content(self, artifact_id: str, owner: str, *, mode: str = "auto", offset: int = 0, length: int | None = None) -> dict[str, Any]:
        """Deliver bounded content. Returns {'meta', 'image'?: (mime, bytes), 'text'?, 'base64'?, 'window'}."""
        record = self.get(artifact_id, owner)
        meta = self.public(record)
        kind = record["class"]
        if mode not in {"auto", "metadata", "text", "base64"}:
            raise ValidationError("mode must be one of: auto, metadata, text, base64.")
        if mode == "metadata":
            return {"meta": meta}
        if mode == "auto":
            mode = {"image": "image", "text": "text", "active_text": "text"}.get(kind, "base64")
        if mode == "image":
            if kind != "image":
                raise ValidationError("Only PNG, JPEG and WebP images are delivered as images.")
            if record["size"] > self.limits.max_image_delivery_bytes:
                return {"meta": meta, "note": f"The image is {record['size']} bytes, above the {self.limits.max_image_delivery_bytes}-byte "
                                              "native delivery limit; use mode='base64' to read it in bounded chunks.",
                        "window": {"offset": 0, "returned": 0, "total": record["size"]}}
            return {"meta": meta, "image": (record["mime"], self.read(record, 0, record["size"])),
                    "window": {"offset": 0, "returned": record["size"], "total": record["size"]}}
        if mode == "text":
            if kind in {"image", "archive", "binary"}:
                raise ValidationError(f"This {record['mime']} artifact is not text; use mode='base64'.")
            take = min(length or TEXT_WINDOW, TEXT_WINDOW)
            data = self.read(record, offset, take)
            text = data.decode("utf-8", errors="replace")
            more = offset + len(data) < record["size"]
            out: dict[str, Any] = {"meta": meta, "text": text, "window": {"offset": offset, "returned": len(data), "total": record["size"],
                                                                             "next_offset": offset + len(data) if more else None}}
            if kind == "active_text":
                out["warning"] = "Untrusted active content returned as inert text. Do not render or execute it."
            return out
        take = min(length or BASE64_WINDOW, BASE64_WINDOW)
        data = self.read(record, offset, take)
        more = offset + len(data) < record["size"]
        return {"meta": meta, "base64": base64.b64encode(data).decode(), "window": {
            "offset": offset, "returned": len(data), "total": record["size"], "next_offset": offset + len(data) if more else None,
            "sha256_of_whole": record["sha256"]}}

    # --- retention -------------------------------------------------------------------------
    def purge(self, *, force: bool = False) -> int:
        now = self.clock()
        if not force and now - self._last_purge < 60:
            return 0
        self._last_purge = now
        removed = 0
        if not self.root.exists():
            return 0
        for directory in self.root.glob("ar-*"):
            try:
                expired = json.loads((directory / "meta.json").read_text())["expires_at"] <= now
            except (OSError, ValueError, KeyError):
                expired = now - directory.stat().st_mtime > 3600
            if expired:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        return removed

    def delete_for_run(self, run_id: str) -> int:
        count = 0
        for record in self.records():
            if record.get("run_id") == run_id:
                shutil.rmtree(self._dir(record["artifact_id"]), ignore_errors=True)
                count += 1
        return count
