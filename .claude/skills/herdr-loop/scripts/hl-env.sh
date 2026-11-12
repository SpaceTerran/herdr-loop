# Sourced inside a worker pane before the agent starts:
#
#   . <kit>/scripts/hl-env.sh <claude-oauth-ref|-> <gh-token-ref> <nonce> [<loop root>]
#
# Exports the worker's secrets into this shell only. The values never appear
# on screen, in argv, or in a file. Any empty read fails closed: nothing is
# exported and the line `hl-env FAILED <nonce>` is printed, so a worker can
# never start on the pane's signed-in Claude account or on gh's keyring token
# by accident.
#
# Without a loop root each secret is read here with `hl-read-secret`, beside
# this file: one read of the store each, every time a worker starts (for an
# op:// reference, one request to 1Password). With one, the pane asks the
# program's keeper (`hl-secrets`, beside this file), which read each secret
# once and holds it in memory; the two references then only say whether a
# Claude token is wanted. Either way the failure line carries what the
# reader said, which is never a value.
#
# Pass `-` as the first argument for a Cursor worker, which needs no Claude
# token, and for a Claude worker of a program whose `worker_claude_ref` is
# `-`, which runs on the signed-in account by the operator's choice. On
# success the last line printed is `hl-env ok <nonce>-ready`. The spawn
# script waits for that line or the failure line. Both carry the nonce
# in a form that is not in the command that was typed, so neither the typed
# line nor an older run's output can end the wait.

# Where this file is. zsh names a sourced file in %x, bash in BASH_SOURCE.
if [ -n "${ZSH_VERSION:-}" ]; then
  eval 'hl_env_dir="${${(%):-%x}:A:h}"'
else
  hl_env_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

hl_env() {
  local claude_ref="${1:-}" gh_ref="${2:-}" nonce="${3:-}" root="${4:-}" claude_token="" gh_token="" answer="" errs=""
  unset CLAUDE_CODE_OAUTH_TOKEN GH_TOKEN GITHUB_TOKEN ANTHROPIC_API_KEY
  if [ -z "$claude_ref" ] || [ -z "$gh_ref" ] || [ -z "$nonce" ]; then
    echo "hl-env FAILED: usage: hl-env.sh <claude-ref|-> <gh-ref> <nonce> [<loop root>]"
    return 1
  fi
  if [ -n "$root" ]; then
    if [ ! -x "$hl_env_dir/hl-secrets" ]; then
      printf '%s\n' "hl-env FAILED ${nonce}: $hl_env_dir/hl-secrets is not there to ask"
      return 1
    fi
    # One capture says which it got: `ok <value>`, or `no <reason>`. Anything else is shown to nobody. The
    # exit status is not what is read, so a shell that stops at a failure still gets to print its line.
    if [ "$claude_ref" != "-" ]; then
      answer="$("$hl_env_dir/hl-secrets" --root "$root" get claude --tagged 2>/dev/null)" || :
      case "$answer" in
        "ok "?*) claude_token="${answer#ok }" ;;
        "no "*) printf '%s\n' "hl-env FAILED ${nonce}: no Claude token from the keeper: ${answer#no }"; return 1 ;;
        *) printf '%s\n' "hl-env FAILED ${nonce}: no Claude token from the keeper: hl-secrets gave no answer"; return 1 ;;
      esac
    fi
    answer="$("$hl_env_dir/hl-secrets" --root "$root" get gh --tagged 2>/dev/null)" || :
    case "$answer" in
      "ok "?*) gh_token="${answer#ok }" ;;
      "no "*) printf '%s\n' "hl-env FAILED ${nonce}: no GitHub token from the keeper: ${answer#no }"; return 1 ;;
      *) printf '%s\n' "hl-env FAILED ${nonce}: no GitHub token from the keeper: hl-secrets gave no answer"; return 1 ;;
    esac
  else
    if [ ! -x "$hl_env_dir/hl-read-secret" ]; then
      printf '%s\n' "hl-env FAILED ${nonce}: $hl_env_dir/hl-read-secret is not there to read with"
      return 1
    fi
    # The reader's one line on stderr says why a read failed; it never holds a value. It goes to a file of
    # this user's own for as long as it takes to read its first line.
    errs="$(mktemp "${TMPDIR:-/tmp}/hl-env.XXXXXX" 2>/dev/null)" || :
    if [ -z "$errs" ]; then
      printf '%s\n' "hl-env FAILED ${nonce}: no temporary file for the reader's messages"
      return 1
    fi
    if [ "$claude_ref" != "-" ]; then
      claude_token="$("$hl_env_dir/hl-read-secret" "$claude_ref" 2>"$errs")" || :
      if [ -z "$claude_token" ]; then
        answer="$(head -n 1 "$errs" 2>/dev/null)" || :
        rm -f "$errs"
        printf '%s\n' "hl-env FAILED ${nonce}: no Claude token: ${answer:-hl-read-secret said nothing}"
        return 1
      fi
    fi
    gh_token="$("$hl_env_dir/hl-read-secret" "$gh_ref" 2>"$errs")" || :
    if [ -z "$gh_token" ]; then
      answer="$(head -n 1 "$errs" 2>/dev/null)" || :
      rm -f "$errs"
      printf '%s\n' "hl-env FAILED ${nonce}: no GitHub token: ${answer:-hl-read-secret said nothing}"
      return 1
    fi
    rm -f "$errs"
  fi
  [ -n "$claude_token" ] && export CLAUDE_CODE_OAUTH_TOKEN="$claude_token"
  export GH_TOKEN="$gh_token"
  export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
  export BASH_DEFAULT_TIMEOUT_MS=1800000 BASH_MAX_TIMEOUT_MS=3600000
  echo "hl-env ok ${nonce}-ready"
}

hl_env "$@"
hl_env_rc=$?
unset -f hl_env
unset hl_env_dir
return $hl_env_rc
