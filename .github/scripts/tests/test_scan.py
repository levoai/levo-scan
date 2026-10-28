import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scan  # noqa: E402


def entry(i):
    return {"repo": f"acme/r{i}", "owner": "acme", "name": f"r{i}", "id": i, "language": "python",
            "app_name": f"r{i}", "env_name": "staging", "timeout": 1800}


def run(n, fail=None):
    """Runs scan.main over n repositories with the network, git and docker
    replaced; `fail` maps a repository id to how its scan goes wrong."""
    fail = fail or {}
    seen = {"passes": [], "revoked": []}

    def levo_pass(repository_id, levo_api):
        seen["passes"].append(repository_id)
        if fail.get(repository_id) == "refused":
            raise scan.Failed("Levo refused a read pass: no")
        return f"token{repository_id}"

    def fake_scan(e, token, product, levo_api, control_repo, job):
        how = fail.get(e["id"])
        if how == "unsafe":
            raise scan.Unsafe("the clone token can reach the control repository; it is too broad")
        if how == "failed":
            raise scan.Failed("the scanner exited with code 3 | see log")
        if how == "bug":
            raise KeyError("oops")

    with tempfile.TemporaryDirectory() as d:
        env = {"REPOS": json.dumps([entry(i) for i in range(1, n + 1)]), "JOB": "7",
               "GITHUB_WORKSPACE": d, "RUNNER_TEMP": d, "LEVO_BASE_URL": "https://levo.invalid",
               "CONTROL_REPO": "acme/_levoai", "APP_ID": "",
               "GITHUB_OUTPUT": os.path.join(d, "out"), "GITHUB_STEP_SUMMARY": os.path.join(d, "summary")}
        log = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(scan, "levo_pass", levo_pass), \
                mock.patch.object(scan, "scan", fake_scan), mock.patch.object(scan, "summarise", lambda *a: 5), \
                mock.patch.object(scan, "revoke", seen["revoked"].append), contextlib.redirect_stdout(log):
            code = scan.main()
        with open(env["GITHUB_OUTPUT"]) as fh:
            outputs = dict(line.split("=", 1) for line in fh.read().splitlines())
        with open(env["GITHUB_STEP_SUMMARY"]) as fh:
            summary = fh.read()
    return code, log.getvalue(), summary, outputs, seen


class Loop(unittest.TestCase):
    def test_all_scanned(self):
        code, log, summary, _, seen = run(3)
        self.assertEqual(code, 0)
        self.assertIn("3 of 3 repositories scanned", log)
        self.assertEqual(seen["revoked"], ["token1", "token2", "token3"])
        self.assertEqual(summary.count("| acme/r"), 3)

    def test_a_failing_repository_does_not_stop_the_others(self):
        code, log, summary, _, seen = run(3, {2: "failed"})
        self.assertEqual(code, 1)
        self.assertEqual(seen["passes"], [1, 2, 3])
        self.assertEqual(seen["revoked"], ["token1", "token2", "token3"])
        self.assertIn("2 of 3 repositories scanned", log)
        self.assertIn("code 3 \\| see log", summary)

    def test_an_unexpected_error_fails_only_that_repository(self):
        code, log, _, _, seen = run(3, {2: "bug"})
        self.assertEqual(code, 1)
        self.assertEqual(seen["passes"], [1, 2, 3])
        self.assertIn("2 of 3 repositories scanned", log)

    def test_a_refused_pass_skips_only_that_repository(self):
        code, _, _, _, seen = run(3, {2: "refused"})
        self.assertEqual(code, 1)
        self.assertEqual(seen["revoked"], ["token1", "token3"])

    def test_a_too_broad_token_stops_the_job(self):
        code, _, summary, _, seen = run(3, {1: "unsafe"})
        self.assertEqual(code, 1)
        self.assertEqual(seen["passes"], [1])
        self.assertEqual(seen["revoked"], ["token1"])
        self.assertEqual(summary.count("not scanned:"), 2)

    def test_one_repository_keeps_its_artifact_name(self):
        _, log, _, outputs, _ = run(1)
        self.assertEqual(outputs, {"artifact": "spec-r1", "path": "specs/r1"})
        self.assertNotIn("::group::", log)

    def test_a_group_shares_one_artifact(self):
        _, log, _, outputs, _ = run(2)
        self.assertEqual(outputs, {"artifact": "specs-7", "path": "specs"})
        self.assertIn("::group::1/2 acme/r1 (python)", log)


