# pr-check

Unlike generic LLM wrappers, pr-check enforces a strict programmatic evidence-grounding layer (every risk flag requires an exact diff quote verified in code) followed by an adversarial second pass that isolates unverified external type assumptions. Runs 100% locally via git pre-push hook.

A local, private pre-push code review assistant. It reads your outgoing git commits (or a public GitHub PR URL), sends diffs plus full file context to Gemini for risk analysis, and flags which files carry real risk before you push.

**Never writes to GitHub or posts comments on PRs.** In `--pr` mode, it only reads public diffs via the GitHub API. 

Check Google's current data retention policy for the free tier before pointing this at anything sensitive.

---

## Quickstart (Try it in 2 minutes)

### Windows (No Python required)
1. Download `pr-check-windows-x64.zip` from [Releases](https://github.com/Spacexcx/pr-check/releases) and extract `pr-check.exe`.
2. Open terminal in any git repository and run:
   ```cmd
   pr-check.exe --install-hook
That's it! Every time you type git push, your outgoing commits are automatically verified.
Clean code passes silently.
High-confidence verified risks intercept the push.
Bypass anytime with: git push --no-verify
(Linux / macOS users can run python3 pr_check.py --install-hook with zero dependencies).
Why
Automated AI bot comments on GitHub PRs are broadly considered spam by maintainers. This tool exists to give you private feedback before you push — the one point in the loop where a human is still genuinely willing to be interrupted — instead of adding noise to public PR threads.
Every flag requires an exact quote from the added/removed lines of the diff as evidence. If the model can't point to a real line supporting its reasoning, the flag is demoted automatically.
Installation & Setup
Standalone Executable (Windows)
Download pr-check-windows-x64.zip from Releases, extract pr-check.exe, and drop it anywhere in your PATH.
From Source (Linux / macOS / Windows)
Requirements: Python 3.8+ and git.
Standard library only — no pip install required:
code
Bash
git clone https://github.com/Spacexcx/pr-check.git
cd pr-check
python3 pr_check.py --help
API Key (Free, No Credit Card)
Get a free Gemini API key at aistudio.google.com.
Interactive: Run the tool once; it will prompt and save locally to ~/.pr_check_key.
Command Line: Pass --key "AIza...".
Environment: export GEMINI_API_KEY="AIza..." or $env:GEMINI_API_KEY="AIza...".
Usage
code
Bash
# Automated Git Hook (Recommended)
pr-check.exe --install-hook

# Manual Checks
pr-check.exe                       # analyze unstaged + staged local changes
pr-check.exe --staged              # only staged changes
pr-check.exe --pr <url>            # analyze a public GitHub PR without cloning
Example Output
When a change is safe:
code
Text
Prevents crashes on invalid rendering_method settings by defaulting to Forward+ renderer.

Low risk: servers/rendering/renderer_rd/renderer_compositor_rd.cpp
Nothing flagged. Looks clean.
When a file warrants a closer look:
code
Text
🟡 modules/gdscript/language_server/gdscript_text_document.cpp
   Alters thread synchronization by deferring script reloading to the main thread.
   ↳ "callable_mp(this, &GDScriptTextDocument::reload_script).call_deferred(scr);"

1 file(s) worth a second look before you push.
How It Works (Epistemic Risk Gate)
Exact Outgoing Diffs: In hook mode, reads Git's stdin ref stream (<remote_sha>..<local_sha>) to inspect the exact commits being pushed. Reads file contents directly from the pushed commit SHA via git show, preventing uncommitted working-tree edits from contaminating the context.
Context Building: Combines the diff with full file contents (capped at 300,000 characters) to catch contradictions between modified logic and docstrings/signatures above or below the diff hunk.
Evidence Grounding: Programmatically checks that every reported evidence_quote matches a changed diff line verbatim. Any flag failing this is demoted immediately.
Adversarial Critique Pass: For remaining flagged files, a second pass isolates unverified assumptions about outside libraries/types (e.g. assuming a mutex type is non-recursive).
If an unverified outside assumption is detected, the risk is demoted to medium (warns the user without blocking git push).
Only high-confidence, fully self-contained risks block the push.
Graceful Fail-Open: If the Gemini API experiences downtime (503), rate limits, or network drops, pr-check prints a warning but allows the push to proceed. You are never trapped by third-party downtime.
