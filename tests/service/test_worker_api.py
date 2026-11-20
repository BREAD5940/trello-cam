"""The job API (panel/worker_api.py) and the shop PC's side of it (fusion/autocam_addin/autocam_worker/remote.py),
wired straight together: no sockets, a real hot folder on each side."""
import hashlib
import io
import json
import zipfile

from autocam_core.hotfolder import Queue
from autocam_service.panel.api import WORKER_PREFIX, body_limit
from autocam_service.panel.worker_api import WorkerAPI
from autocam_worker.remote import UPLOADED, Remote, localize

TOKEN = "worker-token"


def transport_for(api):
    def send(method, path, body, headers):
        resp = api.handle(method, WORKER_PREFIX + path, {k.lower(): v for k, v in headers.items()}, body)
        return resp.status, resp.body
    return send


class Setup:
    """A server (its repo, a STEP cache outside the repo like the team server's, its queue) and a shop PC."""

    def __init__(self, tmp_path, token=TOKEN):
        self.server_repo = tmp_path / "server" / "repo"
        for f in ("fusion/templates/alu_4mm.f3dhsm-template", "fusion/posts/shopsabre.cps"):
            (self.server_repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.server_repo / f).write_text("server copy")
        self.step = tmp_path / "server" / "data" / "cache" / "step" / "plate.step"
        self.step.parent.mkdir(parents=True)
        self.step.write_bytes(b"ISO-10303-21; a plate")
        self.sha = hashlib.sha256(self.step.read_bytes()).hexdigest()
        self.server_q = Queue(tmp_path / "server" / "queue").ensure()
        self.clock = lambda: 1_800_000_000.0
        self.api = WorkerAPI(self.server_q, TOKEN, max_attempts=2, repo_root=self.server_repo, clock=self.clock)
        self.pc_repo = tmp_path / "pc" / "repo"
        self.pc_q = Queue(self.pc_repo / "queue").ensure()
        self.logs = []
        self.remote = Remote(self.pc_q, token, self.pc_repo, transport_for(self.api), self.logs.append)

    def job(self, job_id):
        root = self.server_repo.as_posix()
        return json.dumps({
            "job_id": job_id,
            "parts": [{"name": "plate", "step": self.step.as_posix(), "step_sha256": self.sha}],
            "tooling": {"tools": {"t1": {"template_path": f"{root}/fusion/templates/alu_4mm.f3dhsm-template"}}},
            "face": {"tool": {"template_path": f"{root}/fusion/templates/alu_4mm.f3dhsm-template"}},
            "post": {"path": f"{root}/fusion/posts/shopsabre.cps", "sha256": "0" * 64},
            "parts_url": "https://trello.com/c/abc",
        })


