"""copy_and_finalize must never jam on an archive that already exists."""
import archive
import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    table_dir = tmp_path / "db" / "bus_positions_v2~17"
    table_dir.mkdir(parents=True)
    arch = tmp_path / "arch"
    (arch / "bus_positions").mkdir(parents=True)
    monkeypatch.setattr(archive, "table_dir", lambda: str(table_dir))
    monkeypatch.setattr(archive, "ARCHIVE_ROOT", str(arch))

    def make(root, body):
        root.mkdir()
        (root / "lat.d").write_bytes(body)
        return root

    local = make(table_dir / "2026-09-08.detached", b"x" * 100)
    return local, arch / "bus_positions" / "2026-09-08", make


def test_copies_when_not_yet_archived(env):
    local, dest, _ = env
    assert archive.copy_and_finalize("2026-09-08") is True
    assert (dest / "lat.d").read_bytes() == b"x" * 100
    assert not local.exists()


def test_identical_archive_is_skipped_and_local_freed(env):
    local, dest, make = env
    make(dest, b"y" * 100)  # same size, distinguishable content
    assert archive.copy_and_finalize("2026-09-08") is True
    assert (dest / "lat.d").read_bytes() == b"y" * 100  # never overwritten
    assert not local.exists()


def test_differing_archive_is_skipped_but_local_kept(env):
    local, dest, make = env
    make(dest, b"y" * 40)  # e.g. a partial copy from a crashed run
    assert archive.copy_and_finalize("2026-09-08") is True  # run carries on
    assert (dest / "lat.d").read_bytes() == b"y" * 40  # never overwritten
    assert local.exists()  # the only complete copy survives
