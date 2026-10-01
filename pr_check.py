#!/usr/bin/env python3
"""
pr-check — private, pre-push code review assistant.

Runs locally on your machine. Never writes to GitHub or posts comments
on pull requests. (In --pr mode, it only reads PR data via the GitHub API.)

Reads outgoing git commits or PR diffs plus full file contents,
sends them to Gemini for risk analysis, and prints results to your terminal.
"""

import os
import sys
import json
import subprocess
import argparse
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    os.system("")

RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_FULL_FILE_CONTEXT_CHARS = 300_000
MAX_FULL_FILE_SIZE = 40_000

KEY_FILE = Path.home() / ".pr_check_key"

MODEL_NAME = "gemini-3.6-flash"


# ---------------------------------------------------------------------------
# Gemini prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior code reviewer's assistant. You will be given a git diff AND the full current content of each changed file (when available). Use the full file content to check whether the changed lines contradict a docstring, comment, or naming convention defined elsewhere in the file — the diff hunk alone often can't show this, so actively look above/below the changed lines in the full file content for that kind of contradiction. Identify which changed files carry real risk that a human should double-check before committing/pushing.

LEARNED PATTERN: both human reviewers and naive LLM analysis have a systematic bias toward flagging visible business-logic files while UNDER-WEIGHTING config/wiring/initializer files (auth middleware, dependency injection, routing, environment setup) — even though real incidents disproportionately show up in those wiring files. When you see both business-logic and config/wiring changes, give genuine scrutiny to what the config change actually does at runtime rather than defaulting to flagging the more readable business logic.

GROUNDING REQUIREMENT (critical): For every file marked risky, you MUST include an 'evidence_quote' field containing a short EXACT substring copied verbatim from the diff (max ~15 words) that directly supports your reasoning. If you cannot find an exact line to quote that supports the specific failure mode you're describing, do NOT flag the file as risky.

CATEGORY-KEYWORD BIAS WARNING (critical): Do not flag a file as risky merely because its path or imports contain scary-sounding words like "crypto", "auth", "security", "password", "entropy", "admin". Many changes to security-adjacent code are safe, boilerplate, or equivalent-behavior refactors. Before flagging anything in a security-adjacent file, you must be able to articulate a SPECIFIC behavioral difference between the old and new code, not just "this touches security so it's risky." If the diff shows the same underlying function/logic being called through a different mechanism with equivalent effect, that is NOT risky.

CONCURRENCY EXCEPTION (important): Changes to locking, thread-local state, mutex/lock ordering, copy-on-write semantics under concurrent access, or any code where correctness depends on the relative timing/ordering of threads are a special case. Unlike other categories, these should generally be flagged at least medium confidence EVEN IF the change looks correct and even if you cannot articulate a concrete failure scenario — the nature of concurrency bugs is that they are usually not visible from reading the code alone, only from execution under specific timing conditions. If the PR author's own description expresses uncertainty ("might have edge cases", "expecting some regressions", "not sure why this happens") about a concurrency-related change, treat that as a strong signal to flag it, quoting the author's own words as evidence if useful. The bar for flagging concurrency changes should be "does this change how/when threads can observe or mutate shared state" — not "can I point to a specific bug."

Do NOT flag: pure documentation/comments/test files (unless the test reveals a logic gap), mechanical/generated code, trivial declaration-only changes, straightforward code whose only 'risk' is that you can't fully see its context.

Respond ONLY with valid JSON, no markdown fences, no other text:

{"files":[{"filename":"...","risky":true,"confidence":"low|medium|high","reason":"one sentence","evidence_quote":"exact substring from the diff, or empty string if not risky"}],"summary":"one sentence overview of the change"}
"""


CRITIQUE_SYSTEM_PROMPT = """You will be shown a list of risk claims made about a code change, each with the exact diff line quoted as evidence. Your job is NOT to re-judge whether the risk is real — it is to find unverified assumptions the claim's REASONING depends on.

A claim can quote a completely real line and still be wrong, if it silently assumes something about how a type, library, or framework behaves that isn't shown in the visible code.

Common categories:
- whether a lock/mutex type is recursive or reentrant
- whether a function is thread-safe, atomic, or idempotent by contract
- whether a callback runs synchronously or is deferred
- whether a default value/initializer has the effect assumed
- whether an inherited/overridden method preserves the base behavior described

For each claim, ask:

"Does this reasoning depend on a specific behavioral property of a type/library/framework that is NOT directly visible in the diff or file content I was given?"

