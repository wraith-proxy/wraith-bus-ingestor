"""Event-triggered build (TJP-14114 #53): marker consumed before the build, the
old timer and the path unit never both enabled, one marker path everywhere,
alert rules re-pointed so waiting does not page."""

import re
from pathlib import Path

import yaml

OPS = Path(__file__).resolve().parent
SYSTEMD = OPS / "systemd"
MARKER = "/home/thomaspeoples/pjt-nas/nas/compose-data/wraith-bus-ingestor/dbt/data/.extract_ready"
INSTALL = OPS / "install_event_trigger.sh"
RULES = OPS / "grafana/provisioning/alerting/rules.yaml"


def lines(unit, key):
    text = (SYSTEMD / unit).read_text()
    return [ln.split("=", 1)[1].strip() for ln in text.splitlines() if ln.startswith(key + "=")]


def test_marker_consumed_before_build():
    pre = lines("wraith-bus-build.service", "ExecStartPre")
    assert pre, "no ExecStartPre"
    assert pre[0].lstrip("-").split()[1:] == ["-f", MARKER] or MARKER in pre[0]
    assert pre[0].lstrip("-").endswith(MARKER) and "rm" in pre[0]
    assert not pre[0].startswith("-"), "consume must not be skippable"


def test_one_marker_path_across_units():
    assert lines("wraith-bus-extract.path", "PathExists") == [MARKER]
    assert lines("wraith-bus-extract.path", "Unit") == ["wraith-bus-build.service"]
    ex = " ".join(lines("wraith-bus-extract.service", "ExecStart"))
    assert ex.count(MARKER) == 1 and "extract.py" in ex and "ready_gate.py" in ex
    assert lines("wraith-bus-extract.service", "ExecStart")[0].endswith("extract.py")


def test_build_keeps_limits_and_hooks():
    assert lines("wraith-bus-build.service", "MemoryMax") == ["13000M"]
    assert any("record_build_failure" in x for x in lines("wraith-bus-build.service", "ExecStopPost"))
    assert lines("wraith-bus-build.service", "Environment") == ["SKIP_EXTRACT=1"]


def test_extract_on_old_schedule():
    assert "03:00:00" in (SYSTEMD / "wraith-bus-extract.timer").read_text()


def test_timer_and_path_unit_not_both_enabled():
    sh = INSTALL.read_text()
    enabled = [ln for ln in sh.splitlines() if re.search(r"systemctl\s+enable", ln)]
    disabled = [ln for ln in sh.splitlines() if re.search(r"systemctl\s+disable", ln)]
    assert any("wraith-bus-extract.path" in ln for ln in enabled)
    assert not any("wraith-bus-build.timer" in ln for ln in enabled)
    assert any("wraith-bus-build.timer" in ln for ln in disabled)
    ordered = sh.index("disable --now wraith-bus-build.timer") < sh.index("enable --now wraith-bus-extract.path")
    assert ordered, "old timer must be disabled before the path unit is enabled"


def test_install_script_stage_echoes_and_log():
    sh = INSTALL.read_text()
    assert sh.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in sh and "tee" in sh and ".log" in sh
    assert len(re.findall(r'^stage ', sh, re.M)) >= 4


def test_alerts_waiting_does_not_page():
    rules = yaml.safe_load(RULES.read_text())["groups"][0]["rules"]
    by = {r["uid"]: r for r in rules}
    waiting = by["wraith-extract-waiting"]
    assert waiting["labels"]["severity"] == "waiting"
    assert "waiting" in waiting["title"]
    assert by["wraith-nightly-build"]["labels"]["severity"] != "critical"
    assert by["wraith-marts-state"]["labels"]["severity"] == "critical"
    assert by["wraith-nightly-build"]["noDataState"] != "Alerting"
