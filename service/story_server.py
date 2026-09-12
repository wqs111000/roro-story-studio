#!/usr/bin/env python3
"""Small dependency-free HTTP service for approved Roro stories."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
import wave
import uuid
from datetime import datetime, timezone
from email.utils import formatdate
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from PIL import Image

from narration_alignment import alignment_issues, narration_pages, page_number
from review_store import ReviewError, ReviewStore, atomic_write_json, read_json, sha256_file
from sync_protocol import SyncError, SyncManager, build_catalog, safe_child, safe_relative

SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
from build_picture_book import build_from_assets  # noqa: E402


AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg"}
PDF_LAYOUT_VERSION = 10
# Internal audio-profile identifiers are intentionally kept out of the parent-facing UI.
# This is the single source of truth for the narration-voice display mapping.
VOICE_DISPLAY_MAP = {
    "lady": {"label": "温柔女声", "mark": "温"},
    "roro-01": {"label": "可爱女声", "mark": "可"},
    "girl": {"label": "清亮童声", "mark": "清"},
    "man": {"label": "沉稳男声", "mark": "稳"},
    "还不错的男生": {"label": "成熟男声", "mark": "熟"},
    "wqs": {"label": "低沉男声", "mark": "低"},
    "huihui": {"label": "亲切女声", "mark": "亲"},
}
VOICE_ORDER = {voice: index for index, voice in enumerate(VOICE_DISPLAY_MAP)}
CATALOG_CACHE_SECONDS = 8
WORKBENCH_RECENT_DRAFT_DAYS = 14
WORKBENCH_RECENT_DISCARDED_LIMIT = 6
mimetypes.add_type("image/webp", ".webp")


class CatalogCache:
    """Small shared cache for filesystem-derived page catalogues."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, object]] = {}
        self._lock = threading.RLock()

    def get(self, key: str, builder):
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry[0] > now:
                return entry[1]
            value = builder()
            # A bind-mounted media directory can take longer to scan than the
            # cache TTL. Start the lifetime after the expensive build finishes.
            self._entries[key] = (time.monotonic() + CATALOG_CACHE_SECONDS, value)
            return value

    def invalidate(self, *keys: str) -> None:
        with self._lock:
            if keys:
                for key in keys:
                    self._entries.pop(key, None)
            else:
                self._entries.clear()


class ReviewAuth:
    """Review session store with an explicit development bypass mode."""

    def __init__(
        self,
        pin: str | None,
        mode: str = "pin",
        lifetime_seconds: int = 12 * 60 * 60,
        max_failed_attempts: int = 5,
        failure_window_seconds: int = 5 * 60,
        lockout_seconds: int = 5 * 60,
    ):
        if mode not in {"development", "pin"}:
            raise ValueError(f"Unsupported review auth mode: {mode}")
        if mode == "pin" and not re.fullmatch(r"\d{6}", str(pin or "")):
            raise ValueError("PIN review mode requires exactly six digits")
        self.pin = str(pin or "")
        self.mode = mode
        self.lifetime_seconds = lifetime_seconds
        self.max_failed_attempts = max_failed_attempts
        self.failure_window_seconds = failure_window_seconds
        self.lockout_seconds = lockout_seconds
        self.sessions: dict[str, dict] = {}
        self.failed_logins: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.development_session = {
            "csrf": secrets.token_urlsafe(24),
            "expires_at": float("inf"),
        }

    def login(self, pin: str, client_key: str = "local") -> dict:
        if self.mode == "development":
            return {"session_id": "development", **self.development_session}
        now = time.time()
        client_key = str(client_key or "unknown")[:200]
        with self.lock:
            failure = self.failed_logins.get(client_key, {})
            locked_until = float(failure.get("locked_until", 0))
            if locked_until > now:
                raise ReviewError("login_rate_limited", "PIN 尝试次数过多，请稍后再试。", 429)
            first_failed_at = float(failure.get("first_failed_at", 0))
            if first_failed_at and now - first_failed_at > self.failure_window_seconds:
                failure = {}

            if not hmac.compare_digest(str(pin), self.pin):
                attempts = int(failure.get("attempts", 0)) + 1
                failure = {
                    "attempts": attempts,
                    "first_failed_at": first_failed_at or now,
                    "locked_until": now + self.lockout_seconds if attempts >= self.max_failed_attempts else 0,
                }
                self.failed_logins[client_key] = failure
                if failure["locked_until"]:
                    raise ReviewError("login_rate_limited", "PIN 尝试次数过多，请稍后再试。", 429)
                raise ReviewError("invalid_pin", "家长 PIN 不正确。", 401)

            self.failed_logins.pop(client_key, None)
            expired = [session_id for session_id, value in self.sessions.items() if value["expires_at"] <= now]
            for session_id in expired:
                self.sessions.pop(session_id, None)
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        expires_at = now + self.lifetime_seconds
        with self.lock:
            self.sessions[session_id] = {"csrf": csrf, "expires_at": expires_at}
        return {"session_id": session_id, "csrf": csrf, "expires_at": expires_at}

    def session(self, session_id: str) -> dict | None:
        if self.mode == "development":
            return dict(self.development_session)
        if not session_id:
            return None
        with self.lock:
            value = self.sessions.get(session_id)
            if not value:
                return None
            if value["expires_at"] <= time.time():
                self.sessions.pop(session_id, None)
                return None
            return dict(value)

    def logout(self, session_id: str) -> None:
        if self.mode == "development":
            return
        with self.lock:
            self.sessions.pop(session_id, None)


