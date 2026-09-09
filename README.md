pr-check

A local, private pre-push code review assistant. It reads your git diff (or a GitHub PR URL), sends it to Gemini for risk analysis, and prints which changed files are actually worth a second look before you commit or push.

It never touches GitHub or your PR — it only reads. That said, the diff and file contents do leave your machine over the network to reach Gemini's API. Check Google's current data retention policy for the free tier before pointing this at anything sensitive.

Why

Automated AI comments on GitHub PRs are broadly considered spam by maintainers. This tool exists to give you that feedback before you push — the one point in the loop where a human is still genuinely willing to be interrupted — instead of adding more noise to the PR itself.

Every flag requires an exact quote from the added/removed lines of the diff as evidence. If the model can't point to a real line supporting its reasoning, the flag is demoted automatically — this cuts down on the model just making things up.

Requirements
Python 3.8+
git (only needed for local-diff mode, not for --pr mode)
No pip install needed — the script only uses the Python standard library.
Setup
Get a free Gemini API key (no credit card required): go to aistudio.google.com → "Get API key" → Create API key.
Set it as an environment variable: Windows (PowerShell):
   $env:GEMINI_API_KEY="AIza..."

Windows (Command Prompt):

   set GEMINI_API_KEY=AIza...

macOS / Linux:

   export GEMINI_API_KEY="AIza..."

This only lasts for the current terminal session — you'll need to set it again in a new window, or add it to your shell profile / a .env loader if you want it to persist.

Usage
python pr_check.py                 # analyze unstaged + staged local changes
python pr_check.py --staged        # only staged changes
python pr_check.py --pr <url>      # analyze a GitHub PR by URL instead of local git

Example:

python pr_check.py --pr https://github.com/godotengine/godot/pull/84241
Example output
Prevents crashes on invalid rendering_method settings by defaulting to Forward+ renderer.

Low risk: servers/rendering/renderer_rd/renderer_compositor_rd.cpp
Nothing flagged. Looks clean.

or, when something needs a second look:

🟡 modules/gdscript/language_server/gdscript_text_document.cpp
   Alters thread synchronization by deferring script reloading to the main thread.
   ↳ "callable_mp(this, &GDScriptTextDocument::reload_script).call_deferred(scr);"

1 file(s) worth a second look before you push.
Known limitations

This has been tested against a small batch of real merged Godot PRs, not a large benchmark — treat it as an early-stage tool, not a proven one.

Evidence quotes are verified, reasoning is not fully. The tool checks that every quoted line genuinely comes from the diff (not fabricated, not pulled from unchanged context) — but a true quote doesn't guarantee a true conclusion. In one test, it correctly quoted a real line but incorrectly concluded it caused a deadlock, because it assumed a non-recursive mutex where the actual type was recursive. A second pass now targets exactly this gap: for every file still flagged after evidence verification, the model is asked to inspect its own reasoning for unverified assumptions about type/library/framework behavior. Re-run against the deadlock case above, it correctly surfaced: ⚠ Assumes: the Mutex type used here is non-recursive — verify before trusting this flag. This doesn't reject the flag automatically — it can't confirm the assumption is wrong, only that it's unverified — so it surfaces the assumption instead of silently trusting or silently discarding it.
It's currently better at catching structural/concurrency risk than logic-vs-intent mismatches (code that contradicts its own docstring), though sending full file content alongside the diff (rather than just the diff hunk) has measurably helped with this.
Large PRs (many changed files) degrade gracefully rather than breaking. Full file content is sent for extra context, but that's capped by a total character budget (not just a per-file size cap) — once a PR has enough files to exceed it, remaining files fall back to diff-only instead of growing the request unbounded. This is printed as a visible warning, not silent. Tested against a real 30-file PR (react/react#28711) with no issues; haven't yet found a real 40+ file PR to push past the budget itself, so that specific edge is simulated rather than field-tested.
No packaging yet — this is a single script, not a pip-installable package.
How it works (short version)
Gathers the diff plus the full current content of each changed file (a diff hunk alone can't show a docstring several lines above the change that the edit now contradicts).
Sends it to Gemini with a system prompt tuned against real merged PRs, including explicit rules against common false-positive patterns (keyword-triggered security flags, business-logic bias over config/wiring changes).
For every flag, verifies in code (not by trusting the model's self-report) that the quoted evidence is an actual added/removed line in the diff.
For flags that survive that check, asks the model a second, adversarial question: what unverified assumption about type/library/framework behavior does this reasoning depend on? Surfaces the answer as a warning rather than silently trusting or discarding the flag.
Prints only what's worth your attention — clean files are summarized, not narrated.
If Gemini is temporarily overloaded (503/429/500-504), automatically retries with backoff (1s/2s/4s) before giving up — a busy server isn't treated the same as a bad request.
