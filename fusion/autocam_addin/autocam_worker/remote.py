"""Remote mode: the service and its job queue run on the team server, and this PC only runs Fusion.

The worker (worker.py) doesn't change: it still runs jobs from a local hot folder, which is now a spool that keeps
a claimed job and its outputs safe across a Fusion crash. `Remote.sync()` runs on the add-in's background thread
(never Fusion's main thread) and moves jobs between that spool and the server's job API
(service/autocam_service/panel/worker_api.py):

  1. finished jobs in done/ and failed/ are uploaded as one zip each, then marked with an `.uploaded` file;
  2. the heartbeat goes to the server, which stores it with its own clock;
  3. when nothing is waiting or running here, it claims the next job, downloads its STEP files into
     cache/remote/ (checked against their sha256), points the job's paths at this PC (STEP files, and the
     templates and post in this checkout), and puts it in incoming/ for the worker.

The first sync also tells the server which jobs this PC still holds, so ones lost here go back in the server's
queue. Standard library only: Fusion's Python can't install packages.
"""

import hashlib
import io
import json
import ssl
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from autocam_core.hotfolder import Queue

UPLOADED = ".uploaded"
TIMEOUT_S = 120
USER_AGENT = "AutoCAM-worker/1"

# (method, path under /api/worker/, body, headers) -> (status, body)
Transport = Callable[[str, str, bytes, Dict[str, str]], Tuple[int, bytes]]


class RemoteError(Exception):
    pass


def urllib_transport(base_url: str, timeout_s: float = TIMEOUT_S) -> Transport:
    base = base_url.rstrip("/") + "/api/worker/"
    context = ssl.create_default_context()

    def send(method: str, path: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, bytes]:
        req = urllib.request.Request(base + path, data=body if method == "POST" else None, method=method,
                                     headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s, context=context) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            raise RemoteError(f"can't reach {base_url}: {e}") from None

    return send


