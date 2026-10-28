import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import plan  # noqa: E402

CONTROL = "acme/_levoai"


def repos(n, archived=0):
    return [{"full_name": f"acme/r{i:04d}", "name": f"r{i:04d}", "id": 1000 + i,
             "owner": {"login": "acme"}, "archived": i < archived} for i in range(n)]


def run(granted, langs=None, config="version: 1\n", max_jobs=""):
    """Runs plan.main against a fake GitHub; returns (exit code, log, outputs)."""
    langs = langs or {}
    granted = granted + [{"full_name": CONTROL, "name": "_levoai", "id": 1, "owner": {"login": "acme"}}]

    def api(path):
        if path.startswith("/installation/repositories"):
            page = int(path.rsplit("=", 1)[1])
            return {"repositories": granted[(page - 1) * 100:page * 100]}
        value = langs.get(path[len("/repos/"):-len("/languages")], {"Python": 100})
        if isinstance(value, int):
            raise urllib.error.HTTPError(path, value, "error", {}, None)
        return value

    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "levo-config.yml"), "w") as fh:
            fh.write(config)
        out = os.path.join(d, "out")
        env = {"CONTROL_REPO": CONTROL, "GITHUB_OUTPUT": out, "MAX_JOBS": max_jobs}
        log, code, cwd = io.StringIO(), 0, os.getcwd()
        os.chdir(d)
        try:
            with mock.patch.object(plan, "api", api), mock.patch.dict(os.environ, env), \
                    contextlib.redirect_stdout(log):
                plan.main()
        except SystemExit as exc:
            code = exc.code
        finally:
            os.chdir(cwd)
        outputs = {}
        if os.path.exists(out):
            with open(out) as fh:
                outputs = dict(line.split("=", 1) for line in fh.read().splitlines())
    return code, log.getvalue(), outputs


def groups(outputs):
    return json.loads(outputs["groups"])[1:]


class Qualify(unittest.TestCase):
    def test_skips_with_reasons_and_picks_the_largest_supported_language(self):
        granted = repos(7, archived=1)
        langs = {"acme/r0001": {"C": 900, "Shell": 10},
                 "acme/r0002": {},
                 "acme/r0003": 403,
                 "acme/r0004": {"HTML": 9000, "TypeScript": 50},
                 "acme/r0005": {"C#": 10}}
        config = "version: 1\nexclude:\n  - acme/r0006\n"
        code, log, out = run(granted, langs, config)
        self.assertEqual(code, 0)
        scanned = [(e["repo"], e["language"]) for g in groups(out) for e in g]
        self.assertEqual(scanned, [("acme/r0004", "typescript"), ("acme/r0005", "csharp")])
        for reason in ("archived", "language 'c' is not supported", "GitHub reports no language",
                       "could not read languages (403)", "excluded in levo-config.yml"):
            self.assertIn(reason, log)
        self.assertNotIn(CONTROL, [e["repo"] for g in groups(out) for e in g])

    def test_nothing_to_scan_stops(self):
        code, log, out = run(repos(2), {"acme/r0000": {"C": 1}, "acme/r0001": {"C": 1}})
        self.assertEqual(code, 1)
        self.assertIn("Nothing to scan", log)
        self.assertEqual(out, {})


class Group(unittest.TestCase):
    def test_one_repository_per_job_up_to_256(self):
        code, log, out = run(repos(256))
        self.assertEqual(code, 0)
        self.assertEqual([len(g) for g in groups(out)], [1] * 256)
        matrix = json.loads(out["matrix"])["include"]
        self.assertEqual([m["job"] for m in matrix], list(range(1, 257)))
        self.assertEqual(matrix[0]["repositories"], "acme/r0000")
        self.assertEqual(json.loads(out["groups"])[0], [])
        self.assertEqual(out["job_timeout"], "55")
        self.assertNotIn("More than", log)

    def test_more_than_256_share_jobs_in_order(self):
        code, log, out = run(repos(300))
        self.assertEqual(code, 0)
        self.assertEqual(len(groups(out)), 150)
        self.assertTrue(all(len(g) == 2 for g in groups(out)))
        flat = [e["repo"] for g in groups(out) for e in g]
        self.assertEqual(flat, [r["full_name"] for r in repos(300)])
        self.assertEqual(json.loads(out["matrix"])["include"][0]["repositories"], "acme/r0000 +1")
        self.assertEqual(out["job_timeout"], "76")
        self.assertIn("each job scans up to 2, one after another: 150 jobs", log)

    def test_2000_fit_in_256_jobs(self):
        code, _, out = run(repos(2000))
        self.assertEqual(code, 0)
        self.assertLessEqual(len(groups(out)), 256)
        self.assertEqual(max(len(g) for g in groups(out)), 8)
        self.assertEqual(sum(len(g) for g in groups(out)), 2000)

    def test_too_many_for_the_scan_timeout_stops(self):
        code, log, out = run(repos(2000), config="version: 1\ndefaults:\n  scan_timeout: 2400\n")
        self.assertEqual(code, 1)
        self.assertIn("one run can scan at most 1792", log)
        self.assertEqual(out, {})

    def test_max_jobs_variable_forces_groups(self):
        code, _, out = run(repos(7), max_jobs="2")
        self.assertEqual(code, 0)
        self.assertEqual([len(g) for g in groups(out)], [4, 3])

    def test_max_jobs_variable_must_be_a_number(self):
        code, log, _ = run(repos(3), max_jobs="abc")
        self.assertEqual(code, 1)
        self.assertIn("must be a number", log)


if __name__ == "__main__":
    unittest.main()
