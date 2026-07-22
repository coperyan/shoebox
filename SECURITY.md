# Security Policy

## Reporting a vulnerability

Please report security issues **privately** rather than opening a public issue.
Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(the **Security → Report a vulnerability** tab on this repo), or contact the
maintainer directly. You'll get a response as time permits — this is a
portfolio/reference project with no guaranteed SLA.

## Handling credentials

This project talks to **live** eBay, Google Cloud, and Slack accounts and can
create, reprice, and end real listings. Keep credentials out of the repo:

- Real config lives in `configs/app.yaml`, `configs/gcp.json`,
  `configs/ebay_rest.json`, and `configs/ebay_legacy.json`. **All are
  gitignored** — only the `*.example` templates are tracked.
- Prefer Application Default Credentials or a secret manager over committing a
  service-account key file.
- If a secret is ever committed or exposed, **rotate it first** (eBay keys,
  Slack tokens, GCP service-account key), then remove it from the tree and
  history. Rotation matters more than cleanup: anything pushed to a public repo
  may already be cached, forked, or indexed.

## Before running publishing pipelines

Every mutating pipeline supports `--dry-run` and creates offers unpublished
unless `--publish` is passed. Review the code and start with `--dry-run`
against your own accounts before running anything that touches live listings.
