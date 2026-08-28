# ops

Operational tooling that runs on the deployment host rather than inside a
container. Versioned here so it has history and review; the copy on the host is
what actually runs, so a change here must be copied there to take effect.

## stack-watch.sh

Whole-stack health watcher. One line per run, `ok` or `FAIL`, appended to a log
on the host. Covers: both cameras' state, fps, drop and audio counters with
per-interval deltas, publisher liveness, the frame-to-publish lag, consecutive
failures and last error; the talkdown counters; the bridge and Home Assistant
containers' running state, restart count and start time; the bridge's memory
against its limit; Home Assistant integration errors; and whether the key
entities have gone unavailable.

Reading it: `grep FAIL <logfile>` first, then skim. Known-benign FAIL sources
are listed in the script header comments beside the thresholds that produce
them.

### Installation

Copy to the host, make executable, and register the cron entry (this is the
part that is otherwise captured nowhere; the entry lives in the operating
user's crontab, not in any package):

```
crontab -l | grep -v stack-watch; echo "*/5 * * * * /home/<user>/telus-stack-watch.sh"
```

piped back into `crontab -`. The script needs passwordless sudo for `docker`
and for reading the bridge's control secret; both are prerequisites of the
deployment itself, not extra grants for the watcher.

### Honest status (backlog E75)

This is shell written quickly against a live system. It has already produced
three false positives, each the same mistake: alerting on a state the system
defines as normal. Each is fixed and commented in the script, but the remaining
conditions have not been systematically checked against what the entities and
the control API actually promise, and the script has no test suite. Treat a
novel FAIL line with suspicion in both directions: it may be real, and it may
be the fourth instance of that mistake.
