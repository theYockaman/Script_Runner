"""Tests for multi-language execution, inline code, and the env-var fix.

Unit tests patch SCRIPTS_DIR to a temp dir and need no interpreters. Runtime
smoke tests skip when an interpreter is missing, so the suite passes on a plain
checkout and fully exercises every language inside the backend image.
"""
import glob
import os
import shutil
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app import main as app_main


CLIENT = TestClient(app_main.app)
assert CLIENT.post("/api/auth/login", json={"username": "tester", "password": "testpass"}).status_code == 200


def poll_for_runs(script_id: int, timeout: int = 15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = CLIENT.get(f"/api/scripts/{script_id}/logs")
        if r.status_code == 200 and r.json() and r.json()[0]["finished_at"]:
            return r.json()
        time.sleep(0.25)
    raise AssertionError("No finished run appeared within timeout")


class FakeScript:
    """Minimal stand-in for the ORM row, for the pure resolution helpers."""

    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.command = kw.get("command", "")
        self.code = kw.get("code")
        self.language = kw.get("language")


def create(payload):
    r = CLIENT.post("/api/scripts", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def delete(script_id):
    CLIENT.delete(f"/api/scripts/{script_id}")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_parse_env_text():
    text = (
        "FOO=bar\n"
        "\n"
        "# a comment\n"
        "export BAZ='qu ux'\n"
        "QUOTED=\"x=y\"\n"
        "NOEQUALS\n"
        "  SPACED = v  \n"
    )
    assert app_main.parse_env_text(text) == {"FOO": "bar", "BAZ": "qu ux", "QUOTED": "x=y", "SPACED": "v"}
    assert app_main.parse_env_text(None) == {}
    assert app_main.parse_env_text("") == {}


def test_normalize_newlines():
    assert app_main.normalize_newlines("a\r\nb\rc") == "a\nb\nc\n"
    assert app_main.normalize_newlines("a\n") == "a\n"
    assert app_main.normalize_newlines("") == ""


@pytest.mark.parametrize("lang,spec", list(app_main.LANGUAGES.items()))
def test_detect_language_each_extension(lang, spec):
    for ext in spec["extensions"]:
        assert app_main.detect_language(f"/app/scripts/job{ext}") == lang
        assert app_main.detect_language(f"job{ext.upper()}") == lang


def test_detect_language_unknown():
    assert app_main.detect_language("notes.txt") is None
    assert app_main.detect_language("echo") is None
    assert app_main.detect_language("/app/scripts/bin/tool") is None


def test_resolve_file_auto_detects_python(tmp_path, monkeypatch):
    monkeypatch.setattr(app_main, "SCRIPTS_DIR", str(tmp_path))
    (tmp_path / "foo.py").write_text("print(1)\n")
    r = app_main.resolve_command(FakeScript(command="foo.py --flag 'two words'"), run_id=1)
    assert r.error is None
    assert r.shell is False
    assert r.lang == "python"
    assert r.args == ["python", "-u", str(tmp_path / "foo.py"), "--flag", "two words"]
    assert r.cwd == str(tmp_path)
    assert r.cleanup_path is None


def test_resolve_bash_file_without_exec_bit(tmp_path, monkeypatch):
    monkeypatch.setattr(app_main, "SCRIPTS_DIR", str(tmp_path))
    (tmp_path / "x.sh").write_text("echo hi\r\n")  # CRLF on purpose
    r = app_main.resolve_command(FakeScript(command="x.sh"), run_id=1)
    assert r.args == ["bash", str(tmp_path / "x.sh")]
    assert (tmp_path / "x.sh").read_bytes() == b"echo hi\n"  # line endings converted


def test_resolve_unknown_extension_falls_back_to_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(app_main, "SCRIPTS_DIR", str(tmp_path))
    r = app_main.resolve_command(FakeScript(command="echo hi"), run_id=1)
    assert r.error is None
    assert r.shell is True
    assert r.args == "echo hi"
    assert r.cwd == str(tmp_path)
    assert r.lang is None


def test_resolve_explicit_language_missing_file_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(app_main, "SCRIPTS_DIR", str(tmp_path))
    r = app_main.resolve_command(FakeScript(command="echo hi", language="bash"), run_id=1)
    assert r.error is not None
    assert r.error[0] == 2
    assert "not found" in r.error[1]


def test_resolve_inline_requires_language(tmp_path, monkeypatch):
    monkeypatch.setattr(app_main, "SCRIPTS_DIR", str(tmp_path))
    r = app_main.resolve_command(FakeScript(code="print(1)"), run_id=1)
    assert r.error is not None and r.error[0] == 2


def test_write_inline_normalizes_crlf_and_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(app_main, "SCRIPTS_DIR", str(tmp_path))
    script = FakeScript(id=7, code="Write-Output 'a'\r\nWrite-Output 'b'", language="powershell")
    path = app_main.write_inline_script(script, run_id=3)
    assert path == os.path.join(str(tmp_path), ".inline", "script_7_run_3.ps1")
    with open(path, "rb") as f:
        assert f.read() == b"Write-Output 'a'\nWrite-Output 'b'\n"
    app_main.cleanup_inline_files(7)
    assert not os.path.exists(path)


@pytest.mark.parametrize("command,code,language", [
    (None, None, None),                 # neither
    ("", "   ", "python"),              # whitespace-only code counts as nothing
    ("x.py", "print(1)", "python"),     # both
    (None, "print(1)", None),           # inline without language
    (None, "print(1)", "auto"),         # inline with auto
    ("x.py", None, "cobol"),            # unknown language
])
def test_validate_script_fields_rejects(command, code, language):
    with pytest.raises(ValueError):
        app_main.validate_script_fields(command, code, language)


def test_validate_script_fields_normalises():
    assert app_main.validate_script_fields(" x.py ", None, "auto") == ("x.py", None, None)
    assert app_main.validate_script_fields("x.py", "", "") == ("x.py", None, None)
    assert app_main.validate_script_fields(None, "a\r\nb", "Python") == ("", "a\nb\n", "python")


def test_ensure_schema_adds_missing_columns(tmp_path):
    db_file = tmp_path / "old.db"
    con = sqlite3.connect(str(db_file))
    con.execute(
        "CREATE TABLE scripts (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, "
        "command TEXT NOT NULL, schedule VARCHAR(100), enabled BOOLEAN, env TEXT, "
        "created_at DATETIME, updated_at DATETIME)"
    )
    con.execute("INSERT INTO scripts (name, command, enabled) VALUES ('old', 'echo hi', 1)")
    con.commit()
    con.close()

    eng = create_engine(f"sqlite:///{db_file.as_posix()}")
    assert set(app_main.ensure_schema(eng)) == {"language", "code"}
    assert app_main.ensure_schema(eng) == []  # idempotent

    con = sqlite3.connect(str(db_file))
    cols = {row[1] for row in con.execute("PRAGMA table_info(scripts)")}
    assert {"language", "code"} <= cols
    assert con.execute("SELECT language, code FROM scripts").fetchone() == (None, None)
    con.close()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_languages_endpoint():
    r = CLIENT.get("/api/languages")
    assert r.status_code == 200
    langs = {l["id"]: l for l in r.json()}
    assert set(langs) == set(app_main.LANGUAGES)
    assert "auto" not in langs
    assert langs["python"]["available"] is True
    assert langs["bash"]["extensions"] == [".sh", ".bash"]
    assert langs["powershell"]["label"] == "PowerShell"


@pytest.mark.parametrize("payload", [
    {"name": "neither"},
    {"name": "both", "command": "x.py", "code": "print(1)", "language": "python"},
    {"name": "no-lang", "code": "print(1)"},
    {"name": "bad-lang", "command": "x.py", "language": "cobol"},
])
def test_create_validation_422(payload):
    r = CLIENT.post("/api/scripts", json=payload)
    assert r.status_code == 422, r.text


def test_update_validation_after_merge():
    sid = create({"name": "upd", "command": "echo hi"})
    try:
        # partial patches still work
        r = CLIENT.put(f"/api/scripts/{sid}", json={"enabled": False})
        assert r.status_code == 200 and r.json()["enabled"] is False

        # clearing the only source is rejected on the merged row
        r = CLIENT.put(f"/api/scripts/{sid}", json={"command": None})
        assert r.status_code == 422

        # switching to inline mode in one request works
        r = CLIENT.put(f"/api/scripts/{sid}", json={"command": None, "code": "print('x')", "language": "python"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["command"] == ""
        assert body["language"] == "python"
        assert body["code"] == "print('x')\n"

        # and back to file mode
        r = CLIENT.put(f"/api/scripts/{sid}", json={"command": "echo hi", "code": None, "language": None})
        assert r.status_code == 200, r.text
        assert r.json()["code"] is None and r.json()["language"] is None
    finally:
        delete(sid)


def test_scriptout_includes_env_language_code():
    sid = create({"name": "out", "code": "print(1)", "language": "python", "env": "A=1"})
    try:
        body = CLIENT.get(f"/api/scripts/{sid}").json()
        assert body["env"] == "A=1"
        assert body["language"] == "python"
        assert body["code"] == "print(1)\n"
        assert body["command"] == ""
        listed = {s["id"]: s for s in CLIENT.get("/api/scripts").json()}
        assert listed[sid]["code"] == "print(1)\n"
    finally:
        delete(sid)


def test_inline_python_runs_with_env_via_api():
    code = "import os\nprint('FOO=' + os.environ.get('FOO', 'missing'))\n"
    sid = create({"name": "inline-py", "code": code, "language": "python", "env": "FOO=bar\n# ignored\nexport BAZ=1"})
    try:
        r = CLIENT.post(f"/api/scripts/{sid}/run")
        assert r.status_code == 200
        runs = poll_for_runs(sid)
        assert runs[0]["exit_code"] == 0, runs[0]
        assert "FOO=bar" in runs[0]["stdout"]
        # per-run temp file is removed afterwards
        assert glob.glob(os.path.join(app_main.inline_dir(), f"script_{sid}_*")) == []
    finally:
        delete(sid)


def test_file_mode_python_receives_env(tmp_path):
    scripts_dir = app_main.SCRIPTS_DIR
    os.makedirs(scripts_dir, exist_ok=True)
    path = os.path.join(scripts_dir, "test_env_echo.py")
    with open(path, "w", newline="\n") as f:
        f.write("import os\nprint('TOKEN=' + os.environ.get('TOKEN', 'missing'))\n")
    sid = create({"name": "env-file", "command": "test_env_echo.py", "env": "TOKEN=\"s3cret\""})
    try:
        app_main.run_script(sid)
        runs = CLIENT.get(f"/api/scripts/{sid}/logs").json()
        assert runs[0]["exit_code"] == 0, runs[0]
        assert "TOKEN=s3cret" in runs[0]["stdout"]
    finally:
        delete(sid)
        try:
            os.remove(path)
        except OSError:
            pass


def test_missing_interpreter_records_127(monkeypatch):
    monkeypatch.setitem(app_main.LANGUAGES, "fake", {
        "label": "Fake", "extensions": [".fake"], "argv": ["definitely-not-an-installed-interpreter"],
    })
    sid = create({"name": "fake", "code": "whatever", "language": "fake"})
    try:
        app_main.run_script(sid)  # synchronous so the monkeypatch is still active
        runs = CLIENT.get(f"/api/scripts/{sid}/logs").json()
        assert runs[0]["exit_code"] == 127
        assert "not installed" in runs[0]["stderr"]
    finally:
        delete(sid)


def test_explicit_language_missing_file_records_exit_2():
    sid = create({"name": "nofile", "command": "does-not-exist.rb", "language": "ruby"})
    try:
        app_main.run_script(sid)
        runs = CLIENT.get(f"/api/scripts/{sid}/logs").json()
        assert runs[0]["exit_code"] == 2
        assert "not found" in runs[0]["stderr"]
    finally:
        delete(sid)


def test_delete_removes_inline_files():
    sid = create({"name": "del", "code": "print(1)", "language": "python"})
    os.makedirs(app_main.inline_dir(), exist_ok=True)
    stray = os.path.join(app_main.inline_dir(), f"script_{sid}_run_1.py")
    with open(stray, "w") as f:
        f.write("print(1)\n")
    assert CLIENT.delete(f"/api/scripts/{sid}").status_code == 200
    assert not os.path.exists(stray)


# ---------------------------------------------------------------------------
# Runtime smoke tests (one per language; skipped when the interpreter is absent)
# ---------------------------------------------------------------------------

HELLO = {
    "bash": "echo hello-from-bash",
    "python": "print('hello-from-python')",
    "node": "console.log('hello-from-node')",
    "typescript": "const msg: string = 'hello-from-typescript';\nconsole.log(msg);",
    "powershell": "Write-Output 'hello-from-powershell'",
    "ruby": "puts 'hello-from-ruby'",
    "perl": 'print "hello-from-perl\\n";',
    "php": '<?php echo "hello-from-php\\n";',
}


@pytest.mark.parametrize("lang", list(HELLO))
def test_inline_runtime_smoke(lang):
    exe = app_main.LANGUAGES[lang]["argv"][0]
    if shutil.which(exe) is None:
        pytest.skip(f"{exe} is not installed")
    sid = create({"name": f"smoke-{lang}", "code": HELLO[lang] + "\r\n", "language": lang})
    try:
        app_main.run_script(sid)
        runs = CLIENT.get(f"/api/scripts/{sid}/logs").json()
        assert runs[0]["exit_code"] == 0, runs[0]
        assert f"hello-from-{lang}" in runs[0]["stdout"]
    finally:
        delete(sid)


@pytest.mark.parametrize("lang,ext,body", [
    ("bash", ".sh", "exit 3"),
    ("python", ".py", "import sys\nsys.exit(3)"),
    ("node", ".js", "process.exit(3)"),
    ("powershell", ".ps1", "exit 3"),
    ("ruby", ".rb", "exit 3"),
    ("perl", ".pl", "exit 3;"),
    ("php", ".php", "<?php exit(3);"),
])
def test_exit_codes_propagate(lang, ext, body):
    exe = app_main.LANGUAGES[lang]["argv"][0]
    if shutil.which(exe) is None:
        pytest.skip(f"{exe} is not installed")
    sid = create({"name": f"exit-{lang}", "code": body, "language": lang})
    try:
        app_main.run_script(sid)
        runs = CLIENT.get(f"/api/scripts/{sid}/logs").json()
        assert runs[0]["exit_code"] == 3, runs[0]
    finally:
        delete(sid)
