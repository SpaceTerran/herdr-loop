# Read first by every zsh a Herdr loop worker starts: `hl-spawn start` points
# the worker's pane at this directory with ZDOTDIR. The shell writes its pid
# and start time to the attempt's shells file (HL_SHELLS), so that whatever
# it leaves running can be found and stopped once the worker is closed
# (`hl/reap.py`). Then it puts ZDOTDIR back and reads the operator's own
# .zshenv: it is otherwise the shell it would have been.
if [[ -n ${HL_SHELLS-} ]]; then
  zmodload -F zsh/datetime p:EPOCHSECONDS 2>/dev/null
  print -r -- "$$ ${EPOCHSECONDS:-$(date +%s)}" >>| $HL_SHELLS 2>/dev/null
fi
# Claude Code gives a Bash command no input: stdin is /dev/null, except in a
# command that holds a heredoc, which gets a socket nobody writes to or closes.
# Anything else in that command that reads stdin (a `python3 -` with no heredoc
# of its own) then waits for good: a reviewer sat ten minutes on one
# (the help-desk catalog program, in one manager session). A worker's
# non-interactive shell reads /dev/null instead. A pipe, a file or a heredoc
# it is given is not a socket.
if [[ -n ${HL_SHELLS-} && ! -o interactive && -S /dev/fd/0 ]]; then
  exec </dev/null
fi
if [[ -n ${HL_ZDOTDIR-} ]]; then
  ZDOTDIR=$HL_ZDOTDIR
else
  unset ZDOTDIR
fi
if [[ -r ${ZDOTDIR:-$HOME}/.zshenv ]]; then
  builtin source ${ZDOTDIR:-$HOME}/.zshenv
fi