class StoryHandler(SimpleHTTPRequestHandler):
    server_version = "RoroStoryService/2.0"

    def __init__(
        self,
        *args,
        directory: str,
        review_store: ReviewStore,
        review_auth: ReviewAuth,
        catalog_cache: CatalogCache | None = None,
        public_origin: str = "",
        display_only: bool = False,
        sync_manager: SyncManager | None = None,
        **kwargs,
    ):
        self.approved_root = Path(directory).resolve()
        self.workspace_root = self.approved_root.parent
        self.review_store = review_store
        self.review_auth = review_auth
        self.catalog_cache = catalog_cache
        self.public_origin = public_origin
        self.display_only = display_only
        self.sync_manager = sync_manager
        self._range: tuple[int, int] | None = None
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def do_GET(self) -> None:
        route = urlsplit(getattr(self, "path", "")).path
        if self._display_route_blocked(route):
            self.send_error(HTTPStatus.NOT_FOUND, "Not available on the display server")
            return
        if route == "/health":
            self._send_json(
                {
                    "status": "ok",
                    "service": "roro-story-service",
                    "version": "2.0",
                    "review_mode": self.review_auth.mode,
                    "display_only": getattr(self, "display_only", False),
                    "time": datetime.now(timezone.utc).isoformat(),
                }
            )
            return
        if route == "/api/sync/catalog":
            try:
                catalog = build_catalog(
                    self.approved_root,
                    self._sync_runtime_root(),
                    service_data_root=Path(__file__).resolve().parent / "data",
                    character_root=Path(__file__).resolve().parent / "assets" / "characters",
                )
                self._send_json(catalog, headers={"ETag": f'"{catalog["catalog_digest"]}"'})
            except (OSError, ReviewError, SyncError) as error:
                self._send_json_error("sync_catalog_unavailable", str(error), 503)
            return
        asset_match = re.fullmatch(r"/api/sync/assets/([^/]+)/([^/]+)/(.*)", route)
        if asset_match:
            self._send_sync_asset(*[unquote(value) for value in asset_match.groups()])
            return
        character_match = re.fullmatch(r"/api/sync/character-assets/(.*)", route)
        if character_match:
            self._send_sync_character_asset(unquote(character_match.group(1)))
            return
        if route == "/api/sync/status":
            if self.sync_manager is None:
                self._send_json_error("sync_unavailable", "当前服务未启用 NAS 同步。", 404)
            else:
                self._send_json(self.sync_manager.status())
            return
        if route == "/api/stories":
            self._send_json({"stories": self._cached_catalog("approved_stories", self._approved_stories)})
            return
        if route.startswith("/api/stories/"):
            story_id = route.removeprefix("/api/stories/").strip("/")
            if story_id and "/" not in story_id:
                detail = self._approved_story_detail(story_id)
                if detail is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Story not found")
                else:
                    self._send_json(detail)
                return
        if route.startswith("/api/shares/"):
            token = route.removeprefix("/api/shares/").strip("/")
            if token and "/" not in token:
                try:
                    share = self.review_store.resolve_share(unquote(token))
                    detail = self._approved_story_detail(str(share["story_id"]))
                    if detail is None:
                        raise ReviewError("share_unavailable", "这个绘本当前暂时不能分享。", 410)
                    detail = {**detail, "share": {key: share[key] for key in ("share_id", "release_id", "expires_at", "allow_download") if key in share}}
                    if not share.get("allow_download"):
                        detail["download_url"] = ""
                    self._send_json(detail)
                except ReviewError as error:
                    self._send_json_error(error.code, str(error), error.status)
                return
        if route == "/api/studio/stories":
            self._send_json({"stories": self._cached_catalog("studio_stories", self._studio_stories)})
            return
        if route == "/api/studio/story-library":
            self._send_json(self._cached_catalog("story_library", self._story_library_payload))
            return
        if route.startswith("/api/studio/story-library/"):
            library_id = route.removeprefix("/api/studio/story-library/").strip("/")
            detail = self._story_library_detail(library_id)
            if detail is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Story candidate not found")
            else:
                self._send_json(detail)
            return
        if route == "/api/studio/review-queue":
            stories = self._cached_catalog("studio_stories", self._studio_stories)
            self._send_json({"stories": [item for item in stories if item.get("review_state") == "ready_for_parent"]})
            return
        match = re.fullmatch(r"/api/studio/stories/([A-Za-z0-9._-]+)/shares", route)
        if match:
            try:
                self._require_session()
                self._send_json({"shares": self.review_store.list_shares(match.group(1))})
            except ReviewError as error:
                self._send_json_error(error.code, str(error), error.status)
            return
        if route.startswith("/api/studio/preview/"):
            story_id = route.removeprefix("/api/studio/preview/").strip("/")
            if story_id and "/" not in story_id:
                detail = self._studio_preview_detail(story_id)
                if detail is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Story candidate not found")
                else:
                    self._send_json(detail)
                return
        if route == "/api/studio/auth/status":
            session = self._current_session()
            self._send_json(
                {
                    "authenticated": bool(session),
                    "csrf_token": session.get("csrf", "") if session else "",
                    "review_mode": self.review_auth.mode,
                    "parent_verification_required": self.review_auth.mode == "pin",
                }
            )
            return
        if route.startswith("/api/studio/stories/"):
            suffix = route.removeprefix("/api/studio/stories/").strip("/")
            if suffix.endswith("/decision-history"):
                story_id = suffix.removesuffix("/decision-history").strip("/")
                if story_id and "/" not in story_id:
                    self._send_json(self.review_store.load_parent_review(story_id) or {"schema_version": 1, "current": {}, "history": []})
                    return
            story_id = suffix
            if story_id and "/" not in story_id:
                detail = self._studio_story_detail(story_id)
                if detail is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Story not found")
                else:
                    self._send_json(detail)
                return
        if route == "/api/characters":
            self._send_json({"characters": self._cached_catalog("approved_characters", self._approved_characters)})
            return
        if route in {"/", "/library", "/library.html"}:
            self._send_library()
            return
        if route in {"/characters", "/characters.html"}:
            self._send_characters()
            return
        if route in {"/workbench", "/workbench.html"}:
            self._send_ui("workbench.html")
            return
        if route in {"/app-icon.svg", "/app-icon.png"}:
            self._send_project_asset("project-icon" + Path(route).suffix)
            return
        if route.startswith("/studio-preview/"):
            story_id = route.removeprefix("/studio-preview/").strip("/")
            if story_id and "/" not in story_id and self._studio_preview_detail(story_id) is not None:
                self._send_ui("player.html")
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Story candidate not found")
            return
        if route.startswith("/player/"):
            story_id = route.removeprefix("/player/").strip("/")
            if story_id and "/" not in story_id and self._approved_story_detail(story_id) is not None:
                self._send_ui("player.html")
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Story not found")
            return
        if route.startswith("/share/"):
            token = route.removeprefix("/share/").strip("/")
            if token and "/" not in token:
                try:
                    self.review_store.resolve_share(unquote(token))
                    self._send_ui("player.html")
                except ReviewError as error:
                    self.send_error(error.status, str(error))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Share not found")
            return
        if route.startswith("/download/"):
            story_id = route.removeprefix("/download/").strip("/")
            if story_id and "/" not in story_id:
                self._send_published_pdf(story_id)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Story not found")
            return
        if route.startswith("/studio-assets/"):
            self._send_studio_asset(route)
            return
        if route.startswith("/studio-thumbnails/"):
            self._send_studio_thumbnail(route)
            return
        if route.startswith("/character-assets/"):
            self._send_character_asset(route)
            return
        super().do_GET()

    def do_POST(self) -> None:
        route = urlsplit(getattr(self, "path", "")).path
        if getattr(self, "display_only", False) and not route.startswith("/api/sync/"):
            self._send_json_error("display_only", "此服务仅供浏览和播放，制作审核请在电脑端操作。", 403)
            return
        route = urlsplit(getattr(self, "path", "")).path
        try:
            self._require_same_origin()
            if route == "/api/sync/start":
                if self.sync_manager is None:
                    self._send_json_error("sync_unavailable", "当前服务未启用 NAS 同步。", 404)
                    return
                self._send_json(self.sync_manager.start())
                return
            if route == "/api/sync/settings":
                if self.sync_manager is None:
                    self._send_json_error("sync_unavailable", "当前服务未启用 NAS 同步。", 404)
                    return
                payload = self._read_json_body()
                self._send_json(self.sync_manager.update_settings(
                    enabled=payload.get("enabled") if "enabled" in payload else None,
                    interval_minutes=payload.get("interval_minutes") if "interval_minutes" in payload else None,
                ))
                return
            if route == "/api/studio/auth/login":
                payload = self._read_json_body()
                session = self.review_auth.login(str(payload.get("pin", "")), self.client_address[0])
                cookie = self._review_cookie(session["session_id"], self.review_auth.lifetime_seconds)
                self._send_json({"ok": True, "authenticated": True, "csrf_token": session["csrf"]}, headers={"Set-Cookie": cookie})
                return
            if route == "/api/studio/auth/logout":
                session_id = self._session_id()
                session = self._require_session()
                self._require_csrf(session)
                self.review_auth.logout(session_id)
                self._send_json({"ok": True}, headers={"Set-Cookie": self._review_cookie("", 0)})
                return
            match = re.fullmatch(r"/api/studio/stories/([A-Za-z0-9._-]+)/shares", route)
            if match:
                session = self._require_session()
                self._require_csrf(session)
                story_id = match.group(1)
                if self.command == "POST":
                    payload = self._read_json_body()
                    days = payload.get("expires_in_days", 7)
                    try:
                        days = int(days)
                    except (TypeError, ValueError):
                        raise ReviewError("invalid_share_expiry", "分享有效期不正确。")
                    result = self.review_store.create_share(
                        story_id,
                        expires_in_days=days,
                        allow_download=bool(payload.get("allow_download", False)),
                        actor=("local-development-workbench" if self.review_auth.mode == "development" else "parent-workbench"),
                    )
                    result["share_url"] = f"/share/{quote(result['token'])}"
                    self._send_json({"ok": True, "share": result}, status=HTTPStatus.CREATED)
                    return
            match = re.fullmatch(r"/api/studio/stories/([A-Za-z0-9._-]+)/shares/([A-Za-z0-9._-]+)/revoke", route)
            if match:
                session = self._require_session()
                self._require_csrf(session)
                result = self.review_store.revoke_share(
                    match.group(1), match.group(2),
                    actor=("local-development-workbench" if self.review_auth.mode == "development" else "parent-workbench"),
                )
                self._send_json({"ok": True, "share": result})
                return
            if route == "/api/studio/story-library/selection":
                session = self._require_session()
                self._require_csrf(session)
                payload = self._read_json_body()
                story_ids = payload.get("story_ids", [])
                if not isinstance(story_ids, list) or any(not isinstance(item, str) for item in story_ids):
                    raise ReviewError("invalid_selection", "制作清单格式不正确。", 400)
                available = {item["id"] for item in self._story_library_items() if item.get("selectable")}
                selected = sorted({item for item in story_ids if item in available})
                path = self._story_library_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                _, discarded = self._story_library_state()
                document = self._read_json(path)
                atomic_write_json(path, {"schema_version": 1, "story_ids": selected, "discarded_ids": sorted(discarded), "discarded_history": document.get("discarded_history", []), "updated_at": datetime.now(timezone.utc).isoformat()})
                self._invalidate_catalogs("studio_stories", "story_library")
                self._send_json({"ok": True, "story_ids": selected})
                return
            if route == "/api/studio/story-library/disposition":
                session = self._require_session()
                self._require_csrf(session)
                payload = self._read_json_body()
                story_id = str(payload.get("story_id", ""))
                action = str(payload.get("action", ""))
                if action not in {"discard", "restore"}:
                    raise ReviewError("invalid_disposition", "备选故事操作不正确。", 400)
                available = {item["id"] for item in self._story_library_items()}
                if story_id not in available:
                    raise ReviewError("story_not_found", "没有找到这个备选故事。", 404)
                selected, discarded = self._story_library_state()
                path = self._story_library_path()
                document = self._read_json(path)
                history = document.get("discarded_history", [])
                if not isinstance(history, list):
                    history = []
                history = [entry for entry in history if isinstance(entry, dict) and entry.get("story_id") != story_id]
                if action == "discard":
                    selected.discard(story_id)
                    discarded.add(story_id)
                    history.append({"story_id": story_id, "discarded_at": datetime.now(timezone.utc).isoformat()})
                else:
                    discarded.discard(story_id)
                history = history[-100:]
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(path, {"schema_version": 1, "story_ids": sorted(selected), "discarded_ids": sorted(discarded), "discarded_history": history, "updated_at": datetime.now(timezone.utc).isoformat()})
                self._invalidate_catalogs("studio_stories", "story_library")
                self._send_json({"ok": True, "story_id": story_id, "action": action})
                return
            match = re.fullmatch(r"/api/studio/stories/([A-Za-z0-9._-]+)/decision", route)
            if match:
                session = self._require_session()
                self._require_csrf(session)
                payload = self._read_json_body()
                result = self.review_store.decide(
                    match.group(1),
                    decision=str(payload.get("decision", "")),
                    candidate_revision=str(payload.get("candidate_revision", "")),
                    package_digest=str(payload.get("package_digest", "")),
                    request_id=str(payload.get("request_id", "")),
                    note=str(payload.get("note", "")),
                    issue_tags=payload.get("issue_tags", []) if isinstance(payload.get("issue_tags", []), list) else [],
                    actor=(
                        "local-development-workbench"
                        if self.review_auth.mode == "development"
                        else "parent-workbench"
                    ),
                )
                self._invalidate_catalogs()
                self._send_json(result)
                return
            match = re.fullmatch(r"/api/studio/stories/([A-Za-z0-9._-]+)/shelf", route)
            if match:
                session = self._require_session()
                self._require_csrf(session)
                payload = self._read_json_body()
                action = str(payload.get("action", ""))
                if action not in {"publish", "unpublish"}:
                    raise ReviewError("invalid_shelf_action", "书架操作不正确。", 400)
                result = self.review_store.set_shelf_status(
                    match.group(1),
                    status="published" if action == "publish" else "unpublished",
                    reason=str(payload.get("reason", "")),
                    release_id=str(payload.get("release_id", "")),
                    actor=("local-development-workbench" if self.review_auth.mode == "development" else "parent-workbench"),
                )
                self._invalidate_catalogs()
                self._send_json({"ok": True, "action": action, "shelf": result})
                return
            match = re.fullmatch(r"/api/studio/stories/([A-Za-z0-9._-]+)/pdf", route)
            if match:
                session = self._require_session()
                self._require_csrf(session)
                self._export_candidate_pdf(match.group(1))
                return
            self._send_json_error("not_found", "接口不存在。", 404)
        except ReviewError as error:
            self._send_json_error(error.code, str(error), error.status)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self._send_json_error("invalid_request", str(error) or "请求格式不正确。", 400)
        except Exception as error:
            print(f"Review write failed: {error}", file=sys.stderr, flush=True)
            self._send_json_error("internal_error", "服务暂时无法完成写入，旧书架内容未改变。", 500)

    def do_HEAD(self) -> None:
        route = urlsplit(self.path).path
        if self._display_route_blocked(route):
            self.send_error(HTTPStatus.NOT_FOUND, "Not available on the display server")
            return
        if route in {"/health", "/api/stories", "/api/characters", "/api/studio/stories", "/api/studio/story-library", "/api/studio/review-queue", "/api/studio/auth/status"} or route.startswith("/api/stories/") or route.startswith("/api/studio/stories/") or route.startswith("/api/studio/preview/"):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            return
        if route.startswith("/character-assets/"):
            self._send_character_asset(route, head_only=True)
            return
        if route.startswith("/download/"):
            story_id = route.removeprefix("/download/").strip("/")
            if story_id and "/" not in story_id:
                self._send_published_pdf(story_id, head_only=True)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Story not found")
            return
        if route.startswith("/studio-assets/"):
            self._send_studio_asset(route, head_only=True)
            return
        if route.startswith("/studio-thumbnails/"):
            self._send_studio_thumbnail(route, head_only=True)
            return
        if route in {"/app-icon.svg", "/app-icon.png"}:
            self._send_project_asset("project-icon" + Path(route).suffix, head_only=True)
            return
        if route.startswith("/studio-preview/"):
            story_id = route.removeprefix("/studio-preview/").strip("/")
            if story_id and "/" not in story_id and self._studio_preview_detail(story_id) is not None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Story candidate not found")
            return
        if route.startswith("/player/"):
            story_id = route.removeprefix("/player/").strip("/")
            if story_id and "/" not in story_id and self._approved_story_detail(story_id) is not None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Story not found")
            return
        super().do_HEAD()

    def _session_id(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get("roro_review_session")
        return morsel.value if morsel else ""

    def _review_cookie(self, session_id: str, max_age: int) -> str:
        cookie = f"roro_review_session={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        if urlsplit(self.headers.get("Origin", "")).scheme == "https":
            cookie += "; Secure"
        return cookie

    def _current_session(self) -> dict | None:
        return self.review_auth.session(self._session_id())

    def _require_session(self) -> dict:
        session = self._current_session()
        if not session:
            raise ReviewError("authentication_required", "请输入家长 PIN 后再提交审核。", 401)
        return session

    def _require_csrf(self, session: dict) -> None:
        supplied = self.headers.get("X-Roro-CSRF", "")
        if not supplied or not hmac.compare_digest(supplied, str(session.get("csrf", ""))):
            raise ReviewError("invalid_csrf", "审核会话已失效，请重新验证家长 PIN。", 403)

    def _require_same_origin(self) -> None:
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        allowed = {f"http://{host}", f"https://{host}"}
        if self.public_origin:
            allowed.add(self.public_origin)
        if not origin or not host or origin not in allowed:
            raise ReviewError("invalid_origin", "只允许从当前工作台提交审核。", 403)

    def _read_json_body(self) -> dict:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ReviewError("invalid_content_type", "写接口只接受 JSON。", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ReviewError("invalid_request", "请求长度不正确。") from error
        if length <= 0 or length > 64 * 1024:
            raise ReviewError("invalid_request_size", "请求内容为空或过大。", 413)
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ReviewError("invalid_request", "请求必须是 JSON 对象。")
        return payload

    def _studio_story_directories(self):
        drafts_root = self.workspace_root / "drafts"
        if not drafts_root.is_dir():
            return []
        selected, discarded = self._story_library_state()
        discarded_at = self._story_library_discarded_at()
        recent_discarded = {
            story_id
            for story_id, _ in sorted(
                discarded_at.items(), key=lambda item: item[1], reverse=True
            )[:WORKBENCH_RECENT_DISCARDED_LIMIT]
        }
        recent_discarded.update(story_id for story_id in discarded if story_id not in discarded_at)
        cutoff = time.time() - WORKBENCH_RECENT_DRAFT_DAYS * 24 * 60 * 60
        shelf_path = (
            self.review_store.shelf_status_path()
            if hasattr(self, "review_store")
            else self.workspace_root / "service" / ".runtime" / "shelf-status.json"
        )
        shelf_status = self._read_json(shelf_path)
        directories = []
        for item in sorted(drafts_root.iterdir(), reverse=True):
            if not item.is_dir() or not (item / "story.json").is_file():
                continue
            story_id = item.name
            queue_id = f"draft-{story_id}"
            approved_root = getattr(self, "approved_root", self.workspace_root / "approved")
            approved_pointer = approved_root / story_id / "current.json"
            shelf = shelf_status.get(story_id, {}) if isinstance(shelf_status, dict) else {}
            # Published immutable releases are represented by the approved shelf
            # catalogue below. Keep unpublished releases here so the workbench
            # can still show their shelf reason and restore action.
            if approved_pointer.is_file() and shelf.get("status", "published") != "unpublished":
                continue
            relevant_paths = (
                item,
                item / "story.json",
                item / "review",
                item / "review" / "candidate-manifest.json",
                item / "review" / "ai-review.json",
                item / "review" / "parent-review.json",
            )
            latest_mtime = max(
                (path.stat().st_mtime for path in relevant_paths if path.exists()),
                default=0,
            )
            if (
                queue_id in selected
                or queue_id in recent_discarded
                or latest_mtime >= cutoff
            ):
                directories.append(item)
        return directories

    def _story_library_path(self) -> Path:
        return self.workspace_root / "inbox" / "story-production-queue.json"

    def _story_library_state(self) -> tuple[set[str], set[str]]:
        payload = self._read_json(self._story_library_path())
        selected_value = payload.get("story_ids", [])
        discarded_value = payload.get("discarded_ids", [])
        selected = {item for item in selected_value if isinstance(item, str)} if isinstance(selected_value, list) else set()
        discarded = {item for item in discarded_value if isinstance(item, str)} if isinstance(discarded_value, list) else set()
        return selected - discarded, discarded

    def _story_library_discarded_at(self) -> dict[str, str]:
        payload = self._read_json(self._story_library_path())
        history = payload.get("discarded_history", [])
        if not isinstance(history, list):
            return {}
        return {
            entry["story_id"]: entry["discarded_at"]
            for entry in history
            if isinstance(entry, dict)
            and isinstance(entry.get("story_id"), str)
            and isinstance(entry.get("discarded_at"), str)
        }

    def _story_library_queue(self) -> set[str]:
        selected, _ = self._story_library_state()
        return selected

    def _story_library_items(self, summaries: dict[str, dict] | None = None) -> list[dict]:
        selected, discarded = self._story_library_state()
        discarded_at = self._story_library_discarded_at()
        items: list[dict] = []
        if summaries is not None:
            for story_id, summary in summaries.items():
                if int(summary.get("page_count", 0) or 0) < 4:
                    continue
                counts = summary.get("asset_counts", {})
                review_state = summary.get("review_state", "authoring")
                if summary.get("published"):
                    library_state, status_label = "published", "已上架"
                elif review_state == "ready_for_parent":
                    library_state, status_label = "review", "待最终审核"
                elif review_state in {"changes_requested", "ai_review_failed", "stale", "publish_failed"}:
                    library_state, status_label = "revision", "返修中"
                elif counts.get("page_images") or counts.get("audio") or counts.get("has_cover"):
                    library_state, status_label = "production", "制作中"
                else:
                    library_state, status_label = "ready", "可加入制作"
                if library_state != "ready":
                    continue
                queue_id = f"draft-{story_id}"
                is_discarded = queue_id in discarded
                items.append({
                    "id": queue_id, "story_id": story_id,
                    "title": summary.get("title", story_id), "source": "draft", "source_label": "备选故事", "source_path": story_id,
                    "synopsis": summary.get("synopsis", ""), "theme": summary.get("theme", "原创故事"),
                    "status": "已丢弃" if is_discarded else status_label,
                    "library_state": "discarded" if is_discarded else library_state,
                    "source_mode": summary.get("source_mode", "original_adventure"), "page_count": summary.get("page_count", 0),
                    "cover_url": summary.get("cover_url", ""), "workbench_story_id": story_id,
                    "selectable": not is_discarded, "selected": queue_id in selected and not is_discarded,
                    "discarded": is_discarded,
                    "discarded_at": discarded_at.get(queue_id, summary.get("updated_at", "")) if is_discarded else "",
                    "updated_at": summary.get("updated_at", ""),
                })
            return self._visible_story_library_items(items)
        for directory in self._studio_story_directories():
            story_path = directory / "story.json"
            if not story_path.is_file():
                continue
            story = self._read_json(story_path)
            narration = self._read_json(directory / "narration.json")
            spoken_by_page = narration_pages(story, narration) if narration else {}
            narration_issues = alignment_issues(story, narration) if narration else []
            story_id = directory.name
            if not isinstance(story.get("pages"), list) or len(story.get("pages", [])) < 4:
                continue
            summary = (summaries or {}).get(story_id) or self._studio_story_summary(directory)
            counts = summary.get("asset_counts", {})
            review_state = summary.get("review_state", "authoring")
            if summary.get("published"):
                library_state = "published"
                status_label = "已上架"
            elif review_state == "ready_for_parent":
                library_state = "review"
                status_label = "待最终审核"
            elif review_state in {"changes_requested", "ai_review_failed", "stale", "publish_failed"}:
                library_state = "revision"
                status_label = "返修中"
            elif counts.get("page_images") or counts.get("audio") or counts.get("has_cover"):
                library_state = "production"
                status_label = "制作中"
            else:
                library_state = "ready"
                status_label = "可加入制作"
            if library_state != "ready":
                continue
            queue_id = f"draft-{story_id}"
            is_discarded = queue_id in discarded
            items.append({
                "id": queue_id, "story_id": story_id,
                "title": story.get("title", story_id), "source": "draft",
                "source_label": "备选故事", "source_path": story_id,
                "synopsis": story.get("synopsis", ""), "theme": story.get("theme", "原创故事"),
                "status": "已丢弃" if is_discarded else status_label,
                "library_state": "discarded" if is_discarded else library_state,
                "source_mode": story.get("source_mode", "original_adventure"),
                "page_count": len(story["pages"]),
                "pages": [
                    {
                        "page": page.get("page", index + 1),
                        "scene": page.get("scene", ""),
                        "narration": page.get("narration", ""),
                        "dialogue": page.get("dialogue", []),
                        "interaction": page.get("interaction"),
                        **spoken_by_page.get(page_number(page.get("page", index + 1)) or 0, {}),
                    }
                    for index, page in enumerate(story["pages"])
                    if isinstance(page, dict)
                ],
                "cover_url": summary.get("cover_url", ""),
                "cover_spoken_text": f"《{story.get('title', story_id)}》。" if narration else "",
                "narration_status": "needs_attention" if narration_issues else ("ready" if narration else "pending"),
                "narration_issues": narration_issues,
                "workbench_story_id": story_id,
                "selectable": not is_discarded,
                "selected": queue_id in selected and not is_discarded,
                "discarded": is_discarded,
                "discarded_at": discarded_at.get(queue_id, summary.get("updated_at", "")) if is_discarded else "",
                "updated_at": summary.get("updated_at", ""),
            })
        return self._visible_story_library_items(items)

    @staticmethod
    def _visible_story_library_items(items: list[dict]) -> list[dict]:
        candidates = [item for item in items if not item.get("discarded")]
        recent_discarded = sorted(
            (item for item in items if item.get("discarded")),
            key=lambda item: item.get("discarded_at", ""),
            reverse=True,
        )[:6]
        return candidates + recent_discarded

    def _story_library_detail(self, library_id: str) -> dict | None:
        if not re.fullmatch(r"draft-[A-Za-z0-9._-]+", library_id):
            return None
        story_id = library_id.removeprefix("draft-")
        summaries = {
            item["id"]: item
            for item in self._cached_catalog("studio_stories", self._studio_stories)
        }
        summary = summaries.get(story_id)
        if summary is None:
            return None
        item = next(iter(self._story_library_items({story_id: summary})), None)
        directory = self.workspace_root / "drafts" / story_id
        story = self._read_json(directory / "story.json")
        if item is None or not isinstance(story.get("pages"), list):
            return None
        narration = self._read_json(directory / "narration.json")
        spoken_by_page = narration_pages(story, narration) if narration else {}
        item["pages"] = [
            {
                "page": page.get("page", index + 1), "scene": page.get("scene", ""),
                "narration": page.get("narration", ""), "dialogue": page.get("dialogue", []),
                "interaction": page.get("interaction"),
                **spoken_by_page.get(page_number(page.get("page", index + 1)) or 0, {}),
            }
            for index, page in enumerate(story["pages"]) if isinstance(page, dict)
        ]
        issues = alignment_issues(story, narration) if narration else []
        item["cover_spoken_text"] = f"《{story.get('title', story_id)}》。" if narration else ""
        item["narration_status"] = "needs_attention" if issues else ("ready" if narration else "pending")
        item["narration_issues"] = issues
        return item

    def _story_library_payload(self) -> dict:
        # Workbench opens the production board and the candidate library
        # together. Reuse the already-expensive draft summaries instead of
        # rescanning every story directory for the second endpoint.
        summaries = {
            item["id"]: item
            for item in self._cached_catalog("studio_stories", self._studio_stories)
        }
        items = self._story_library_items(summaries)
        return {
            "items": items,
            "selected_ids": [item["id"] for item in items if item.get("selected")],
            "discarded_ids": [item["id"] for item in items if item.get("discarded")],
            "source_summary": {
                "generation": "日常改编只使用家长提供的真实生活素材；原创冒险从主题轮换生成；母题再创作只借用通用叙事结构并全部重写",
                "library": "仅收录已有完整正文的备选故事；灵感稿不会进入这里",
                "production": "加入后进入制作清单；丢弃只是移入可恢复区域，都不会自动插图、配音、审核或上架",
            },
        }

    def _cached_catalog(self, key: str, builder):
        cache = getattr(self, "catalog_cache", None)
        return cache.get(key, builder) if cache is not None else builder()

    def _invalidate_catalogs(self, *keys: str) -> None:
        cache = getattr(self, "catalog_cache", None)
        if cache is not None:
            cache.invalidate(*keys)

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _asset_version(asset: Path) -> str:
        stat = asset.stat()
        return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"

    def _studio_asset_url(self, story_id: str, relative: Path) -> str:
        asset = self._candidate_asset_file(story_id, str(relative))
        suffix = f"?v={self._asset_version(asset)}" if asset and asset.is_file() else ""
        return f"/studio-assets/{quote(story_id)}/{quote(relative.as_posix())}{suffix}"

    def _studio_thumbnail_url(self, story_id: str, relative: Path) -> str:
        asset = self._candidate_asset_file(story_id, str(relative))
        suffix = f"?v={self._asset_version(asset)}" if asset and asset.is_file() else ""
        return f"/studio-thumbnails/{quote(story_id)}/{quote(relative.as_posix())}{suffix}"

    @staticmethod
    def _audio_voice(filename: str) -> str:
        lower = filename.lower()
        for voice in VOICE_DISPLAY_MAP:
            if re.search(rf"(?:^|[-_]){re.escape(voice)}(?:[-_.]|$)", lower):
                return voice
        return ""

    def _audio_option(self, item: Path, url: str, primary: bool) -> dict:
        voice = self._audio_voice(item.name)
        display = VOICE_DISPLAY_MAP.get(voice, {"label": "特色声音", "mark": "声"})
        option = {
            "voice": voice or item.stem,
            "label": display["label"],
            "mark": display["mark"],
            "url": url,
            "filename": item.name,
            "kiroro": "kiroro" in item.stem.lower(),
            "primary": primary,
            "duration_seconds": self._audio_duration(item),
        }
        sync = self._read_json(item.with_suffix(".sync.json"))
        pages = sync.get("pages", []) if sync.get("audio") == item.name else []
        timeline = []
        previous_end = 0.0
        for page in pages if isinstance(pages, list) else []:
            if not isinstance(page, dict):
                continue
            try:
                page_number = int(page.get("page", 0))
                start = float(page.get("start_seconds", -1))
                end = float(page.get("end_seconds", -1))
            except (TypeError, ValueError):
                continue
            if page_number <= 0 or start < previous_end or end <= start:
                timeline = []
                break
            timeline.append(
                {
                    "page": page_number,
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                }
            )
            previous_end = end
        if timeline:
            option["page_timeline"] = timeline
            option["timing_mode"] = "voice_exact"
        else:
            option["page_timeline"] = []
            option["timing_mode"] = "estimated"
        return option

    def _published_audio_options(self, audio_files: list[Path], primary_audio: Path, asset_base: str) -> list[dict]:
        ordered = sorted(
            audio_files,
            key=lambda item: (
                0 if item == primary_audio else 1,
                VOICE_ORDER.get(self._audio_voice(item.name), len(VOICE_ORDER)),
                item.name.lower(),
            ),
        )
        return [
            self._audio_option(
                item,
                f"{asset_base}/audio/{quote(item.name)}",
                item == primary_audio,
            )
            for item in ordered
        ]

    def _candidate_asset_file(self, story_id: str, relative: str) -> Path | None:
        try:
            _, path = self.review_store._asset_path(story_id, relative)
            return path
        except ReviewError:
            return None

    def _candidate_pdf_path(self, story_id: str, revision: str, package_digest: str) -> tuple[str, Path] | None:
        if not revision or not package_digest:
            return None
        root = self.review_store.draft_directory(story_id)
        relative = Path("exports", "pdf", revision, f"{story_id}.pdf")
        pdf = (root / relative).resolve()
        metadata = pdf.with_suffix(".pdf.json")
        if root not in pdf.parents or not pdf.is_file() or pdf.is_symlink():
            return None
        try:
            record = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if record.get("package_digest") != package_digest or record.get("layout_version") != PDF_LAYOUT_VERSION:
            return None
        return relative.as_posix(), pdf

    def _export_candidate_pdf(self, story_id: str) -> None:
        candidate = self.review_store.verify_candidate(story_id)
        ai_review = self.review_store.load_ai_review(story_id)
        if ai_review.get("result") != "passed" or ai_review.get("package_digest") != candidate["package_digest"]:
            raise ReviewError("internal_review_required", "候选尚未完成当前版本的内部审核。", 409)
        assets = candidate.get("assets", {})
        story_path = self._candidate_asset_file(story_id, str(assets.get("story", "story.json")))
        cover_path = self._candidate_asset_file(story_id, str(assets.get("cover", "")))
        page_paths = [self._candidate_asset_file(story_id, str(relative)) for relative in assets.get("pages", [])]
        if not story_path or not cover_path or any(path is None for path in page_paths):
            raise ReviewError("missing_asset", "候选缺少完整的封面或页面插图。", 409)
        relative = Path("exports", "pdf", candidate["candidate_revision"], f"{story_id}.pdf")
        root = self.review_store.draft_directory(story_id)
        output = root / relative
        existing = self._candidate_pdf_path(story_id, candidate["candidate_revision"], candidate["package_digest"])
        if existing:
            self._invalidate_catalogs("studio_stories", "story_library")
            self._send_json({"ok": True, "story_id": story_id, "candidate_revision": candidate["candidate_revision"], "package_digest": candidate["package_digest"], "pdf_url": self._studio_asset_url(story_id, Path(existing[0])), "cached": True})
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        try:
            build_from_assets(story_path, cover_path, [path for path in page_paths if path], temporary)
            os.replace(temporary, output)
            atomic_write_json(
                output.with_suffix(".pdf.json"),
                {
                    "schema_version": 1,
                    "story_id": story_id,
                    "candidate_revision": candidate["candidate_revision"],
                    "package_digest": candidate["package_digest"],
                    "layout_version": PDF_LAYOUT_VERSION,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        finally:
            if temporary.exists():
                temporary.unlink()
        self._invalidate_catalogs("studio_stories", "story_library")
        self._send_json({"ok": True, "story_id": story_id, "candidate_revision": candidate["candidate_revision"], "package_digest": candidate["package_digest"], "pdf_url": self._studio_asset_url(story_id, relative), "cached": False})

    def _studio_story_summary(self, directory: Path) -> dict:
        story = self._read_json(directory / "story.json")
        pages = story.get("pages", []) if isinstance(story.get("pages", []), list) else []
        page_count = len(pages)
        story_id = directory.name
        candidate = self.review_store.load_candidate(story_id)
        candidate_assets = candidate.get("assets", {}) if isinstance(candidate.get("assets"), dict) else {}
        page_relatives = candidate_assets.get("pages", []) if isinstance(candidate_assets.get("pages"), list) else []
        page_images = [item for item in (self._candidate_asset_file(story_id, str(relative)) for relative in page_relatives) if item]
        cover_relative = str(candidate_assets.get("cover", ""))
        cover = self._candidate_asset_file(story_id, cover_relative) if cover_relative else None
        primary_audio_relative = str(candidate_assets.get("primary_audio", ""))
        primary_audio = self._candidate_asset_file(story_id, primary_audio_relative) if primary_audio_relative else None
        image_dir = directory / "images"
        audio_dir = directory / "audio"
        images = sorted(image_dir.glob("*.png")) if image_dir.is_dir() else []
        if not candidate:
            page_images = [item for item in images if re.search(r"page[-_]0?\d", item.name, re.I)]
            cover = next((item for item in images if item.name.lower().startswith("cover")), None)
            primary_audio = next((item for item in sorted(audio_dir.iterdir()) if item.is_file() and item.suffix.lower() in {".wav", ".mp3", ".m4a", ".ogg"}), None) if audio_dir.is_dir() else None
        has_player = (directory / "player.html").is_file()
        review = self.review_store.state(story_id)
        pdf_info = self._candidate_pdf_path(story_id, review.get("candidate_revision", ""), review.get("package_digest", ""))
        has_pdf = pdf_info is not None
        review_state = review.get("state", "authoring")
        queue_id = f"draft-{story_id}"
        selected, discarded = self._story_library_state()
        stage_labels = {
            "authoring": "制作中",
            "ai_review_failed": "自动返修中",
            "ready_for_parent": "等待家长审核",
            "changes_requested": "已退回修改",
            "publishing": "正在上架",
            "published": "已上架",
            "publish_failed": "上架失败",
            "stale": "候选已变化",
        }
        stage = stage_labels.get(review_state, "制作中")
        ai_review = self.review_store.load_ai_review(story_id)
        ai_passed = ai_review.get("result") == "passed" and ai_review.get("package_digest") == review.get("package_digest")
        has_started_assets = bool(page_images or cover or primary_audio or candidate)
        production_selected = queue_id in selected or review_state != "authoring" or has_started_assets
        is_discarded = queue_id in discarded
        if queue_id in selected and review_state == "authoring" and not has_started_assets:
            production_phase = "queued"
        elif review_state in {"changes_requested", "ai_review_failed", "stale", "publish_failed"}:
            production_phase = "revision"
        elif review_state == "ready_for_parent":
            production_phase = "review"
        elif review_state == "published":
            production_phase = "published"
        else:
            production_phase = "active"
        if is_discarded:
            workflow_state = "discarded"
        elif review_state == "published":
            workflow_state = "published"
        elif review_state == "ready_for_parent":
            workflow_state = "review"
        elif production_selected:
            workflow_state = "production"
        else:
            workflow_state = "candidate"
        stages = {
            "text": {"label": "故事文本", "state": "ready" if (directory / "story.json").is_file() else "missing"},
            "storyboard": {"label": "角色与分镜", "state": "ready" if (directory / "storyboard.json").is_file() else "missing"},
            "visual": {"label": "封面与插图", "state": "ready" if cover and len(page_images) == page_count else ("partial" if page_images or cover else "missing"), "count": len(page_images), "total": page_count},
            "audio": {"label": "旁白音频", "state": "ready" if primary_audio else "missing", "count": 1 if primary_audio else 0},
            "internal_review": {"label": "内部质量检查", "state": "ready" if ai_passed else "missing"},
            "package": {"label": "书架发布", "state": "approved" if review_state == "published" else "missing"},
        }
        return {
            "id": story_id,
            "title": story.get("title", story_id),
            "status": review_state,
            "review_state": review_state,
            "published": bool(review.get("published")),
            "shelf_status": review.get("shelf_status", "published"),
            "shelf_reason": review.get("shelf", {}).get("reason", ""),
            "shelf_updated_at": review.get("shelf", {}).get("updated_at", ""),
            "current_release_id": review.get("shelf", {}).get("release_id", ""),
            "stage": stage,
            "theme": story.get("theme", "未设置主题"),
            "synopsis": story.get("synopsis", ""),
            "source_mode": story.get("source_mode", ""),
            "age_years": story.get("audience", {}).get("age_years", 4),
            "bedtime": bool(story.get("audience", {}).get("bedtime", False)),
            "page_count": page_count,
            "stages": stages,
            "asset_counts": {"images": len(images), "page_images": len(page_images), "audio": 1 if primary_audio else 0, "has_cover": bool(cover), "has_pdf": has_pdf, "has_player": has_player},
            # List pages receive a compact card image.  The review/player detail
            # below restores cover_url to the original master illustration.
            "cover_url": self._studio_thumbnail_url(story_id, Path(cover_relative)) if cover_relative and cover else "",
            "cover_full_url": self._studio_asset_url(story_id, Path(cover_relative)) if cover_relative and cover else "",
            "pdf_url": self._studio_asset_url(story_id, Path(pdf_info[0])) if pdf_info else "",
            "candidate_revision": review.get("candidate_revision", ""),
            "package_digest": review.get("package_digest", ""),
            "ai_review_passed": ai_passed,
            "production_selected": production_selected and not is_discarded,
            "production_phase": production_phase,
            "discarded": is_discarded,
            "workflow_state": workflow_state,
            "updated_at": datetime.fromtimestamp(directory.stat().st_mtime, timezone.utc).isoformat(),
        }

    def _studio_stories(self) -> list[dict]:
        stories = [self._studio_story_summary(directory) for directory in self._studio_story_directories()]
        published = []
        for item in self._cached_catalog("approved_stories", self._approved_stories):
            pointer = self._read_json(self.approved_root / item["id"] / "current.json")
            release_id = str(pointer.get("release_id", ""))
            published.append(
                {
                    "id": item["id"], "title": item["title"], "status": "published",
                    "review_state": "published", "published": True, "shelf_status": "published",
                    "shelf_reason": "", "shelf_updated_at": "", "current_release_id": release_id,
                    "stage": "已上架", "theme": item.get("theme", "原创故事"),
                    "synopsis": item.get("synopsis", ""), "source_mode": item.get("source_mode", ""),
                    "age_years": item.get("age_years", 4), "bedtime": bool(item.get("bedtime", False)),
                    "page_count": item.get("page_count", 0), "stages": {},
                    "asset_counts": {
                        "images": item.get("page_count", 0) + 1,
                        "page_images": item.get("page_count", 0),
                        "audio": 1 if item.get("audio_url") else 0,
                        "has_cover": bool(item.get("cover_url")),
                        "has_pdf": bool(item.get("pdf_url")), "has_player": True,
                    },
                    "cover_url": item.get("cover_url", ""), "cover_full_url": item.get("cover_url", ""),
                    "pdf_url": item.get("pdf_url", ""), "candidate_revision": release_id,
                    "package_digest": str(pointer.get("package_digest", "")), "ai_review_passed": True,
                    "production_selected": True, "production_phase": "published", "discarded": False,
                    "workflow_state": "published", "updated_at": item.get("production_time", ""),
                }
            )
        return published + stories

    def _studio_story_detail(self, story_id: str) -> dict | None:
        directory = next((item for item in self._studio_story_directories() if item.name == story_id), None)
        if directory is None or not (directory / "story.json").is_file():
            return None
        story = self._read_json(directory / "story.json")
        summary = self._studio_story_summary(directory)
        candidate = self.review_store.load_candidate(story_id)
        assets = candidate.get("assets", {}) if isinstance(candidate.get("assets"), dict) else {}
        narration_relative = str(assets.get("narration", ""))
        narration_path = self._candidate_asset_file(story_id, narration_relative) if narration_relative else directory / "narration.json"
        narration = self._read_json(narration_path) if narration_path and narration_path.is_file() else {}
        spoken_by_page = narration_pages(story, narration) if narration else {}
        narration_issues = alignment_issues(story, narration) if narration else []
        page_relatives = assets.get("pages", []) if isinstance(assets.get("pages"), list) else []
        page_map = {}
        for relative in page_relatives:
            image = self._candidate_asset_file(story_id, str(relative))
            match = re.search(r"page[-_]0?(\d+)", image.name, re.I) if image else None
            if image and match:
                page_map[int(match.group(1))] = (str(relative), image)
        pages = []
        for item in story.get("pages", []):
            if not isinstance(item, dict):
                continue
            page_no = page_number(item.get("page", len(pages) + 1))
            if page_no is None or page_no < 1:
                continue
            image_entry = page_map.get(page_no)
            pages.append({**item, **spoken_by_page.get(page_no, {}), "image_url": self._studio_asset_url(story_id, Path(image_entry[0])) if image_entry else "", "asset_state": "generated" if image_entry else "missing", "review_state": summary["review_state"]})
        primary_audio_relative = str(assets.get("primary_audio", ""))
        primary_audio = self._candidate_asset_file(story_id, primary_audio_relative) if primary_audio_relative else None
        audio_relatives = [primary_audio_relative] if primary_audio_relative else []
        extras = assets.get("extras", []) if isinstance(assets.get("extras"), list) else []
        audio_relatives.extend(
            str(relative)
            for relative in extras
            if Path(str(relative)).suffix.lower() in AUDIO_EXTENSIONS
        )
        audio_options = []
        seen_audio: set[str] = set()
        for relative in audio_relatives:
            if relative in seen_audio:
                continue
            seen_audio.add(relative)
            item = self._candidate_asset_file(story_id, relative)
            if item:
                audio_options.append(
                    self._audio_option(
                        item,
                        self._studio_asset_url(story_id, Path(relative)),
                        relative == primary_audio_relative,
                    )
                )
        audio_options.sort(
            key=lambda option: (
                0 if option["primary"] else 1,
                VOICE_ORDER.get(option["voice"], len(VOICE_ORDER)),
                option["filename"].lower(),
            )
        )
        ai_review = self.review_store.load_ai_review(story_id)
        parent_review = self.review_store.load_parent_review(story_id)
        return {
            **summary,
            "cover_url": summary.get("cover_full_url", summary.get("cover_url", "")),
            "story": story,
            "pages": pages,
            "audio_urls": [option["url"] for option in audio_options],
            "audio_options": audio_options,
            "default_audio_voice": audio_options[0]["voice"] if audio_options else "lady",
            "cover_spoken_text": f"《{story.get('title', story_id)}》。" if narration else "",
            "narration_status": "needs_attention" if narration_issues else ("ready" if narration else "pending"),
            "narration_issues": narration_issues,
            "ai_review": ai_review,
            "parent_review": parent_review,
            "files": {
                "story": self._studio_asset_url(story_id, Path(str(assets.get("story", "story.json")))),
                "storyboard": self._studio_asset_url(story_id, Path(str(assets.get("storyboard", "storyboard.json")))) if (directory / "storyboard.json").is_file() else "",
                "review": self._studio_asset_url(story_id, Path("review.md")) if (directory / "review.md").is_file() else "",
                "pdf": summary.get("pdf_url", ""),
            },
            "pdf_url": summary.get("pdf_url", ""),
        }

    def _studio_preview_detail(self, story_id: str) -> dict | None:
        """Return the manifest-bound candidate in the same shape as the player API."""
        detail = self._studio_story_detail(story_id)
        if detail is None or not detail.get("candidate_revision") or not detail.get("audio_options"):
            return None
        candidate = self.review_store.load_candidate(story_id)
        assets = candidate.get("assets", {}) if isinstance(candidate.get("assets"), dict) else {}
        narration_relative = str(assets.get("narration", ""))
        narration_path = self._candidate_asset_file(story_id, narration_relative) if narration_relative else None
        narration = self._read_json(narration_path) if narration_path else {}
        primary = next((option for option in detail["audio_options"] if option.get("primary")), detail["audio_options"][0])
        duration = float(primary.get("duration_seconds") or 0)
        primary_timeline = primary.get("page_timeline", [])
        starts = (
            {int(item["page"]): float(item["start_seconds"]) for item in primary_timeline}
            if primary_timeline
            else self._estimated_page_starts(detail["story"], narration, duration)
        )
        pages = [
            {**page, "start_seconds": starts.get(int(page.get("page", 0) or 0), 0)}
            for page in detail.get("pages", [])
        ]
        return {
            "id": detail["id"],
            "title": detail["title"],
            "status": "candidate_preview",
            "candidate_revision": detail["candidate_revision"],
            "package_digest": detail["package_digest"],
            "cover_url": detail["cover_url"],
            "pages": pages,
            "duration_seconds": duration,
            "audio_url": primary["url"],
            "audio_options": detail["audio_options"],
            "default_audio_voice": detail["default_audio_voice"],
        }

    def _send_studio_asset(self, route: str, head_only: bool = False) -> None:
        parts = route.removeprefix("/studio-assets/").split("/")
        if len(parts) < 2 or any(not part or part in {".", ".."} for part in parts):
            self.send_error(HTTPStatus.NOT_FOUND, "Asset not found")
            return
        story_id = parts[0]
        relative = Path(*parts[1:])
        if any(part in {".", ".."} for part in relative.parts) or not re.fullmatch(r"[A-Za-z0-9._/ -]+", "/".join(relative.parts)):
            self.send_error(HTTPStatus.NOT_FOUND, "Asset not found")
            return
        root = (self.workspace_root / "drafts" / story_id).resolve()
        asset = (root / relative).resolve()
        if root not in asset.parents or not asset.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Asset not found")
            return
        requested_version = parse_qs(urlsplit(self.path).query).get("v", [""])[0]
        cache_control = "public, max-age=31536000, immutable" if requested_version == self._asset_version(asset) else "no-cache"
        self._send_stream_file(asset, head_only=head_only, cache_control=cache_control)

    def _send_studio_thumbnail(self, route: str, head_only: bool = False) -> None:
        parts = route.removeprefix("/studio-thumbnails/").split("/")
        if len(parts) < 2 or any(not part or part in {".", ".."} for part in parts):
            self.send_error(HTTPStatus.NOT_FOUND, "Thumbnail not found")
            return
        story_id = unquote(parts[0])
        relative = Path(*(unquote(part) for part in parts[1:]))
        if any(part in {".", ".."} for part in relative.parts) or relative.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            self.send_error(HTTPStatus.NOT_FOUND, "Thumbnail not found")
            return
        asset = self._candidate_asset_file(story_id, str(relative))
        if asset is None or not asset.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Thumbnail not found")
            return
        requested_version = parse_qs(urlsplit(self.path).query).get("v", [""])[0]
        version = self._asset_version(asset)
        if requested_version != version:
            self.send_error(HTTPStatus.NOT_FOUND, "Thumbnail version expired")
            return
        thumbnail_root = self.review_store.shares_path().parent / "thumbnail-cache"
        source_key = hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()[:24]
        thumbnail = thumbnail_root / story_id / f"{source_key}-{version}-480.webp"
        temporary = thumbnail.with_name(f"{thumbnail.name}.{uuid.uuid4().hex}.tmp")
        try:
            if not thumbnail.is_file():
                thumbnail.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(asset) as image:
                    image.thumbnail((480, 480), Image.Resampling.LANCZOS)
                    if image.mode not in {"RGB", "RGBA"}:
                        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                    image.save(temporary, format="WEBP", quality=78, method=4)
                try:
                    os.replace(temporary, thumbnail)
                except PermissionError:
                    # Another request may have installed and opened the same
                    # version on Windows, which prevents replacing it again.
                    if not thumbnail.is_file():
                        raise
        except (OSError, ValueError):
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Thumbnail unavailable")
            return
        finally:
            temporary.unlink(missing_ok=True)
        self._send_stream_file(thumbnail, head_only=head_only, cache_control="public, max-age=31536000, immutable")

    def _send_stream_file(self, asset: Path, *, head_only: bool, cache_control: str) -> None:
        """Serve a local asset without buffering the whole file in memory.

        Media elements commonly issue Range requests. Supporting them here is
        important for candidate previews: a large WAV should become playable
        from its header/initial range instead of waiting for read_bytes() and a
        full response.
        """
        try:
            size = asset.stat().st_size
            stream = asset.open("rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Asset not found")
            return

        if size == 0:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", self.guess_type(str(asset)))
            self.send_header("Content-Length", "0")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            stream.close()
            return

        start, end = 0, size - 1
        range_header = self.headers.get("Range", "").strip()
        try:
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                if not match:
                    raise ValueError
                first, last = match.groups()
                if not first and not last:
                    raise ValueError
                if first:
                    start = int(first)
                    end = int(last) if last else end
                else:
                    suffix = int(last)
                    start = max(size - suffix, 0)
                if start >= size or start > end:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    stream.close()
                    return
                end = min(end, size - 1)

            status = HTTPStatus.PARTIAL_CONTENT if range_header else HTTPStatus.OK
            content_length = end - start + 1
            self.send_response(status)
            if range_header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Type", self.guess_type(str(asset)))
            self.send_header("Content-Length", str(content_length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            if head_only:
                stream.close()
                return
            stream.seek(start)
            remaining = content_length
            while remaining:
                chunk = stream.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        except (ValueError, OSError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Range header")
        finally:
            stream.close()

    def _approved_stories(self) -> list[dict]:
        stories: list[dict] = []
        for story_root in sorted(self.approved_root.iterdir(), reverse=True):
            if not story_root.is_dir() or story_root.name.startswith("."):
                continue
            directory = self.review_store.resolve_published_directory(story_root.name)
            if directory is None:
                continue
            if self.review_store.shelf_status(story_root.name).get("status") != "published":
                continue
            manifest_path = directory / "package-manifest.json"
            manifest = self._read_json(manifest_path)
            story = self._read_json(directory / "story.json")
            has_recorded_approval = (directory / "approval.md").is_file() or (directory / "approval.json").is_file()
            is_approved = manifest.get("status") == "approved" or (story.get("status") == "approved" and has_recorded_approval)
            if not is_approved:
                continue

            image_dir = directory / "images"
            audio_dir = directory / "audio"
            manifest_assets = manifest.get("assets", {}) if isinstance(manifest.get("assets"), dict) else {}
            cover_relative = str(manifest_assets.get("cover", ""))
            cover = directory / Path(cover_relative) if cover_relative else None
            if cover is None or not cover.is_file():
                cover = next(iter(sorted(image_dir.glob("cover*.png"))), None) if image_dir.is_dir() else None
                cover_relative = f"images/{cover.name}" if cover else ""
            page_relatives = manifest_assets.get("pages", []) if isinstance(manifest_assets.get("pages"), list) else []
            page_images = [directory / Path(str(relative)) for relative in page_relatives]
            page_images = [image for image in page_images if image.is_file()]
            if not page_images:
                page_images = sorted(image_dir.glob("page*.png")) if image_dir.is_dir() else []
            audio_files = sorted(
                item for item in audio_dir.iterdir()
                if item.is_file() and item.suffix.lower() in {".wav", ".mp3", ".m4a", ".ogg"}
            ) if audio_dir.is_dir() else []
            primary_audio_path = manifest.get("primary_audio_spec", {}).get("path", "")
            primary_audio = directory / primary_audio_path if primary_audio_path else None
            if primary_audio is None or not primary_audio.is_file():
                primary_audio = next((item for item in audio_files if "higgs-lady" in item.name), audio_files[0] if audio_files else None)
            if cover is None or not page_images or primary_audio is None:
                continue

            story_id = story_root.name
            slug = quote(story_id)
            relative_directory = directory.relative_to(self.approved_root)
            asset_base = "/" + "/".join(quote(part) for part in relative_directory.parts)
            storyboard = self._read_json(directory / "storyboard.json")
            audience = manifest.get("audience", {})
            audio = manifest.get("primary_audio_spec", {})
            audio_options = self._published_audio_options(audio_files, primary_audio, asset_base)
            # Older immutable releases may not contain an explicit cast list.
            # Recover it from the authoritative storyboard character bindings
            # without rewriting those releases.
            cast = manifest.get("cast", []) or self._cast_from_story(story, storyboard)
            cast = self._annotate_cast_forms(cast, story)
            approved_at = manifest.get("parent_review", {}).get("approved_at", "") or (story_id[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", story_id) else "")
            pdf_url = self._artifact_url(asset_base, manifest, ".pdf")
            if not pdf_url:
                pdf = next(iter(sorted(directory.glob("*.pdf"))), None)
                pdf_url = f"{asset_base}/{quote(pdf.name)}" if pdf else ""
            stories.append(
                {
                    "id": story_id,
                    "title": manifest.get("title") or story.get("title", story_id),
                    "status": "approved",
                    "theme": story.get("theme", "原创故事"),
                    "synopsis": story.get("synopsis", "一段属于佑佑和 Roro 的温柔冒险。"),
                    "source_mode": story.get("source_mode", "original_adventure"),
                    "age_years": audience.get("age_years") or story.get("audience", {}).get("age_years", 4),
                    "language": audience.get("language", "zh-CN"),
                    "bedtime": bool(audience.get("bedtime", story.get("audience", {}).get("bedtime", False))),
                    "page_count": len(story.get("pages", [])) if isinstance(story.get("pages"), list) else 0,
                    "duration_seconds": audio.get("duration_seconds") or self._audio_duration(primary_audio),
                    "cast": [
                        {
                            "character_id": member.get("character_id", ""),
                            "name": member.get("name", ""),
                            "type": member.get("type", ""),
                            "form_id": member.get("form_id", ""),
                        }
                        for member in cast
                        if isinstance(member, dict) and member.get("name")
                    ],
                    "production_time": self._story_production_time(directory),
                    "approved_at": approved_at,
                    "cover_url": f"{asset_base}/{quote(cover_relative)}",
                    # Keep playback timing in one maintained implementation. Older
                    # packaged players contain hard-coded page starts that drift
                    # behind the rendered narration.
                    "player_url": f"/player/{slug}",
                    "download_url": f"/download/{slug}",
                    "pdf_url": pdf_url,
                    "audio_url": f"{asset_base}/audio/{quote(primary_audio.name)}",
                    "audio_options": audio_options,
                    "default_audio_voice": audio_options[0]["voice"],
                }
            )
        stories.sort(key=lambda item: (item.get("production_time", ""), item["id"]), reverse=True)
        return stories

    def _story_production_time(self, directory: Path) -> str:
        catalog = self._read_json(Path(__file__).resolve().parent / "data" / "story-production-times.json")
        # Release names may be reused across books. Migration records use the
        # full relative path so Linux ctime changes cannot reorder the shelf.
        relative = directory.relative_to(self.approved_root).as_posix()
        for key in dict.fromkeys((relative, directory.name)):
            for item in catalog.get("stories", []):
                if isinstance(item, dict) and item.get("id") == key and item.get("production_time"):
                    return str(item["production_time"])
        return datetime.fromtimestamp(directory.stat().st_ctime, timezone.utc).isoformat()

    def _approved_story_detail(self, story_id: str) -> dict | None:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", story_id):
            return None
        summary = next((item for item in self._approved_stories() if item["id"] == story_id), None)
        if summary is None:
            return None
        directory = self.review_store.resolve_published_directory(story_id)
        if directory is None:
            return None
        relative_directory = directory.relative_to(self.approved_root)
        asset_base = "/" + "/".join(quote(part) for part in relative_directory.parts)
        story = self._read_json(directory / "story.json")
        narration = self._read_json(directory / "narration.json")
        manifest = self._read_json(directory / "package-manifest.json")
        manifest_assets = manifest.get("assets", {}) if isinstance(manifest.get("assets"), dict) else {}
        image_dir = directory / "images"
        page_map: dict[int, tuple[str, Path]] = {}
        page_relatives = manifest_assets.get("pages", []) if isinstance(manifest_assets.get("pages"), list) else []
        image_entries = [(str(relative), directory / Path(str(relative))) for relative in page_relatives]
        if not image_entries:
            image_entries = [(f"images/{image.name}", image) for image in image_dir.glob("page*.png")] if image_dir.is_dir() else []
        for relative, image in image_entries:
            match = re.search(r"page[-_]0?(\d+)", image.name, re.I)
            if match and image.is_file():
                page_map[int(match.group(1))] = (relative, image)
        primary_option = next(
            (option for option in summary.get("audio_options", []) if option.get("primary")),
            (summary.get("audio_options") or [{}])[0],
        )
        primary_timeline = primary_option.get("page_timeline", [])
        starts = (
            {int(item["page"]): float(item["start_seconds"]) for item in primary_timeline}
            if primary_timeline
            else self._estimated_page_starts(story, narration, float(summary.get("duration_seconds") or 0))
        )
        pages = []
        for index, item in enumerate(story.get("pages", [])):
            if not isinstance(item, dict):
                continue
            page_no = int(item.get("page", index + 1))
            image_entry = page_map.get(page_no)
            pages.append(
                {
                    "page": page_no,
                    "scene": item.get("scene", ""),
                    "narration": item.get("narration", ""),
                    "dialogue": item.get("dialogue", []),
                    "interaction": item.get("interaction"),
                    "image_url": f"{asset_base}/{quote(image_entry[0])}" if image_entry else "",
                    "start_seconds": starts.get(page_no, 0),
                }
            )
        return {**summary, "pages": pages}

    def _cast_from_story(self, story: dict, storyboard: dict | None = None) -> list[dict]:
        catalog = self._read_json(Path(__file__).resolve().parent / "data" / "characters.json")
        cast_signals = [story.get("title", ""), story.get("synopsis", ""), story.get("refrain", "")]
        referenced_ids: set[str] = set()
        if isinstance(storyboard, dict):
            visual_production = storyboard.get("visual_production", {})
            if isinstance(visual_production, dict) and isinstance(visual_production.get("characters"), dict):
                referenced_ids.update(str(key) for key in visual_production["characters"])
            for shot in [storyboard.get("cover"), *(storyboard.get("shots", []) or [])]:
                if isinstance(shot, dict):
                    referenced_ids.update(str(key) for key in shot.get("characters", []) or [])
        for page in story.get("pages", []):
            if not isinstance(page, dict):
                continue
            cast_signals.extend([page.get("scene", ""), page.get("visual_prompt", "")])
            cast_signals.extend(
                line.get("speaker", "")
                for line in page.get("dialogue", [])
                if isinstance(line, dict)
            )
        story_text = "\n".join(str(value) for value in cast_signals)
        return [
            {"character_id": item.get("character_id", ""), "name": item.get("name", ""), "type": item.get("type", "")}
            for item in catalog.get("characters", [])
            if isinstance(item, dict)
            and item.get("name")
            and (item["name"] in story_text or item.get("character_id") in referenced_ids)
        ]

    @staticmethod
    def _audio_duration(path: Path) -> float | None:
        if path.suffix.lower() != ".wav":
            return None
        try:
            with wave.open(str(path), "rb") as audio:
                return round(audio.getnframes() / audio.getframerate(), 3)
        except (OSError, wave.Error, ZeroDivisionError):
            return None

    @staticmethod
    def _estimated_page_starts(story: dict, narration: dict, duration: float) -> dict[int, float]:
        pages = [item for item in story.get("pages", []) if isinstance(item, dict)]
        if not pages:
            return {}
        segments = [item for item in narration.get("segments", []) if isinstance(item, dict)]
        weights = []
        for page in pages:
            page_no = int(page.get("page", len(weights) + 1))
            page_segments = [item for item in segments if int(item.get("page", 0) or 0) == page_no]
            text_length = sum(len(str(item.get("text", ""))) for item in page_segments)
            pauses = sum(float(item.get("pause_after_ms", 0) or 0) / 1000 for item in page_segments)
            weights.append(max(text_length / 5.2 + pauses, 1.0))
        cover_seconds = min(4.0, max(duration * 0.04, 2.0)) if duration > 0 else 3.0
        available = max(duration - cover_seconds, float(len(pages))) if duration > 0 else sum(weights)
        scale = available / sum(weights) if sum(weights) else 1.0
        cursor = cover_seconds
        starts: dict[int, float] = {}
        for page, weight in zip(pages, weights):
            page_no = int(page.get("page", len(starts) + 1))
            starts[page_no] = round(cursor, 3)
            cursor += weight * scale
        return starts

    def _approved_characters(self) -> list[dict]:
        catalog_path = Path(__file__).resolve().parent / "data" / "characters.json"
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        stories_by_character: dict[str, list[dict]] = {}
        for story in self._approved_stories():
            story_link = {
                "id": story["id"],
                "title": story["title"],
                "player_url": story["player_url"],
                "cover_url": story["cover_url"],
            }
            for member in story.get("cast", []):
                if not isinstance(member, dict) or not member.get("name"):
                    continue
                character_id = member.get("form_id") or member.get("character_id") or member["name"]
                stories = stories_by_character.setdefault(character_id, [])
                if not any(item["id"] == story["id"] for item in stories):
                    stories.append(story_link)

        characters: list[dict] = []
        for item in catalog.get("characters", []):
            if not isinstance(item, dict) or not item.get("character_id"):
                continue
            character = dict(item)
            character["stories"] = stories_by_character.get(item["character_id"], [])
            if isinstance(character.get("forms"), list):
                character["forms"] = [
                    {
                        **form,
                        "stories": stories_by_character.get(form.get("form_id", ""), []),
                    }
                    for form in character["forms"]
                    if isinstance(form, dict)
                ]
            characters.append(character)
        return characters

    def _annotate_cast_forms(self, cast: list[dict], story: dict) -> list[dict]:
        """Attach the explicitly depicted character form to each story cast member.

        Published manifests historically used the base character id only.  The
        story synopsis/storyboard remains the source of truth for which approved
        form was actually drawn, so this enrichment keeps old releases immutable
        while allowing the character atlas to show stories on the right form card.
        """
        signals = [story.get("title", ""), story.get("synopsis", ""), story.get("refrain", "")]
        for page in story.get("pages", []):
            if isinstance(page, dict):
                signals.extend([page.get("scene", ""), page.get("visual_prompt", ""), page.get("narration", "")])
        text = "\n".join(str(value) for value in signals)
        # The family atlas is authoritative when older story prompts used a
        # temporary Roro look. Keep this explicit release mapping ahead of
        # text heuristics so the same story cannot drift between form cards.
        roro_form_by_story = {
            "2026-08-30-slow-bridge": "roro",
            "2026-08-30-cloud-station-rain": "roro-warm-sun-form",
            "2026-08-30-crooked-paper-house": "roro-warm-sun-form",
            "2026-08-30-lost-shadow": "roro-warm-sun-form",
            "2026-08-30-roro-goodnight-workbench": "roro-warm-sun-form",
            "2026-08-30-sneezing-tiger": "roro-warm-sun-form",
            "2026-08-30-three-answers": "roro-warm-sun-form",
            "2026-08-31-windy-picnic-cloth": "roro-warm-sun-form",
            "2026-08-31-gentle-footprints": "roro-warm-sun-form",
            "2026-08-31-moon-post-letter": "roro-starlight-form",
            "2026-09-05-park-mushroom-friend": "roro-warm-sun-form",
            "park-mushroom-friend-2026-09-05": "roro-warm-sun-form",
        }
        explicit_roro_form = roro_form_by_story.get(str(story.get("story_id", "")))
        result: list[dict] = []
        for member in cast:
            if not isinstance(member, dict):
                continue
            enriched = dict(member)
            if not enriched.get("form_id"):
                character_id = str(enriched.get("character_id", ""))
                name = str(enriched.get("name", ""))
                if character_id == "roro" or name == "Roro":
                    if explicit_roro_form:
                        enriched["form_id"] = explicit_roro_form
                    elif "星光形态" in text or "星光 Roro" in text or "星光Roro" in text:
                        enriched["form_id"] = "roro-starlight-form"
                    elif "暖阳形态" in text or "暖阳 Roro" in text or "暖阳Roro" in text:
                        enriched["form_id"] = "roro-warm-sun-form"
                    elif "暖白" in text or "白色圆角" in text or "白色机器人" in text:
                        enriched["form_id"] = "roro"
                elif character_id == "youba" or name == "佑爸":
                    if "霸王龙形态" in text or "霸王龙佑爸" in text:
                        enriched["form_id"] = "youba-trex-form"
                    elif "孙悟空形态" in text or "神话猴形态" in text or "神话猴佑爸" in text:
                        enriched["form_id"] = "youba-wukong-form"
            result.append(enriched)
        return result

    @staticmethod
    def _artifact_url(asset_base: str, manifest: dict, suffix: str) -> str:
        for artifact in manifest.get("artifacts", []):
            path = artifact.get("path", "")
            if path.lower().endswith(suffix):
                return f"{asset_base}/" + "/".join(quote(part) for part in Path(path).parts)
        return ""

    @staticmethod
    def _download_filename(title: str, release_id: str) -> str:
        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(title or "").strip())
        safe_title = re.sub(r"\s+", "-", safe_title).strip(" .-") or "家庭绘本"
        safe_title = safe_title[:64].rstrip(" .-") or "家庭绘本"
        safe_release = re.sub(r"[^A-Za-z0-9._-]", "-", str(release_id or "current"))
        return f"Roro绘本-{safe_title}-{safe_release}.pdf"

    def _published_pdf_path(self, story_id: str) -> tuple[Path, str] | None:
        directory = self.review_store.resolve_published_directory(story_id)
        if directory is None:
            return None
        manifest = self._read_json(directory / "package-manifest.json")
        assets = manifest.get("assets", {}) if isinstance(manifest.get("assets"), dict) else {}
        release_id = str(manifest.get("release_id") or directory.name)
        package_digest = str(manifest.get("package_digest", ""))
        story_relative = str(assets.get("story", "story.json"))
        cover_relative = str(assets.get("cover", ""))
        page_relatives = assets.get("pages", []) if isinstance(assets.get("pages"), list) else []
        story_path = directory / Path(story_relative)
        cover_path = directory / Path(cover_relative)
        page_paths = [directory / Path(str(relative)) for relative in page_relatives]
        required = [story_path, cover_path, *page_paths]
        if not package_digest or not cover_relative or not page_paths or any(not path.is_file() for path in required):
            return None
        hashes = manifest.get("asset_hashes", {}) if isinstance(manifest.get("asset_hashes"), dict) else {}
        for relative, path in [(story_relative, story_path), (cover_relative, cover_path), *[(str(relative), path) for relative, path in zip(page_relatives, page_paths)]]:
            expected = str(hashes.get(relative, ""))
            if not expected or sha256_file(path) != expected:
                return None

        output_root = self.workspace_root / "exports" / "published-pdf" / story_id / release_id
        output = output_root / f"roro-story-{story_id}-{release_id}.pdf"
        metadata = output.with_suffix(".pdf.json")
        record = self._read_json(metadata)
        if output.is_file() and record.get("package_digest") == package_digest and record.get("layout_version") == PDF_LAYOUT_VERSION:
            story = self._read_json(story_path)
            return output, self._download_filename(story.get("title", story_id), release_id)

        output_root.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        try:
            build_from_assets(story_path, cover_path, page_paths, temporary)
            os.replace(temporary, output)
            atomic_write_json(
                metadata,
                {
                    "schema_version": 1,
                    "story_id": story_id,
                    "release_id": release_id,
                    "package_digest": package_digest,
                    "layout_version": PDF_LAYOUT_VERSION,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        finally:
            if temporary.exists():
                temporary.unlink()
        story = self._read_json(story_path)
        return output, self._download_filename(story.get("title", story_id), release_id)

    def _send_published_pdf(self, story_id: str, head_only: bool = False) -> None:
        try:
            result = self._published_pdf_path(story_id)
        except (OSError, ValueError):
            result = None
        if result is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Published PDF unavailable")
            return
        pdf, filename = result
        try:
            size = pdf.stat().st_size
            stream = None if head_only else pdf.open("rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Published PDF unavailable")
            return
        fallback = f"roro-story-{story_id}.pdf"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "private, no-cache")
        self.send_header("Content-Disposition", f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}")
        self.end_headers()
        if stream is not None:
            with stream:
                shutil.copyfileobj(stream, self.wfile)

    def _display_route_blocked(self, route: str) -> bool:
        return getattr(self, "display_only", False) and (
            route in {"/workbench", "/workbench.html"}
            or route.startswith(("/api/studio/", "/studio-preview/", "/studio-assets/", "/studio-thumbnails/"))
        )

    def _sync_runtime_root(self) -> Path:
        return Path("/runtime") if Path("/runtime").is_dir() else self.workspace_root / "service" / ".runtime"

    def _send_sync_asset(self, story_id: str, release_id: str, relative: str) -> None:
        try:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", story_id) or not re.fullmatch(r"[A-Za-z0-9._-]+", release_id):
                raise SyncError("invalid remote asset identity")
            store = ReviewStore(self.approved_root.parent)
            if store.shelf_status(story_id).get("status") != "published":
                raise SyncError("story is not currently published")
            story_root = safe_child(self.approved_root, story_id)
            pointer = read_json(story_root / "current.json")
            if pointer.get("release_id") != release_id:
                raise SyncError("release is no longer current")
            release = safe_child(story_root, "releases", release_id)
            manifest = read_json(release / "package-manifest.json")
            normalized = safe_relative(relative)
            hashes = manifest.get("asset_hashes", {})
            expected = hashes.get(normalized) if isinstance(hashes, dict) else None
            if not expected:
                raise SyncError("asset is not listed in the published manifest")
            path = safe_child(release, normalized)
            if not path.is_file() or sha256_file(path) != expected:
                raise SyncError("published asset failed verification")
            size = path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", self.guess_type(str(path)) or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with path.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
        except (OSError, ReviewError, SyncError):
            self.send_error(HTTPStatus.NOT_FOUND, "Sync asset unavailable")

    def _send_sync_character_asset(self, relative: str) -> None:
        try:
            normalized = safe_relative(relative)
            # The approved shelf is mounted below /data, while application
            # code and character assets are mounted below /app/service. Do not
            # derive the application asset root from approved_root.parent.
            character_root = Path(__file__).resolve().parent / "assets" / "characters"
            path = safe_child(character_root, normalized)
            catalog = read_json(Path(__file__).resolve().parent / "data" / "characters.json")
            names: set[str] = set()
            def collect(value: Any) -> None:
                if isinstance(value, dict):
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
                elif isinstance(value, str) and value.startswith("/character-assets/"):
                    names.add(safe_relative(value.removeprefix("/character-assets/")))
            collect(catalog)
            if normalized not in names or not path.is_file():
                raise SyncError("character asset is not published")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", self.guess_type(str(path)) or "application/octet-stream")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with path.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
        except (OSError, SyncError):
            self.send_error(HTTPStatus.NOT_FOUND, "Character asset unavailable")

    def _send_library(self) -> None:
        self._send_ui("library.html")

    def _send_characters(self) -> None:
        self._send_ui("characters.html")

    def _send_ui(self, filename: str) -> None:
        library_path = Path(__file__).resolve().parent / "ui" / filename
        try:
            body = library_path.read_bytes()
            if getattr(self, "display_only", False):
                body = re.sub(rb'<a\b[^>]*href=[\"\x27]/workbench[\"\x27][^>]*>.*?</a>', b'', body, flags=re.S)
                if filename == "library.html":
                    body = body.replace(b'</head>', b'<style>.share-action{display:none!important}.actions{grid-template-columns:1fr 1fr}</style></head>')
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "UI not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_project_asset(self, filename: str, head_only: bool = False) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
            self.send_error(HTTPStatus.NOT_FOUND, "Project asset not found")
            return
        asset_path = Path(__file__).resolve().parent / "assets" / filename
        try:
            body = asset_path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Project asset not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", self.guess_type(str(asset_path)))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_character_asset(self, route: str, head_only: bool = False) -> None:
        asset_name = Path(route).name
        if not re.fullmatch(r"[A-Za-z0-9._-]+", asset_name):
            self.send_error(HTTPStatus.NOT_FOUND, "Character asset not found")
            return
        asset_path = Path(__file__).resolve().parent / "assets" / "characters" / asset_name
        try:
            body = asset_path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Character asset not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", self.guess_type(str(asset_path)))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json_error(self, code: str, message: str, status: int) -> None:
        self._send_json({"ok": False, "error": {"code": code, "message": message}}, status=status)

    def send_head(self):
        self._range = None
        path = self.translate_path(self.path)
        file_path = Path(path)
        if file_path.is_dir():
            return super().send_head()
        try:
            file = file_path.open("rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None

        try:
            size = file_path.stat().st_size
            start, end = 0, max(size - 1, 0)
            range_header = self.headers.get("Range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                if not match:
                    raise ValueError("invalid range")
                first, last = match.groups()
                if not first and not last:
                    raise ValueError("empty range")
                if first:
                    start = int(first)
                    end = int(last) if last else end
                else:
                    suffix = int(last)
                    start = max(size - suffix, 0)
                if start >= size or start > end:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    file.close()
                    return None
                end = min(end, size - 1)
                self._range = (start, end)
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                content_length = end - start + 1
            else:
                self.send_response(HTTPStatus.OK)
                content_length = size

            content_type = self.guess_type(str(file_path))
            if content_type.startswith("text/"):
                content_type += "; charset=utf-8"
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Last-Modified", formatdate(file_path.stat().st_mtime, usegmt=True))
            self.send_header("Accept-Ranges", "bytes")
            if file_path.suffix.lower() in {".html", ".json"}:
                self.send_header("Cache-Control", "no-cache")
            else:
                self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            return file
        except ValueError:
            file.close()
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Range header")
            return None
        except Exception:
            file.close()
            raise

    def copyfile(self, source, outputfile) -> None:
        try:
            if self._range is None:
                shutil.copyfileobj(source, outputfile)
                return
            start, end = self._range
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Browsers routinely cancel media/image requests after buffering enough.
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve approved Roro stories")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument(
        "--directory",
        default=str(Path(__file__).resolve().parents[1] / "approved"),
    )
    parser.add_argument(
        "--pin-file",
        default=str(Path(__file__).resolve().parent / ".runtime" / "parent-pin.txt"),
    )
    parser.add_argument(
        "--review-auth-mode",
        choices=("development", "pin"),
        default="development",
        help="development skips parent PIN verification; pin enables the protected review flow",
    )
    parser.add_argument(
        "--public-origin",
        default=os.environ.get("RORO_PUBLIC_ORIGIN", ""),
        help="Exact public HTTP(S) origin used behind a trusted reverse proxy",
    )
    parser.add_argument("--display-only", action="store_true", help="Read-only shelf/player; disable workbench and all write APIs")
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Approved directory not found: {root}", file=sys.stderr)
        return 2

    pin = None
    if args.review_auth_mode == "pin":
        pin_file = Path(args.pin_file).resolve()
        try:
            pin = pin_file.read_text(encoding="ascii").strip()
        except OSError:
            print(f"Parent PIN file not found: {pin_file}", file=sys.stderr)
            return 2
        if not re.fullmatch(r"\d{6}", pin):
            print(f"Parent PIN must contain exactly six digits: {pin_file}", file=sys.stderr)
            return 2

    public_origin = str(args.public_origin or "").strip().rstrip("/")
    if public_origin:
        parsed_origin = urlsplit(public_origin)
        if (
            parsed_origin.scheme not in {"http", "https"}
            or not parsed_origin.netloc
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            print("Public origin must be an exact HTTP(S) origin without a path", file=sys.stderr)
            return 2

    review_store = ReviewStore(root.parent)
    review_auth = ReviewAuth(pin, mode=args.review_auth_mode)
    catalog_cache = CatalogCache()
    sync_manager = None
    if args.display_only:
        runtime_root = Path("/runtime") if Path("/runtime").is_dir() else root.parent / "service" / ".runtime"
        sync_manager = SyncManager(
            root,
            runtime_root,
            os.environ.get("RORO_SYNC_SOURCE_URL", ""),
            on_commit=lambda: catalog_cache.invalidate("approved_stories", "approved_characters"),
            service_data_root=Path("/app/service/data") if Path("/app/service/data").is_dir() else root.parent / "service" / "data",
            character_root=Path("/app/service/assets/characters") if Path("/app/service/assets/characters").is_dir() else root.parent / "character-assets",
        )
        sync_manager.start_auto_loop()

    handler = lambda *h_args, **h_kwargs: StoryHandler(
        *h_args,
        directory=str(root),
        review_store=review_store,
        review_auth=review_auth,
        catalog_cache=catalog_cache,
        public_origin=public_origin,
        display_only=args.display_only,
        sync_manager=sync_manager,
        **h_kwargs,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(
        f"Roro Story Service listening on http://{args.host}:{args.port} from {root} "
        f"(review auth: {args.review_auth_mode})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
