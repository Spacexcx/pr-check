#!/usr/bin/env python3
"""
pr-check — private, pre-push code review assistant.

Runs on your machine. Reads your staged/unstaged git diff plus the full
content of each changed file (for context the diff alone can't show,
like docstrings above the changed lines), sends it to Gemini for risk
analysis, prints results to your terminal.

Never touches GitHub or your PR. Check Google's current data retention
policy for the free tier before pointing this at anything sensitive.
"""

import os
import sys
import json
import subprocess
import argparse
import time
import urllib.request
import urllib.error
from pathlib import Path

# Enable VT100 / ANSI escape sequences in Windows console without external packages
if sys.platform == "win32":
    os.system("")

# ANSI colors for terminal output
RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

MAX_FULL_FILE_CONTEXT_CHARS = 300_000
KEY_FILE = Path.home() / ".pr_check_key"

SYSTEM_PROMPT = """You are a senior code reviewer's assistant. You will be given a git diff AND the full current content of each changed file (when available). Use the full file content to check whether the changed lines contradict a docstring, comment, or naming convention defined elsewhere in the file — the diff hunk alone often can't show this, so actively look above/below the changed lines in the full file content for that kind of contradiction. Identify which changed files carry real risk that a human should double-check before committing/pushing.

LEARNED PATTERN: both human reviewers and naive LLM analysis have a systematic bias toward flagging visible business-logic files while UNDER-WEIGHTING config/wiring/initializer files (auth middleware, dependency injection, routing, environment setup) — even though real incidents disproportionately show up in those wiring files. When you see both business-logic and config/wiring changes, give genuine scrutiny to what the config change actually does at runtime rather than defaulting to flagging the more readable business logic.

GROUNDING REQUIREMENT (critical): For every file marked risky, you MUST include an 'evidence_quote' field containing a short EXACT substring copied verbatim from the diff (max ~15 words) that directly supports your reasoning. If you cannot find an exact line to quote that supports the specific failure mode you're describing, do NOT flag the file as risky.

CATEGORY-KEYWORD BIAS WARNING (critical): Do not flag a file as risky merely because its path or imports contain scary-sounding words like "crypto", "auth", "security", "password", "entropy", "admin". Many changes to security-adjacent code are safe, boilerplate, or equivalent-behavior refactors (e.g. moving the same underlying call from a runtime registration API to a compile-time macro). Before flagging anything in a security-adjacent file, you must be able to articulate a SPECIFIC behavioral difference between the old and new code, not just "this touches security so it's risky." If the diff shows the same underlying function/logic being called through a different mechanism with equivalent effect, that is NOT risky.

CONCURRENCY EXCEPTION (important): Changes to locking, thread-local state, mutex/lock ordering, copy-on-write semantics under concurrent access, or any code where correctness depends on the relative timing/ordering of threads are a special case. Unlike other categories, these should generally be flagged at least medium confidence EVEN IF the change looks correct and even if you cannot articulate a concrete failure scenario — the nature of concurrency bugs is that they are usually not visible from reading the code alone, only from execution under specific timing conditions. If the PR author's own description expresses uncertainty ("might have edge cases", "expecting some regressions", "not sure why this happens") about a concurrency-related change, treat that as a strong signal to flag it, quoting the author's own words as evidence if useful. The bar for flagging concurrency changes should be "does this change how/when threads can observe or mutate shared state" — not "can I point to a specific bug."

Do NOT flag: pure documentation/comments/test files (unless the test reveals a logic gap), mechanical/generated code, trivial declaration-only changes, straightforward code whose only 'risk' is that you can't fully see its context.

Respond ONLY with valid JSON, no markdown fences, no other text:
{"files":[{"filename":"...","risky":true,"confidence":"low|medium|high","reason":"one sentence","evidence_quote":"exact substring from the diff, or empty string if not risky"}],"summary":"one sentence overview of the change"}
"""

