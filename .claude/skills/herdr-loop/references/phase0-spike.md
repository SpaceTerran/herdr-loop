# Phase 0 spike: prove the Herdr primitives on the mini

Run this from a Claude Code session started **inside a Herdr pane on the Mac mini** (`echo $HERDR_ENV` prints `1`). Start that session in a scratch directory outside any git checkout that something else syncs or reverts, for example `~/loops/spike`. It builds nothing and merges nothing. Its only output is a filled-in copy of the table at the end, saved as `~/loops/spike/phase0-results.md`.

Everything below is paste-ready for that session.

**Last run: 2026-10-11**, on Herdr 0.9.0, Claude Code 2.1.296 and Cursor CLI 2026.10.01. Its results file stayed in the author's private control repository; what it found is what the ops card and the scripts are written from, and `host-facts.md` carries each finding marked "spike". Run it again after an upgrade of any of the three that changes something the ops card relies on: how an agent's state reads, what a dialog looks like, what a flag does.

---

You are checking how Herdr, Claude Code and the Cursor CLI behave on this machine, so that a set of scripts can be written against facts instead of guesses. Work through the checks in order. For each one, record what you ran and exactly what you saw. Do not fix anything you find; write it down.

## Rules

- First run `test "$HERDR_ENV" = 1 && echo inside-herdr`. If it does not print `inside-herdr`, stop and say so.
- Read `herdr <command> --help` before any Herdr command you have not used, and do as it says.
- Create one workspace for this spike (`herdr workspace create --label hl-spike --cwd "$HOME/loops/spike" --no-focus`) and work only inside it. Close only panes and tabs you created, and close the workspace when you finish.
- A workspace, tab or pane you did not create belongs to someone else (another loop, an assistant, the operator). Never type into it, read it, or close it.
- Never run `agent login`, `agent logout`, `agent --api-key`, or set `CURSOR_API_KEY`. Never run `herdr update` or `herdr server stop`.
- Never print an environment or a token. To show that a variable is set, print `${NAME:+set}`.
- Each worker you start gets a trivial prompt (given below). No worker touches a real repository.
- If a check would need something these rules forbid, mark it `not checked` with the reason.

The kit is at `{{KIT}}` (a checkout of the `herdr-loop` skill). `{{KIT}}/references/ops-card.md` lists what is already known; you are filling in what it marks unproven.

## Checks

**1. Environment reaches an agent.** In a new pane: `herdr pane run <pane> ". {{KIT}}/scripts/hl-env.sh 'file:~/.config/herdr-loop/claude-token' 'file:~/.config/herdr-loop/gh-token' spike1"` (any two references `scripts/hl-read-secret` reads: a Claude token for a second account and a GitHub token), then `herdr pane wait-output <pane> --match "hl-env ok spike1-ready" --timeout 60000`. Record whether the sentinel appeared and whether the typed command itself could have matched it. Then `herdr pane run <pane> 'echo "claude=${CLAUDE_CODE_OAUTH_TOKEN:+set} gh=${GH_TOKEN:+set}"'` and record the line.

**2. Which Claude account a pane is on.** In that same pane run `claude auth status` and record the account it names. Do the same in a second pane that did not source `hl-env.sh`. The first should name the account whose token you passed, the second the host's own sign-in. If `auth status` does not reflect the token, find a check that does and record it.

**3. A Claude worker with the worker flags.** In the first pane, start an agent exactly as the ops card's builder recipe says (name `b-spike-1`, a fresh uuid from `uuidgen | tr A-Z a-z`, `mcp-none.json`). Record: the exact result of `agent start`; whether a trust dialog or any other dialog appeared, and what `herdr agent get b-spike-1` reported while it did; whether the first try returned `agent_pane_busy`. Prompt it: `Run the command: echo "claude=${CLAUDE_CODE_OAUTH_TOKEN:+set} gh=${GH_TOKEN:+set}" and then write the single word done to ~/loops/spike/b-spike-1.done`. Record whether both variables read `set` inside the agent, and how long the done file took.

