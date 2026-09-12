"""Prepare only the currently visible shelf releases and current character atlas.

No drafts, historical releases, workbench queues, audit logs, or credentials are
copied. The running PC shelf defines selection; all media is hash-verified.
"""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    name = PurePosixPath(relative)
    if not relative or name.is_absolute() or ".." in name.parts or "\\" in relative or ":" in relative:
        raise ValueError(f"Unsafe relative asset path: {relative}")
    path = root.joinpath(*name.parts)
    for component in (path, *path.parents):
        if component == root.parent:
            break
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise ValueError(f"Linked asset path: {relative}")
    if root.resolve() not in path.resolve().parents or not path.is_file():
        raise ValueError(f"Missing/unsafe asset: {relative}")
    return path


def copy_verified(source: Path, target: Path, expected: str | None = None) -> dict:
    before = digest(source)
    if expected and before != expected:
        raise ValueError(f"Published asset no longer matches its manifest: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if digest(target) != before or digest(source) != before:
        raise ValueError(f"Asset changed in transit: {source}")
    stat = source.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return {"sha256": before, "bytes": stat.st_size}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def shelf(source_url: str):
    with urllib.request.urlopen(source_url.rstrip("/") + "/api/stories", timeout=120) as response:
        return json.load(response)["stories"]


def plan(workspace: Path, visible: list[dict]):
    selected, books = {}, []
    if not visible:
        raise ValueError("Source shelf is empty; refusing to prepare an empty replacement")
    for item in visible:
        story_id = item["id"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", story_id):
            raise ValueError("Unsafe story ID")
        root = workspace / "approved" / story_id
        pointer_path = safe_path(root, "current.json")
        pointer = read_json(pointer_path)
        revision = pointer["release_id"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", revision):
            raise ValueError("Unsafe release ID")
        release = root / "releases" / revision
        manifest_path = safe_path(release, "package-manifest.json")
        manifest = read_json(manifest_path)
        if manifest.get("status") != "approved" or manifest.get("story_id") != story_id or manifest.get("release_id") != revision or manifest.get("package_digest") != pointer.get("package_digest"):
            raise ValueError(f"Published pointer/manifest mismatch: {story_id}")
        hashes = manifest.get("asset_hashes")
        if not isinstance(hashes, dict) or not hashes:
            raise ValueError(f"Published release has no asset hashes: {story_id}")
        prefix = f"data/approved/{story_id}"
        selected[f"{prefix}/current.json"] = (pointer_path, digest(pointer_path))
        selected[f"{prefix}/releases/{revision}/package-manifest.json"] = (manifest_path, digest(manifest_path))
        for relative, expected in hashes.items():
            selected[f"{prefix}/releases/{revision}/{relative}"] = (safe_path(release, relative), expected)
        books.append({"id": story_id, "title": item["title"], "release_id": revision,
            "package_digest": pointer["package_digest"], "production_time": item["production_time"],
            "audio_options": len(item.get("audio_options", []))})
    catalog_path = workspace / "service/data/characters.json"
    catalog = read_json(catalog_path)
    selected["service-data/characters.json"] = (catalog_path, digest(catalog_path))
    def walk(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)
        elif isinstance(value, str) and value.startswith("/character-assets/"):
            yield value.removeprefix("/character-assets/")
    for name in set(walk(catalog)):
        path = safe_path(workspace / "service/assets/characters", name)
        selected["character-assets/" + name] = (path, digest(path))
    return selected, books


def application_files(workspace: Path):
    root = workspace / "service"
    result = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        relative_dir = Path(directory).relative_to(root)
        dirs[:] = sorted(name for name in dirs if name not in {".runtime", "__pycache__"})
        if relative_dir == Path("assets"):
            dirs[:] = [name for name in dirs if name != "characters"]
        if relative_dir == Path("data"):
            dirs[:] = []
            # Keep an empty mountpoint in the external code tree. Runtime data
            # is mounted separately at /app/service/data in the NAS compose.
            names = [name for name in names if name == ".gitkeep"]
        for name in dirs:
            child = Path(directory) / name
            if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
                raise ValueError(f"Linked application directory: {child}")
        for name in sorted(names):
            if name.endswith((".pyc", ".pyo")):
                continue
            source = Path(directory) / name
            if source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
                raise ValueError(f"Linked application source: {source}")
            result[f"app/service/{(relative_dir / name).as_posix()}"] = source
    if not any(key.endswith("/story_server.py") for key in result):
        raise ValueError("Application code is incomplete")
    pdf_builder = workspace / "scripts/build_picture_book.py"
    if not pdf_builder.is_file() or pdf_builder.is_symlink():
        raise ValueError("PDF application code is incomplete")
    result["app/scripts/build_picture_book.py"] = pdf_builder
    return result


def verify(bundle: Path):
    report = read_json(bundle / "preparation.json")
    if report.get("scope") != "published_display_only" or report.get("status") != "prepared_not_activated":
        raise ValueError("Not a completed display-only bundle")
    for relative, expected in report["files"].items():
        path = safe_path(bundle, relative)
        if path.stat().st_size != expected["bytes"] or digest(path) != expected["sha256"]:
            raise ValueError(f"Verification failed: {relative}")
    for name in ("data/drafts", "data/inbox", "runtime", "source-metadata"):
        if (bundle / name).exists():
            raise ValueError(f"Unexpected private/history directory: {name}")
    for name in ("data/approved", "service-data", "character-assets", "app/service", "app/scripts"):
        actual = {p.relative_to(bundle).as_posix() for p in (bundle / name).rglob("*") if p.is_file()}
        expected = {p for p in report["files"] if p.startswith(name + "/")}
        if actual != expected:
            raise ValueError(f"Unexpected/missing display files: {name}")
    for book in report["books"]:
        root = bundle / "data/approved" / book["id"]
        revisions = {p.name for p in (root / "releases").iterdir()}
        if revisions != {book["release_id"]} or read_json(root / "current.json")["release_id"] != book["release_id"]:
            raise ValueError("Historical/incorrect release in display bundle")
    return report


def prepare(workspace: Path, destination: Path, linux_path: str, image: str,
            image_id: str, image_archive: Path, listen_address: str,
            desktop_url: str = "",
            source_url: str = "http://127.0.0.1:8877"):
    workspace, destination = workspace.resolve(), destination.resolve()
    if workspace == destination or workspace in destination.parents or destination in workspace.parents:
        raise ValueError("Destination must be outside the source workspace")
    if not re.fullmatch(r"/volume[0-9]+/[A-Za-z0-9/_.-]+", linux_path) or ".." in linux_path:
        raise ValueError("Unsafe NAS path")
    if not re.fullmatch(r"roro-story-runtime:[a-z0-9][a-z0-9._-]*", image) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Invalid versioned image identity")
    ipaddress.IPv4Address(listen_address)
    if desktop_url:
        parsed_desktop = urllib.parse.urlsplit(desktop_url)
        if parsed_desktop.scheme not in {"http", "https"} or not parsed_desktop.netloc or parsed_desktop.path not in {"", "/"} or any(char in desktop_url for char in "\r\n"):
            raise ValueError("Invalid desktop sync URL")
    selected, books = plan(workspace, shelf(source_url))
    selected.update({relative: (source, digest(source)) for relative, source in application_files(workspace).items()})
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "INCOMPLETE").write_text("Display bundle preparation incomplete. Do not start.\n")
    # Docker must see the nested character mountpoint before mounting it over
    # the read-only /app/service bind. Keep the directory empty; its contents
    # come from the separate character-assets bind mount.
    (destination / "app/service/assets/characters").mkdir(parents=True, exist_ok=True)
    records = {}
    for index, (relative, (path, expected)) in enumerate(sorted(selected.items()), 1):
        try:
            records[relative] = copy_verified(path, destination / relative, expected)
        except OSError as exc:
            raise OSError(f"Failed to copy {relative}: {exc}") from exc
        if index % 50 == 0:
            print(f"Verified {index}/{len(selected)} selected display files", flush=True)
    times = destination / "service-data/story-production-times.json"
    write_json(times, {"schema_version": "1.0", "sort": "newest_first", "stories": [
        {"id": f"{book['id']}/releases/{book['release_id']}", "production_time": book["production_time"]} for book in books]})
    records["service-data/story-production-times.json"] = {"sha256": digest(times), "bytes": times.stat().st_size}
    for name in ("compose.yaml", "START-HERE.md"):
        records[name] = copy_verified(workspace / "deploy/nas" / name, destination / name)
    compose = destination / "compose.yaml"
    if image not in compose.read_text(encoding="utf-8"):
        raise ValueError("Compose does not reference the prepared runtime image")
    records["verify-bundle.py"] = copy_verified(Path(__file__), destination / "verify-bundle.py")
    records["image.tar"] = copy_verified(image_archive, destination / "image.tar")
    (destination / "cache/exports").mkdir(parents=True)
    (destination / "sync-state").mkdir(parents=True)
    (destination / ".env").write_text(f"PUID=1000\nPGID=10\nRORO_LISTEN_ADDRESS={listen_address}\nRORO_SYNC_SOURCE_URL={desktop_url}\n", encoding="utf-8", newline="\n")
    current, current_books = plan(workspace, shelf(source_url))
    current.update({relative: (source, digest(source)) for relative, source in application_files(workspace).items()})
    if current_books != books or set(current) != set(selected):
        raise ValueError("Visible shelf changed during preparation; prepare a new snapshot")
    for relative, (path, _) in current.items():
        if digest(path) != records[relative]["sha256"]:
            raise ValueError(f"Selected source changed: {relative}")
    report = {"schema_version": 1, "status": "prepared_not_activated", "scope": "published_display_only",
        "created_at": datetime.now(timezone.utc).isoformat(), "platform": "linux/amd64",
        "image": image, "image_id": image_id, "image_scope": "runtime_dependencies_only",
        "code_mounts": ["./app/service:/app/service:ro", "./app/scripts:/app/scripts:ro"],
        "linux_path": linux_path, "books": books,
        "source_file_count": len(selected), "nas_permissions_verified": False,
        "files": records}
    write_json(destination / "preparation.json", report)
    verify(destination)
    (destination / "INCOMPLETE").unlink()
    print(json.dumps({"books": len(books), "audio_options": sum(b["audio_options"] for b in books),
        "files": len(records), "bytes": sum(f["bytes"] for f in records.values()), "status": report["status"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="command", required=True)
    action = actions.add_parser("prepare")
    for name in ("workspace", "destination", "image-archive"):
        action.add_argument(f"--{name}", type=Path, required=True)
    for name in ("linux-path", "image", "image-id", "listen-address"):
        action.add_argument(f"--{name}", required=True)
    action.add_argument("--desktop-url", default="")
    action.add_argument("--source-url", default="http://127.0.0.1:8877")
    check = actions.add_parser("verify")
    check.add_argument("bundle", type=Path)
    args = vars(parser.parse_args())
    command = args.pop("command")
    try:
        if command == "prepare":
            prepare(**args)
        else:
            report = verify(args["bundle"].resolve())
            print(f"Verified display bundle: {len(report['books'])} books, {len(report['files'])} files")
    except (OSError, ValueError, KeyError) as error:
        parser.exit(2, f"Display preparation failed: {error}\n")
