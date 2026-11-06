"""Live queue files stay off cloud-synced folders and network shares; finished state is exported."""

from __future__ import annotations

import json
import os

import pytest

from locum.rpa import WorkQueue, default_queue_dir, synced_location


@pytest.fixture(autouse=True)
def _no_real_onedrive(monkeypatch):
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)


def test_onedrive_env_folder_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "Corp Docs"
    monkeypatch.setenv("OneDriveCommercial", str(root))
    assert "OneDrive folder" in synced_location(root / "q" / "invoices.json")
    with pytest.raises(ValueError, match="allow_synced=True"):
        WorkQueue("invoices", path=root / "invoices.json")
    assert not (root / "invoices.json").exists()  # refused before anything was written


@pytest.mark.parametrize("folder", ["OneDrive - Motherson Group", "OneDrive", "Dropbox", "Google Drive"])
def test_sync_client_folder_names_are_refused(tmp_path, folder):
    path = tmp_path / folder / "Reports" / "q.json"
    assert "cloud-synced" in synced_location(path)
    with pytest.raises(ValueError):
        WorkQueue("q", path=path)


@pytest.mark.skipif(os.name != "nt", reason="UNC paths exist on Windows only (on POSIX a backslash is a file name character)")
def test_unc_path_is_refused():
    assert "network share" in synced_location(r"\\fileserver\finance\q.json")
    assert "network share" in synced_location("//fileserver/finance/q.json")


def test_local_folder_is_fine(tmp_path):
    assert synced_location(tmp_path / "q.json") == ""
    q = WorkQueue("q", path=tmp_path / "q.json")
    q.add({"invoice": "4711"})
    assert (tmp_path / "q.json").exists()


def test_override(tmp_path):
    path = tmp_path / "Dropbox" / "q.json"
    q = WorkQueue("q", path=path, allow_synced=True)
    q.add({"invoice": "4711"})
    assert path.exists()


def test_open_passes_the_override(tmp_path):
    path = tmp_path / "Dropbox" / "q.json"
    with pytest.raises(ValueError):
        WorkQueue.open(path)
    WorkQueue.open(path, allow_synced=True)


def test_default_queue_dir_is_local(monkeypatch, tmp_path):
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert default_queue_dir() == tmp_path / "Local" / "locum" / "queues"
    else:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        assert default_queue_dir() == tmp_path / "state" / "locum" / "queues"
    assert synced_location(default_queue_dir()) == ""


def test_export_copies_state_into_a_synced_folder(tmp_path):
    live = tmp_path / "local" / "q.json"
    vault = tmp_path / "OneDrive - Org" / "Vault" / "q-2026-11-06.json"
    q = WorkQueue("invoices", path=live)
    q.add({"invoice": "4711"})
    q.add({"invoice": "4712"})
    other = WorkQueue.open(live)          # a second worker's change must be in the export
    it = other.next()
    other.complete(it, {"posted": "5100000001"})

    out = q.export(vault)
    assert out == str(vault)
    data = json.loads(vault.read_text(encoding="utf-8"))
    assert data["format"] == "locum.workqueue"
    assert sorted(i["status"] for i in data["items"]) == ["new", "success"]
    assert not vault.with_name(vault.name + ".lock").exists()  # no lock sidecar in the vault
    # an exported file can be read back (explicitly, since it is in a synced folder)
    copy = WorkQueue.open(vault, allow_synced=True)
    assert len(copy.items) == 2