If yes, state that exact property as a short, checkable assumption.

If the reasoning is fully self-contained in the visible code with no outside assumption, return an empty string for that file.

Do not soften or second-guess claims that don't rely on an outside assumption — only surface genuine unverified dependencies.

Respond ONLY with valid JSON, no markdown fences, no other text:

{"critiques":[{"filename":"...","unverified_assumption":"short specific assumption, or empty string if none"}]}
"""


# ---------------------------------------------------------------------------
# Generic subprocess helper
# ---------------------------------------------------------------------------

def run_git(args, check=False):
    try:
        return subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            check=check,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------

def get_api_key(cli_key=None, hook_mode=False):
    if cli_key:
        return cli_key.strip()

    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key:
        return env_key.strip()

    if KEY_FILE.exists():
        try:
            stored = KEY_FILE.read_text(encoding="utf-8").strip()
            # Emniyet: Eğer dosyanın içine yanlışlıkla boşluklu git satırı yazıldıysa key sayma
            if stored and " " not in stored:
                return stored
        except Exception:
            pass

    # Hook modundayken ASLA input() çağırma! (Git'in stdin'ini bozar)
    if hook_mode or not sys.stdin.isatty():
        print(f"{YELLOW}[pr-check] GEMINI_API_KEY not set. Skipping pre-push check (push allowed).{RESET}")
        print(f"{DIM}Run 'pr-check' once manually in terminal to configure your API key.{RESET}")
        return None

    print(f"{YELLOW}GEMINI_API_KEY is not set.{RESET}")
    print(f"Get a free key at {BOLD}https://aistudio.google.com{RESET}")

    try:
        entered = input("Paste your Gemini API Key here (press Enter to cancel): ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)

    if not entered or " " in entered:
        print(f"{RED}No valid API key provided. Skipping check.{RESET}")
        return None

    try:
        KEY_FILE.write_text(entered, encoding="utf-8")
        if sys.platform != "win32":
            try:
                KEY_FILE.chmod(0o600)
            except OSError:
                pass
        print(f"{GREEN}Saved key to {KEY_FILE} for future runs.{RESET}\n")
    except Exception:
        pass

    return entered


# ---------------------------------------------------------------------------
# Git repository helpers
# ---------------------------------------------------------------------------

def is_git_repo():
    result = run_git(
        ["rev-parse", "--is-inside-work-tree"]
    )
    return bool(result and result.returncode == 0)


def get_git_root():
    result = run_git(
        ["rev-parse", "--show-toplevel"]
    )

    if result and result.returncode == 0:
        return Path(result.stdout.strip())

    return None


# ---------------------------------------------------------------------------
# Git hook installation
# ---------------------------------------------------------------------------

def install_git_hook():
    root = get_git_root()

    if not root:
        print(
            f"{RED}Error: Not inside a git repository. "
            f"Cannot install hook.{RESET}"
        )
        sys.exit(1)

    hooks_dir = root / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_file = hooks_dir / "pre-push"

    if getattr(sys, "frozen", False):
        exec_target = f'"{Path(sys.executable).resolve()}"'
    else:
        exec_target = (
            f'"{sys.executable}" '
            f'"{Path(__file__).resolve()}"'
        )

    hook_script = f"""#!/bin/sh
# pr-check automated pre-push gate

echo ""
echo "[pr-check] Running local pre-push risk analysis on outgoing commits..."

# Git passes the remote name and remote URL as arguments.
# The exact refs being pushed are passed through stdin.
{exec_target} --hook-mode "$1" "$2"

STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo ""
    echo "[pr-check] \\033[91mPush aborted due to HIGH confidence risks.\\033[0m"
    echo "[pr-check] Double check above, or bypass anytime with: \\033[1mgit push --no-verify\\033[0m"
    echo ""
    exit 1
fi

