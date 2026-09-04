# NLnet RfP Recorder

Create RfPs from work on issues and pull requests, record time.

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
