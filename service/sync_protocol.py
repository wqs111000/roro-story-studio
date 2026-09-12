"""Versioned desktop-to-NAS content synchronisation.

The desktop is the publishing authority.  The NAS only installs complete,
digest-verified published releases and switches a pointer after the batch is
ready.  No draft or review file is exposed by this protocol.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from review_store import ReviewError, ReviewStore, atomic_write_json, read_json, sha256_file


class SyncError(RuntimeError):
    pass


STORY_RE = re.compile(r"[A-Za-z0-9._-]+")
REVISION_RE = re.compile(r"[A-Za-z0-9._-]+")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def safe_relative(value: str) -> str:
    pure = PurePosixPath(str(value).replace("\\", "/"))
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise SyncError(f"unsafe asset path: {value}")
    return pure.as_posix()


def safe_child(root: Path, *parts: str) -> Path:
    candidate = (root.joinpath(*parts)).resolve()
    if root.resolve() not in candidate.parents:
        raise SyncError("path escapes sync root")
    return candidate


def source_identity(runtime_root: Path) -> str:
    path = runtime_root / "sync-source-id"
    value = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    if not re.fullmatch(r"[A-Za-z0-9-]{16,80}", value):
        value = uuid.uuid4().hex
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
    return value


def _manifest_assets(manifest: dict[str, Any]) -> dict[str, Any]:
    assets = manifest.get("assets")
    hashes = manifest.get("asset_hashes")
    if not isinstance(assets, dict) or not isinstance(hashes, dict) or not hashes:
        raise SyncError("published manifest has no asset hashes")
    paths: list[str] = []
    for key in ("story", "storyboard", "narration", "cover", "primary_audio"):
        if not isinstance(assets.get(key), str):
            raise SyncError(f"published manifest is missing {key}")
        paths.append(safe_relative(assets[key]))
    pages = assets.get("pages")
    if not isinstance(pages, list) or not pages:
        raise SyncError("published manifest has no pages")
    paths.extend(safe_relative(item) for item in pages)
    extras = assets.get("extras", [])
    if not isinstance(extras, list):
        raise SyncError("published manifest extras are invalid")
    paths.extend(safe_relative(item) for item in extras)
    normalized_hashes = {safe_relative(path): str(value) for path, value in hashes.items()}
    if set(paths) != set(normalized_hashes):
        raise SyncError("published manifest assets and hashes differ")
    return {"assets": assets, "asset_hashes": dict(sorted(normalized_hashes.items()))}


def build_catalog(approved_root: Path, runtime_root: Path, service_data_root: Path | None = None, character_root: Path | None = None) -> dict[str, Any]:
    """Build a complete, explicit desktop publication snapshot."""
    approved_root = approved_root.resolve()
    store = ReviewStore(approved_root.parent)
    stories: list[dict[str, Any]] = []
    if not approved_root.is_dir():
        raise SyncError("approved root is missing")
    for story_root in sorted(approved_root.iterdir(), key=lambda item: item.name):
        if not story_root.is_dir() or story_root.name.startswith("."):
            continue
        # approved/ also contains approved character-form resources. They are
        # published through service/data/characters.json and do not have a
        # story release pointer of their own.
        if not (story_root / "current.json").is_file():
            continue
        story_id = story_root.name
        if not STORY_RE.fullmatch(story_id):
            raise SyncError(f"invalid story id: {story_id}")
        pointer = read_json(story_root / "current.json")
        release_id = str(pointer.get("release_id", ""))
        if not REVISION_RE.fullmatch(release_id):
            raise SyncError(f"invalid release for {story_id}")
        release = safe_child(story_root, "releases", release_id)
        manifest_path = safe_child(release, "package-manifest.json")
        manifest = read_json(manifest_path)
        if manifest.get("story_id") not in {None, story_id} or manifest.get("release_id") not in {None, release_id}:
            raise SyncError(f"manifest identity mismatch: {story_id}")
        assets = _manifest_assets(manifest)
        for relative, expected in assets["asset_hashes"].items():
            path = safe_child(release, relative)
            if not path.is_file() or sha256_file(path) != expected:
                raise SyncError(f"published asset failed verification: {story_id}/{relative}")
        status = store.shelf_status(story_id)
        story = read_json(release / "story.json")
        manifest_digest = str(manifest.get("package_digest", ""))
        pointer_digest = str(pointer.get("package_digest", ""))
        if manifest_digest and pointer_digest and manifest_digest != pointer_digest:
            raise SyncError(f"release digest pointer mismatch: {story_id}")
        entry = {
            "story_id": story_id,
            "title": manifest.get("title") or story.get("title", story_id),
            "status": str(status.get("status", "published")),
            "release_id": release_id,
            "package_digest": pointer_digest or manifest_digest,
            "manifest": manifest,
            "manifest_sha256": sha256_file(manifest_path),
            "asset_hashes": assets["asset_hashes"],
            "updated_at": str(status.get("updated_at", "")),
        }
        if not entry["package_digest"]:
            raise SyncError(f"release has no package digest: {story_id}")
        stories.append(entry)
    characters_root = (service_data_root or approved_root.parent / "service" / "data").resolve()
    characters = characters_root / "characters.json"
    character_document = read_json(characters) if characters.is_file() else {"schema_version": 1, "characters": []}
    character_root = (character_root or approved_root.parent / "service" / "assets" / "characters").resolve()
    character_assets: dict[str, str] = {}
    def collect_assets(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                collect_assets(child)
        elif isinstance(value, list):
            for child in value:
                collect_assets(child)
        elif isinstance(value, str) and value.startswith("/character-assets/"):
            name = safe_relative(value.removeprefix("/character-assets/"))
            path = safe_child(character_root, name)
            if not path.is_file():
                raise SyncError(f"character asset is missing: {name}")
            character_assets[name] = sha256_file(path)
    collect_assets(character_document)
    payload = {
        "schema_version": 1,
        "source_id": source_identity(runtime_root),
        "characters_revision": digest_json(character_document),
        "characters": character_document,
        "character_assets": dict(sorted(character_assets.items())),
        "stories": stories,
    }
    payload["catalog_revision"] = digest_json(payload)
    payload["catalog_digest"] = digest_json(payload)
    return payload


def sync_asset_url(base_url: str, story_id: str, release_id: str, relative: str) -> str:
    encoded = "/".join(urllib.parse.quote(part, safe="") for part in safe_relative(relative).split("/"))
    return f"{base_url.rstrip('/')}/api/sync/assets/{urllib.parse.quote(story_id, safe='')}/{urllib.parse.quote(release_id, safe='')}/{encoded}"


class SyncManager:
    def __init__(self, approved_root: Path, runtime_root: Path, source_url: str = "", on_commit: Callable[[], None] | None = None, service_data_root: Path | None = None, character_root: Path | None = None):
        self.approved_root = approved_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.source_url = source_url.rstrip("/")
        self.service_data_root = (service_data_root or self.approved_root.parent / "service" / "data").resolve()
        self.character_root = (character_root or self.approved_root.parent / "service" / "assets" / "characters").resolve()
        self.on_commit = on_commit
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.state_path = self.runtime_root / "sync-state.json"
        self.settings_path = self.runtime_root / "sync-settings.json"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        if not self.settings_path.is_file():
            atomic_write_json(self.settings_path, {"enabled": False, "interval_minutes": 5, "source_url": self.source_url})

    def settings(self) -> dict[str, Any]:
        value = read_json(self.settings_path)
        try:
            interval = int(value.get("interval_minutes", 5))
        except (TypeError, ValueError):
            interval = 5
        candidate_url = str(value.get("source_url") or self.source_url).strip().rstrip("/")
        parsed = urllib.parse.urlsplit(candidate_url)
        source = candidate_url if parsed.scheme in {"http", "https"} and parsed.netloc and parsed.path in {"", "/"} else ""
        return {"enabled": bool(value.get("enabled", False)), "interval_minutes": max(1, min(1440, interval)), "source_url": source}

    def update_settings(self, enabled: bool | None = None, interval_minutes: int | None = None, source_url: str | None = None) -> dict[str, Any]:
        current = self.settings()
        if enabled is not None:
            current["enabled"] = bool(enabled)
        if interval_minutes is not None:
            current["interval_minutes"] = max(1, min(1440, int(interval_minutes)))
        if source_url is not None and source_url.strip():
            parsed = urllib.parse.urlsplit(source_url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
                raise SyncError("sync source must be an HTTP(S) origin")
            current["source_url"] = source_url.rstrip("/")
        atomic_write_json(self.settings_path, current)
        return current

    def status(self) -> dict[str, Any]:
        state = read_json(self.state_path)
        state.setdefault("state", "idle")
        state["settings"] = self.settings()
        state["running"] = bool(self.thread and self.thread.is_alive())
        return state

    def start(self, automatic: bool = False) -> dict[str, Any]:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.status()
            self.thread = threading.Thread(target=self._run, args=(automatic,), daemon=True, name="roro-sync")
            self.thread.start()
        return self.status()

    def start_auto_loop(self) -> None:
        threading.Thread(target=self._auto_loop, daemon=True, name="roro-sync-scheduler").start()

    def _auto_loop(self) -> None:
        while True:
            settings = self.settings()
            if settings["enabled"] and settings["source_url"]:
                self.start(automatic=True)
            threading.Event().wait(settings["interval_minutes"] * 60)

    def _write_state(self, **values: Any) -> None:
        current = read_json(self.state_path)
        current.update(values, updated_at=now())
        atomic_write_json(self.state_path, current)

    def _request_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Roro-NAS-Sync/1"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as error:
            raise SyncError(f"desktop update source unavailable: {error}") from error
        if not isinstance(value, dict):
            raise SyncError("desktop update index is invalid")
        return value

    def _download(self, url: str, target: Path, expected: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Roro-NAS-Sync/1"})
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as stream:
                shutil.copyfileobj(response, stream, length=1024 * 1024)
            if sha256_file(temporary) != expected:
                raise SyncError(f"download digest mismatch: {target.name}")
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _install_story(self, entry: dict[str, Any], base_url: str) -> None:
        story_id = str(entry["story_id"])
        release_id = str(entry["release_id"])
        if not STORY_RE.fullmatch(story_id) or not REVISION_RE.fullmatch(release_id):
            raise SyncError("invalid remote story identity")
        final_story = safe_child(self.approved_root, story_id)
        final_release = safe_child(final_story, "releases", release_id)
        stage = safe_child(self.approved_root, ".sync-staging", f"{story_id}-{release_id}-{uuid.uuid4().hex}")
        try:
            for relative, expected in dict(entry["asset_hashes"]).items():
                self._download(sync_asset_url(base_url, story_id, release_id, relative), safe_child(stage, relative), str(expected))
            stage.mkdir(parents=True, exist_ok=True)
            manifest_path = stage / "package-manifest.json"
            manifest_path.write_text(json.dumps(entry["manifest"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if sha256_file(manifest_path) != entry.get("manifest_sha256"):
                raise SyncError(f"manifest digest mismatch: {story_id}")
            final_story.mkdir(parents=True, exist_ok=True)
            final_release.parent.mkdir(parents=True, exist_ok=True)
            if final_release.exists():
                existing_manifest = read_json(final_release / "package-manifest.json")
                if existing_manifest.get("package_digest") not in {None, "", entry["package_digest"]}:
                    raise SyncError(f"immutable release conflict: {story_id}/{release_id}")
                shutil.rmtree(stage)
            else:
                stage.replace(final_release)
            atomic_write_json(final_story / "current.json", {"release_id": release_id, "package_digest": entry["package_digest"], "updated_at": now()})
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    def _install_characters(self, catalog: dict[str, Any], base_url: str, previous: dict[str, Any]) -> None:
        expected_assets = dict(catalog.get("character_assets", {}))
        assets_ready = self.character_root.is_dir() and all(
            safe_child(self.character_root, safe_relative(name)).is_file()
            and sha256_file(safe_child(self.character_root, safe_relative(name))) == expected
            for name, expected in expected_assets.items()
        )
        if catalog.get("characters_revision") == previous.get("characters_revision") and assets_ready:
            return
        self.character_root.mkdir(parents=True, exist_ok=True)
        for name, expected in dict(catalog.get("character_assets", {})).items():
            self._download(
                f"{base_url.rstrip('/')}/api/sync/character-assets/" + "/".join(urllib.parse.quote(part, safe="") for part in safe_relative(name).split("/")),
                safe_child(self.character_root, safe_relative(name)),
                str(expected),
            )
        self.service_data_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.service_data_root / "characters.json", catalog.get("characters", {"schema_version": 1, "characters": []}))

    def _apply_shelf_status(self, entries: list[dict[str, Any]]) -> None:
        status_path = self.runtime_root / "shelf-status.json"
        current = read_json(status_path)
        for entry in entries:
            current[str(entry["story_id"])] = {"status": entry.get("status", "published"), "release_id": entry.get("release_id", ""), "updated_at": now(), "updated_by": "desktop-sync"}
        atomic_write_json(status_path, current)

    def _run(self, automatic: bool) -> None:
        settings = self.settings()
        base_url = settings["source_url"]
        if not base_url:
            self._write_state(state="failed", error="未配置桌面端地址", automatic=automatic)
            return
        self._write_state(state="checking", error="", automatic=automatic, started_at=now())
        try:
            catalog = self._request_json(base_url + "/api/sync/catalog")
            entries = catalog.get("stories")
            if not isinstance(entries, list) or not isinstance(catalog.get("catalog_digest"), str) or catalog.get("catalog_digest") != digest_json({key: value for key, value in catalog.items() if key != "catalog_digest"}):
                raise SyncError("desktop update index is incomplete")
            previous = read_json(self.runtime_root / "sync-catalog.json")
            if previous.get("source_id") and previous.get("source_id") != catalog.get("source_id"):
                raise SyncError("桌面端发布源已变化，需要重新配对")
            old = {str(item.get("story_id")): item for item in previous.get("stories", []) if isinstance(item, dict)}
            remote_ids = {str(item.get("story_id")) for item in entries if isinstance(item, dict)}
            changed = [item for item in entries if isinstance(item, dict) and item.get("status") == "published" and (old.get(str(item.get("story_id")), {}).get("package_digest") != item.get("package_digest") or not safe_child(self.approved_root, str(item.get("story_id")), "current.json").is_file())]
            removed = [
                {"story_id": story_id, "release_id": old_entry.get("release_id", ""), "status": "unpublished"}
                for story_id, old_entry in old.items()
                if story_id not in remote_ids
            ] if previous else []
            self._write_state(state="downloading", found=len(changed) + len(removed), total=len(entries), catalog_revision=catalog.get("catalog_revision"))
            self._install_characters(catalog, base_url, previous)
            for item in changed:
                self._install_story(item, base_url)
            self._apply_shelf_status([item for item in entries if isinstance(item, dict)] + removed)
            atomic_write_json(self.runtime_root / "sync-catalog.json", catalog)
            self._write_state(state="complete", found=len(changed) + len(removed), completed_at=now(), error="")
            if self.on_commit:
                self.on_commit()
        except Exception as error:  # keep the old shelf serving on every failure
            self._write_state(state="failed", error=str(error), completed_at=now())
