"""Persistent review state and immutable publishing for Roro Story Studio."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
import threading
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


STORY_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
REVISION_RE = re.compile(r"[A-Za-z0-9._-]+")
ALLOWED_EXTENSIONS = {".json", ".md", ".png", ".jpg", ".jpeg", ".wav", ".mp3", ".m4a", ".ogg", ".pdf", ".html"}
REQUIRED_ASSET_KEYS = ("story", "storyboard", "narration", "cover", "pages", "primary_audio")


class ReviewError(RuntimeError):
    """A user-safe review or publish error with an HTTP-friendly code."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def flatten_assets(assets: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in REQUIRED_ASSET_KEYS:
        value = assets.get(key)
        if key == "pages":
            if not isinstance(value, list) or not value:
                raise ReviewError("invalid_manifest", "候选清单缺少正文页面。")
            paths.extend(str(item) for item in value)
        else:
            if not isinstance(value, str) or not value:
                raise ReviewError("invalid_manifest", f"候选清单缺少 {key}。")
            paths.append(value)
    extras = assets.get("extras", [])
    if extras:
        if not isinstance(extras, list):
            raise ReviewError("invalid_manifest", "候选附加资产格式不正确。")
        paths.extend(str(item) for item in extras)
    return paths


def normalize_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewError("invalid_asset_path", "候选资产路径为空。")
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ReviewError("invalid_asset_path", f"候选资产路径不安全：{value}")
    if pure.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ReviewError("invalid_asset_type", f"候选资产类型不允许：{value}")
    return pure.as_posix()


