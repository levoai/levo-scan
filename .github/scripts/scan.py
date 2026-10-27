"""Scans one job's repositories, one after another.

Each repository gets its own read-only token for that repository alone -- a
pass from Levo, or one minted from your own Scan App -- which is checked
against this control repository, used to clone, and revoked when its scan
ends. One repository failing does not stop the others.
"""
import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

IMAGE = "levoai/code-scanner:latest"
METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
# A path derived from a source filename is not an endpoint. Counting them
# inflates every downstream number, including billing.
JUNK = re.compile(r"/(py|java|go|js|ts|rb|cs|php)$")


class Failed(Exception):
    """This repository could not be scanned; the job carries on."""


class Unsafe(Failed):
    """A token reached beyond its repository; the job stops."""


def mask(secret):
    print(f"::add-mask::{secret}", flush=True)


def call(method, url, headers, body=None):
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode(errors="replace"), r.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace"), exc.headers


def github(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def levo_pass(repository_id, levo_api):
    """A read-only pass for one repository, issued by Levo only to this
    repository's workflow running in the levo-scan environment."""
    for attempt in range(6):
        status, body, _ = call(
            "GET", os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=" + levo_api,
            {"Authorization": "bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
        if status != 200:
            raise Failed(f"GitHub did not issue this job's identity token (HTTP {status})")
        status, body, headers = call(
            "POST", f"{levo_api}/github/passes/scan",
            {"X-Levo-Workflow-Token": json.loads(body)["value"]},
            {"repository_id": repository_id})
        try:
            granted = json.loads(body)
        except ValueError:
            granted = {}
        if granted.get("token"):
            mask(granted["token"])
            print(json.dumps({k: granted.get(k) for k in ("permissions", "repositories", "expires_at")}))
            return granted["token"]
        if status in (429, 503) and attempt < 5:
            try:
                wait = min(120, max(1, int(headers.get("Retry-After") or 30)))
            except ValueError:
                wait = 30
            print(f"Levo asked to retry in {wait}s (HTTP {status})", flush=True)
            time.sleep(wait)
            continue
        raise Failed(f"Levo refused a read pass: {body[:300]}")


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def app_jwt(app_id, key_file):
    now = int(time.time())
    signing = (b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + "."
               + b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode()))
    signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_file],
                            input=signing.encode(), capture_output=True)
    if signed.returncode != 0:
        raise Failed("APP_PRIVATE_KEY is not a valid private key")
    return signing + "." + b64url(signed.stdout)


def own_app_token(owner, name, app_id, key_file, installations):
    """A read-only token for one repository from your own Scan App."""
    jwt = app_jwt(app_id, key_file)
    if owner not in installations:
        status, body, _ = call("GET", f"https://api.github.com/repos/{owner}/{name}/installation", github(jwt))
        if status != 200:
            raise Failed(f"your Scan App is not installed on {owner}/{name} (HTTP {status})")
        installations[owner] = json.loads(body)["id"]
    status, body, _ = call(
        "POST", f"https://api.github.com/app/installations/{installations[owner]}/access_tokens",
        github(jwt), {"repositories": [name], "permissions": {"contents": "read"}})
    if status != 201:
        raise Failed(f"your Scan App could not issue a token for {owner}/{name} (HTTP {status}): {body[:200]}")
    token = json.loads(body)["token"]
    mask(token)
    return token


def revoke(token):
    try:
        status, _, _ = call("DELETE", "https://api.github.com/installation/token", github(token))
        print(f"revoke -> HTTP {status}")
    except Exception as exc:
        print(f"::warning::Could not revoke the token; it expires within the hour: {exc}")


def scan(entry, token, product, levo_api, control_repo, job):
    status, _, _ = call("GET", f"https://api.github.com/repos/{control_repo}", github(token))
    print(f"control repo via clone token -> HTTP {status} (404 expected)")
    if status != 404:
        raise Unsafe("the clone token can reach the control repository; it is too broad")

    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    mask(auth)
    # GitHub's git servers can refuse a newly issued token with a 404 for
    # tens of seconds after its API accepts it, so keep trying for two minutes.
    waits = [10, 20, 30, 30, 30]
    for attempt in range(len(waits) + 1):
        shutil.rmtree(product, ignore_errors=True)
        try:
            clone = subprocess.run(
                ["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}",
                 "clone", "--depth", "1", "--no-tags", "--quiet",
                 f"https://github.com/{entry['repo']}.git", product],
                env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"},
                capture_output=True, text=True, timeout=1200)
        except subprocess.TimeoutExpired:
            raise Failed("clone took longer than 20 minutes") from None
        if clone.returncode == 0:
            break
        if attempt < len(waits):
            print(f"Clone failed, retrying in {waits[attempt]}s: {clone.stderr.strip()[-200:]}", flush=True)
            time.sleep(waits[attempt])
    else:
        raise Failed(f"clone failed: {clone.stderr.strip()[-300:]}")

    timeout = int(entry["timeout"])
    args = ["--language", entry["language"], "--scan-timeout", str(timeout)]
    if os.environ.get("LEVO_AUTH_KEY"):
        print(f"Upload mode: results go to {levo_api} as '{entry['app_name']}' / {entry['env_name']}")
        args += ["--app-name", entry["app_name"], "--env-name", entry["env_name"],
                 "-k", os.environ["LEVO_AUTH_KEY"], "-o", os.environ["LEVO_ORG_ID"]]
    else:
        print("::notice::LEVOAI_AUTH_KEY not set - generating the spec locally only.")
        args += ["--no-upload", "--output", "/workspace/levo-openapi.json"]
    container = f"levo-scan-{job}-{entry['id']}"
    try:
        ran = subprocess.run(
            ["docker", "run", "--rm", "--name", container, "--user", f"{os.getuid()}:{os.getgid()}",
             "-e", "LEVO_BASE_URL", "-e", f"LEVO_SCAN_TIMEOUT={timeout}",
             "-v", f"{product}:/workspace:rw", IMAGE, *args],
            timeout=timeout + 120)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        raise Failed(f"still running {timeout + 120}s after it started, so it was stopped") from None
    if ran.returncode != 0:
        raise Failed(f"the scanner exited with code {ran.returncode}")


