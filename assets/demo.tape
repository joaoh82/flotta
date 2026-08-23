# Flotta — the README demo.
#
# Regenerate with `just demo`, which runs this from the repo root. Do not run
# `vhs` from anywhere else: the CLI reads `.env` from the *current directory*
# to resolve the Modal workspace profile, so a tape recorded from /tmp would
# silently target whatever profile happens to be active.
#
# This performs a REAL spawn — a Modal container and a real model call — so
# every take costs a cent or two, and the answer will differ slightly between
# takes because a live model writes it.
#
# The store goes to a temp path rather than the repo's ./fleet.db for two
# reasons: the demo should show one worker instead of whatever accumulated
# while you were testing, and a GIF in a public README should not display
# somebody's home directory.

Output assets/demo.gif

Set Shell bash
Set FontSize 20
Set Width 1180
Set Height 430
Set Padding 24
Set Framerate 24
Set TypingSpeed 45ms

Hide
Type "export PS1='$ ' PATH=$HOME/.local/bin:$PATH FLOTTA_STORE=/tmp/flotta-demo/fleet.db"
Enter
Type "rm -rf /tmp/flotta-demo && mkdir -p /tmp/flotta-demo"
Enter
Type "clear"
Enter
Show

Sleep 1s

# Beat 1 — hand a task to a machine that does not exist yet.
Type `flotta spawn "Where are you running? Kernel and CPU count, plain text." --wait`
Sleep 500ms
Enter

# Wait on the CLI's own status word, not on a fixed duration: a cold image
# pull takes far longer than a warm one, and `Sleep 8s` would clip the answer
# off the end of the GIF on the take where that happens.
Wait+Screen@300s /done/
Sleep 2500ms

# Beat 2 — the live fleet is empty again. The container is gone.
Type "flotta ps"
Enter
Sleep 2500ms

# Beat 3 — but the store remembers what ran.
Type "flotta ps -a"
Enter
Sleep 3s