**4. What the flags did.** Find that session's transcript (`~/.claude/projects/<cwd with / and . replaced by ->/<uuid>.jsonl`) and record: that the file exists at the expected path; the tools the session had; whether any hook ran; whether any MCP server or plugin beyond the built-in ones loaded. Ask the agent `Which tools do you have? List their names only.` if the transcript does not say.

**5. Herdr's reading of Claude states.** Record `herdr agent get b-spike-1` `.result.agent.agent_status` in each of these: while it works on `Run: sleep 20 && echo ok`; just after it finishes; after you prompt it `Run the command sleep 45 in the background, tell me you started it, and end your turn` (record the status before and after the background task ends, and whether the session speaks again by itself).

**6. Resume.** Close that agent (`/exit` through `herdr agent prompt`, or close its pane). In a fresh pane, after `hl-env.sh` again, start `b-spike-2` with `--resume <the uuid>` and `--effort xhigh` in place of `--session-id` and `--effort high`. Ask it `What was the first command I asked you to run?`. Record whether it remembers, and whether `--resume` accepted the other flags.

**7. An evaluator with a browser.** Start `e-spike-1` with the evaluator recipe (`mcp-playwright.json`). Prompt it: `Open https://example.com in the browser, take a screenshot to ~/loops/spike/shot.png, and write done to ~/loops/spike/e-spike-1.done`. Record whether the browser tools were there on the first turn, how long the first tool call took, and the size of the screenshot. Record the memory figures of check 10 while the browser is open.

**8. A Cursor worker.** In a new pane (run `hl-env.sh` with `-` as the first argument), start `b-spike-3` with `--kind cursor` and the Cursor recipe. Try `--model 'composer-2.5[fast=false]'` first; if the CLI rejects it, read its error, try `--model composer-2.5`, and record what the session's header says the model is. Record: the process name (`herdr pane process-info --pane <pane>`), whether `agent start` detected it, any dialog, and that `~/.cursor/cli-config.json` still names the account it named before. Prompt it: `Run: echo "gh=${GH_TOKEN:+set}" and write done to ~/loops/spike/b-spike-3.done`. Then, in a scratch git repository you create under `~/loops/spike/repo`, ask it to make one commit and record whether the commit message carries any `Co-authored-by` line.

**9. Herdr's reading of Cursor states.** As check 5, for `b-spike-3`: working, finished, and waiting on any dialog you can reach without approving anything risky.

**10. Memory.** Before any worker starts, and again with one Claude worker idle, one Cursor worker idle, and the evaluator's browser open, record `memory_pressure | tail -1`, `sysctl vm.swapusage`, and the resident size of each agent process (`ps -o pid,rss,comm -p <pid>`; find the pids with `herdr pane process-info`).

**11. A served process and its pane.** In a new pane: `herdr pane run <pane> "/bin/sh -c 'python3 -m http.server 8399 --bind 127.0.0.1'"`. Record what `herdr pane process-info --pane <pane>` reports (is there a process group id?). Confirm `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8399/` prints 200. Then `herdr pane close <pane>` and record, five seconds later, whether port 8399 still answers and whether the python process still exists. If it survives, find the gentlest thing that stops it (`herdr pane send-keys <pane> ctrl+c` before closing) and record that.

**12. Prompt delivery.** Record what `herdr agent prompt` returns for an idle agent, and what it returns when sent to an agent that is already working. Send one prompt of about 2,000 characters and confirm from the agent's reply that all of it arrived.

**13. Limit signals.** Do not try to exhaust anything. Look only for where a limit would show up without reading the screen: search one Claude transcript for records whose type or subtype mentions `rate_limit` or `usage`, and record what you find, including "nothing".

## Finish

Close every pane, tab and the workspace you created. Confirm with `herdr workspace list` that `hl-spike` is gone and the others are as you found them. Then write `~/loops/spike/phase0-results.md`:

| # | Check | Result | Exactly what was seen |
|---|---|---|---|
| 1 | Environment reaches an agent | works / does not / not checked | … |
| … | | | |

After the table, list anything that surprised you, and every command from the ops card that needed changing, with the corrected form. End your reply with the path of that file and the three results that matter most.