def zip_folder(folder: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.name != UPLOADED:
                z.write(path, path.relative_to(folder).as_posix())
    return buf.getvalue()


def localize(job: Dict[str, Any], server_root: str, local_root: Path, steps: Dict[str, Path]) -> Dict[str, Any]:
    """The job as this PC must see it: STEP files from cache/remote/ (by their sha256), and anything else that
    lived in the server's checkout (templates, the post) from this checkout."""
    for part in job.get("parts") or []:
        sha = part.get("step_sha256")
        if sha in steps:
            part["step"] = steps[sha].as_posix()
    prefix = server_root.rstrip("/") + "/"

    def fix(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if isinstance(value, str) and server_root and value.startswith(prefix):
            return (local_root / value[len(prefix):]).as_posix()
        return value

    return fix(job)


class Remote:
    def __init__(self, queue: Queue, token: str, repo: Path, transport: Transport,
                 log: Callable[[str], None] = lambda msg: None):
        self.queue = queue
        self.token = token
        self.repo = Path(repo)
        self.cache = self.repo / "cache" / "remote"
        self.transport = transport
        self.log = log
        self.recovered = False
        self._last_error = ""

    # ---- the one call the add-in makes
    def sync(self) -> None:
        """Never raises: a network problem is logged once and retried on the next call."""
        try:
            if not self.recovered:
                self.recover()
            self.upload_finished()
            self.send_heartbeat()
            if self.idle():
                self.claim()
            self._last_error = ""
        except RemoteError as e:
            if str(e) != self._last_error:
                self.log(f"remote: {e}")
                self._last_error = str(e)

    # ---- steps
    def recover(self) -> None:
        held = self.holding()
        got = self._call_json("POST", "recover", {"holding": held})
        self.recovered = True
        if got.get("requeued") or got.get("failed"):
            self.log(f"remote: the server re-queued {got.get('requeued')} and failed {got.get('failed')}")

    def holding(self) -> List[str]:
        """Jobs this PC has and the server should leave alone: waiting, running, or finished but not uploaded."""
        q = self.queue
        jobs = {p.stem for d in (q.incoming, q.processing) for p in d.glob("*.json") if not p.name.startswith(".")}
        jobs |= {d.name for d in self._unsent()}
        return sorted(jobs)

    def upload_finished(self) -> None:
        for folder in self._unsent():
            kind = "done" if folder.parent == self.queue.done else "failed"
            status, body = self._send("POST", f"{kind}/{folder.name}", zip_folder(folder),
                                      {"Content-Type": "application/zip"})
            if status == 200:
                (folder / UPLOADED).write_text("ok\n", encoding="utf-8")
                self.log(f"remote: sent {folder.name} ({kind})")
            elif status == 409:      # the server no longer has it claimed: don't send it forever
                (folder / UPLOADED).write_text(f"refused: {body[:300].decode('utf-8', 'replace')}\n", encoding="utf-8")
                self.log(f"remote: the server refused {folder.name}: {body[:200].decode('utf-8', 'replace')}")
            else:
                raise RemoteError(f"uploading {folder.name} got HTTP {status}")

    def send_heartbeat(self) -> None:
        info = self.queue.read_heartbeat()
        if info:
            self._call_json("POST", "heartbeat", info)

    def idle(self) -> bool:
        q = self.queue
        return not q.pending() and not any(p for p in q.processing.glob("*.json") if not p.name.startswith("."))

    def claim(self) -> Optional[str]:
        status, body = self._send("POST", "claim", b"", {})
        if status == 204:
            return None
        if status != 200:
            raise RemoteError(f"claiming a job got HTTP {status}")
        got = json.loads(body.decode("utf-8"))
        job_id = got["job_id"]
        if self.queue.where(job_id) is not None:
            self.log(f"remote: {job_id} is already here; not taking it twice")
            return None
        steps = {f["sha256"]: self.fetch(f["sha256"], f.get("name", "")) for f in got.get("files") or []}
        job = localize(json.loads(got["job"]), got.get("repo_root", ""), self.repo, steps)
        self.queue.submit(job_id, json.dumps(job, indent=2) + "\n")
        self.log(f"remote: claimed {job_id} (attempt {got.get('attempt')}, {len(steps)} STEP file(s))")
        return job_id

    def fetch(self, sha: str, name: str) -> Path:
        suffix = Path(name).suffix.lower() or ".step"
        target = self.cache / f"{sha}{suffix}"
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == sha:
            return target
        status, data = self._send("GET", f"file/{sha}", b"", {})
        if status != 200:
            raise RemoteError(f"downloading STEP file {sha[:12]} got HTTP {status}")
        if hashlib.sha256(data).hexdigest() != sha:
            raise RemoteError(f"STEP file {sha[:12]} came down damaged (sha256 differs)")
        self.cache.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        return target

    # ---- plumbing
    def _unsent(self) -> List[Path]:
        out = []
        for d, need in ((self.queue.done, "result.json"), (self.queue.failed, "error.txt")):
            if d.exists():
                out += [f for f in d.iterdir() if f.is_dir() and (f / need).is_file() and not (f / UPLOADED).exists()]
        return sorted(out, key=lambda f: (f.stat().st_mtime, f.name))

    def _send(self, method: str, path: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, bytes]:
        status, data = self.transport(method, path, body, {"x-autocam-worker": self.token, **headers})
        if status == 403:
            raise RemoteError("the server refused the worker token (AUTOCAM_WORKER_TOKEN in .env)")
        return status, data

    def _call_json(self, method: str, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        status, body = self._send(method, path, json.dumps(data).encode("utf-8"), {"Content-Type": "application/json"})
        if status != 200:
            raise RemoteError(f"{path} got HTTP {status}")
        return json.loads(body.decode("utf-8") or "{}")


def read_env_value(env_file: Path, key: str) -> str:
    """One value from the checkout's .env (KEY=value lines), without python-dotenv."""
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.strip().partition("=")
            if name.strip().removeprefix("export ").strip() == key:
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def make_remote(queue: Queue, repo: Path, url: str, log: Callable[[str], None]) -> Optional[Remote]:
    """The add-in's Remote, or None in local mode ([fusion] remote = ""). Raises if remote mode has no token."""
    if not url:
        return None
    token = read_env_value(Path(repo) / ".env", "AUTOCAM_WORKER_TOKEN")
    if not token:
        raise RemoteError("[fusion] remote is set but AUTOCAM_WORKER_TOKEN isn't in .env")
    return Remote(queue, token, repo, urllib_transport(url), log)
