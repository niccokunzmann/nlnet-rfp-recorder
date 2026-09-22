# NLnet RfP Recorder

Create RfPs from work on issues and pull requests, record time.

## Purpose

This tool allows fine grained tracking of issue and PR activity, be it review or implementation, maps them to tasks and shows the progress and budget avaliable for each MoU.
After some amount of work, a report can be created to request a payment.

Example report:

```text
1b: 155€
  Issues:
    - https://github.com/collective/icalendar/issues/1635 - 95€
    - https://github.com/collective/icalendar/issues/1735 - 35€ (review)
  Pull Requests:
    - https://github.com/collective/icalendar/pull/1362 - 15€ (review)
    - https://github.com/collective/icalendar/pull/1559 (review)
    - https://github.com/collective/icalendar/pull/1588 (review)
    - https://github.com/collective/icalendar/pull/1598 (review)
    - https://github.com/collective/icalendar/pull/1644 (review)

1h: 135€
  Issues:
    - https://github.com/collective/icalendar/issues/1487 - 15€
    - https://github.com/collective/icalendar/issues/1493 - 55€
    - https://github.com/collective/icalendar/issues/1695 - 10€ (review)
  Pull Requests:
    - https://github.com/collective/icalendar/pull/1668 - 20€ (review)
    - https://github.com/collective/icalendar/pull/1680 - 15€
    - https://github.com/collective/icalendar/pull/1705 - 10€ (review)
    - https://github.com/collective/icalendar/pull/1772 (review)
  Links:
    - github.com/collective/icalendar/pull/1747 (review)

1n: 340€
  Issues:
    - https://github.com/collective/icalendar/issues/1667 (review)
    - https://github.com/collective/icalendar/issues/1722 - 90€
  Pull Requests:
    - https://github.com/collective/icalendar/pull/1615 - 15€ (review)
    - https://github.com/collective/icalendar/pull/1654 - 10€ (review)
    - https://github.com/collective/icalendar/pull/1665 (review)
    - https://github.com/collective/icalendar/pull/1672 - 55€ (review)
    - https://github.com/collective/icalendar/pull/1696 - 65€ (review)
    - https://github.com/collective/icalendar/pull/1698 - 15€ (review)
    - https://github.com/collective/icalendar/pull/1703 - 15€
    - https://github.com/collective/icalendar/pull/1713 - 20€ (review)
    - https://github.com/collective/icalendar/pull/1721 - 15€ (review)
  Discussions:
    - https://github.com/collective/icalendar/discussions/1673 - 30€ (review)

Total: 630€
Total for tasks above 50€: 630€

```

## Installation

```bash
git clone https://github.com/niccokunzmann/nlnet-rfp-recorder.git
cd nlnet-rfp-recorder
make install
```

## Getting started

```bash
rfp mou add nlnet-2026
rfp mou import path/to/budget.txt          # milestone table or takentaal doc
rfp task select 10a
rfp start https://github.com/collective/icalendar/issues/1708
rfp stop
rfp report
```

| Command | Description |
| --- | --- |
| `rfp review <link>` | Instead of `rfp start`: tags the entry as review, not implementation. |
| `rfp start <link>` with no task selected | Starts tracking right away, then asks which task to assign it to - Enter picks the last task worked on, `?` lists tasks (as `rfp task list` does). |
| `rfp edit --duration 50` / `--duration 1:20` | Fix the last entry's duration - forgot to `rfp stop`? Sets it outright (minutes, or `H:MM`), moving its start time; a still-running entry keeps running. |
| `rfp edit --duration +15` / `--duration -1:20` | Nudge the last entry's duration up or down by that amount instead of setting it outright. |
| `rfp status` / `rfp task` / `rfp mou status` | Check where things stand. |
| `rfp report` | Counts issues once finished regardless of GitHub status, but only counts a PR once it's closed (checked live against GitHub). |
| `rfp token <token>` (`rfp token` alone for setup steps) | Avoids the unauthenticated GitHub API rate limit, which the report can hit quickly. |
| `rfp --test <command>` | Try things against a disposable database instead of your real one. |

## Environment variables

- `RFP_EUROS_PER_HOUR` (default `50`): hourly rate, needed to compute budgets. Leave unset to disable money/time-left figures entirely.
- `REVIEW_DEFAULT_EXCLUDE_BELOW` (default `50`): tasks totalling less than this (EUR) default to excluded in `rfp report review`, and get their own subtotal line in `rfp report print`.
- `RFP_DB` (default: see `XDG_DATA_HOME` below): path to the sqlite database file. Same as passing `--db`.
- `XDG_DATA_HOME` (default `~/.local/share`): base directory for the default database, used as `$XDG_DATA_HOME/nlnet-rfp-recorder/rfp.db` when `RFP_DB` isn't set.

## Development

```bash
make init    # uv sync + pre-commit install
make test
```