CRITIQUE_SYSTEM_PROMPT = """You will be shown a list of risk claims made about a code change, each with the exact diff line quoted as evidence. Your job is NOT to re-judge whether the risk is real — it is to find unverified assumptions the claim's REASONING depends on.

A claim can quote a completely real line and still be wrong, if it silently assumes something about how a type, library, or framework behaves that isn't shown in the visible code. Common categories: whether a lock/mutex type is recursive or reentrant; whether a function is thread-safe, atomic, or idempotent by contract; whether a callback runs synchronously or is deferred; whether a default value/initializer has the effect assumed; whether an inherited/overridden method preserves the base behavior described.

For each claim, ask: "Does this reasoning depend on a specific behavioral property of a type/library/framework that is NOT directly visible in the diff or file content I was given?" If yes, state that exact property as a short, checkable assumption (e.g. "assumes the Mutex type used here is non-recursive"). If the reasoning is fully self-contained in the visible code with no outside assumption, return an empty string for that file.

Do not soften or second-guess claims that don't rely on an outside assumption — only surface genuine unverified dependencies.

Respond ONLY with valid JSON, no markdown fences, no other text:
{"critiques":[{"filename":"...","unverified_assumption":"short specific assumption, or empty string if none"}]}
"""


def get_api_key(cli_key=None):
    """Retrieves the API key from argument, environment, stored config, or prompts the user interactively."""
    if cli_key:
        return cli_key.strip()
    
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key:
        return env_key.strip()

    if KEY_FILE.exists():
        try:
            stored = KEY_FILE.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        except Exception:
            pass

    # Interactive fallback
    print(f"{YELLOW}GEMINI_API_KEY is not set.{RESET}")
    print(f"Get a free key (no credit card needed) at {BOLD}https://aistudio.google.com{RESET}")
    try:
        entered = input("Paste your Gemini API Key here (press Enter to cancel): ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(1)

    if not entered:
        print(f"{RED}No API key provided. Exiting.{RESET}")
        sys.exit(1)

    # Offer to save locally for convenience
    try:
        KEY_FILE.write_text(entered, encoding="utf-8")
        print(f"{GREEN}Saved key to {KEY_FILE} for future runs.{RESET}\n")
    except Exception:
        pass

    return entered


def fetch_github_pr(pr_url):
    import re
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not m:
        print(f"{RED}Invalid PR URL. Expected: https://github.com/owner/repo/pull/1234{RESET}")
        sys.exit(1)
    owner, repo, num = m.groups()

    api_base = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "pr-check-cli"
    }

    def gh_get(url):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"{RED}GitHub API error ({e.code}): {e.read().decode('utf-8')[:300]}{RESET}")
            sys.exit(1)

    pr_data = gh_get(f"{api_base}/pulls/{num}")
    head_sha = pr_data.get("head", {}).get("sha", "")
    author_association = pr_data.get("author_association", "UNKNOWN")
    is_external = author_association in ("NONE", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "CONTRIBUTOR")

    assoc_note = (
        f"{GREEN}(external contributor — zero comments is a stronger 'nothing to find' signal){RESET}"
        if is_external else
        f"{YELLOW}(author association: {author_association} — zero comments may just mean trusted/commit-access author, not 'nothing to find'){RESET}"
    )
    print(f"{DIM}Author association: {author_association}. {assoc_note}{RESET}")

    files_data = gh_get(f"{api_base}/pulls/{num}/files?per_page=100")

    parts = [f"=== PR TITLE ===\n{pr_data.get('title','')}\n\n=== PR DESCRIPTION ===\n{(pr_data.get('body') or '')[:1500]}"]
    diff_chunks = []
    full_file_chars_used = 0
    files_truncated = 0
    for f in files_data:
        fname = f["filename"]
        patch = f.get("patch", "(binary or too large to diff)")
        diff_chunks.append(f"--- a/{fname}\n+++ b/{fname}\n{patch}")

        if full_file_chars_used >= MAX_FULL_FILE_CONTEXT_CHARS:
            files_truncated += 1
            continue

        if f.get("status") != "removed" and f.get("changes", 0) < 2000:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{head_sha}/{fname}"
            try:
                req = urllib.request.Request(raw_url, headers={"User-Agent": "pr-check-cli"})
                with urllib.request.urlopen(req) as resp:
                    content = resp.read().decode("utf-8", errors="ignore")
                if len(content) >= 40_000:
                    pass
                elif full_file_chars_used + len(content) <= MAX_FULL_FILE_CONTEXT_CHARS:
                    parts.append(f"\n=== FULL FILE (current state): {fname} ===\n{content}")
                    full_file_chars_used += len(content)
                else:
                    files_truncated += 1
            except Exception:
                pass

    if files_truncated:
        print(f"{YELLOW}Large PR: sent full file content for the first files within budget, diff-only for the remaining {files_truncated} file(s).{RESET}")

    diff_only_text = "\n\n".join(diff_chunks)
    description_text = f"{pr_data.get('title','')}\n{(pr_data.get('body') or '')}"
    parts.insert(1, "=== DIFF (what changed) ===\n" + diff_only_text)
    return "\n".join(parts), diff_only_text, description_text


