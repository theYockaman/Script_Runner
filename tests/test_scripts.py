import os
import time
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from app import main as app_main


CLIENT = TestClient(app_main.app)
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"


def ensure_scripts_dir():
    SCRIPTS_DIR.mkdir(exist_ok=True)


def make_executable(p: Path):
    mode = p.stat().st_mode
    p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def poll_for_runs(script_id: int, timeout: int = 10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = CLIENT.get(f"/api/scripts/{script_id}/logs")
        if r.status_code == 200 and len(r.json()) > 0:
            return r.json()
        time.sleep(0.5)
    raise AssertionError("No runs appeared within timeout")


def test_run_various_scripts_and_retries():
    ensure_scripts_dir()

    # create a simple bash script
    bash_script = SCRIPTS_DIR / "test_echo.sh"
    bash_script.write_text("""#!/usr/bin/env bash\necho hello from bash\nexit 0\n""")
    make_executable(bash_script)

    # create a python script
    py_script = SCRIPTS_DIR / "test_py.py"
    py_script.write_text("""#!/usr/bin/env python3\nprint('hello from python')\n""")
    make_executable(py_script)

    # create a failing script
    fail_script = SCRIPTS_DIR / "test_fail.sh"
    fail_script.write_text("""#!/usr/bin/env bash\necho failing; exit 2\n""")
    make_executable(fail_script)

    # create entries via API
    for cmd, name in [("/app/scripts/test_echo.sh", "bash-echo"), ("/app/scripts/test_py.py", "py-echo"), ("/app/scripts/test_fail.sh", "fail")]:
        payload = {"name": name, "command": cmd, "schedule": None, "enabled": True}
        r = CLIENT.post("/api/scripts", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == name

    # fetch scripts to get IDs
    r = CLIENT.get("/api/scripts")
    assert r.status_code == 200
    scripts = {s['name']: s for s in r.json()}

    # run each and assert outputs
    for name, expected_exit in [("bash-echo", 0), ("py-echo", 0), ("fail", 2)]:
        sid = scripts[name]['id']
        r = CLIENT.post(f"/api/scripts/{sid}/run")
        assert r.status_code == 200
        # poll for run
        runs = poll_for_runs(sid, timeout=15)
        assert len(runs) >= 1
        last = runs[0]
        # exit code check (fail may be non-zero)
        assert last['exit_code'] == expected_exit

    # test repeated runs for idempotency / multiple entries
    sid = scripts['bash-echo']['id']
    CLIENT.post(f"/api/scripts/{sid}/run")
    CLIENT.post(f"/api/scripts/{sid}/run")
    runs = poll_for_runs(sid, timeout=15)
    # expect at least 2 runs total
    assert len(runs) >= 2


def test_frontend_bundle_present():
    root = Path(__file__).resolve().parents[1]
    index = root / "frontend" / "index.html"
    assert index.exists()
    content = index.read_text()
    assert "Script Runner" in content


def test_cron_scheduling_and_disable():
    """
    Tests that a script can be scheduled, runs automatically,
    can be disabled, and then deleted.
    """
    ensure_scripts_dir()

    # Create a script that touches a file
    marker_path = SCRIPTS_DIR / "cron_marker.txt"
    if marker_path.exists():
        marker_path.unlink()

    cron_script_path = SCRIPTS_DIR / "test_cron.sh"
    cron_script_path.write_text(f"""#!/usr/bin/env bash
date >> {marker_path.as_posix()}
""")
    make_executable(cron_script_path)

    # Create a script scheduled to run every second
    payload = {
        "name": "cron-test",
        "command": f"/app/scripts/{cron_script_path.name}",
        "schedule": "* * * * * *",  # Every second
        "enabled": True
    }
    r = CLIENT.post("/api/scripts", json=payload)
    assert r.status_code == 200
    script_data = r.json()
    script_id = script_data["id"]

    # Wait for a few seconds to let the scheduler run it
    time.sleep(3)

    # Check that the script has run multiple times by polling logs
    runs = poll_for_runs(script_id, timeout=5)
    assert len(runs) >= 2, "Script should have run at least twice on schedule"
    assert all(run['exit_code'] == 0 for run in runs)

    # Disable the script
    r = CLIENT.put(f"/api/scripts/{script_id}", json={"enabled": False})
    assert r.status_code == 200
    assert not r.json()["enabled"]

    # Get current run count
    r = CLIENT.get(f"/api/scripts/{script_id}/logs")
    assert r.status_code == 200
    run_count_after_disable = len(r.json())

    # Wait again and ensure no new runs have occurred
    time.sleep(3)

    r = CLIENT.get(f"/api/scripts/{script_id}/logs")
    assert r.status_code == 200
    assert len(r.json()) == run_count_after_disable, "Script should not run when disabled"

    # Delete the script
    r = CLIENT.delete(f"/api/scripts/{script_id}")
    assert r.status_code == 200

    # Verify it's gone
    r = CLIENT.get(f"/api/scripts/{script_id}")
    assert r.status_code == 404


def test_api_validation():
    """Tests API input validation."""
    # Invalid schedule
    payload = {"name": "invalid-cron", "command": "echo", "schedule": "not a cron", "enabled": True}
    r = CLIENT.post("/api/scripts", json=payload)
    assert r.status_code == 422  # Unprocessable Entity

    # Non-existent script
    r = CLIENT.post("/api/scripts/99999/run")
    assert r.status_code == 404

    # Update non-existent script
    r = CLIENT.put("/api/scripts/99999", json={"enabled": False})
    assert r.status_code == 404


def test_run_python_and_bash():
    """
    Tests that both a python script and a bash script can be executed.
    """
    ensure_scripts_dir()

    # Create a bash script
    bash_script_path = SCRIPTS_DIR / "test_bash.sh"
    bash_script_path.write_text("#!/bin/bash\necho 'hello from bash'")
    make_executable(bash_script_path)

    # Create a python script
    python_script_path = SCRIPTS_DIR / "test_python.py"
    python_script_path.write_text("print('hello from python')")
    make_executable(python_script_path)

    # Add scripts to the db
    bash_payload = {
        "name": "bash-test-script",
        "command": str(bash_script_path.relative_to(ROOT)),
        "enabled": True
    }
    python_payload = {
        "name": "python-test-script",
        "command": str(python_script_path.relative_to(ROOT)),
        "enabled": True
    }

    bash_response = CLIENT.post("/api/scripts", json=bash_payload)
    assert bash_response.status_code == 200
    bash_script_id = bash_response.json()["id"]

    python_response = CLIENT.post("/api/scripts", json=python_payload)
    assert python_response.status_code == 200
    python_script_id = python_response.json()["id"]

    # Run scripts
    CLIENT.post(f"/api/scripts/{bash_script_id}/run")
    CLIENT.post(f"/api/scripts/{python_script_id}/run")

    # Verify runs
    bash_runs = poll_for_runs(bash_script_id, timeout=15)
    assert len(bash_runs) >= 1
    assert bash_runs[0]['exit_code'] == 0
    assert 'hello from bash' in bash_runs[0]['stdout']

    python_runs = poll_for_runs(python_script_id, timeout=15)
    assert len(python_runs) >= 1
    assert python_runs[0]['exit_code'] == 0
    assert 'hello from python' in python_runs[0]['stdout']
