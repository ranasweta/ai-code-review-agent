# 🎬 Demo recording script

A tight, ~60-second walkthrough that shows the agent doing something real:
fetching a PR, *deciding* what to review, running tools + LLM, and producing a
scored, structured report. Follow it top to bottom while recording.

---

## Before you record

- [ ] Keys are set (`.env` has `GITHUB_TOKEN` + `GEMINI_API_KEY`).
- [ ] App runs clean: `streamlit run app.py` → sidebar shows ✅ for both keys.
- [ ] Pick a **small** public PR (≈2–6 changed files) so the review finishes in
      under a minute. Good candidates (small, real, merged):
  - `https://github.com/pallets/click/pull/3526`
  - `https://github.com/pallets/click/pull/3528`
  - …or any small PR you like. Have a backup URL ready in case one is slow.
- [ ] Zoom your browser to ~110–125% so text is legible in the GIF.
- [ ] Close noisy tabs/notifications.

---

## The recording (step by step)

1. **Open on the landing screen.** Let the title "🔍 AI Code Review Agent" and
   the sidebar (provider = Gemini, ✅ key status) sit for ~1s.
2. **Paste the PR URL** into the input box. (Pre-copy it so there's no typing.)
3. **Click "Review PR".**
4. **Let the live status box play.** This is the money shot — it shows the agent
   reasoning step by step:
   `Fetching PR data… → Understanding intent… → Routing decision: … →
   Reviewing code quality… → Reviewing security… → Synthesizing… → Done.`
5. **Land on the results.** Pause on:
   - the top metrics row (overall score, **verdict badge**, total findings, duration),
   - the four score progress bars,
   - the PR summary box.
6. **Expand one finding** — ideally a `critical` one — to reveal the
   description, suggestion, and the **fix diff** in a code block.
7. **Click a filter** (e.g. "Critical") to show the findings re-filter instantly.
8. **Hover the download buttons** (Markdown / JSON) to show export exists.
9. Stop recording.

---

## Suggested narration / captions

> "Paste any GitHub PR URL. The agent fetches the diff, then a **router**
> decides which reviews this change actually needs. Specialized agents combine
> **real tools** — pylint, AST — with LLM reasoning, and a synthesizer scores it
> and gives a verdict. Output is structured JSON with line numbers and fix
> diffs — not a wall of text."

---

## Recording tools

| Platform | Tool |
|----------|------|
| Windows | [ScreenToGif](https://www.screentogif.com/) (free, exports GIF directly) |
| macOS | [Kap](https://getkap.co/) or `Cmd+Shift+5` then convert to GIF |
| Cross-platform | [Peek](https://github.com/phw/peek) (Linux), OBS Studio (video) |

Keep the final GIF **under ~10 MB** so it loads on the README. Trim dead air;
~30–60s is ideal.

---

## Generate the static sample (for the README, no recording needed)

```bash
# Curated, deterministic sample (fast, no API calls):
python demo/generate_sample_output.py

# …or a REAL review of a specific PR (uses your keys):
python demo/generate_sample_output.py https://github.com/pallets/click/pull/3526
```

This writes [`sample_review.md`](sample_review.md) and
[`sample_review.json`](sample_review.json), which the README links to.

---

## Where the GIF goes

Save the recording as `demo/demo.gif` and reference it from the README's
**Demo** section:

```markdown
![Demo](demo/demo.gif)
```
