# pr-check

A local, private pre-push code review assistant. It reads your git diff (or a GitHub PR URL), sends it to Gemini for risk analysis, and prints which changed files are actually worth a second look before you commit or push.

It never touches GitHub or your PR — it only reads. That said, the diff and file contents do leave your machine over the network to reach Gemini's API. Check Google's current data retention policy for the free tier before pointing this at anything sensitive.

## Why

Automated AI comments on GitHub PRs are broadly considered spam by maintainers. This tool exists to give you that feedback *before* you push — the one point in the loop where a human is still genuinely willing to be interrupted — instead of adding more noise to the PR itself.

Every flag requires an exact quote from the added/removed lines of the diff as evidence. If the model can't point to a real line supporting its reasoning, the flag is demoted automatically — this cuts down on the model just making things up.

## Requirements

- Python 3.8+
- `git` (only needed for local-diff mode, not for `--pr` mode)
- **No `pip install` needed** — the script only uses the Python standard library.

## Setup

1. Get a free Gemini API key (no credit card required): go to [aistudio.google.com](https://aistudio.google.com) → "Get API key" → Create API key.
2. Set it as an environment variable:

   **Windows (PowerShell):**
   ```
   $env:GEMINI_API_KEY="AIza..."
   ```
   **Windows (Command Prompt):**
   ```
   set GEMINI_API_KEY=AIza...
   ```
   **macOS / Linux:**
   ```
   export GEMINI_API_KEY="AIza..."
   ```
   This only lasts for the current terminal session — you'll need to set it again in a new window, or add it to your shell profile / a `.env` loader if you want it to persist.

## Usage

```
python pr_check.py                 # analyze unstaged + staged local changes
python pr_check.py --staged        # only staged changes
python pr_check.py --pr <url>      # analyze a GitHub PR by URL instead of local git
```

Example:
```
python pr_check.py --pr https://github.com/godotengine/godot/pull/84241
```

## Example output

```
Prevents crashes on invalid rendering_method settings by defaulting to Forward+ renderer.

Low risk: servers/rendering/renderer_rd/renderer_compositor_rd.cpp
Nothing flagged. Looks clean.
```

or, when something needs a second look:

```
🟡 modules/gdscript/language_server/gdscript_text_document.cpp
   Alters thread synchronization by deferring script reloading to the main thread.
   ↳ "callable_mp(this, &GDScriptTextDocument::reload_script).call_deferred(scr);"

1 file(s) worth a second look before you push.
```

## Known limitations

This has been tested against a small batch of real merged Godot PRs, not a large benchmark — treat it as an early-stage tool, not a proven one.

- **Evidence quotes are verified, reasoning is not.** The tool checks that every quoted line genuinely comes from the diff (not fabricated, not pulled from unchanged context) — but it can't yet verify that the *reasoning* built on a real quote is technically correct. In one test, it correctly quoted a real line but incorrectly concluded it caused a deadlock, because it assumed a non-recursive mutex where the actual type was recursive. A true quote does not guarantee a true conclusion.
- It's currently better at catching structural/concurrency risk than logic-vs-intent mismatches (code that contradicts its own docstring), though sending full file content alongside the diff (rather than just the diff hunk) has measurably helped with this.
- No packaging yet — this is a single script, not a pip-installable package.

## How it works (short version)

1. Gathers the diff plus the full current content of each changed file (a diff hunk alone can't show a docstring several lines above the change that the edit now contradicts).
2. Sends it to Gemini with a system prompt tuned against real merged PRs, including explicit rules against common false-positive patterns (keyword-triggered security flags, business-logic bias over config/wiring changes).
3. For every flag, verifies in code (not by trusting the model's self-report) that the quoted evidence is an actual added/removed line in the diff.
4. Prints only what's worth your attention — clean files are summarized, not narrated.
