# pr-check

A local, private pre-push code review assistant. It reads your git diff (or a GitHub PR URL), sends it to Gemini for risk analysis, and prints which changed files are actually worth a second look before you commit or push.

It never touches GitHub or your PR — it only reads. That said, the diff and file contents do leave your machine over the network to reach Gemini's API. Check Google's current data retention policy for the free tier before pointing this at anything sensitive.

---

## Why

Automated AI comments on GitHub PRs are broadly considered spam by maintainers. This tool exists to give you that feedback before you push — the one point in the loop where a human is still genuinely willing to be interrupted — instead of adding more noise to the PR itself.

Every flag requires an exact quote from the added/removed lines of the diff as evidence. If the model can't point to a real line supporting its reasoning, the flag is demoted automatically — this cuts down on the model just making things up.

---

## Installation

### Windows (No Python required)
1. Download `pr-check-windows-x64.zip` from the latest [Releases](https://github.com/Spacexcx/pr-check/releases).
2. Extract `pr-check.exe` anywhere you like (or drop it into your system PATH).
3. Run it directly from PowerShell or Command Prompt:
   ```cmd
   pr-check.exe --pr <url>
Linux / macOS (or from source)
Requirements: Python 3.8+ and git (only needed for local git diffs, not for --pr).
No dependencies: Standard library only — no pip install required.
Clone and run directly:
code
Bash
git clone https://github.com/Spacexcx/pr-check.git
cd pr-check
python3 pr_check.py --help
Setup (Gemini API Key)
Get a free Gemini API key (no credit card required) at aistudio.google.com → Get API key → Create API key.
You have three ways to provide it:
Interactive (simplest): Just run the tool. If no key is detected, it will prompt you once and save it locally to ~/.pr_check_key so you don't have to enter it again.
Command line argument: Pass --key "AIza..." directly.
Environment variable:
Windows (PowerShell): $env:GEMINI_API_KEY="AIza..."
Windows (CMD): set GEMINI_API_KEY=AIza...
macOS / Linux: export GEMINI_API_KEY="AIza..."
Usage
Using the standalone executable (Windows):
code
Cmd
pr-check.exe                       # analyze unstaged + staged local git changes
pr-check.exe --staged              # only staged changes
pr-check.exe --pr <url>            # analyze a public GitHub PR without cloning
Using Python (Linux / macOS / Source):
code
Bash
python pr_check.py                 # analyze unstaged + staged local git changes
python pr_check.py --staged        # only staged changes
python pr_check.py --pr <url>      # analyze a public GitHub PR without cloning
Example:
code
Bash
pr-check.exe --pr https://github.com/godotengine/godot/pull/84241
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
Known Limitations
Early-stage testing: Tested across real merged PRs from Godot (C++), React (JS), PyTorch (Python), OpenTelemetry (Go), and Apache RocketMQ (Java). Treat it as an early-stage tool, not an infallible auditor.
Verified evidence vs. unverified reasoning: The tool programmatically asserts that every quoted line genuinely comes from the added/removed lines in the diff (not fabricated or pulled from unchanged context). However, a genuine quote doesn't guarantee a sound conclusion. In one test, it correctly quoted a line but inferred a deadlock by assuming a mutex was non-recursive when it was actually recursive. A second critique pass specifically asks the model to isolate unverified behavioral assumptions:
code
Text
⚠ Assumes: the Mutex type used here is non-recursive — verify before trusting this flag
Context budget on large PRs: Diff lines are always sent in full. Extra full-file context is capped by a total budget (300,000 chars). Files exceeding the budget fall back to diff-only with a visible warning rather than silently truncating or breaking the API call.
Packaging: Standalone binary is currently provided for Windows x64. macOS and Linux run via pr_check.py without package installation.
How It Works
Context building: Collects the git diff plus full current file contents for context (e.g., catching changes that invert logic documented in an earlier docstring).
Analysis pass: Evaluates risk via gemini-3.6-flash, tuned with strict negative rules against keyword bias (e.g., refactors simply touching "crypto" or "auth" paths are not flagged unless a concrete behavioral diff is shown).
Deterministic grounding: Programmatically checks that every reported evidence_quote matches a changed diff line verbatim. Any flag failing this is demoted immediately.
Adversarial critique pass: For remaining flagged files, runs a second pass isolating unverified framework/type assumptions and surfaces them explicitly.
Resilience: Retries transient API errors (429, 500, 503) with exponential backoff (1s, 2s, 4s).
