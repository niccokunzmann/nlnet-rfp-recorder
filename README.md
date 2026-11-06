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
rfp mou secret "https://nlnet.nl/rfp/?project=...&secret=..."   # your project's RfP page
rfp mou update                             # scrape tasks, descriptions and remaining budget
rfp task select 10a
rfp start https://github.com/collective/icalendar/issues/1708
rfp stop
rfp report
```

| Command | Description |
| --- | --- |
| `rfp review <link>` | Instead of `rfp start`: tags the entry as review, not implementation. |
| `rfp review [<task>] --batch` | Reads one link per line; each is run as `rfp review [<task>] <link>` as soon as you press Enter. Ctrl+D ends. |
| `rfp start <link>` with no task selected | Starts tracking right away, then asks which task to assign it to - Enter picks the last task worked on, `?` lists tasks (as `rfp task list` does). |
| `rfp edit --duration 50` / `--duration 1:20` | Fix the last entry's duration - forgot to `rfp stop`? Sets it outright (minutes, or `H:MM`), moving its start time; a still-running entry keeps running. |
| `rfp edit --duration +15` / `--duration -1:20` | Nudge the last entry's duration up or down by that amount instead of setting it outright. |
| `rfp edit --start 9:00` / `--end 17:30` | Set the last entry's start or end to a local time (today; a time later than now asks if you mean yesterday). Then asks whether the duration changes (default) or the whole entry moves by that amount. A running entry only accepts `--start`. |
| `rfp background-task` / `--check` / `--log` | Start it with your session (Ctrl-C to quit): when the laptop suspends (a gap of over a minute between one-second checks) the running entry is ended at the last moment before it. If the previous run never shut down cleanly (shutdown, crash), the entry is ended at its last check-in when this starts. It restarts itself when a newer version migrates the database. All output goes to the terminal and to `rfp-background-task.log` next to the database - `--log` prints it, `--recover` recovers right away and exits, `--check` reports whether it is running (pid, last check-in; exit code 1 if not). |
| `rfp task plot complete [--hours\|--percent] [--all]` | Stacked bar per task (`--hours` shows h:mm and `--percent` the percent of the maximum instead of €) in percent of its maximum, so all bars are equally wide: light green reported, dark green worked since, yellow rest of the assigned budget, light yellow what is over the budget, red rest of the maximum, light red the part of it NLnet has no budget left for, and right-most grey what others used on the RfP page (used minus my reports) - the limit for what you can still claim. Each row shows the used and the assigned personal budget (`xxx€ / XXX€`, right-aligned). Tasks without an assigned budget are left out unless you pass `--all`; a blank line separates task groups. Fits the terminal width. |
| `rfp mou plot weeks [--weeks N]` | Bars of the hours worked in every week since the MoU started, newest first (the current one both actual and projected: time worked so far ÷ time passed this week × 7 days) against the weekly hours required to finish by the deadline: green when over it, yellow when under it, followed by red for what is missing. |
| `rfp mou plot complete` | Bars for the whole MoU: personal budget in €, light green submitted, green worked since, and yellow where you should be today if the budget is used up evenly from the start date to the deadline. Ends with how many hours you are behind or in front, your average daily (Mo-Fr) and weekly work in total and over the last 30 days, and what is required per day and week to finish by the deadline. |
| `rfp mou plot calendar` | Heat map of hours worked per day, from the MoU's start date to its deadline (or today); fits the terminal width. |
| `rfp mou deadline [YYYY-MM-DD]` / `--clear` | Set, show or remove the MoU's deadline: the date until which all assigned personal budget needs to be used up. `rfp mou status` shows the start date (the first time record on one of its tasks), the days left and the personal budget still to use. |
| `rfp mou secret [<link\|secret>]` | Remember the secret of your NLnet Request for Payment page, from the whole link or - once the project is known, or when the MoU is named like it (2026-04-3e6) - just the secret. Without an argument, it asks for the link on standard input so the secret stays out of your shell history. |
| `rfp mou update [saved-page.html]` | Reads NLnet's RfP pages with GET requests only - it never sends a POST, so nothing can be submitted. Every request on the overview goes to the matching report (the most links and task totals in common) with its status (confirmed, approved and paid date, declined or retracted - those claim no money) and the amounts requested per task; reports of this tool are renamed to `<MoU>-<date>-<recipient>-<index on the page>`, other people's requests become reports of their own. The task list is only on the "New request" form, which NLnet opens with a POST: open it in your browser, save the page and pass the file to also update the task descriptions, maximum, remaining and used budgets and what is left. |
| `rfp mou import <path\|link>` | Alternatively import a budget document (milestone table or takentaal) from a file or stdin, or an RfP link (remembers the secret and reads the requests). |
| `rfp status` / `rfp task` / `rfp mou status` | Check where things stand. |
| `rfp report` | Counts issues once finished regardless of GitHub status, but only counts a PR once it's closed (checked live against GitHub). |
| `rfp token <token>` (`rfp token` alone for setup steps) | Avoids the unauthenticated GitHub API rate limit, which the report can hit quickly. |
| `rfp --test <command>` | Try things against a disposable database instead of your real one. |
| `rfp sync` | Sync time entries and links with your other computers through a private git repository, running `rfp mou update` at the same time. See [Sync between computers](#sync-between-computers). |
| `rfp sync --set-url <url>` / `--set-name <name>` / `--set-email <email>` | Set the repository, and the author of the sync commits - used by rfp only, no git config is changed. An empty `--set-url ""` turns sync off. GitHub repositories that are public are refused. |
| `rfp sync --auto yes\|no` | Whether every command working on time entries or links syncs before and after it runs (default: yes). `rfp sync` itself always syncs; `--no-sync` on any command skips it once. |
| `rfp sync --set-machine <id>` | This computer's id in the repository (default: host name and a random part). |

## Sync between computers

Create an empty **private** git repository (e.g. on GitHub) and run on every computer:

```bash
rfp sync --set-url git@github.com:me/rfp-sync.git
rfp sync --set-machine laptop      # optional: this computer's id
rfp sync
```

The repository is cloned to `rfp-sync/` next to the database. From then on every command that reads or changes time entries, links or tasks syncs before it runs (taking the other computers' changes) and after it ran (pushing its own) - if the repository can be reached. Commands that only touch what is not synced (`rfp version`, `rfp token`, `rfp mou list/add/select/secret/export`, `rfp alias ...`, `rfp migrate`, `rfp backup`) don't sync. Ctrl+C skips the sync, not the command; without a network the command just runs. `--no-sync` skips it for one command (`rfp start <link> --no-sync`, `rfp --no-sync status`), `rfp sync --auto no` turns it off until `--auto yes`. `rfp sync` itself always syncs, and runs `rfp mou update` for the selected MoU while it fetches.

What is synced:

- **Time entries** - start, end, why they stopped, their link, and the computer they were started on.
- **Links** - URL, task (by MoU and task name), tags, excluded from reports, and the cached GitHub title and closed date.
- **Tasks** - by MoU and task name: maximum, personal, used and remaining budget, and description. Which task is selected stays with each computer.

Not synced: MoUs (a MoU a synced task or link needs is made by name), which task is selected, reports, aliases, the GitHub token and the RfP secret.

How changes are decided:

- Every time entry and link has a last-modified time and the id of the computer it was changed on. The database stamps them whenever an entry changes, however it was changed. The newest change wins - an edit made offline at 9:00 loses to one made elsewhere at 9:30, even if it is synced later. Equal times are decided by the computer id.
- Tasks are merged value by value: every budget and the description has its own last-modified time and computer, and the newest wins. A value that was never set on a computer has none, so that computer never wipes it on the others - e.g. one that got the task only by name with a link, or never ran `rfp mou update`; it takes the value over instead. A value cleared on purpose (e.g. by `rfp task import`) stays cleared. A task is only deleted while no link or report on this computer needs it; removing a MoU removes its tasks from the sync too.
- A deletion is kept in the files as `"deleted": true` with its time and computer, so it reaches every computer.
- Time entries come first. A link deleted on one computer comes back if another computer has time entries on it, and a time entry that is in a report on this computer is kept even if another computer deleted it.
- Every computer writes only its own file, `<computer id>.jsonl`, so git never has to merge. A computer's id can't be one another computer already uses; a database copied to another host gets an id of its own.
- The same entry on two computers from before sync (e.g. a copied database, where the upgrade gave each copy its own id) - same link, start and end to the microsecond - is kept once.
- Times are written with their UTC offset, so computers in different time zones agree on the moment.
- Only the computer that started a timer stops it on suspend (`rfp background-task`). `rfp stop` and `rfp start` still stop a timer started on another computer.

Sync warns when timers run on several computers at once, when a computer's clock is ahead (its changes would win over newer ones), and when it skips an entry it can't read.

Starting on a new computer works from an empty database:

```bash
rfp sync --set-url git@github.com:me/rfp-sync.git
rfp sync                       # all time entries, links and tasks with their budgets
rfp mou select <name>          # then the reports
rfp mou secret <link>
rfp sync
```

## Budgets

Every task has these budgets (EUR):

| Budget | Meaning |
| --- | --- |
| personal | What you decided to spend on the task. Fixed, but you can change it by hand (`rfp task set budget`). Falls back to the max budget. |
| max | What the MoU allows for the task. Fixed (`rfp mou import`/`update`). |
| reported | Calculated from the reports of the task's time records. Variable. |
| used | What NLnet's RfP page counts as used (`rfp mou update`, `rfp task set used`). Variable and independent of the reported budget. |
| remaining | What NLnet still has available for the task (`rfp mou update`). |
| unreported | Calculated from the time records that are in no report yet. |
| used by others | The used budget minus your reported budget (never below 0). |

`rfp task plot complete` draws them as one bar per task, left to right: light green reported, dark green unreported (worked since), yellow the rest of the personal budget, light yellow what is over it, red the rest of the maximum, light red the part of it NLnet has no budget left for, and - right-most - grey what others used. The grey part limits how much you can still claim.

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
# ai.pycal.org