def extract_changed_lines(diff_text):
    changed = []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            changed.append(line[1:].strip())
    return changed


def verify_evidence(files, diff_text, extra_valid_text=""):
    normalized_lines = [" ".join(line.split()) for line in extract_changed_lines(diff_text)]
    normalized_extra = " ".join(extra_valid_text.split())

    for f in files:
        if not f.get("risky"):
            continue
        quote = (f.get("evidence_quote") or "").strip()
        if not quote:
            f["risky"] = False
            f["reason"] = f.get("reason", "") + " [demoted: no evidence quote provided]"
            continue
        normalized_quote = " ".join(quote.split())
        found_in_a_line = any(normalized_quote in line for line in normalized_lines)
        if not found_in_a_line and normalized_quote not in normalized_extra:
            f["risky"] = False
            f["reason"] = f.get("reason", "") + " [demoted: evidence quote not found in a single added/removed diff line or PR description — likely unverified]"
    return files


def get_git_diff(staged_only=False):
    cmd = ["git", "diff", "--cached"] if staged_only else ["git", "diff", "HEAD"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout
    except subprocess.CalledProcessError:
        print(f"{RED}Not a git repository, or git error.{RESET}")
        sys.exit(1)
    except FileNotFoundError:
        print(f"{RED}git not found. Is it installed?{RESET}")
        sys.exit(1)


def get_changed_filenames(staged_only=False):
    cmd = ["git", "diff", "--cached", "--name-only"] if staged_only else ["git", "diff", "HEAD", "--name-only"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [f for f in result.stdout.splitlines() if f.strip()]


def build_context(diff_text, staged_only=False):
    filenames = get_changed_filenames(staged_only=staged_only)
    parts = [f"=== DIFF (what changed) ===\n{diff_text}"]

    full_file_chars_used = 0
    files_truncated = 0
    for fname in filenames:
        try:
            size = os.path.getsize(fname)
        except OSError:
            continue
        if size > 40_000:
            continue
        if full_file_chars_used + size > MAX_FULL_FILE_CONTEXT_CHARS:
            files_truncated += 1
            continue
        try:
            with open(fname, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            continue
        parts.append(f"\n=== FULL FILE (current state): {fname} ===\n{content}")
        full_file_chars_used += len(content)

    if files_truncated:
        print(f"{YELLOW}Large changeset: sent full file content for the first files within budget, diff-only for the remaining {files_truncated} file(s).{RESET}")

    return "\n".join(parts)


def _call_gemini(text, system_prompt, api_key, max_output_tokens=6000, max_retries=3):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": text}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "maxOutputTokens": max_output_tokens,
            "response_mime_type": "application/json",
        }
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    TRANSIENT_CODES = {429, 500, 502, 503, 504}
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            last_error = (e.code, body)
            if e.code not in TRANSIENT_CODES or attempt == max_retries:
                print(f"{RED}Gemini API error ({e.code}): {body}{RESET}")
                sys.exit(1)
            wait = 2 ** attempt
            print(f"{YELLOW}Gemini API busy ({e.code}), retrying in {wait}s... (attempt {attempt + 1}/{max_retries}){RESET}")
            time.sleep(wait)
    else:
        code, body = last_error
        print(f"{RED}Gemini API error ({code}) after {max_retries} retries: {body}{RESET}")
        sys.exit(1)

    response_parts = data["candidates"][0]["content"].get("parts", [])
    raw = "".join(p.get("text", "") for p in response_parts if not p.get("thought"))
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"{RED}Could not parse model response as JSON: {e}{RESET}")
        print(f"{DIM}--- raw response (first 1000 chars) ---{RESET}")
        print(raw[:1000])
        sys.exit(1)


def analyze(context_text, api_key):
    return _call_gemini(context_text, SYSTEM_PROMPT, api_key, max_output_tokens=6000)


def critique_reasoning(risky_files, api_key):
    if not risky_files:
        return {}
    claims_text = "\n".join(
        f"- {f['filename']}: {f.get('reason', '')} (quoted: \"{f.get('evidence_quote', '')}\")"
        for f in risky_files
    )
    result = _call_gemini(claims_text, CRITIQUE_SYSTEM_PROMPT, api_key, max_output_tokens=2000)
    return {
        c.get("filename", ""): (c.get("unverified_assumption") or "").strip()
        for c in result.get("critiques", [])
    }


def print_results(result):
    print()
    print(f"{BOLD}{result.get('summary', '')}{RESET}")
    print()

    files = result.get("files", [])
    risky = [f for f in files if f.get("risky")]
    safe = [f for f in files if not f.get("risky")]

    for f in risky:
        color = RED if f.get("confidence") == "high" else YELLOW
        icon = "\U0001f534" if f.get("confidence") == "high" else "\U0001f7e1"
        print(f"{color}{icon} {f['filename']}{RESET}")
        print(f"   {f.get('reason', '')}")
        if f.get("evidence_quote"):
            print(f"   {DIM}\u21b3 \"{f['evidence_quote']}\"{RESET}")
        if f.get("unverified_assumption"):
            print(f"   {YELLOW}\u26a0 Assumes: {f['unverified_assumption']} — verify before trusting this flag{RESET}")
        print()

    if safe:
        print(f"{GREEN}{DIM}Low risk: {', '.join(f['filename'] for f in safe)}{RESET}")
        print()

    if risky:
        print(f"{BOLD}{len(risky)} file(s) worth a second look before you push.{RESET}")
    else:
        print(f"{GREEN}{BOLD}Nothing flagged. Looks clean.{RESET}")


def main():
    parser = argparse.ArgumentParser(description="Local, private PR risk check.")
    parser.add_argument("--staged", action="store_true", help="Only analyze staged changes")
    parser.add_argument("--pr", type=str, default=None, help="GitHub PR URL to analyze instead of local git diff")
    parser.add_argument("--key", type=str, default=None, help="Gemini API Key (optional, can also be prompted or use GEMINI_API_KEY env)")
    args = parser.parse_args()

    api_key = get_api_key(args.key)

    pr_description = ""
    if args.pr:
        print(f"{DIM}Fetching {args.pr} from GitHub...{RESET}")
        context_text, diff_text, pr_description = fetch_github_pr(args.pr)
    else:
        diff_text = get_git_diff(staged_only=args.staged)
        if not diff_text.strip():
            print(f"{GREEN}No changes detected.{RESET}")
            sys.exit(0)
        context_text = build_context(diff_text, staged_only=args.staged)

    print(f"{DIM}Analyzing changes — sending to Gemini...{RESET}")
    result = analyze(context_text, api_key)
    result["files"] = verify_evidence(result.get("files", []), diff_text, extra_valid_text=pr_description)

    still_risky = [f for f in result["files"] if f.get("risky")]
    if still_risky:
        print(f"{DIM}Checking flagged files for unverified assumptions...{RESET}")
        assumptions = critique_reasoning(still_risky, api_key)
        for f in result["files"]:
            assumption = assumptions.get(f["filename"], "")
            if assumption:
                f["unverified_assumption"] = assumption

    print_results(result)


if __name__ == "__main__":
    main()
