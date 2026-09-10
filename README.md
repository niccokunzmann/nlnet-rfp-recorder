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

- `rfp review <link>` instead of `rfp start`: tags the entry as review, not implementation.
- `rfp status` / `rfp task` / `rfp mou status`: check where things stand.
- `RFP_EUROS` env var: hourly rate, needed to compute budgets.
- `rfp report` counts issues once finished regardless of GitHub status, but only counts a PR once it's closed (checked live against GitHub).
- `rfp token <token>` (`rfp token` alone for setup steps): avoids the unauthenticated GitHub API rate limit, which the report can hit quickly.
- `rfp --test <command>`: try things against a disposable database instead of your real one.

## Development

```bash
make init    # uv sync + pre-commit install
make test
```