class Pass(unittest.TestCase):
    def ask(self, responses):
        responses, waits = list(responses), []

        def call(method, url, headers, body=None):
            if "oidc" in url:
                return 200, json.dumps({"value": "card"}), {}
            return responses.pop(0)

        env = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.invalid/?api-version=2.0",
               "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t"}
        with mock.patch.dict(os.environ, env), mock.patch.object(scan, "call", call), \
                mock.patch.object(scan.time, "sleep", waits.append), contextlib.redirect_stdout(io.StringIO()):
            try:
                return scan.levo_pass(42, "https://levo.invalid"), waits
            except scan.Failed as exc:
                return f"failed: {exc}", waits

    granted = (200, json.dumps({"token": "tok", "permissions": {}, "repositories": [], "expires_at": "x"}), {})

    def test_waits_as_levo_asks_then_retries(self):
        self.assertEqual(self.ask([(429, "{}", {"Retry-After": "60"}), self.granted]), ("tok", [60]))

    def test_an_unreadable_retry_after_waits_30_seconds(self):
        self.assertEqual(self.ask([(503, "{}", {"Retry-After": "soon"}), self.granted]), ("tok", [30]))

    def test_gives_up_after_six_tries(self):
        result, waits = self.ask([(429, "{}", {"Retry-After": "999"})] * 6)
        self.assertTrue(result.startswith("failed: Levo refused a read pass"))
        self.assertEqual(waits, [120] * 5)

    def test_a_refusal_is_not_retried(self):
        result, waits = self.ask([(403, '{"error":"not linked"}', {})])
        self.assertIn("not linked", result)
        self.assertEqual(waits, [])


class Clone(unittest.TestCase):
    def scan(self, git_codes):
        """Runs scan.scan with git exiting with each of git_codes in turn."""
        codes, commands, waits = list(git_codes), [], []

        def run(cmd, **kwargs):
            commands.append(cmd[0])
            code = codes.pop(0) if cmd[0] == "git" else 0
            return subprocess.CompletedProcess(cmd, code, "", "error: RPC failed; HTTP 404")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(scan, "call", lambda *a, **k: (404, "", {})), \
                mock.patch.object(scan.subprocess, "run", run), \
                mock.patch.object(scan.time, "sleep", waits.append), \
                mock.patch.dict(os.environ, {"LEVO_AUTH_KEY": ""}), contextlib.redirect_stdout(io.StringIO()):
            try:
                scan.scan(entry(1), "token", os.path.join(d, "product"), "https://levo.invalid", "acme/_levoai", "1")
                return "scanned", commands, waits
            except scan.Failed as exc:
                return f"failed: {exc}", commands, waits

    def test_a_clone_refused_once_is_retried(self):
        self.assertEqual(self.scan([128, 0]), ("scanned", ["git", "git", "docker"], [10]))

    def test_a_clone_refused_for_a_minute_still_succeeds(self):
        self.assertEqual(self.scan([128, 128, 128, 0]), ("scanned", ["git"] * 4 + ["docker"], [10, 20, 30]))

    def test_gives_up_after_two_minutes_of_refusals(self):
        result, commands, waits = self.scan([128] * 6)
        self.assertTrue(result.startswith("failed: clone failed"))
        self.assertEqual((commands, waits), (["git"] * 6, [10, 20, 30, 30, 30]))


class OwnScanApp(unittest.TestCase):
    def test_signs_a_jwt_the_public_key_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            key, pub = os.path.join(d, "key.pem"), os.path.join(d, "pub.pem")
            subprocess.run(["openssl", "genrsa", "-out", key, "2048"], check=True, capture_output=True)
            subprocess.run(["openssl", "rsa", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
            jwt = scan.app_jwt("12345", key)
            header, payload, signature = jwt.split(".")
            pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
            self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(payload)))["iss"], "12345")
            sig = os.path.join(d, "sig")
            with open(sig, "wb") as fh:
                fh.write(base64.urlsafe_b64decode(pad(signature)))
            verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub, "-signature", sig],
                                      input=f"{header}.{payload}".encode(), capture_output=True)
            self.assertEqual(verified.returncode, 0)

    def test_an_invalid_key_fails_clearly(self):
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as bad:
            bad.write("not a key")
            bad.flush()
            with self.assertRaisesRegex(scan.Failed, "not a valid private key"):
                scan.app_jwt("12345", bad.name)


if __name__ == "__main__":
    unittest.main()