exit 0
"""

    try:
        hook_file.write_text(
            hook_script,
            encoding="utf-8",
            newline="\n",
        )

        if sys.platform != "win32":
            hook_file.chmod(0o755)

        print(
            f"{GREEN}{BOLD}"
            f"✓ Successfully installed pr-check pre-push hook!"
            f"{RESET}"
        )

        print(
            f"{DIM}Installed to: {hook_file}{RESET}\n"
        )

        print(f"{BOLD}How it works:{RESET}")
        print(
            f"  • Runs automatically whenever you type "
            f"'{BOLD}git push{RESET}'."
        )
        print(
            f"  • Analyzes the exact refs Git is attempting to push."
        )
        print(
            f"  • Only blocks push if {BOLD}"
            f"verified high confidence{RESET} risks are flagged."
        )
        print(
            f"  • Bypass anytime via: "
            f"{BOLD}git push --no-verify{RESET}"
        )
        print(
            f"  • {YELLOW}Privacy note:{RESET} outgoing diff and "
            f"relevant file content are sent to Gemini."
        )
        print(
            f"  • Never writes comments or changes anything on GitHub."
        )

        sys.exit(0)

    except Exception as e:
        print(
            f"{RED}Failed to install hook: {e}{RESET}"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Git push ref handling
# ---------------------------------------------------------------------------

ZERO_SHA = "0" * 40


def get_remote_default_branch(remote_name):
    """
    Try to resolve refs/remotes/<remote>/HEAD.
    Example:
        refs/remotes/origin/main
    """

    if not remote_name:
        return None

    result = run_git(
        [
            "symbolic-ref",
            f"refs/remotes/{remote_name}/HEAD",
        ]
    )

    if result and result.returncode == 0:
        ref = result.stdout.strip()

        if ref:
            return ref

    # Fallbacks for common default branch names.
    for branch in ("main", "master"):
        ref = f"refs/remotes/{remote_name}/{branch}"

        check = run_git(["rev-parse", "--verify", ref])

        if check and check.returncode == 0:
            return ref

    return None


def get_new_branch_base(local_sha, remote_name):
    """
    Find a sensible base for a newly-created remote branch.

    Priority:
      1. remote's default branch
      2. common remote branches
      3. first parent of local commit
    """

    default_ref = get_remote_default_branch(remote_name)

    if default_ref:
        merge_base = run_git(
            ["merge-base", local_sha, default_ref]
        )

        if merge_base and merge_base.returncode == 0:
            base = merge_base.stdout.strip()

            if base:
                return base

    # Try common branches directly.
    for branch in (
        f"refs/remotes/{remote_name}/main",
        f"refs/remotes/{remote_name}/master",
    ):
        merge_base = run_git(
            ["merge-base", local_sha, branch]
        )

        if merge_base and merge_base.returncode == 0:
            base = merge_base.stdout.strip()

            if base:
                return base

    # Last resort: previous commit.
    parent = run_git(
        ["rev-parse", f"{local_sha}^"]
    )

    if parent and parent.returncode == 0:
        return parent.stdout.strip()

    return None


def get_git_diff(staged_only=False, hook_mode=False, remote_name=None):
    """
    Returns:
        diff_text
        source_shas: filename -> local SHA whose file contents should be read

    In hook-mode, Git provides:

        <local ref> <local sha> <remote ref> <remote sha>

    through stdin.

    Normal push:
        remote_sha..local_sha

    New branch:
        merge-base(local_sha, remote default branch)..local_sha

    Deleted remote branch:
        skipped
    """

    if not hook_mode:
        if staged_only:
            result = run_git(["diff", "--cached"])
        else:
            result = run_git(["diff", "HEAD"])

        if not result or result.returncode != 0:
            return "", {}

        return result.stdout, {}

    diff_outputs = []
    source_shas = {}

    try:
        # IMPORTANT:
        # Read Git's hook stdin before running subprocesses.
        stdin_input = sys.stdin.read().strip()

        if not stdin_input:
            result = run_git(["diff", "HEAD~1..HEAD"])

            if not result or result.returncode != 0:
                return "", {}

            return result.stdout, {}

        for line in stdin_input.splitlines():
            parts = line.strip().split()

            if len(parts) < 4:
                continue

            local_ref, local_sha, remote_ref, remote_sha = parts[:4]

            # Remote branch deletion.
            if local_sha == ZERO_SHA:
                continue

            # Normal push.
            if remote_sha != ZERO_SHA:
                diff_range = f"{remote_sha}..{local_sha}"

            # New remote branch.
            else:
                base = get_new_branch_base(
                    local_sha,
                    remote_name,
                )

                if base:
                    diff_range = f"{base}..{local_sha}"
                else:
                    # Absolute last resort.
                    diff_range = f"{local_sha}^..{local_sha}"

            result = run_git(
                ["diff", diff_range]
            )

            if not result or result.returncode != 0:
                continue

            if result.stdout.strip():
                diff_outputs.append(result.stdout)

                # Extract filenames from this exact diff and associate
                # them with the commit that is actually being pushed.
                for fname in get_changed_filenames(result.stdout):
                    source_shas[fname] = local_sha

        return "\n\n".join(diff_outputs), source_shas

    except Exception:
        return "", {}


# ---------------------------------------------------------------------------
# Git diff parsing
# ---------------------------------------------------------------------------

def extract_changed_lines(diff_text):
    changed = []

    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue

        if line.startswith("+") or line.startswith("-"):
            changed.append(line[1:].strip())

    return changed


def get_changed_filenames(diff_text):
    fnames = []

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            fname = line[6:].strip()

            if fname and fname != "/dev/null":
                fnames.append(fname)

    return list(dict.fromkeys(fnames))


# ---------------------------------------------------------------------------
# Full file context
# ---------------------------------------------------------------------------

def git_show_file(sha, filename):
    """
    Read a file exactly as it exists in a pushed commit.
    """

    if not sha:
        return None

    result = run_git(
        ["show", f"{sha}:{filename}"]
    )

    if not result or result.returncode != 0:
        return None

    return result.stdout


def build_context(diff_text, source_shas=None):
    """
    Build Gemini context.

    In normal local mode:
        reads files from working tree.

    In hook mode:
        source_shas is populated and files are read from the exact
        commit being pushed, avoiding contamination from uncommitted
        working-tree changes.
    """

    source_shas = source_shas or {}

    filenames = get_changed_filenames(diff_text)

    parts = [
        "=== DIFF (what changed) ===\n"
        + diff_text
    ]

    full_file_chars_used = 0
    files_truncated = 0

    for fname in filenames:
        content = None

        # ---------------------------------------------------------------
        # Hook mode: exact pushed commit
        # ---------------------------------------------------------------

        if fname in source_shas:
            content = git_show_file(
                source_shas[fname],
                fname,
            )

        # ---------------------------------------------------------------
        # Normal local mode: working tree
        # ---------------------------------------------------------------

        else:
            try:
                size = os.path.getsize(fname)
            except OSError:
                continue

            if size > MAX_FULL_FILE_SIZE:
                continue

            try:
                with open(
                    fname,
                    "r",
                    encoding="utf-8",
                    errors="ignore",
                ) as f:
                    content = f.read()

            except OSError:
                continue

        if content is None:
            continue

        if len(content) > MAX_FULL_FILE_SIZE:
            continue

        if (
            full_file_chars_used + len(content)
            > MAX_FULL_FILE_CONTEXT_CHARS
        ):
            files_truncated += 1
            continue

        parts.append(
            f"\n=== FULL FILE (current pushed/local state): "
            f"{fname} ===\n{content}"
        )

        full_file_chars_used += len(content)

    if files_truncated:
        print(
            f"{YELLOW}"
            f"Large changeset: sent full file content for the first "
            f"files within budget, diff-only for the remaining "
            f"{files_truncated} file(s)."
            f"{RESET}"
        )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Evidence verification
# ---------------------------------------------------------------------------

def verify_evidence(files, diff_text, extra_valid_text=""):
    normalized_lines = [
        " ".join(line.split())
        for line in extract_changed_lines(diff_text)
    ]

    normalized_extra = " ".join(
        extra_valid_text.split()
    )

    for f in files:
        if not f.get("risky"):
            continue

        quote = (
            f.get("evidence_quote") or ""
        ).strip()

        if not quote:
            f["risky"] = False

            f["reason"] = (
                f.get("reason", "")
                + " [demoted: no evidence quote provided]"
            )

            continue

        normalized_quote = " ".join(
            quote.split()
        )

        found_in_diff = any(
            normalized_quote in line
            for line in normalized_lines
        )

        found_in_description = (
            normalized_quote in normalized_extra
            if normalized_extra
            else False
        )

        if not found_in_diff and not found_in_description:
            f["risky"] = False

            f["reason"] = (
                f.get("reason", "")
                + " [demoted: evidence quote not found in "
                "a single changed diff line or PR description]"
            )

    return files


# ---------------------------------------------------------------------------
# GitHub PR handling
# ---------------------------------------------------------------------------

def fetch_github_pr(pr_url):
    import re

    m = re.match(
        r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)",
        pr_url,
    )

    if not m:
        print(
            f"{RED}"
            f"Invalid PR URL. Expected: "
            f"https://github.com/owner/repo/pull/1234"
            f"{RESET}"
        )
        sys.exit(1)

    owner, repo, num = m.groups()

    api_base = (
        f"https://api.github.com/repos/"
        f"{owner}/{repo}"
    )

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "pr-check-cli",
    }

    def gh_get(url):
        req = urllib.request.Request(
            url,
            headers=headers,
        )

        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(
                    resp.read().decode("utf-8")
                )

        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8")[:300]
            except Exception:
                body = str(e)

            print(
                f"{RED}"
                f"GitHub API error ({e.code}): {body}"
                f"{RESET}"
            )
            sys.exit(1)

        except urllib.error.URLError as e:
            print(
                f"{RED}"
                f"GitHub connection error: {e}"
                f"{RESET}"
            )
            sys.exit(1)

    # ---------------------------------------------------------------
    # PR metadata
    # ---------------------------------------------------------------

    pr_data = gh_get(
        f"{api_base}/pulls/{num}"
    )

    head_sha = (
        pr_data.get("head", {})
        .get("sha", "")
    )

    author_association = pr_data.get(
        "author_association",
        "UNKNOWN",
    )

    is_external = author_association in (
        "NONE",
        "FIRST_TIME_CONTRIBUTOR",
        "FIRST_TIMER",
        "CONTRIBUTOR",
    )

    assoc_note = (
        f"{GREEN}"
        f"(external contributor — zero comments is a stronger "
        f"'nothing to find' signal)"
        f"{RESET}"
        if is_external
        else
        f"{YELLOW}"
        f"(author association: {author_association} — zero comments "
        f"may just mean trusted/commit-access author, not "
        f"'nothing to find')"
        f"{RESET}"
    )

    print(
        f"{DIM}"
        f"Author association: {author_association}. "
        f"{assoc_note}"
        f"{RESET}"
    )

    # ---------------------------------------------------------------
    # Fetch ALL PR files through pagination
    # ---------------------------------------------------------------

    files_data = []

    page = 1

    while True:
        page_data = gh_get(
            f"{api_base}/pulls/{num}/files"
            f"?per_page=100&page={page}"
        )

        if not isinstance(page_data, list):
            break

        files_data.extend(page_data)

        if len(page_data) < 100:
            break

        page += 1

    if len(files_data) >= 100:
        print(
            f"{DIM}"
            f"GitHub PR contains {len(files_data)} changed files."
            f"{RESET}"
        )

    # ---------------------------------------------------------------
    # Build context
    # ---------------------------------------------------------------

    parts = [
        "=== PR TITLE ===\n"
        + pr_data.get("title", "")
        + "\n\n=== PR DESCRIPTION ===\n"
        + (pr_data.get("body") or "")[:1500]
    ]

    diff_chunks = []

    full_file_chars_used = 0
    files_truncated = 0

    for f in files_data:
        fname = f.get("filename", "")

        patch = f.get(
            "patch",
            "(binary or too large to diff)",
        )

        diff_chunks.append(
            f"--- a/{fname}\n"
            f"+++ b/{fname}\n"
            f"{patch}"
        )

        if (
            full_file_chars_used
            >= MAX_FULL_FILE_CONTEXT_CHARS
        ):
            files_truncated += 1
            continue

        if f.get("status") == "removed":
            continue

        if f.get("changes", 0) >= 2000:
            continue

        # Encode filename safely while preserving directory separators.
        encoded_fname = urllib.parse.quote(
            fname,
            safe="/",
        )

        raw_url = (
            f"https://raw.githubusercontent.com/"
            f"{owner}/{repo}/{head_sha}/{encoded_fname}"
        )

        try:
            req = urllib.request.Request(
                raw_url,
                headers={
                    "User-Agent": "pr-check-cli",
                },
            )

            with urllib.request.urlopen(req) as resp:
                content = resp.read().decode(
                    "utf-8",
                    errors="ignore",
                )

            if len(content) > MAX_FULL_FILE_SIZE:
                continue

            if (
                full_file_chars_used
                + len(content)
                <= MAX_FULL_FILE_CONTEXT_CHARS
            ):
                parts.append(
                    f"\n=== FULL FILE (PR head state): "
                    f"{fname} ===\n{content}"
                )

                full_file_chars_used += len(content)

            else:
                files_truncated += 1

        except Exception:
            continue

    if files_truncated:
        print(
            f"{YELLOW}"
            f"Large PR: sent full file content for files within "
            f"the context budget, diff-only for the remaining "
            f"{files_truncated} file(s)."
            f"{RESET}"
        )

    diff_only_text = "\n\n".join(
        diff_chunks
    )

    description_text = (
        f"{pr_data.get('title', '')}\n"
        f"{pr_data.get('body') or ''}"
    )

    parts.insert(
        1,
        "=== DIFF (what changed) ===\n"
        + diff_only_text,
    )

    return (
        "\n".join(parts),
        diff_only_text,
        description_text,
    )


# ---------------------------------------------------------------------------
# Gemini API
# ---------------------------------------------------------------------------

def _call_gemini(
    text,
    system_prompt,
    api_key,
    max_output_tokens=8192,
    max_retries=5,
):
    url = (
        f"https://generativelanguage.googleapis.com/"
        f"v1beta/models/{MODEL_NAME}:generateContent"
        f"?key={api_key}"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": text
                    }
                ]
            }
        ],
        "systemInstruction": {
            "parts": [
                {
                    "text": system_prompt
                }
            ]
        },
        "generationConfig": {
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
        },
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json"
        },
        method="POST",
    )

    transient_codes = {
        429,
        500,
        502,
        503,
        504,
    }

    backoff_steps = [
        2,
        4,
        8,
        12,
        16,
    ]

    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(
                    resp.read().decode("utf-8")
                )

            break

        except urllib.error.HTTPError as e:
            code = getattr(e, "code", 503)

            try:
                body = e.read().decode(
                    "utf-8",
                    errors="ignore",
                )
            except Exception:
                body = str(e)

            if (
                code not in transient_codes
                or attempt == max_retries
            ):
                print(
                    f"{YELLOW}"
                    f"[pr-check] Gemini API unavailable "
                    f"({code}). Skipping check "
                    f"(push allowed)."
                    f"{RESET}"
                )
                return None

            wait = (
                backoff_steps[attempt]
                if attempt < len(backoff_steps)
                else 15
            )

            print(
                f"{YELLOW}"
                f"{MODEL_NAME} capacity busy ({code}), "
                f"waiting {wait}s... "
                f"(attempt {attempt + 1}/{max_retries})"
                f"{RESET}"
            )

            time.sleep(wait)

        except urllib.error.URLError as e:
            if attempt == max_retries:
                print(
                    f"{YELLOW}"
                    f"[pr-check] Gemini connection unavailable. "
                    f"Skipping check (push allowed)."
                    f"{RESET}"
                )
                return None

            wait = (
                backoff_steps[attempt]
                if attempt < len(backoff_steps)
                else 15
            )

            print(
                f"{YELLOW}"
                f"Gemini connection problem, waiting {wait}s..."
                f"{RESET}"
            )

            time.sleep(wait)

        except Exception as e:
            print(
                f"{YELLOW}"
                f"[pr-check] Unexpected Gemini error: {e}. "
                f"Skipping check (push allowed)."
                f"{RESET}"
            )
            return None

    else:
        print(
            f"{YELLOW}"
            f"[pr-check] Gemini unavailable after retries. "
            f"Skipping check (push allowed)."
            f"{RESET}"
        )
        return None

    # ---------------------------------------------------------------
    # Validate Gemini response
    # ---------------------------------------------------------------

    if not isinstance(data, dict):
        print(
            f"{YELLOW}"
            f"[pr-check] Invalid Gemini response. "
            f"Skipping check (push allowed)."
            f"{RESET}"
        )
        return None

    candidates = data.get("candidates", [])

    if not candidates:
        print(
            f"{YELLOW}"
            f"[pr-check] Gemini returned no candidates. "
            f"Skipping check (push allowed)."
            f"{RESET}"
        )
        return None

    first_candidate = candidates[0]

    if not isinstance(first_candidate, dict):
        print(
            f"{YELLOW}"
            f"[pr-check] Invalid Gemini candidate. "
            f"Skipping check (push allowed)."
            f"{RESET}"
        )
        return None

    content = first_candidate.get(
        "content",
        {},
    )

    if not isinstance(content, dict):
        print(
            f"{YELLOW}"
            f"[pr-check] Gemini returned invalid content. "
            f"Skipping check (push allowed)."
            f"{RESET}"
        )
        return None

    response_parts = content.get(
        "parts",
        [],
    )

    raw = ""

    for part in response_parts:
        if not isinstance(part, dict):
            continue

        if part.get("thought"):
            continue

        part_text = part.get("text", "")

        if part_text:
            raw += part_text

    raw = raw.strip()

    # Remove accidental markdown fences.
    raw = raw.replace(
        "```json",
        "",
    ).replace(
        "```",
        "",
    ).strip()

    # Try extracting JSON object.
    start = raw.find("{")
    end = raw.rfind("}")

    if (
        start != -1
        and end != -1
        and end > start
    ):
        json_candidate = raw[
            start:end + 1
        ]

        try:
            parsed = json.loads(
                json_candidate
            )

            if isinstance(parsed, dict):
                return parsed

        except json.JSONDecodeError:
            pass

    try:
        parsed = json.loads(raw)

        if isinstance(parsed, dict):
            return parsed

    except json.JSONDecodeError:
        pass

    print(
        f"{YELLOW}"
        f"[pr-check] Could not parse model response as JSON. "
        f"Skipping check (push allowed)."
        f"{RESET}"
    )

    return None


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze(context_text, api_key):
    return _call_gemini(
        context_text,
        SYSTEM_PROMPT,
        api_key,
        max_output_tokens=8192,
    )


def critique_reasoning(risky_files, api_key):
    if not risky_files:
        return {}

    claims_text = "\n".join(
        f"- {f.get('filename', '')}: "
        f"{f.get('reason', '')} "
        f"(quoted: "
        f"\"{f.get('evidence_quote', '')}\")"
        for f in risky_files
    )

    result = _call_gemini(
        claims_text,
        CRITIQUE_SYSTEM_PROMPT,
        api_key,
        max_output_tokens=2000,
    )

    if not result:
        return {}

    critiques = result.get(
        "critiques",
        [],
    )

    if not isinstance(critiques, list):
        return {}

    return {
        c.get("filename", ""):
        (
            c.get("unverified_assumption")
            or ""
        ).strip()
        for c in critiques
        if isinstance(c, dict)
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_results(result):
    print()

    print(
        f"{BOLD}"
        f"{result.get('summary', '')}"
        f"{RESET}"
    )

    print()

    files = result.get(
        "files",
        [],
    )

    if not isinstance(files, list):
        files = []

    risky = [
        f
        for f in files
        if isinstance(f, dict)
        and f.get("risky")
    ]

    safe = [
        f
        for f in files
        if isinstance(f, dict)
        and not f.get("risky")
    ]

    for f in risky:
        confidence = f.get(
            "confidence",
            "medium",
        )

        color = (
            RED
            if confidence == "high"
            else YELLOW
        )

        icon = (
            "\U0001f534"
            if confidence == "high"
            else "\U0001f7e1"
        )

        print(
            f"{color}"
            f"{icon} "
            f"{f.get('filename', '(unknown file)')}"
            f"{RESET}"
        )

        print(
            f"   {f.get('reason', '')}"
        )

        if f.get("evidence_quote"):
            print(
                f"   {DIM}"
                f"\u21b3 \"{f['evidence_quote']}\""
                f"{RESET}"
            )

        if f.get("unverified_assumption"):
            print(
                f"   {YELLOW}"
                f"\u26a0 Assumes: "
                f"{f['unverified_assumption']} "
                f"— verify before trusting this flag"
                f"{RESET}"
            )

        print()

    if safe:
        safe_names = [
            f.get(
                "filename",
                "(unknown)",
            )
            for f in safe
        ]

        print(
            f"{GREEN}"
            f"{DIM}"
            f"Low risk: {', '.join(safe_names)}"
            f"{RESET}"
        )

        print()

    if risky:
        print(
            f"{BOLD}"
            f"{len(risky)} file(s) worth a second look "
            f"before you push."
            f"{RESET}"
        )
    else:
        print(
            f"{GREEN}"
            f"{BOLD}"
            f"Nothing flagged. Looks clean."
            f"{RESET}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Local, private PR risk check."
    )

    parser.add_argument(
        "--staged",
        action="store_true",
        help="Only analyze staged changes",
    )

    parser.add_argument(
        "--pr",
        type=str,
        default=None,
        help="GitHub PR URL to analyze instead of local git diff",
    )

    parser.add_argument(
        "--key",
        type=str,
        default=None,
        help="Gemini API Key",
    )

    parser.add_argument(
        "--install-hook",
        action="store_true",
        help="Install pr-check as a git pre-push hook",
    )

    parser.add_argument(
        "--hook-mode",
        action="store_true",
        help="Internal flag used by the git pre-push hook",
    )

    # Hook receives:
    #   argv[1] = remote name
    #   argv[2] = remote URL
    parser.add_argument(
        "hook_remote_name",
        nargs="?",
        default=None,
        help=argparse.SUPPRESS,
    )

    parser.add_argument(
        "hook_remote_url",
        nargs="?",
        default=None,
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args()

    # ---------------------------------------------------------------
    # Install hook
    # ---------------------------------------------------------------

    if args.install_hook:
        install_git_hook()

    # ---------------------------------------------------------------
    # API key
    # ---------------------------------------------------------------

    api_key = get_api_key(args.key, hook_mode=args.hook_mode)
    if not api_key:
        sys.exit(0)

    # ---------------------------------------------------------------
    # Repository check
    # ---------------------------------------------------------------

    if (
        not args.pr
        and not is_git_repo()
    ):
        print(
            f"{YELLOW}"
            f"No active git repository detected in this directory."
            f"{RESET}"
        )

        try:
            entered_pr = input(
                "Enter a GitHub PR URL to analyze "
                "(or press Enter to exit): "
            ).strip()

        except (
            KeyboardInterrupt,
            EOFError,
        ):
            print()
            sys.exit(0)

        if entered_pr:
            args.pr = entered_pr
        else:
            sys.exit(0)

    # ---------------------------------------------------------------
    # Fetch / construct analysis context
    # ---------------------------------------------------------------

    pr_description = ""

    if args.pr:
        print(
            f"{DIM}"
            f"Fetching {args.pr} from GitHub..."
            f"{RESET}"
        )

        (
            context_text,
            diff_text,
            pr_description,
        ) = fetch_github_pr(
            args.pr
        )

    else:
        (
            diff_text,
            source_shas,
        ) = get_git_diff(
            staged_only=args.staged,
            hook_mode=args.hook_mode,
            remote_name=args.hook_remote_name,
        )

        if (
            not diff_text
            or not diff_text.strip()
        ):
            if not args.hook_mode:
                print(
                    f"{GREEN}"
                    f"No changes detected."
                    f"{RESET}"
                )

            sys.exit(0)

        context_text = build_context(
            diff_text,
            source_shas=source_shas,
        )

    # ---------------------------------------------------------------
    # Gemini analysis
    # ---------------------------------------------------------------

    print(
        f"{DIM}"
        f"Analyzing changes — sending to "
        f"{MODEL_NAME}..."
        f"{RESET}"
    )

    result = analyze(
        context_text,
        api_key,
    )

    # Fail-open if API is unavailable.
    if not result:
        sys.exit(0)

    # ---------------------------------------------------------------
    # Evidence verification
    # ---------------------------------------------------------------

    files = result.get(
        "files",
        [],
    )

    if not isinstance(files, list):
        files = []

    result["files"] = verify_evidence(
        files,
        diff_text,
        extra_valid_text=pr_description,
    )

    # ---------------------------------------------------------------
    # Second-pass reasoning critique
    # ---------------------------------------------------------------

    still_risky = [
        f
        for f in result["files"]
        if isinstance(f, dict)
        and f.get("risky")
    ]

    if still_risky:
        print(
            f"{DIM}"
            f"Checking flagged files for unverified assumptions..."
            f"{RESET}"
        )

        assumptions = critique_reasoning(
            still_risky,
            api_key,
        )

        for f in result["files"]:
            if not isinstance(f, dict):
                continue

            filename = f.get(
                "filename",
                "",
            )

            assumption = assumptions.get(
                filename,
                "",
            )

            if assumption:
                f["unverified_assumption"] = assumption

                # A risk depending on an outside library/framework
                # assumption cannot block a push.
                if f.get("confidence") == "high":
                    f["confidence"] = "medium"

    # ---------------------------------------------------------------
    # Print
    # ---------------------------------------------------------------

    print_results(result)

    # ---------------------------------------------------------------
    # Pre-push gate
    # ---------------------------------------------------------------

    final_risks = [
        f
        for f in result["files"]
        if isinstance(f, dict)
        and f.get("risky")
    ]

    high_risks = [
        f
        for f in final_risks
        if f.get("confidence") == "high"
        and not f.get("unverified_assumption")
        and f.get("evidence_quote")
    ]

    # ONLY block in hook mode.
    #
    # A manual `pr-check` run can show high risks but never blocks.
    if args.hook_mode and high_risks:
        print()
        print(
            f"{RED}"
            f"{BOLD}"
            f"[pr-check] HIGH confidence risk detected. "
            f"Push blocked."
            f"{RESET}"
        )

        sys.exit(1)

    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()