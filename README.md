# Levo control repository

This repository controls Levo's code-side API discovery for your organisation.

It holds the scan workflow, the scan configuration, and the credential that
reads your source. **It does not contain your source code, and Levo cannot reach
any repository other than this one.**

---

## What Levo can and cannot do

| | |
|---|---|
| Levo can read and write **this** repository | to keep `levo-config.yml` current |
| Levo can open pull requests **here** | to propose changes you review |
| Levo **cannot** read your source repositories | it holds no credential that can |
| Levo **cannot** modify anything under `.github/workflows/` | it does not hold that permission — GitHub refuses |

The credential that clones your source is one **you** create, stored as a secret
in this repository. Levo never receives it.

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

If your Levo tenant is not on `api.levo.ai`, add a variable named
`LEVOAI_BASE_URL` as well.

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

---

## Recommended hardening

Worth doing before you rely on this, and quick.

**Put the credential behind an Environment.** Settings → Environments → new
environment named `levo-scan` → move `APP_PRIVATE_KEY` into it → set
**Deployment branches** to your default branch only.

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

Uninstall the Levo App, or delete the Scan App you created. Either stops access
immediately. Specs already sent to Levo are retained unless you ask otherwise.
