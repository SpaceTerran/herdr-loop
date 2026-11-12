# Herdr Loop

One Claude Opus manager works through a backlog by starting ordinary Claude Code and Cursor sessions in [Herdr](https://herdr.dev/) panes. Builders build, a fresh session reviews, and one gated script merges. A launchd watchdog keeps exactly one manager alive, and hands a program to a fresh manager when its context fills up.

The rule the kit hangs on: **a script may refuse; it may not decide.** Judgment lives in the prompts. Anything that must not slip lives in a script, and the scripts only ever say no.

The write-up, with the reasoning, the figures and the numbers, is on the blog: [The Herdr Loop: One Opus Manager, a Herd of Coding Sessions](https://spaceterran.com/posts/herdr-loop-claude-code-manager/).

> **This is version five, and it moves.** This is a copy of the kit I run my own repos with, cleaned of my hosts, accounts and project names. I change it every week as models and tools change. It's shared so you can read it, borrow from it, and run it if you want to. It isn't a product, and there's no support promise. Issues and ideas are welcome.

## What's in here

```
.claude/skills/herdr-loop/        the kit (also a Claude Code skill: open this repo in Claude Code and it's loaded)
  SKILL.md                        what each part is, and how a program is run
  references/
    manager-prompt.template.md    the manager's prompt (stateless; filled once per program)
    briefs/                       what each kind of worker is told
    ops-card.md                   the manager's commands and what every answer means
    authoring.md                  how to write backlog items and a spec  <- read this first
    troubles.md                   what to do when a script reports a problem
    host-facts.md                 what a host needs, and what was measured on mine
    design.md                     why things are the way they are, with the failures that shaped them
    program.spec.template.json    a program's spec
    program.template.json         the kit's settings, which a spec's `settings` override
  scripts/
    hl-flow, hl-merge, hl-verify, hl-wait, hl-spawn, hl-watchdog, hl-secrets, ...
    stage.sh, install-watchdog.sh, hl-read-secret
    tests/                        about 1,600 tests; staging runs them before a program starts
programs/example/                 a filled-in example spec and backlog file
```

## How it fits together

You write a **program** (a spec plus backlog files) in your own **control repository**, a private copy of this one. A script **stages** it onto an always-on Mac, the **loop host**: a loop root under `~/loops/<program>`, a clone of each target repository per stream, the filled manager prompt, and a **ledger** on its own branch of your control repository. The **watchdog** starts a manager in Herdr. From there the manager works the backlog, and you watch in Herdr or read the ledger.

Specs, backlogs and ledgers say a lot about your work. Keep your control repository private.

## Before you start

You need:

- **A Mac that stays on and stays logged in**, Apple silicon. The kit uses launchd, the macOS Keychain and macOS process details. It has only ever run on macOS, because that's what I work on. Linux would very likely work with a systemd timer in place of launchd and a few other changes, but it's untested.
- **[Herdr](https://herdr.dev/)** on that Mac, with its server running.
- **Claude Code**, signed in on at least one Claude seat that isn't your own day-to-day sign-in (more on seats below).
- **Cursor's CLI** (`agent`), optional. The manager uses Cursor's Composer only for clearly mechanical items, and never for reviews.
- **git, the GitHub CLI (`gh`), Python 3.9 or newer, `jq`, and Node.js** (the reviewer's browser is the Playwright MCP server, run with `npx`).
- Whatever your target repositories need to build and test (Docker, a language toolchain, and so on).

And on GitHub:

- **A token for the loop that isn't an admin's.** A fine-grained personal access token, limited to your control repository and the target repositories, with: Contents read and write, Pull requests read and write, Issues read and write (comments and labels), Actions read, Checks read, Commit statuses read, Metadata read, and Administration **read-only** (`hl-merge` reads which checks the branch protection requires; it never changes it). Add Workflows read and write if items may change files under `.github/workflows`.
- **Branch protection on every target repository's base branch**: require a pull request, and require the status checks your CI runs. The merge gate leans on GitHub saying no, and a non-admin token can't push past it.

Start with a scratch repository. The loop merges real pull requests.

## Step by step

### 1. Make your control repository

Use this repository as a template (**Use this template** on GitHub), and make the new one **private**. Clone it to your laptop. Everything you write for a program goes here.

### 2. Prepare the loop host

On the Mac that will run the loop. Do the sign-ins **in a terminal on that Mac, or in a Herdr pane on it**, not over SSH: the login Keychain is locked in an SSH shell, so neither Claude Code nor Cursor can sign in there.

1. Install the tools above, then install the reviewer's browser once:

   ```sh
   npx --yes @playwright/mcp@0.0.82 install-browser chrome-for-testing
   ```

2. **Make Claude seats.** A seat is a Claude Code configuration directory beside `~/.claude`, with its own sign-in and its own usage windows. The manager and Claude workers run on a seat, never on your own `~/.claude`. With more than one seat, several programs can run at once, each with its own manager: the watchdog decides which seat each program runs on from the seats' usage, and pauses a program when no seat has room.

   ```sh
   mkdir ~/.claude-seat-a
   CLAUDE_CONFIG_DIR=~/.claude-seat-a claude      # sign in, then quit
   ```

   Repeat for `~/.claude-seat-b` if you have a second seat.

3. **Give every seat Herdr's hook and the permission setting.** Install Herdr's integrations once (`herdr integration install claude`, and `herdr integration install cursor` if you use Cursor), so Herdr can see what each session is doing. The manager starts with `--dangerously-skip-permissions`, so each seat's `settings.json` needs `"skipDangerousModePermissionPrompt": true`, or the manager stops at a confirmation screen. The simplest way, and what I do, is to link each seat's `settings.json`, `hooks/` and `statusline.sh` to the ones in `~/.claude`, so every seat behaves the same. Workers don't read these: they start with only the kit's own settings.

4. **Record seat usage.** The watchdog reads each seat's usage from files the status line writes. Add the lines under "The seat recorder" in `references/host-facts.md` to your status line script, after it reads its input.

5. **Sign in to Cursor** (optional): run `agent` once in a terminal on the Mac and sign in. Don't sign in again while a program runs; one login replaces the account for every running Cursor session.

### 3. Put the secrets where the kit can read them

The kit never puts a secret on a command line or in a file it writes. It reads each one through `scripts/hl-read-secret`, which takes a reference:

| Reference | Reads |
|---|---|
| `file:~/.config/herdr-loop/gh-token` | a file only you can read (`chmod 600`). Works everywhere: over SSH, in panes, in launchd. The simplest choice. |
| `op://<vault>/<item>/<field>` | 1Password, through the `op` CLI and whatever sign-in it has |
| `keychain:<service>` | the macOS login Keychain. Works in panes and launchd jobs, **not** in an SSH shell, so not for staging |
| `env:<NAME>` | an environment variable (not `GH_TOKEN` itself) |

Put the loop's GitHub token in place on the loop host, for example:

```sh
mkdir -p ~/.config/herdr-loop && chmod 700 ~/.config/herdr-loop
pbpaste > ~/.config/herdr-loop/gh-token && chmod 600 ~/.config/herdr-loop/gh-token
```

Then put the reader itself on the loop host, once. Staging arrives over SSH with nothing beside it, so it looks for `hl-read-secret` on `PATH`, `~/.local/bin` first. From your laptop, in your control repository:

```sh
ssh <you>@<loop-host> 'mkdir -p ~/.local/bin && cat > ~/.local/bin/hl-read-secret && chmod 755 ~/.local/bin/hl-read-secret' \
  < .claude/skills/herdr-loop/scripts/hl-read-secret
```

Check it on the host with `~/.local/bin/hl-read-secret file:~/.config/herdr-loop/gh-token | wc -c`: it prints the token's length, never the token.

With `"secrets": "keeper"` (the template's setting), one small process per program reads each secret once and answers from memory, so a store with a request limit isn't hammered. If you use something else entirely, set `secrets_reader` in the spec's settings to a command that takes the reference as its last argument and prints the value.

**Notifications.** The manager tells you about milestones, and about the few things it's allowed to ask you, over [ntfy](https://ntfy.sh/). Set `ntfy_url` (a topic you control), `ntfy_user` and `ntfy_password_ref` in the spec's settings. Without them, the manager has no way to reach you, and everything it would have sent is only in the ledger.

### 4. Write the program

Read `references/authoring.md` first. Then copy `programs/example/` to `programs/<name>/` in your control repository and edit it:

- **`program.spec.json`**: the program's name; its loop root (`/Users/<you>/loops/<name>`); its ledger branch; the target repository; one or more **streams** (a stream is a lane of work with its own clone, the paths it may write, and its first backlog file); the facts of each repository, said once (what its required checks run, the command that makes a checkout ready, the paths a person sees, the paths no item may change); and four short pieces of prose for the manager prompt (the files in order with any gates between them, what counts as production, how much may run at once, and who else uses the seats). `references/program.spec.template.json` explains every key.
- **Backlog files** (`*-ITEMS.md`): one `## <ID> — <title>` section per item, each with a **Scope**, an **Accept** list of observable results with their values before the change, optional **Depends** and **Floor**, and a **Verify** line the loop runs after the deploy. Most of the work is here. Write behavior, not "the test suite passes".

Commit and push both to your control repository.

### 5. Stage it on the loop host

From your laptop, with the script piped over SSH:

```sh
ssh <you>@<loop-host> \
  'HL_GH_TOKEN_REF=file:~/.config/herdr-loop/gh-token zsh -s -- <owner>/<control-repo> main programs/<name>/program.spec.json' \
  < .claude/skills/herdr-loop/scripts/stage.sh
```

It clones your control repository to `<root>/ledger` on the program's ledger branch, installs the kit from it at one commit, writes `program.json`, clones each target per stream (with a commit hook), fills the manager prompt, starts the ledger, makes each stream's first run, runs the kit's tests, and prints what it made, with any remarks about your backlog. Running it again later installs a newer kit between items and leaves the ledger, runs and worktrees alone.

### 6. Start the first manager

```sh
ssh <you>@<loop-host> '/Users/<you>/loops/<name>/.claude/skills/herdr-loop/scripts/hl-watchdog \
  --root /Users/<you>/loops/<name> --herdr ~/.local/bin/herdr start --note "first start"'
```

(Use wherever `herdr` is installed.) It opens a Herdr workspace named for the program, starts the manager on the program's seat, and waits until the manager has taken its prompt. If it answers "did not take its prompt", run it again.

### 7. Install the watchdog

About a minute later, once the manager has pinned the binaries:

```sh
ssh <you>@<loop-host> '/Users/<you>/loops/<name>/.claude/skills/herdr-loop/scripts/install-watchdog.sh /Users/<you>/loops/<name>'
```

This adds a LaunchAgent that looks every five minutes and starts a manager whenever none is alive. It starts disarmed, and the manager arms it. Set `HL_LAUNCHD_PREFIX` to choose the label's prefix (the default is `local.herdr-loop`). Remove it when the program is done: `install-watchdog.sh --remove <root>`.

### 8. Watch it

- **In Herdr:** `herdr --remote <you>@<loop-host>`. Each run has its own workspace, named for its work, with the workers as tabs. Look, but don't type into the panes.
- **In the ledger:** `LEDGER.md` beside the spec, on the program's ledger branch. Its "Now" section says where things stand. You answer a parked decision by pushing to that branch.
- **To tell the manager something:** `herdr agent prompt <its pane> "<one short line>"`. Anything longer goes in a file you point it at.

### 9. When it's done

When the last backlog file is complete, the manager proves the closing gates, closes every run's workspace, writes the closing state in the ledger, disarms the watchdog and tells you. Then remove the watchdog (`install-watchdog.sh --remove <root>`), and merge the ledger branch into your control repository if you want to keep the record.

## When something goes wrong

`references/troubles.md` is organized by what a script reported. `references/host-facts.md` says what the kit expects of the host and how each fact was learned. The manager reads the same files.

## Changing the kit

Run the tests from the kit's `scripts/` directory:

```sh
python3 -m unittest discover -s tests -t .
```

The rules for changes are at the end of `SKILL.md`. The short version: a script may refuse but not decide; a new behavior gets a setting whose absence means the old behavior; and a rule leaves a brief only when a script holds it.

## License

MIT. See `LICENSE`.

Claude, Claude Code, Opus and Sonnet are trademarks of Anthropic. Cursor and Composer are trademarks of Anysphere. 1Password is a trademark of AgileBits. Herdr is an independent open-source project. None of them is affiliated with this repository.
