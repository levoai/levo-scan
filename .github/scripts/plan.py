"""Decides what the scan job runs.

Lists the repositories granted to the GitHub App, works out which qualify and
as what language and application, and groups them into jobs. Metadata only:
nothing here can read code.
"""
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import yaml

NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,80}$")

# Anything atom can analyse. A repository whose main language is not
# here is skipped with a reason rather than scanned pointlessly.
ALIASES = {
    "c#": "csharp", "csharp": "csharp", ".net": "csharp", "dotnet": "csharp",
    "javascript": "javascript", "typescript": "typescript",
    "java": "java", "kotlin": "kotlin",
    "python": "python", "ruby": "ruby", "php": "php",
    "rust": "rust", "go": "go",
}

# GitHub starts at most 256 jobs from one matrix and rejects the rest of the
# run with an error that does not say why.
MAX_JOBS = 256

# A job must end inside GitHub's six hours, with room to start up.
JOB_MINUTES = 330


def die(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def api(path):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def all_granted():
    """Every page, not just the first. A single page silently caps
    discovery at 100 repositories and drops the rest without a word."""
    out, page = [], 1
    while True:
        batch = api(f"/installation/repositories?per_page=100&page={page}")
        repos = batch.get("repositories", [])
        out.extend(repos)
        if len(repos) < 100:
            return out
        page += 1
        if page > 30:  # 3,000 repositories; far past any real installation
            print("::warning::Stopped paging at 3,000 repositories.")
            return out


def load_config(control_repo):
    try:
        with open("levo-config.yml") as fh:
            cfg = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        cfg = {}
    except Exception as exc:
        die(f"levo-config.yml is not valid YAML: {exc}")

    if cfg.get("version") not in (None, 1):
        die(f"Unsupported config version: {cfg.get('version')!r}. Expected 1.")

    defaults = cfg.get("defaults") or {}
    env_name = str(defaults.get("env_name", "staging"))
    timeout = max(60, min(2400, int(defaults.get("scan_timeout", 1800))))

    # Per-repository overrides, keyed by "owner/name". Everything in here
    # is optional; a repository with no entry still gets scanned.
    overrides = {}
    for item in (cfg.get("repositories") or []):
        repo = str(item.get("repo", ""))
        if repo:
            overrides[repo] = item

    exclude = {str(x) for x in (cfg.get("exclude") or [])}
    exclude.add(control_repo)
    return env_name, timeout, overrides, exclude


def languages(full):
    try:
        return api(f"/repos/{full}/languages")
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc


def qualify(granted, control_repo, env_name, timeout, overrides, exclude):
    # Read several at once: one by one, a large organisation outlasts the
    # plan job's time limit before the first scan starts.
    to_read = [r["full_name"] for r in granted
               if r["full_name"] != control_repo
               and r["full_name"] not in exclude
               and not r.get("archived")
               and not str(overrides.get(r["full_name"], {}).get("language", "")).strip()]
    with ThreadPoolExecutor(max_workers=8) as pool:
        read = dict(zip(to_read, pool.map(languages, to_read)))

    entries, skipped = [], []
    for repo in granted:
        full = repo["full_name"]
        if full == control_repo:
            continue  # the control repo holds configuration, not an API
        if full in exclude:
            skipped.append((full, "excluded in levo-config.yml"))
            continue
        if repo.get("archived"):
            skipped.append((full, "archived"))
            continue

        item = overrides.get(full, {})

        lang = str(item.get("language", "")).strip().lower()
        if not lang:
            langs = read[full]
            if isinstance(langs, urllib.error.HTTPError):
                skipped.append((full, f"could not read languages ({langs.code})"))
                continue
            if not langs:
                skipped.append((full, "GitHub reports no language for this repository"))
                continue
            # The largest language the scanner reads, not the largest
            # overall: HTML or a notebook can outweigh the API beside it.
            supported = {n: b for n, b in langs.items() if n.lower() in ALIASES}
            if not supported:
                top = max(langs.items(), key=lambda kv: kv[1])[0].lower()
                skipped.append((full, f"language '{top}' is not supported by the scanner"))
                continue
            lang = max(supported.items(), key=lambda kv: kv[1])[0].lower()

        resolved = ALIASES.get(lang)
        if resolved is None:
            skipped.append((full, f"language '{lang}' is not supported by the scanner"))
            continue

        app = str(item.get("app_name", "")).strip() or repo["name"]
        if not NAME_RE.match(app):
            skipped.append((full, f"app_name {app!r} contains unsupported characters"))
            continue

        entries.append({
            "repo": full, "owner": repo["owner"]["login"], "name": repo["name"], "id": repo["id"],
            "language": resolved, "app_name": app,
            "env_name": env_name, "timeout": timeout,
        })
    return entries, skipped


def group(entries, timeout, max_jobs):
    """One repository per job while they fit in max_jobs; past that, each job
    scans several one after another. Returns the groups and the job's limit."""
    per_job = math.ceil(len(entries) / max_jobs)
    # A repository may use all of scan_timeout plus a few minutes to clone
    # and upload.
    per_repo = timeout / 60 + 3
    fits = int(JOB_MINUTES // per_repo)
    if per_job > fits:
        die(f"{len(entries)} repositories qualify, but with scan_timeout {timeout} "
            f"one run can scan at most {max_jobs * fits}. Lower scan_timeout, "
            "narrow the GitHub App's repository access, or add repositories to "
            "exclude in levo-config.yml.")
    groups = [entries[i:i + per_job] for i in range(0, len(entries), per_job)]
    # Never shorter than the 55 minutes a one-repository job always had.
    return groups, min(355, max(55, math.ceil(per_job * per_repo) + 10))


def label(repos):
    first = repos[0]["repo"]
    return first if len(repos) == 1 else f"{first} +{len(repos) - 1}"


def main():
    control_repo = os.environ["CONTROL_REPO"]
    env_name, timeout, overrides, exclude = load_config(control_repo)

    # The GitHub App's installation is the source of truth for scope. A
    # repository listed in config but not granted cannot be scanned, and a
    # repository granted but not listed still should be.
    granted = all_granted()
    if not granted:
        die("The GitHub App has not been granted access to any repository.")

    entries, skipped = qualify(granted, control_repo, env_name, timeout, overrides, exclude)

    print(f"{len(granted)} repository/repositories granted to the GitHub App\n")
    if entries:
        print("Will scan:")
        for e in entries:
            print(f"  {e['repo']:45} {e['language']:12} -> {e['app_name']}")
    if skipped:
        print("\nSkipped:")
        for full, reason in skipped:
            print(f"  {full:45} {reason}")

    if not entries:
        die("Nothing to scan. Grant the GitHub App access to a repository, "
            "or check the skip reasons above.")

    try:
        max_jobs = max(1, min(MAX_JOBS, int(os.environ.get("MAX_JOBS") or MAX_JOBS)))
    except ValueError:
        die("The LEVO_MAX_JOBS variable must be a number.")

    groups, job_timeout = group(entries, timeout, max_jobs)
    if len(groups) < len(entries):
        print(f"\nMore than {max_jobs} repositories qualify, so each job scans up to "
              f"{len(groups[0])}, one after another: {len(groups)} jobs.")

    # Jobs are numbered from 1; groups[0] is never used.
    matrix = {"include": [{"job": n, "repositories": label(g)} for n, g in enumerate(groups, 1)]}
    with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
        fh.write(f"matrix={json.dumps(matrix)}\n")
        fh.write(f"count={len(entries)}\n")
        fh.write(f"groups={json.dumps([[]] + groups, separators=(',', ':'))}\n")
        fh.write(f"job_timeout={job_timeout}\n")


if __name__ == "__main__":
    main()
