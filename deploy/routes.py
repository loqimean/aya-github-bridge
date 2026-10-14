#!/usr/bin/env python3
"""Write the bot's GitHub webhook routes into Hermes config.yaml. Idempotent.

Usage (on the Hermes host):  python3 routes.py config.json [--dry-run]

Keeps existing route secrets, backs up config.yaml first, validates the YAML after writing, and removes
any `legacy_routes` listed in the config.

Static routes and their prompts are read when the gateway starts, so restart the gateway afterwards
(see README, "Operating it"). The gate script is read per request, so script-only changes are live
immediately.
"""
import json
import os
import secrets
import shutil
import sys
import time

import yaml

HERMES_CONFIG = os.environ.get("HERMES_CONFIG", "/root/.hermes/config.yaml")
EVENTS = ["issues", "issue_comment", "pull_request", "pull_request_review", "pull_request_review_comment"]

OPUS_DELEGATE = """- Tier "opus": hand the whole job to ONE subagent right away with delegate_task (it runs on Opus).
  In its context pass: repo, number, link, the owner's message, the thread summary, and these rules verbatim:
  the POSTING and REACTIONS rules below, the git identity rule, the style rules, and "load skills with
  skill_view as needed (github-issue-to-pr, the repo codebase skill)". The subagent posts the plan and the
  results on GitHub itself. Before delegating, post a short plan comment with rough effort. After it returns,
  verify what it claims (e.g. the PR or comment exists) and post a correction if it didn't happen."""

OPUS_SELF = """- Tier "opus": no Opus subagent in this profile. Do the work yourself, carefully: post a short plan with
  rough effort first, load skills with skill_view as needed, then do it and report back."""


def prompt(cfg: dict, opus_rule: str) -> str:
    name, bot, owner = cfg["bot_name"], cfg["bot_login"], cfg["owner_login"]
    tok = f"GH_TOKEN=$(cat {cfg['agent_token_path']})"
    return f"""You are {name}, working on GitHub as user {bot}. The repo owner ({owner}) just triggered you.

Trigger: {{bot.kind}}
Repo: {{bot.repo}}
{{bot.target_type}} #{{bot.number}}: {{bot.title}}
Link: {{bot.url}}
{{bot.context}}

Router decision: tier "{{bot.tier}}" ({{bot.why}})

Instant reply ALREADY POSTED by a fast model (no repo access) a few seconds ago:
---
{{bot.instant_reply}}
---
Don't repeat it. Build on it. If it's wrong or a guess, post a short correction.

Earlier conversation in this thread (oldest first):
---
{{bot.thread}}
---

The owner's new message / description:
---
{{bot.text}}
---

A 👀 reaction is already on it. Communication happens ONLY on GitHub.

POSTING (always as {bot}, via REST):
  Comment:  {tok} gh api repos/{{bot.repo}}/issues/{{bot.number}}/comments -F body=@- <<'EOF'
            your markdown reply
            EOF
  Inline review thread reply:
            {tok} gh api repos/{{bot.repo}}/pulls/{{bot.number}}/comments/COMMENT_ID/replies -F body=@- <<'EOF' ... EOF
  Reaction on the owner's message (content: +1, heart, hooray, rocket, laugh, confused, eyes):
            {tok} gh api {{bot.react_path}} -f content=rocket
  Chain commands with && in ONE terminal call when doing several (e.g. comment && reaction).
git commit / push / gh pr create stay as {owner} (no bot token).

REACTIONS INSTEAD OF FILLER
- Message needs no words back: react only (heart, +1, hooray), no comment.
- When you finish a task, add a rocket reaction to the owner's message in the same terminal call as the final comment.
- Never post comments like "Glad you like it", "Thanks!", "On it!" alone.

TIERS (a router already picked one; trust it unless clearly wrong, and if so just act at the right level):
- Tier "sonnet": bounded work. Do it yourself with the minimum reads, then post. Under ~8 tool calls for
  questions; small fixes may take more.
  If the owner only asked a question and the real work would be big, post what it would take + rough effort +
  "Want me to start?" and stop.
{opus_rule}

Reviews: {tok} gh pr review ..., --comment or --request-changes, never --approve unless the owner asked.

STYLE on GitHub: direct, concise, active voice, contractions, no em dashes, real markdown links. Screenshots
only in PR descriptions. Never claim CI green or done without checking.

Your final chat answer is only written to a log. Make it a few words, e.g. "posted" or "delegated"."""


def build_route(cfg: dict, spec: dict, secret: str) -> dict:
    opus = bool(spec.get("opus_subagent"))
    route = {
        "events": EVENTS,
        "secret": secret,
        "filters": [{"field": "sender.login", "equals": cfg["owner_login"]}],
        "script": "hermes_github_gate.py",
        "prompt": prompt(cfg, OPUS_DELEGATE if opus else OPUS_SELF),
        "skills": [],
        "toolsets": ["terminal", "web", "skills"] + (["delegation"] if opus else []),
        "deliver": "log",
    }
    if spec.get("profile"):
        route["profile"] = spec["profile"]
    return route


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cfg = json.load(open(sys.argv[1]))
    dry = "--dry-run" in sys.argv
    c = yaml.safe_load(open(HERMES_CONFIG))
    routes = c["platforms"]["webhook"]["extra"].setdefault("routes", {})
    for name in cfg.get("legacy_routes", []):
        if routes.pop(name, None) is not None:
            print(f"removed legacy route {name}")
    for name, spec in cfg["routes"].items():
        secret = (routes.get(name) or {}).get("secret") or secrets.token_hex(24)
        routes[name] = build_route(cfg, spec, secret)
        prof = spec.get("profile")
        print(f"route {name}: {'/p/' + prof if prof else ''}/webhooks/{name} profile={prof or 'default'} "
              f"opus_subagent={bool(spec.get('opus_subagent'))}")
    if dry:
        print("dry run, nothing written")
        return
    shutil.copy(HERMES_CONFIG, f"{HERMES_CONFIG}.bak-{time.strftime('%Y%m%dT%H%M%S')}-github-routes")
    yaml.safe_dump(c, open(HERMES_CONFIG, "w"), allow_unicode=True, sort_keys=False, width=1000)
    yaml.safe_load(open(HERMES_CONFIG))
    print("config.yaml written and re-parsed OK. Restart the gateway to load prompt/route changes.")


if __name__ == "__main__":
    main()
