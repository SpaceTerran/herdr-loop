#!/bin/zsh
# Stage a program's loop root on the loop host from its spec. Safe to run again:
# that is also how a newer kit is installed, between items.
#
# Run it on the loop host, from the laptop:
#
#   ssh <you>@<loop-host> 'HL_GH_TOKEN_REF=file:~/.config/herdr-loop/gh-token zsh -s -- <owner>/<control-repo> main programs/<name>/program.spec.json' < .claude/skills/herdr-loop/scripts/stage.sh
#
# or on the loop host itself, from a checkout of the control repository:
#
#   HL_GH_TOKEN_REF=file:~/.config/herdr-loop/gh-token .claude/skills/herdr-loop/scripts/stage.sh <owner>/<control-repo> main programs/<name>/program.spec.json
#
#   <owner/control-repo>  your control repository on GitHub: it holds the kit
#                         (.claude/skills/herdr-loop), the spec and the
#                         program's backlog files, and gets the ledger branch
#   <branch>              the branch of it to stage from (main, once the kit and
#                         the spec are merged)
#   <spec path>           program.spec.json, as a path in that repository; it
#                         sits in the program's folder beside its backlog files
#
#   HL_GH_TOKEN_REF       required: the reference of the loop's GitHub token, as
#                         `hl-read-secret` reads it (file:, op://, keychain: or
#                         env:). Over SSH the login keychain is locked, so a
#                         keychain: reference does not work there.
#   HL_READ_SECRET        optional: the path of the `hl-read-secret` to read it
#                         with. Otherwise the one beside this file when it is run
#                         from a file, else the first on PATH (~/.local/bin is
#                         searched first). Piped over SSH this script has nothing
#                         beside it, so on a host that has never been staged put
#                         the kit's scripts/hl-read-secret in ~/.local/bin once.
#
# zsh, for `print -r` and the `:A` modifier; every macOS host has it. This
# clones the control repository to <root>/ledger on the program's own branch,
# bringing that branch up to <branch>, and hands over to hl-stage in the
# clone. Nothing here touches Herdr.
set -eu

[ $# -eq 3 ] || { echo "usage: HL_GH_TOKEN_REF=<reference> stage.sh <owner/control-repo> <branch> <spec path in that repository>" >&2; exit 64; }
REPO=$1
BRANCH=$2
SPEC=$3
[ -n "${HL_GH_TOKEN_REF:-}" ] || { echo "usage: HL_GH_TOKEN_REF must name the loop's GitHub token (file:, op://, keychain: or env:), e.g. HL_GH_TOKEN_REF=file:~/.config/herdr-loop/gh-token" >&2; exit 64; }
case "$REPO" in
  */*) ;;
  *) echo "stage: '$REPO' is not <owner>/<repository>" >&2; exit 64 ;;
esac
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

# The reader: named outright, beside this file, or on PATH.
READER=${HL_READ_SECRET:-}
if [ -z "$READER" ]; then
  here=${0:A:h}
  if [[ ${0:t} == stage.sh && -x $here/hl-read-secret ]]; then
    READER=$here/hl-read-secret
  else
    READER=$(command -v hl-read-secret 2>/dev/null) || READER=""
  fi
fi
[ -n "$READER" ] && [ -x "$READER" ] || {
  echo "stage: no hl-read-secret to read HL_GH_TOKEN_REF with. Run stage.sh from a checkout of the kit, set HL_READ_SECRET to the kit's scripts/hl-read-secret, or copy that file to ~/.local/bin on this host" >&2
  exit 1
}

# The loop's own GitHub token. git on this host asks gh for credentials, and gh prefers this variable.
# The reader's one line on stderr says why when it fails; it never holds the value.
GH_TOKEN=$("$READER" "$HL_GH_TOKEN_REF") || { echo "stage: could not read the GitHub token ($HL_GH_TOKEN_REF); the line above says why" >&2; exit 1; }
[ -n "$GH_TOKEN" ] || { echo "stage: could not read the GitHub token ($HL_GH_TOKEN_REF): it is empty" >&2; exit 1; }
export GH_TOKEN

# Where the program lives is in the spec, so read the spec before anything is cloned.
spec=$(gh api "repos/$REPO/contents/$SPEC?ref=$BRANCH" --jq .content | python3 -c 'import base64,sys; sys.stdout.write(base64.b64decode(sys.stdin.read()).decode())')
ROOT=$(print -r -- "$spec" | python3 -c 'import json,sys; print(json.load(sys.stdin)["root"])')
LEDGER_BRANCH=$(print -r -- "$spec" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ledger_branch"])')
case "$ROOT" in
  "") echo "stage: the spec names no root" >&2; exit 1 ;;
  /*) ;;
  *) echo "stage: the spec's root must be an absolute path: '$ROOT'" >&2; exit 1 ;;
esac

mkdir -p "$ROOT"
LEDGER="$ROOT/ledger"
[ -d "$LEDGER/.git" ] || git clone --quiet --branch "$BRANCH" "https://github.com/$REPO.git" "$LEDGER"
git -C "$LEDGER" fetch --quiet origin
if git -C "$LEDGER" rev-parse --verify --quiet "refs/heads/$LEDGER_BRANCH" >/dev/null; then
  git -C "$LEDGER" checkout --quiet "$LEDGER_BRANCH"
elif git -C "$LEDGER" rev-parse --verify --quiet "refs/remotes/origin/$LEDGER_BRANCH" >/dev/null; then
  # The program was staged before, from another clone: carry on from its ledger.
  git -C "$LEDGER" checkout --quiet -b "$LEDGER_BRANCH" "origin/$LEDGER_BRANCH"
else
  git -C "$LEDGER" checkout --quiet -b "$LEDGER_BRANCH" "origin/$BRANCH"
fi
# Bring the program's branch up to the kit's. The manager's own ledger commits
# stay: they are to the ledger, which the branch being staged does not carry.
# Everything else follows that branch, so where the two disagree it wins. They
# disagree without anyone having edited anything once the branch a program was
# staged from has been squash-merged: main then holds the same files in a
# commit that shares no history with the ones this clone took.
if ! git -C "$LEDGER" merge --quiet --no-edit -X theirs "origin/$BRANCH" >&2; then
  git -C "$LEDGER" merge --abort 2>/dev/null || true
  echo "stage: could not bring $LEDGER_BRANCH up to origin/$BRANCH; $LEDGER is as it was. Read the lines above: an uncommitted change in that clone is the usual cause." >&2
  exit 1
fi
# The kit is the staged branch's, file for file. A merge between histories
# that do not meet keeps a file that branch no longer has.
KIT_PATH=.claude/skills/herdr-loop
if ! git -C "$LEDGER" diff --quiet "origin/$BRANCH" HEAD -- "$KIT_PATH"; then
  git -C "$LEDGER" rm -r --quiet "$KIT_PATH"
  git -C "$LEDGER" checkout "origin/$BRANCH" -- "$KIT_PATH"
  git -C "$LEDGER" commit --quiet -m "$LEDGER_BRANCH: take the kit as origin/$BRANCH has it"
fi

# No bytecode: this runs the kit inside the ledger clone, which must stay clean for the manager's commits.
exec env PYTHONDONTWRITEBYTECODE=1 python3 "$LEDGER/.claude/skills/herdr-loop/scripts/hl-stage" --spec "$LEDGER/$SPEC"
