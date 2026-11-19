"""Practice mode (docs/practice.md): the same service on a practice board, with throwaway records, "p" runs and
nothing written to Onshape; practice-reset and practice-done around it."""
import json
from dataclasses import replace

import pytest

from autocam_service import cli
from autocam_service.config import ConfigError, load_config, practice_config
from autocam_service.panel.api import PanelApp
from autocam_service.run_state import RunStore
from autocam_service.tracker.base import Card
from autocam_service.tracker.fake import FakeTracker
from test_panel import KEY, Q, Panel, card_in

LISTS = ("inbox", "ready_for_cam", "needs_fixing", "nested", "sheet_review", "ready_to_cut", "cut", "offcuts", "control")
BOARD = "b" * 24


def practice_toml(path, board=BOARD, lists=None, system="c" * 24):
    lists = lists or {k: f"{i + 1:x}".rjust(24, "e") for i, k in enumerate(LISTS)}
    body = f'[trello]\nboard_id = "{board}"\n\n[trello.lists]\n' + "".join(f'{k} = "{v}"\n' for k, v in lists.items())
    path.write_text(body + f'\n[trello.cards]\nsystem = "{system}"\n', encoding="utf-8")
    return path


def test_practice_mode_is_the_practice_board_its_own_records_and_logs(tmp_path):
    cfg = load_config()
    p = practice_config(cfg, practice_toml(tmp_path / "practice.toml"))
    assert p.practice and not cfg.practice
    assert p.trello.board_id == BOARD and p.trello.lists["inbox"] == "e" * 23 + "1"
    assert p.trello.cards["system"] == "c" * 24
    assert p.paths.state == cfg.root / "state-practice" and p.paths.logs == cfg.paths.logs / "practice"
    assert p.paths.queue == cfg.paths.queue                       # the same Fusion
    assert p.trello.targets == cfg.trello.targets and p.materials == cfg.materials


def test_practice_mode_refuses_the_real_board(tmp_path):
    cfg = load_config()
    with pytest.raises(ConfigError, match="the practice board is the real board"):
        practice_config(cfg, practice_toml(tmp_path / "a.toml", board=cfg.trello.board_id))
    real_list = {k: f"{i + 1:x}".rjust(24, "e") for i, k in enumerate(LISTS)}
    real_list["cut"] = cfg.trello.lists["cut"]
    with pytest.raises(ConfigError, match="one of the real board's"):
        practice_config(cfg, practice_toml(tmp_path / "b.toml", lists=real_list))
    with pytest.raises(ConfigError, match="offcuts is missing"):
        practice_config(cfg, practice_toml(tmp_path / "c.toml", lists={k: v for k, v in real_list.items()
                                                                        if k not in ("offcuts", "cut")}))
    with pytest.raises(ConfigError, match="cannot read"):
        practice_config(cfg, tmp_path / "none.toml")


def test_practice_runs_are_p_runs(tmp_path):
    store = RunStore(tmp_path, prefix="p")
    assert store.next_run_id() == "p001"


def test_in_practice_the_panel_writes_nothing_to_onshape_and_every_page_says_practice(tmp_path):
    p = Panel(tmp_path)
    cfg = replace(p.h.cfg, practice=True)
    p.app = PanelApp(cfg, KEY, p.onshape, p.board, p.names, clock=p.clock)
    p.call("GET", "/api/studio", Q)
    status, data = p.send(p="JHG", material="al5052")
    assert status == 200 and not data["material"]["setInOnshape"]
    assert data["material"]["why"] == "practice mode doesn't change Onshape"
    assert [s for s in p.transport.sent if s[0] == "POST"] == []    # no metadata write
    [card] = card_in(p, "ready_for_cam")
    assert "Material: 5052" in card.desc                            # the card carries it instead
    page = p.app.handle("GET", "/panel", {"key": KEY}, {}).body.decode()
    assert '<body><div class="practice-bar" role="status">Practice mode: nothing you do here is kept</div>' in page
    real = PanelApp(p.h.cfg, KEY, p.onshape, p.board, p.names, clock=p.clock)
    assert "practice-bar" not in real.handle("GET", "/panel", {"key": KEY}, {}).body.decode()


def practice_setup(tmp_path, monkeypatch):
    real = load_config()
    root = tmp_path / "repo"
    cfg = replace(real, root=root, path=root / "config" / "autocam.toml",
                  paths=replace(real.paths, state=root / "state", queue=root / "queue", logs=root / "logs"))
    practice_toml((root / "config").mkdir(parents=True) or root / "config" / "practice.toml")
    pcfg = practice_config(cfg)
    board = FakeTracker([Card("c" * 24, "System", "", "control", "u"),
                         Card("1" * 24, "a part", "", "ready_for_cam", "u"),
                         Card("3" * 24, "How to add a part (read me)", "", "inbox", "u"),
                         Card("4" * 24, "New part", "", "inbox", "u"),
                         Card("2" * 24, "p001 S1", "", "sheet_review", "u")])
    monkeypatch.setattr("autocam_service.app.trello_tracker", lambda c, env: board)
    return cfg, pcfg, board


def test_practice_reset_empties_the_practice_board_and_starts_its_records_from_whos_who(tmp_path, monkeypatch):
    cfg, pcfg, board = practice_setup(tmp_path, monkeypatch)
    cfg.paths.state.mkdir(parents=True)
    for name, text in (("people.json", '{"names": ["Maya"]}'), ("shop_sessions.json", "{}"),
                       ("shafts.json", "[1]"), ("onshape_ledger.jsonl", "a\n")):
        (cfg.paths.state / name).write_text(text)
    pcfg.paths.state.mkdir(parents=True)
    (pcfg.paths.state / "prints.json").write_text("[old practice]")
    assert cli.practice_reset(cfg, tmp_path / ".env") == 0
    assert set(board.archived) == {"1" * 24, "2" * 24}             # not the System card
    assert sorted(p.name for p in pcfg.paths.state.iterdir()) == ["onshape_ledger.jsonl", "people.json",
                                                                   "shop_sessions.json"]
    assert (pcfg.paths.state / "people.json").read_text() == '{"names": ["Maya"]}'


def test_practice_done_brings_back_whos_who_and_drops_its_waiting_jobs(tmp_path, monkeypatch):
    cfg, pcfg, board = practice_setup(tmp_path, monkeypatch)
    for d in (cfg.paths.state, pcfg.paths.state, cfg.paths.queue / "incoming"):
        d.mkdir(parents=True)
    (pcfg.paths.state / "shop_sessions.json").write_text(json.dumps({"new": "Theo"}))
    (pcfg.paths.state / "onshape_ledger.jsonl").write_text("a\nb\n")
    (pcfg.paths.state / "shafts.json").write_text("[practice shafts]")
    (cfg.paths.queue / "incoming" / "p002-al6061.json").write_text("{}")
    (cfg.paths.queue / "incoming" / "r114-al6061.json").write_text("{}")
    assert cli.practice_done(cfg) == 0
    assert json.loads((cfg.paths.state / "shop_sessions.json").read_text()) == {"new": "Theo"}
    assert (cfg.paths.state / "onshape_ledger.jsonl").read_text() == "a\nb\n"
    assert not (cfg.paths.state / "shafts.json").exists()           # practice's lists stay behind
    assert [p.name for p in (cfg.paths.queue / "incoming").iterdir()] == ["r114-al6061.json"]
