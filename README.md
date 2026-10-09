# Levo control repository

This repository controls Levo's code-side API discovery for your organisation.

It holds the scan workflow, the scan configuration, and the credential that
reads your source. **It does not contain your source code, and Levo has no access
to this repository or any other.** Scans run on your GitHub runners; only the
generated API specifications are sent to Levo.

---

## What Levo can and cannot do

| | |
|---|---|
| Your runners clone and scan | one repository at a time, with a token limited to that repository |
| Levo receives | the generated API specification, the application name and the environment |
| Levo **cannot** read your source repositories | it holds no credential that can |
| Levo **cannot** change this repository | it has no access to it |

The credential that clones your source is one **you** create, stored as a secret
in this repository. Levo never receives it.

---

## Using the Levo GitHub App instead

If your Levo account offers it (**Integrations → GitHub App** in Levo), you can
skip creating your own Scan App:

1. In Levo: **Integrations → GitHub App → Install GitHub App**. Choose your
   organisation and the repositories to scan, then **Connect installation**.
2. Create this repository (step 1 below) and add only `LEVOAI_AUTH_KEY` and
   `LEVOAI_ORG_ID` (step 3), plus `LEVOAI_BASE_URL` if your Levo is not
   `app.levo.ai`. Do **not** set `APP_ID`.
3. Run it (step 5).

Without `APP_ID`, the workflow asks Levo for short-lived passes instead of using
a key you hold: a metadata-only pass to list repositories, then, per repository,
a read-only pass for that repository alone, revoked when its scan ends. Levo
issues passes only to this repository's workflow running in the `levo-scan`
environment, so restrict that environment to your default branch (see
Recommended hardening). In this mode the Levo GitHub App holds read access to the
repositories you grant it; the table above describes your own Scan App.

---

## Setup

### 1. Create this repository

Click **Use this template** at the top of this page. Name it `_levoai`. Keep it
private.

### 2. Create your Scan App

This is the credential that reads your code. You create it; Levo never sees it.

Go to your organisation's settings → Developer settings → **New GitHub App**.

| Field | Value |
|---|---|
| Name | `Levo Scan - <Your Company>` |
| Homepage URL | anything |
| Webhook → Active | **untick** |
| Repository permissions → **Contents** | **Read-only** |
| Everything else | No access |

> GitHub App names are unique across the whole of GitHub, so include your
> company name — `Levo Scan - Acme Corp`. The plain form is already taken.

Create it, note the **App ID**, and **Generate a private key** (a `.pem` file
downloads).

Then **Install App** → select **only** the repositories you want scanned.

> Access is controlled here, at install time — not in `levo-config.yml`. Listing
> a repository in the config without granting access here will simply skip it.
> Neither Levo nor anyone holding a Levo credential can widen this; GitHub only
> accepts the change from you.

### 3. Store the credential

In **this** repository: Settings → Secrets and variables → Actions.

**Secrets** tab:

| Secret | Value |
|---|---|
| `APP_PRIVATE_KEY` | the full contents of the `.pem` file |
| `LEVOAI_AUTH_KEY` | from app.levo.ai → Settings → Keys |
| `LEVOAI_ORG_ID` | from app.levo.ai → Settings → Organization |

**Variables** tab:

| Variable | Value |
|---|---|
| `APP_ID` | the App ID from step 2 |

> `APP_ID` is a variable rather than a secret because it is not confidential,
> and a variable can be read back afterwards — so a typo is visible. A mistyped
> secret cannot be read back and stays invisible until a scan fails.

If your Levo tenant is in India, copy the two keys from `app.india-1.levo.ai`
instead and add a variable named `LEVOAI_BASE_URL` with the value
`https://api.india-1.levo.ai`. Without it, results go to `api.levo.ai`.

### 4. Nothing to list

What gets scanned is decided by step 2 -- the repositories you granted your Scan
App access to. The language is read from GitHub's language statistics, and each
application is named after its repository.

`levo-config.yml` is there only if you want to override any of that, or exclude
a repository the Scan App can see.

One setting in it is worth checking: **`env_name`**, which is the environment
your endpoints are tagged with in Levo. It is `staging` by default — change it
to whatever you actually run (`production`, `NonProd`, `uat`).

### 5. Run it

Actions → **Levo scan** → **Run workflow**. After that it runs weekly on its
own.

### Limits

Each repository gets up to 40 minutes (`scan_timeout`, default 30). A repository
that runs longer is stopped and reported; the others carry on. One run scans at
most 256 repositories. If more qualify, the run stops before scanning and says
so; narrow the Scan App's access or use `exclude`.

---

## Recommended hardening

Worth doing before you rely on this, and quick.

**Put the credential behind an Environment.** The workflow already runs in an
environment named `levo-scan`; GitHub creates it on the first run. Settings →
Environments → `levo-scan` → add `APP_PRIVATE_KEY` as an environment secret →
delete the repository secret of the same name → set **Deployment branches** to
your default branch only.

This matters more than it appears: repository secrets are **not** limited to
your default branch, so any workflow pushed to any branch — including an
unmerged pull request — can read them. Branch protection does not prevent that.
The environment restriction does.

We do not recommend adding a **required reviewer** to that environment. It would
gate every scheduled run, so a weekly scan would wait for a human indefinitely.

**Protect this repository's default branch** and require review from the team
named in `CODEOWNERS`. Update that file first — the placeholder team does not
exist.

---

## Removing it

Uninstall or delete the Scan App you created. That stops access to your
repositories immediately. Specs already sent to Levo are retained unless you ask
otherwise.
