"""The job API: the Fusion worker on the shop PC, over HTTPS, when the service runs on the team server.

The server keeps the hot folder (core/autocam_core/hotfolder.py) as the record of every job, so the runner, results,
pictures and the machine screen read it exactly as before. These routes only stand in for the worker's side of it:

  POST /api/worker/heartbeat      the worker's heartbeat; stored with the server's time (health.py compares to it)
  POST /api/worker/recover        the worker restarted: re-queue claimed jobs it no longer holds
  POST /api/worker/claim          the oldest waiting job (204: none), with the STEP files it needs
  GET  /api/worker/file/<sha256>  one of those STEP files (only ones a claimed job names)
  POST /api/worker/done/<job>     the job's output folder as a zip, result.json inside: done/<job> appears at once
  POST /api/worker/failed/<job>   error.txt (+ partial/) as a zip: the job goes to failed/

Every request carries the x-autocam-worker header (AUTOCAM_WORKER_TOKEN in .env). See
fusion/autocam_addin/autocam_worker/remote.py for the shop PC's side and docs/decisions.md (2026-11-19).
"""

import hmac
import io
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List

from autocam_core import CORE_VERSION
from autocam_core.hotfolder import Claim, Queue, QueueError, _check_id

from .api import WORKER_PREFIX, Response, _json

log = logging.getLogger(__name__)

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_UNZIPPED = 400 * 1024 * 1024        # a job's outputs are a few MB; this only stops a runaway zip


class WorkerRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def utc_stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def unzip_into(data: bytes, dest: Path) -> None:
    """Unpacks an upload, refusing anything that would land outside dest (absolute paths, '..', drive letters,
    symlinks) or that unpacks to more than MAX_UNZIPPED."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise WorkerRefused(400, "not a zip file")
    total = 0
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            if not parts or name.startswith("/") or ".." in parts or ":" in parts[0]:
                raise WorkerRefused(400, f"unsafe path in the zip: {info.filename!r}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise WorkerRefused(400, f"symlink in the zip: {info.filename!r}")
            total += info.file_size
            if total > MAX_UNZIPPED:
                raise WorkerRefused(413, "the zip unpacks too big")
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


class WorkerAPI:
    def __init__(self, queue: Queue, token: str, max_attempts: int, repo_root: Path,
                 clock: Callable[[], float] = time.time):
        if not token:
            raise ValueError("the job API needs AUTOCAM_WORKER_TOKEN")
        self.queue = queue
        self._token = token.encode("utf-8")
        self.max_attempts = max_attempts
        self.repo_root = Path(repo_root).resolve()
        self.clock = clock
        self._lock = threading.Lock()      # one claim, upload or recovery at a time

    def handle(self, method: str, path: str, headers: Dict[str, str], body: bytes) -> Response:
        if not hmac.compare_digest(headers.get("x-autocam-worker", "").encode("utf-8"), self._token):
            return _json(403, {"error": "wrong worker token"})
        name = path[len(WORKER_PREFIX):]
        try:
            if method == "POST" and name == "heartbeat":
                return _json(200, self.heartbeat(_json_body(body)))
            if method == "POST" and name == "recover":
                return _json(200, self.recover(_json_body(body)))
            if method == "POST" and name == "claim":
                found = self.claim()
                return Response(204, b"", headers={"Cache-Control": "no-store"}) if found is None else _json(200, found)
            if method == "GET" and name.startswith("file/"):
                return Response(200, self.file(name[len("file/"):]), "application/octet-stream",
                                headers={"Cache-Control": "no-store"})
            if method == "POST" and name.startswith("done/"):
                return _json(200, self.done(name[len("done/"):], body))
            if method == "POST" and name.startswith("failed/"):
                return _json(200, self.failed(name[len("failed/"):], body))
            return _json(404, {"error": "Not found"})
        except WorkerRefused as e:
            return _json(e.status, {"error": str(e)})
        except QueueError as e:
            return _json(409, {"error": str(e)})

    # ---- routes
    def heartbeat(self, info: Dict[str, Any]) -> Dict[str, Any]:
        info = dict(info)
        info["worker_ts"] = info.get("ts")
        info["ts"] = utc_stamp(self.clock())     # the server's clock: health.py measures staleness against it
        self.queue.write_heartbeat(info)
        return {"ok": True, "core_version": CORE_VERSION}

    def recover(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Like Queue.recover, but only for jobs the shop PC no longer holds: the ones it still has, it finishes."""
        holding = set(data.get("holding") or [])
        requeued: List[str] = []
        failed: List[str] = []
        with self._lock:
            for path in sorted(self.queue.processing.glob("*.json")):
                job_id = path.stem
                if job_id in holding or (self.queue.failed / job_id / "error.txt").exists():
                    continue
                n = self.queue.attempts(job_id)
                if n >= self.max_attempts:
                    self.queue.fail(job_id, f"the worker started this job {n} times and never finished it "
                                            "(Fusion crashed or was closed mid-job)")
                    failed.append(job_id)
                else:
                    out = self.queue.processing / f"{job_id}.out"
                    if out.exists():
                        shutil.rmtree(out)
                    os.replace(path, self.queue.incoming / path.name)
                    requeued.append(job_id)
        if requeued or failed:
            log.info("worker recovery: re-queued %s, failed %s", requeued, failed)
        return {"requeued": requeued, "failed": failed}

    def claim(self):
        with self._lock:
            for job_id in self.queue.pending():
                claim = self.queue.claim(job_id)
                if claim is None:
                    continue
                text = claim.job_path.read_text(encoding="utf-8")
                files: Dict[str, str] = {}
                try:
                    for part in json.loads(text).get("parts") or []:
                        sha, step = part.get("step_sha256"), part.get("step")
                        if isinstance(sha, str) and SHA_RE.match(sha) and isinstance(step, str):
                            files.setdefault(sha, PurePosixPath(step.replace("\\", "/")).name)
                except ValueError:
                    pass       # the worker's pipeline reports a broken job the normal way
                log.info("worker claimed %s (attempt %d)", job_id, claim.attempt)
                return {"job_id": job_id, "attempt": claim.attempt, "job": text, "repo_root": self.repo_root.as_posix(),
                        "files": [{"sha256": sha, "name": n} for sha, n in sorted(files.items())]}
        return None

    def file(self, sha: str) -> bytes:
        if not SHA_RE.match(sha):
            raise WorkerRefused(404, "Not found")
        for job_path in self.queue.processing.glob("*.json"):
            try:
                parts = json.loads(job_path.read_text(encoding="utf-8")).get("parts") or []
            except (OSError, ValueError):
                continue
            for part in parts:
                if part.get("step_sha256") == sha and isinstance(part.get("step"), str):
                    path = Path(part["step"])
                    if path.is_file():
                        return path.read_bytes()
        raise WorkerRefused(404, "no claimed job names that file")

    def done(self, job_id: str, body: bytes) -> Dict[str, Any]:
        _check_id(job_id)
        with self._lock:
            job_path = self.queue.processing / f"{job_id}.json"
            if not job_path.is_file():
                raise WorkerRefused(409, f"{job_id} isn't claimed on the server (already finished or re-queued)")
            out = self.queue.processing / f"{job_id}.out"
            if out.exists():
                shutil.rmtree(out)
            out.mkdir()
            try:
                unzip_into(body, out)
            except WorkerRefused:
                shutil.rmtree(out, ignore_errors=True)
                out.mkdir()
                raise
            target = self.queue.complete(Claim(job_id, job_path, self.queue.attempts(job_id), out))
        log.info("worker finished %s", job_id)
        return {"ok": True, "done": target.name}

    def failed(self, job_id: str, body: bytes) -> Dict[str, Any]:
        _check_id(job_id)
        with self._lock:
            if self.queue.where(job_id) not in ("processing", "incoming"):
                raise WorkerRefused(409, f"{job_id} isn't waiting or claimed on the server")
            with tempfile.TemporaryDirectory(dir=self.queue.root) as tmp:
                unzip_into(body, Path(tmp))
                try:
                    error = (Path(tmp) / "error.txt").read_text(encoding="utf-8")
                except OSError:
                    error = "the worker reported a failure without saying why"
                partial = Path(tmp) / "partial"
                if partial.is_dir() and any(partial.iterdir()):
                    out = self.queue.processing / f"{job_id}.out"
                    if out.exists():
                        shutil.rmtree(out)
                    shutil.move(str(partial), str(out))
                self.queue.fail(job_id, error)
        log.info("worker failed %s", job_id)
        return {"ok": True}


def _json_body(body: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError):
        raise WorkerRefused(400, "Bad request")
    if not isinstance(data, dict):
        raise WorkerRefused(400, "Bad request")
    return data