def finish_locally(q, job_id, files):
    claim = q.claim(job_id)
    for name, data in files.items():
        (claim.out_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (claim.out_dir / name).write_bytes(data)
    return q.complete(claim)


def test_a_job_goes_round_trip(tmp_path):
    s = Setup(tmp_path)
    s.server_q.submit("r900-al6061", s.job("r900-al6061"))

    s.remote.sync()      # recover, nothing to send, claim

    assert s.server_q.where("r900-al6061") == "processing"
    assert s.pc_q.pending() == ["r900-al6061"]
    job = json.loads((s.pc_q.incoming / "r900-al6061.json").read_text())
    local_step = s.pc_repo / "cache" / "remote" / f"{s.sha}.step"
    assert job["parts"][0]["step"] == local_step.as_posix()
    assert local_step.read_bytes() == s.step.read_bytes()
    pc = s.pc_repo.as_posix()
    assert job["tooling"]["tools"]["t1"]["template_path"] == f"{pc}/fusion/templates/alu_4mm.f3dhsm-template"
    assert job["face"]["tool"]["template_path"] == f"{pc}/fusion/templates/alu_4mm.f3dhsm-template"
    assert job["post"]["path"] == f"{pc}/fusion/posts/shopsabre.cps"
    assert job["parts_url"] == "https://trello.com/c/abc"      # only the server's paths change

    s.remote.sync()      # the job is waiting here: no second claim
    assert s.server_q.where("r900-al6061") == "processing" and s.pc_q.pending() == ["r900-al6061"]

    finish_locally(s.pc_q, "r900-al6061", {"result.json": b'{"ok": true}', "S1.tap": b"G0 X0\n",
                                            "raw/S1/program.nc": b"raw"})
    s.pc_q.write_heartbeat({"ts": "2000-01-01T00:00:00Z", "job": None, "core_version": "x"})
    s.remote.sync()

    done = s.server_q.done / "r900-al6061"
    assert s.server_q.finished() == ["r900-al6061"]
    assert (done / "S1.tap").read_bytes() == b"G0 X0\n" and (done / "raw" / "S1" / "program.nc").exists()
    assert not (done / UPLOADED).exists()
    assert (s.pc_q.done / "r900-al6061" / UPLOADED).exists()
    beat = s.server_q.read_heartbeat()
    assert beat["ts"] == "2027-01-15T08:00:00Z" and beat["worker_ts"] == "2000-01-01T00:00:00Z"

    s.remote.sync()      # nothing new: the uploaded job isn't sent twice
    assert s.server_q.finished() == ["r900-al6061"]


def test_a_failed_job_reaches_the_server_with_its_partial_outputs(tmp_path):
    s = Setup(tmp_path)
    s.server_q.submit("r901-pc_clear", s.job("r901-pc_clear"))
    s.remote.sync()
    claim = s.pc_q.claim("r901-pc_clear")
    (claim.out_dir / "worker.log").write_text("generate() raised")
    s.pc_q.fail("r901-pc_clear", "Fusion said the toolpath failed")
    s.remote.sync()
    failed = s.server_q.failed / "r901-pc_clear"
    assert "toolpath failed" in (failed / "error.txt").read_text()
    assert (failed / "partial" / "worker.log").read_text() == "generate() raised"
    assert (failed / "job.json").exists()
    assert s.server_q.finished() == ["r901-pc_clear"]


def test_a_restarted_pc_gives_back_the_jobs_it_lost(tmp_path):
    s = Setup(tmp_path)
    s.server_q.submit("r902-al6061", s.job("r902-al6061"))
    s.server_q.submit("r903-al6061", s.job("r903-al6061"))
    s.remote.sync()                                   # claims r902
    (s.pc_q.incoming / "r902-al6061.json").unlink()   # the PC lost it (a new disk, a wiped spool)
    s.server_q.claim("r903-al6061")                   # and r903 was claimed by a PC that's gone

    fresh = Remote(s.pc_q, TOKEN, s.pc_repo, transport_for(s.api), s.logs.append)
    fresh.recover()
    assert s.server_q.where("r902-al6061") == "incoming" and s.server_q.where("r903-al6061") == "incoming"
    assert s.server_q.attempts("r902-al6061") == 1    # the attempt still counts

    s.server_q.claim("r902-al6061")                   # a second lost attempt hits max_attempts (2): failed
    fresh.recover()
    assert s.server_q.where("r902-al6061") == "failed"


def test_the_pc_keeps_what_it_still_holds(tmp_path):
    s = Setup(tmp_path)
    s.server_q.submit("r904-al6061", s.job("r904-al6061"))
    s.remote.sync()
    fresh = Remote(s.pc_q, TOKEN, s.pc_repo, transport_for(s.api), s.logs.append)
    fresh.recover()
    assert s.server_q.where("r904-al6061") == "processing"


def test_a_wrong_token_is_refused_and_logged_once(tmp_path):
    s = Setup(tmp_path, token="not-it")
    s.server_q.submit("r905-al6061", s.job("r905-al6061"))
    s.remote.sync()
    s.remote.sync()
    assert s.server_q.where("r905-al6061") == "incoming"
    assert len([m for m in s.logs if "refused the worker token" in m]) == 1


def test_uploads_cant_write_outside_the_job(tmp_path):
    s = Setup(tmp_path)
    s.server_q.submit("r906-al6061", s.job("r906-al6061"))
    s.remote.sync()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("result.json", "{}")
        z.writestr("../../escaped.txt", "nope")
    resp = s.api.handle("POST", WORKER_PREFIX + "done/r906-al6061", {"x-autocam-worker": TOKEN}, buf.getvalue())
    assert resp.status == 400
    assert not list(tmp_path.rglob("escaped.txt"))
    assert s.server_q.where("r906-al6061") == "processing"


def test_step_files_are_only_served_for_claimed_jobs(tmp_path):
    s = Setup(tmp_path)
    s.server_q.submit("r907-al6061", s.job("r907-al6061"))
    headers = {"x-autocam-worker": TOKEN}
    assert s.api.handle("GET", WORKER_PREFIX + f"file/{s.sha}", headers, b"").status == 404     # only waiting
    s.server_q.claim("r907-al6061")
    got = s.api.handle("GET", WORKER_PREFIX + f"file/{s.sha}", headers, b"")
    assert got.status == 200 and got.body == s.step.read_bytes()
    assert s.api.handle("GET", WORKER_PREFIX + "file/../../etc/passwd", headers, b"").status == 404


def test_results_for_a_job_the_server_doesnt_have_claimed_stop_being_sent(tmp_path):
    s = Setup(tmp_path)
    s.pc_q.submit("r908-al6061", s.job("r908-al6061"))   # a job the server never handed out
    finish_locally(s.pc_q, "r908-al6061", {"result.json": b"{}"})
    s.remote.recovered = True
    s.remote.sync()
    marker = s.pc_q.done / "r908-al6061" / UPLOADED
    assert marker.read_text().startswith("refused")
    assert s.server_q.where("r908-al6061") is None


def test_localize_leaves_other_paths_alone(tmp_path):
    job = {"parts": [{"step": "/srv/x.step", "step_sha256": "a" * 64}], "note": "/srv/bread/other/file",
           "post": {"path": "/srv/bread/repo/apps/autocam/fusion/posts/p.cps"}}
    out = localize(job, "/srv/bread/repo/apps/autocam", tmp_path, {"a" * 64: tmp_path / "s.step"})
    assert out["parts"][0]["step"] == (tmp_path / "s.step").as_posix()
    assert out["post"]["path"] == (tmp_path / "fusion" / "posts" / "p.cps").as_posix()
    assert out["note"] == "/srv/bread/other/file"


def test_the_panel_routes_worker_requests_with_their_own_limit(tmp_path):
    from test_panel import Panel
    p = Panel(tmp_path)
    assert p.app.handle("POST", WORKER_PREFIX + "claim", {}, {"x-autocam-worker": TOKEN}).status == 404   # off
    q = Queue(tmp_path / "q").ensure()
    p.app.worker = WorkerAPI(q, TOKEN, max_attempts=2, repo_root=tmp_path)
    assert p.app.handle("POST", WORKER_PREFIX + "claim", {}, {"x-autocam-worker": TOKEN}).status == 204
    assert p.app.handle("POST", WORKER_PREFIX + "claim", {}, {"x-autocam-key": "panel key"}).status == 403
    assert body_limit(WORKER_PREFIX + "done/r1-al") > body_limit("/api/send")