class ReviewStore:
    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root.resolve()
        self.drafts_root = (self.workspace_root / "drafts").resolve()
        self.approved_root = (self.workspace_root / "approved").resolve()
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._share_lock = threading.Lock()

    def _story_lock(self, story_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(story_id, threading.Lock())

    @staticmethod
    def validate_story_id(story_id: str) -> str:
        if not STORY_ID_RE.fullmatch(story_id or ""):
            raise ReviewError("invalid_story_id", "故事 ID 不正确。", 404)
        return story_id

    def draft_directory(self, story_id: str) -> Path:
        self.validate_story_id(story_id)
        directory = (self.drafts_root / story_id).resolve()
        if self.drafts_root not in directory.parents or not directory.is_dir():
            raise ReviewError("story_not_found", "找不到这个故事草稿。", 404)
        return directory

    def review_directory(self, story_id: str) -> Path:
        return self.draft_directory(story_id) / "review"

    def candidate_path(self, story_id: str) -> Path:
        return self.review_directory(story_id) / "candidate-manifest.json"

    def ai_review_path(self, story_id: str) -> Path:
        return self.review_directory(story_id) / "ai-review.json"

    def parent_review_path(self, story_id: str) -> Path:
        return self.review_directory(story_id) / "parent-review.json"

    def shelf_status_path(self) -> Path:
        root = Path("/runtime") if Path("/runtime").is_dir() else self.workspace_root / "service" / ".runtime"
        return root / "shelf-status.json"

    def shelf_history_path(self) -> Path:
        root = Path("/runtime") if Path("/runtime").is_dir() else self.workspace_root / "service" / ".runtime"
        return root / "shelf-history.jsonl"

    def shares_path(self) -> Path:
        root = Path("/runtime") if Path("/runtime").is_dir() else self.workspace_root / "service" / ".runtime"
        return root / "story-shares.json"

    def _share_document(self) -> dict[str, Any]:
        data = read_json(self.shares_path())
        shares = data.get("shares", [])
        return {"schema_version": 1, "shares": shares if isinstance(shares, list) else []}

    @staticmethod
    def _share_token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_share(self, story_id: str, *, expires_in_days: int, allow_download: bool, actor: str) -> dict[str, Any]:
        self.validate_story_id(story_id)
        if expires_in_days not in {7, 30, 0}:
            raise ReviewError("invalid_share_expiry", "分享有效期只能是 7 天、30 天或永久。")
        pointer = self.published_pointer(story_id)
        release_id = str(pointer.get("release_id", ""))
        digest = str(pointer.get("package_digest", ""))
        if not release_id or not digest or self.shelf_status(story_id).get("status") != "published":
            raise ReviewError("story_not_shareable", "只有已上架绘本可以分享。", 409)
        if self.resolve_published_directory(story_id) is None:
            raise ReviewError("story_not_shareable", "当前上架版本不可用。", 409)
        now = datetime.now(timezone.utc)
        expires_at = "" if expires_in_days == 0 else (now.timestamp() + expires_in_days * 86400)
        expires_text = "" if not expires_at else datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
        token = secrets.token_urlsafe(32)
        entry = {
            "share_id": f"share-{uuid.uuid4().hex}", "story_id": story_id,
            "release_id": release_id, "package_digest": digest,
            "token_hash": self._share_token_hash(token), "status": "active",
            "expires_at": expires_text, "allow_download": bool(allow_download),
            "created_at": now.isoformat(), "created_by": actor,
        }
        with self._share_lock:
            document = self._share_document()
            document["shares"].append(entry)
            atomic_write_json(self.shares_path(), document)
        return {key: value for key, value in entry.items() if key != "token_hash"} | {"token": token}

    def list_shares(self, story_id: str) -> list[dict[str, Any]]:
        self.validate_story_id(story_id)
        now = datetime.now(timezone.utc)
        result = []
        for entry in self._share_document()["shares"]:
            if not isinstance(entry, dict) or entry.get("story_id") != story_id:
                continue
            item = dict(entry)
            expires = str(item.get("expires_at", ""))
            if item.get("status") == "active" and expires:
                try:
                    if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= now:
                        item["status"] = "expired"
                except (ValueError, TypeError):
                    item["status"] = "expired"
            item.pop("token_hash", None)
            result.append(item)
        return sorted(result, key=lambda item: item.get("created_at", ""), reverse=True)

    def resolve_share(self, token: str) -> dict[str, Any]:
        token = str(token or "").strip()
        if not token or len(token) > 200:
            raise ReviewError("share_not_found", "分享链接无效或已失效。", 404)
        token_hash = self._share_token_hash(token)
        found = None
        for entry in self._share_document()["shares"]:
            if isinstance(entry, dict) and hmac.compare_digest(str(entry.get("token_hash", "")), token_hash):
                found = entry
                break
        if not found or found.get("status") != "active":
            raise ReviewError("share_not_found", "分享链接无效或已失效。", 404)
        expires = str(found.get("expires_at", ""))
        if expires:
            try:
                expired = datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
            except (ValueError, TypeError):
                expired = True
            if expired:
                raise ReviewError("share_expired", "这个分享链接已经过期。", 410)
        story_id = str(found.get("story_id", ""))
        pointer = self.published_pointer(story_id)
        if self.shelf_status(story_id).get("status") != "published" or pointer.get("release_id") != found.get("release_id") or pointer.get("package_digest") != found.get("package_digest"):
            raise ReviewError("share_unavailable", "这个绘本当前暂时不能分享。", 410)
        return dict(found)

    def revoke_share(self, story_id: str, share_id: str, *, actor: str) -> dict[str, Any]:
        self.validate_story_id(story_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._share_lock:
            document = self._share_document()
            for entry in document["shares"]:
                if isinstance(entry, dict) and entry.get("story_id") == story_id and entry.get("share_id") == share_id:
                    entry["status"] = "revoked"
                    entry["revoked_at"] = now
                    entry["revoked_by"] = actor
                    atomic_write_json(self.shares_path(), document)
                    item = dict(entry); item.pop("token_hash", None)
                    return item
        raise ReviewError("share_not_found", "没有找到这个分享链接。", 404)

    def shelf_status(self, story_id: str) -> dict[str, Any]:
        self.validate_story_id(story_id)
        data = read_json(self.shelf_status_path())
        entry = data.get(story_id, {}) if isinstance(data.get(story_id), dict) else {}
        current_release = ""
        try:
            current_release = str(read_json(self.approved_root / story_id / "current.json").get("release_id", ""))
        except OSError:
            pass
        return {"status": entry.get("status", "published"), **entry, "release_id": current_release or entry.get("release_id", "")}

    def set_shelf_status(self, story_id: str, *, status: str, reason: str, release_id: str, actor: str) -> dict[str, Any]:
        self.validate_story_id(story_id)
        if status not in {"published", "unpublished"}:
            raise ReviewError("invalid_shelf_status", "书架状态不正确。")
        reason = str(reason or "").strip()[:2000]
        if status == "unpublished" and not reason:
            raise ReviewError("missing_unpublish_reason", "请填写下架原因。")
        current = self.published_pointer(story_id)
        if not current.get("release_id"):
            raise ReviewError("story_not_published", "只有已上架绘本可以调整书架状态。", 409)
        if release_id != current.get("release_id"):
            raise ReviewError("stale_release", "书架版本已变化，请刷新后重试。", 409)
        now = utc_now()
        entry = {"status": status, "release_id": current.get("release_id", ""), "reason": reason, "updated_at": now, "updated_by": actor}
        data = read_json(self.shelf_status_path())
        data[story_id] = entry
        atomic_write_json(self.shelf_status_path(), data)
        history_path = self.shelf_history_path()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"story_id": story_id, "action": "unpublish" if status == "unpublished" else "publish", **entry}, ensure_ascii=False) + "\n")
        return entry

    def load_candidate(self, story_id: str) -> dict[str, Any]:
        return read_json(self.candidate_path(story_id))

    def load_ai_review(self, story_id: str) -> dict[str, Any]:
        return read_json(self.ai_review_path(story_id))

    def load_parent_review(self, story_id: str) -> dict[str, Any]:
        return read_json(self.parent_review_path(story_id))

    def _asset_path(self, story_id: str, relative: str) -> tuple[str, Path]:
        normalized = normalize_relative_path(relative)
        root = self.draft_directory(story_id)
        target = (root / Path(*PurePosixPath(normalized).parts)).resolve()
        if root not in target.parents or not target.is_file() or target.is_symlink():
            raise ReviewError("missing_asset", f"候选资产不存在或不安全：{normalized}", 409)
        return normalized, target

    def calculate_candidate(self, story_id: str, candidate: dict[str, Any] | None = None) -> dict[str, Any]:
        candidate = dict(candidate or self.load_candidate(story_id))
        if not candidate:
            raise ReviewError("candidate_missing", "故事尚未生成审核候选包。", 409)
        if candidate.get("story_id") != story_id:
            raise ReviewError("invalid_manifest", "候选清单的故事 ID 不匹配。", 409)
        revision = str(candidate.get("candidate_revision", ""))
        if not REVISION_RE.fullmatch(revision):
            raise ReviewError("invalid_manifest", "候选版本号不正确。", 409)
        assets = candidate.get("assets")
        if not isinstance(assets, dict):
            raise ReviewError("invalid_manifest", "候选资产清单格式不正确。", 409)
        normalized_assets = json.loads(json.dumps(assets, ensure_ascii=False))
        asset_hashes: dict[str, str] = {}
        seen: set[str] = set()
        for value in flatten_assets(assets):
            normalized, target = self._asset_path(story_id, value)
            if normalized in seen:
                raise ReviewError("duplicate_asset", f"候选资产重复：{normalized}", 409)
            seen.add(normalized)
            asset_hashes[normalized] = sha256_file(target)

        normalized_assets["story"] = normalize_relative_path(str(assets["story"]))
        normalized_assets["storyboard"] = normalize_relative_path(str(assets["storyboard"]))
        normalized_assets["narration"] = normalize_relative_path(str(assets["narration"]))
        normalized_assets["cover"] = normalize_relative_path(str(assets["cover"]))
        normalized_assets["pages"] = [normalize_relative_path(str(item)) for item in assets["pages"]]
        normalized_assets["primary_audio"] = normalize_relative_path(str(assets["primary_audio"]))
        normalized_assets["extras"] = [normalize_relative_path(str(item)) for item in assets.get("extras", [])]

        digest_payload = {
            "schema_version": 1,
            "story_id": story_id,
            "candidate_revision": revision,
            "assets": normalized_assets,
            "asset_hashes": dict(sorted(asset_hashes.items())),
        }
        digest_bytes = json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        package_digest = f"sha256:{hashlib.sha256(digest_bytes).hexdigest()}"
        return {**digest_payload, "package_digest": package_digest}

    def verify_candidate(self, story_id: str) -> dict[str, Any]:
        stored = self.load_candidate(story_id)
        calculated = self.calculate_candidate(story_id, stored)
        if stored.get("package_digest") != calculated["package_digest"] or stored.get("asset_hashes") != calculated["asset_hashes"]:
            raise ReviewError("stale_candidate", "候选内容已经变化，需要重新完成内部审核。", 409)
        return calculated

    def published_pointer(self, story_id: str) -> dict[str, Any]:
        self.validate_story_id(story_id)
        return read_json(self.approved_root / story_id / "current.json")

    def resolve_published_directory(self, story_id: str) -> Path | None:
        self.validate_story_id(story_id)
        root = (self.approved_root / story_id).resolve()
        if not root.is_dir() or self.approved_root not in root.parents:
            return None
        pointer = read_json(root / "current.json")
        release_id = str(pointer.get("release_id", ""))
        if release_id and REVISION_RE.fullmatch(release_id):
            release = (root / "releases" / release_id).resolve()
            if root in release.parents and release.is_dir():
                return release
        return root

    def state(self, story_id: str) -> dict[str, Any]:
        candidate = self.load_candidate(story_id)
        pointer = self.published_pointer(story_id)
        shelf = self.shelf_status(story_id)
        published_directory = self.resolve_published_directory(story_id)
        published = published_directory is not None and (published_directory / "story.json").is_file()
        if not candidate:
            return {"state": "published" if published else "authoring", "published": published, "shelf_status": shelf["status"], "shelf": shelf, "candidate_revision": "", "package_digest": ""}
        try:
            verified = self.verify_candidate(story_id)
        except ReviewError as error:
            if error.code == "stale_candidate":
                return {"state": "stale", "published": published, "shelf_status": shelf["status"], "shelf": shelf, "candidate_revision": candidate.get("candidate_revision", ""), "package_digest": candidate.get("package_digest", ""), "message": str(error)}
            return {"state": "authoring", "published": published, "shelf_status": shelf["status"], "shelf": shelf, "candidate_revision": candidate.get("candidate_revision", ""), "package_digest": candidate.get("package_digest", ""), "message": str(error)}

        digest = verified["package_digest"]
        revision = verified["candidate_revision"]
        ai_review = self.load_ai_review(story_id)
        if ai_review.get("package_digest") != digest:
            return {"state": "stale", "published": published, "shelf_status": shelf["status"], "shelf": shelf, "candidate_revision": revision, "package_digest": digest, "message": "内部审核记录与当前候选不一致。"}
        if ai_review.get("result") != "passed":
            return {"state": "ai_review_failed", "published": published, "shelf_status": shelf["status"], "shelf": shelf, "candidate_revision": revision, "package_digest": digest}

        parent = self.load_parent_review(story_id).get("current", {})
        if parent.get("package_digest") == digest and parent.get("candidate_revision") == revision:
            decision = parent.get("decision")
            publish_status = parent.get("publish_status")
            if decision == "changes_requested":
                return {"state": "changes_requested", "published": published, "shelf_status": shelf["status"], "shelf": shelf, "candidate_revision": revision, "package_digest": digest}
            if decision == "approved":
                if publish_status == "failed":
                    return {"state": "publish_failed", "published": published, "shelf_status": shelf["status"], "shelf": shelf, "candidate_revision": revision, "package_digest": digest, "message": parent.get("publish_error", "发布失败")}
                if pointer.get("package_digest") == digest and pointer.get("release_id") == revision:
                    return {"state": "published", "published": True, "shelf_status": shelf["status"], "shelf": shelf, "candidate_revision": revision, "package_digest": digest}
                return {"state": "publishing", "published": published, "shelf_status": shelf["status"], "shelf": shelf, "candidate_revision": revision, "package_digest": digest}
        return {"state": "ready_for_parent", "published": published, "shelf_status": shelf["status"], "shelf": shelf, "candidate_revision": revision, "package_digest": digest}

    @staticmethod
    def _audio_duration(path: Path) -> float | None:
        if path.suffix.lower() != ".wav":
            return None
        try:
            with wave.open(str(path), "rb") as audio:
                return round(audio.getnframes() / audio.getframerate(), 3)
        except (OSError, wave.Error, ZeroDivisionError):
            return None

    def _publish(
        self,
        story_id: str,
        candidate: dict[str, Any],
        approval_id: str,
        decided_at: str,
        actor: str,
    ) -> dict[str, Any]:
        revision = candidate["candidate_revision"]
        story_root = self.approved_root / story_id
        releases_root = story_root / "releases"
        release = releases_root / revision
        if release.exists():
            existing = read_json(release / "package-manifest.json")
            if existing.get("package_digest") != candidate["package_digest"]:
                raise ReviewError("release_conflict", "同名发布版本已经存在，但内容摘要不同。", 409)
        else:
            stage_root = self.approved_root / ".staging"
            stage = stage_root / f"{story_id}-{uuid.uuid4().hex}"
            try:
                stage.mkdir(parents=True, exist_ok=False)
                draft = self.draft_directory(story_id)
                for relative in candidate["asset_hashes"]:
                    source = draft / Path(*PurePosixPath(relative).parts)
                    target = stage / Path(*PurePosixPath(relative).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    if sha256_file(target) != candidate["asset_hashes"][relative]:
                        raise ReviewError("publish_verification_failed", f"发布校验失败：{relative}", 500)

                story = read_json(stage / candidate["assets"]["story"])
                audio_path = stage / candidate["assets"]["primary_audio"]
                manifest = {
                    "schema_version": 1,
                    "package_id": f"{story_id}-{revision}",
                    "story_id": story_id,
                    "release_id": revision,
                    "title": story.get("title", story_id),
                    "status": "approved",
                    "package_digest": candidate["package_digest"],
                    "candidate_revision": revision,
                    "published_at": decided_at,
                    "audience": story.get("audience", {}),
                    "primary_audio_spec": {
                        "path": candidate["assets"]["primary_audio"],
                        "duration_seconds": self._audio_duration(audio_path),
                    },
                    "parent_review": {
                        "approved": True,
                        "approval_id": approval_id,
                        "approved_at": decided_at,
                        "scope": "complete_story_package",
                        "actor": actor,
                        "identity_verified": actor == "parent-workbench",
                    },
                    "assets": candidate["assets"],
                    "asset_hashes": candidate["asset_hashes"],
                }
                atomic_write_json(stage / "package-manifest.json", manifest)
                atomic_write_json(
                    stage / "approval.json",
                    {
                        "schema_version": 1,
                        "approval_id": approval_id,
                        "decision": "approved",
                        "scope": "complete_story_package",
                        "story_id": story_id,
                        "candidate_revision": revision,
                        "package_digest": candidate["package_digest"],
                        "decided_at": decided_at,
                        "actor": actor,
                    },
                )
                releases_root.mkdir(parents=True, exist_ok=True)
                stage.replace(release)
            finally:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)

        pointer = {
            "schema_version": 1,
            "story_id": story_id,
            "release_id": revision,
            "package_digest": candidate["package_digest"],
            "approval_id": approval_id,
            "published_at": decided_at,
        }
        atomic_write_json(story_root / "current.json", pointer)
        return pointer

    def decide(
        self,
        story_id: str,
        *,
        decision: str,
        candidate_revision: str,
        package_digest: str,
        request_id: str,
        note: str = "",
        issue_tags: list[str] | None = None,
        actor: str = "parent-workbench",
    ) -> dict[str, Any]:
        if decision not in {"approved", "changes_requested"}:
            raise ReviewError("invalid_decision", "审核决定不正确。")
        if not re.fullmatch(r"[A-Za-z0-9._-]{8,128}", request_id or ""):
            raise ReviewError("invalid_request_id", "请求 ID 不正确。")
        note = str(note or "").strip()[:2000]
        issue_tags = [str(item)[:80] for item in (issue_tags or [])][:12]
        if actor not in {"parent-workbench", "local-development-workbench"}:
            raise ReviewError("invalid_actor", "审核操作来源不正确。")
        with self._story_lock(story_id):
            candidate = self.verify_candidate(story_id)
            if candidate["candidate_revision"] != candidate_revision or candidate["package_digest"] != package_digest:
                raise ReviewError("stale_candidate", "页面上的候选版本已经变化，请刷新后重新审核。", 409)
            ai_review = self.load_ai_review(story_id)
            if ai_review.get("result") != "passed" or ai_review.get("package_digest") != package_digest:
                raise ReviewError("ai_review_required", "当前候选尚未通过 Codex 内部审核。", 409)

            parent_path = self.parent_review_path(story_id)
            record = self.load_parent_review(story_id) or {"schema_version": 1, "current": {}, "history": []}
            for event in record.get("history", []):
                if event.get("request_id") == request_id:
                    return {"ok": True, "idempotent": True, "event": event, "state": self.state(story_id)}
            current = record.get("current")
            if isinstance(current, dict) and current.get("request_id") == request_id:
                return {"ok": True, "idempotent": True, "event": current, "state": self.state(story_id)}

            previous = record.get("current")
            history = list(record.get("history", []))
            if isinstance(previous, dict) and previous:
                history.append(previous)
            decided_at = utc_now()
            approval_id = f"approval-{uuid.uuid4().hex}"
            event = {
                "decision": decision,
                "scope": "complete_story_package",
                "candidate_revision": candidate_revision,
                "package_digest": package_digest,
                "decided_at": decided_at,
                "actor": actor,
                "request_id": request_id,
                "note": note,
                "issue_tags": issue_tags,
            }
            if decision == "changes_requested":
                event["publish_status"] = "not_requested"
                atomic_write_json(parent_path, {"schema_version": 1, "current": event, "history": history})
                return {"ok": True, "event": event, "state": self.state(story_id)}

            event.update({"approval_id": approval_id, "publish_status": "publishing"})
            atomic_write_json(parent_path, {"schema_version": 1, "current": event, "history": history})
            try:
                pointer = self._publish(story_id, candidate, approval_id, decided_at, actor)
            except Exception as error:
                event["publish_status"] = "failed"
                event["publish_error"] = str(error)[:500]
                atomic_write_json(parent_path, {"schema_version": 1, "current": event, "history": history})
                raise
            event["publish_status"] = "published"
            event["release_id"] = pointer["release_id"]
            atomic_write_json(parent_path, {"schema_version": 1, "current": event, "history": history})
            return {
                "ok": True,
                "event": event,
                "state": self.state(story_id),
                "library_url": "/",
                "player_url": f"/player/{story_id}",
            }