def summarise(entry, product, specs):
    """Prints what was discovered, keeps the spec for the artifact, and
    returns the number of real operations, or None without a spec."""
    found = [p for p in [os.path.join(product, "levo-openapi.json")]
             + sorted(glob.glob(os.path.join(product, "scan-results", "**", "openapi_spec.json"), recursive=True))
             if os.path.isfile(p)]
    if not found:
        print("No spec produced.")
        return None
    for path in found:
        keep = os.path.join(specs, entry["name"], os.path.relpath(path, product))
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        shutil.copyfile(path, keep)
    with open(found[0]) as fh:
        spec = json.load(fh)
    ops = [(p, m.upper()) for p, i in spec.get("paths", {}).items()
           for m in i if m.lower() in METHODS]
    real = [o for o in ops if not JUNK.search(o[0])]
    print(f"{len(real)} real operations ({len(ops) - len(real)} filename artefacts filtered)")
    for path, method in sorted(real):
        print(f"  {method:7} {path}")
    return len(real)


def write_summary(path, job, results):
    with open(path, "a") as fh:
        fh.write(f"### Job {job}\n\n"
                 "| Repository | Language | Result | Operations | Time |\n|---|---|---|---|---|\n")
        for entry, outcome, operations, seconds in results:
            cell = outcome.replace("|", "\\|").replace("\n", " ")
            ops = "-" if operations is None else operations
            fh.write(f"| {entry['repo']} | {entry['language']} | {cell} | {ops} | {seconds}s |\n")


def main():
    entries = json.loads(os.environ["REPOS"])
    job = os.environ["JOB"]
    workspace = os.environ["GITHUB_WORKSPACE"]
    product = os.path.join(workspace, "product")
    specs = os.path.join(workspace, "specs")
    levo_api = os.environ["LEVO_BASE_URL"]
    control_repo = os.environ["CONTROL_REPO"]
    app_id = os.environ.get("APP_ID", "")

    # One repository keeps the artifact it always had; a group shares one.
    single = len(entries) == 1
    with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
        fh.write(f"artifact={'spec-' + entries[0]['name'] if single else 'specs-' + job}\n")
        fh.write(f"path={'specs/' + entries[0]['name'] if single else 'specs'}\n")

    key_file = os.path.join(os.environ["RUNNER_TEMP"], "scan-app.pem")
    if app_id:
        with os.fdopen(os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            fh.write(os.environ["APP_PRIVATE_KEY"])

    installations, results, stopped = {}, [], None
    try:
        for n, entry in enumerate(entries, 1):
            if stopped:
                results.append((entry, "not scanned: " + stopped, None, 0))
                continue
            if not single:
                print(f"::group::{n}/{len(entries)} {entry['repo']} ({entry['language']})", flush=True)
            start, token, outcome, operations = time.time(), "", "scanned", None
            try:
                if app_id:
                    token = own_app_token(entry["owner"], entry["name"], app_id, key_file, installations)
                else:
                    token = levo_pass(entry["id"], levo_api)
                scan(entry, token, product, levo_api, control_repo, job)
            except Unsafe as exc:
                outcome, stopped = f"failed: {exc}", str(exc)
            except Exception as exc:
                outcome = f"failed: {exc}"
            finally:
                try:
                    operations = summarise(entry, product, specs)
                except Exception as exc:
                    print(f"Could not read the spec: {exc}")
                shutil.rmtree(product, ignore_errors=True)
                if token:
                    revoke(token)
                if not single:
                    print("::endgroup::", flush=True)
            if outcome != "scanned":
                print(f"::error::{entry['repo']}: {outcome}")
            results.append((entry, outcome, operations, round(time.time() - start)))
    finally:
        if app_id and os.path.exists(key_file):
            os.remove(key_file)

    write_summary(os.environ["GITHUB_STEP_SUMMARY"], job, results)
    failed = [r for r in results if r[1] != "scanned"]
    print(f"\n{len(results) - len(failed)} of {len(results)} repositories scanned.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
