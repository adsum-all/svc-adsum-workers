# adsum-workers

Part of the ADSUM platform (membership, QR check-in and attendance).
Subgroup: `services`.

## Role

The scheduler. A process that runs continuously and keeps the commerce queues moving.

It exists for one measurable reason. The commerce tasks are exposed over HTTP and
were triggered by the host's cron, which allows **one run per day** on the plan in
use. A module paid for at 09:05 therefore waited until the next day to be deployed.
Here the queue advances every thirty seconds.

The tasks themselves live in `adsum-commerce`, with the database and the rules. This
service does not reimplement them, it triggers them: duplicating the suspension rule
here would give two versions of it, and one of them would be wrong one day.

## Cadence

| Task | Interval | Why |
|------|----------|-----|
| `deploiements` | 30 s | The most visible to the client: they have just paid and are waiting for their module. |
| `envois` | 60 s | The outbox. A reminder written but not posted serves no purpose. |
| `relances` | 1 h | Dunning is computed on due dates in days; running it every minute finds nothing more. |
| `suspensions` | 1 h | A suspension closes a client's access. Deliberately spaced: nothing urges cutting an hour earlier, and a mistake here is expensive. |

## What it guarantees

- **A task never overlaps itself.** One thread per task: call, wait for the result,
  then sleep. Two simultaneous runs would fight over the same rows; the called
  service protects itself with leases, but relying on that would mean knowingly
  sending wasted work.
- **Different tasks advance in parallel.** A slow dunning run must not delay a
  deployment: these are unrelated queues.
- **Backoff is exponential and capped.** Without the exponential, an unavailable
  database receives one request per second and takes longer to recover. Without the
  cap, twelve consecutive failures push the wait past a day and the queue never
  restarts by itself once the service is back.
- **Jitter is proportional and centred.** Without it, two instances started by the
  same deployment hit at the same second, forever.
- **Shutdown is clean.** On signal the running task finishes and no new one starts.
  Waiting goes through an event, so a task on an hourly cadence still answers a stop
  request immediately.
- **An unexpected exception never kills a thread.** A queue that stops in silence is
  the worst case: nobody sees it before a client complains about a module they paid
  for and never received.

## Alerting

Observability tells whoever is watching. Nobody is watching at three in the morning,
which is exactly when a queue stops. Without an alert, the first signal is a customer
complaining days later about a module they paid for and never received.

The watcher runs in the main thread, not in a task thread: a task that alerts about
itself goes quiet precisely when it is the one that is stuck. It alerts on three
situations and no others, because an alert nobody can act on teaches people to ignore
alerts:

- the queues stopped for good (wrong secret, missing route);
- one queue has failed five times in a row, which is past the point where the
  exponential backoff would have caught a network blip;
- the loop is no longer running at all.

An alert fires once per situation, and the return to normal is announced. Repeating
every thirty seconds drowns the channel and gets notifications muted, which silences
the next alert, the one that mattered. And an alert says what has stopped happening
for customers, not just a task name nobody remembers at three in the morning.

Alerts go out through `adsum-gateway`, which already carries the editor's identity,
its deduplication and its register. An alert sent beside it would be the only message
on the platform nobody keeps a trace of. If the alerting channel is down, the queues
keep advancing: the failure is logged, never propagated, and the incident stays open
to be retried.

## Stack

Python 3.11, httpx. No database: all state lives in the commerce service.

## Configuration

| Variable | Required | Purpose |
|----------|----------|---------|
| `ADSUM_COMMERCE_URL` | yes | Base address of the commerce service. |
| `ADSUM_CRON_SECRET` | yes | Scheduler secret, the same one the commerce service checks. |
| `ADSUM_JOURNAL_NIVEAU` | no | Log level, defaults to `INFO`. |
| `ADSUM_PASSERELLE_URL` | for alerts | Address of the gateway that carries alerts. |
| `ADSUM_PASSERELLE_SECRET` | for alerts | Shared secret the gateway checks. |
| `ADSUM_ALERTE_DESTINATAIRE` | for alerts | Who is called. A Telegram chat id, or an email address. |
| `ADSUM_ALERTE_CANAL` | no | `telegram` by default. |

Alerting is optional: a scheduler that refused to start for want of an alert channel
would leave the queues stopped for a reason that blocks nothing. But the absence is
logged as a warning at startup, otherwise operations believe they are being watched
when nobody will ever be told.

The process refuses to start without the first two, rather than running empty while
letting everyone believe the queues are moving.

## Running

```
python -m ouvriers.main
```

Or the installed entry point, `adsum-ouvriers`. It runs on anything that can keep a
Python process alive: an operator machine, a container elsewhere, a managed service.

Exit codes, chosen so a supervisor can tell the two apart without reading logs:

| Code | Meaning | What a supervisor should do |
|------|---------|-----------------------------|
| 0 | Clean shutdown on signal | Nothing, or restart on redeploy |
| 1 | A task was still running at the deadline | Restart. The lease held at the commerce service releases on its own |
| 2 | Stopped on a fault time does not repair | Do not restart in a loop. Fix the configuration, then start |

Code 2 exists because restarting on a wrong shared secret produces a crash loop that
looks like flapping infrastructure and hides the actual cause.

## Tests

```
python -m pytest tests/ -q
```

No real sleeping. The next-run calculation is a pure function and is verified as
such; the loop itself runs for real, on real threads and millisecond cadences.

## Deployment status

Not deployed. Requires, from the owner: a decision on where this process runs, since
the current host only offers per-day triggers, which is precisely what this service
replaces.
