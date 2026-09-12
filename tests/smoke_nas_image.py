"""Opt-in image smoke check; run ONLY in an isolated disposable container.

Mount approved/drafts/inbox and catalogs read-only. /runtime and /data/exports
must be empty writable tmpfs, /runtime-source is a read-only business-state source.
No host ports are needed. This does not test/approve story contents.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

assert sys.platform == "linux", "Run in the disposable Linux container only"
runtime = Path("/runtime")
if runtime.exists():
    assert not any(runtime.iterdir()), "Runtime must be an empty disposable tmpfs"
    for name in ("shelf-status.json", "shelf-history.jsonl", "story-shares.json"):
        source = Path("/runtime-source") / name
        if source.exists():
            shutil.copyfile(source, runtime / name)

base = "http://127.0.0.1:18877"
def get(path, partial=False):
    request = urllib.request.Request(base + path, headers={"Range": "bytes=0-127"} if partial else {})
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
        assert response.status == (206 if partial else 200), (path, response.status)
        if partial:
            assert response.headers.get("Content-Range", "").startswith("bytes 0-")
        return body

display_only = os.environ.get("RORO_SMOKE_DISPLAY_ONLY") == "1"
arguments = [sys.executable, "/app/service/story_server.py",
    "--host", "127.0.0.1", "--port", "18877", "--directory", "/data/approved",
    "--review-auth-mode", "development"] + (["--display-only"] if display_only else [])
process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for attempt in range(100):
        try:
            health = json.loads(get("/health"))
            break
        except urllib.error.URLError:
            if process.poll() is not None:
                raise RuntimeError("Isolated server exited during startup")
            time.sleep(0.1)
    else:
        raise RuntimeError("Isolated server startup timeout")
    assert health["status"] == "ok" and health["review_mode"] == "development"
    for path in ("/", "/characters"):
        assert b"<html" in get(path).lower()
    if display_only:
        try:
            get("/workbench")
            raise AssertionError("Display-only workbench unexpectedly available")
        except urllib.error.HTTPError as error:
            assert error.code == 404
    else:
        assert b"<html" in get("/workbench").lower()
    stories = json.loads(get("/api/stories"))["stories"]
    characters = json.loads(get("/api/characters"))["characters"]
    studio = [] if display_only else json.loads(get("/api/studio/stories"))["stories"]
    if not display_only:
        json.loads(get("/api/studio/story-library"))
    assert stories and characters and (display_only or studio)
    state_path = runtime / "shelf-status.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    unpublished = {key for key, value in state.items() if value.get("status") == "unpublished"}
    assert not ({item["id"] for item in stories} & unpublished), "Unpublished book leaked onto shelf"
    audio_count = 0
    page_count = 0
    for item in stories:
        detail = json.loads(get("/api/stories/" + item["id"]))
        get(detail["cover_url"], partial=True)
        for page in detail["pages"]:
            get(page["image_url"], partial=True)
            page_count += 1
        for option in detail["audio_options"]:
            get(option["url"], partial=True)
            audio_count += 1
    pdf = get(stories[0]["download_url"])
    assert pdf.startswith(b"%PDF-"), "PDF export failed"
    print(json.dumps({"shelf_books": len(stories), "studio_entries": len(studio),
        "characters": len(characters), "unpublished_records_preserved": len(unpublished),
        "page_image_ranges": page_count, "audio_ranges": audio_count,
        "pdf_download": "passed", "server_user": "non-root",
        "display_only": display_only, "scope": "local disposable image smoke; NAS not started"}, ensure_ascii=False))
finally:
    process.terminate()
    process.wait(timeout=10)
