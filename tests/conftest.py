"""Test environment for the auth layer.

pytest imports this before any test module, so these variables are in place when
`app.main` is imported. Plain assignment (not setdefault): the real `.env` also
reaches `docker compose run`, and the tests must not depend on the operator's users.
COOKIE_SECURE must be false because the test client's cookie jar withholds Secure
cookies over http://testserver, which would turn every protected call into a 401.
"""
import os

os.environ["SCRIPT_RUNNER_USERS"] = "tester:testpass"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["COOKIE_SECURE"] = "false"
os.environ.pop("SESSION_MAX_AGE_HOURS", None)
