# Contributing to Acggr

Thanks for taking the time to improve Acggr. Code is not the only way to help: a reproducible bug report, one translated string, or a documentation fix counts just as much.

**English** · [简体中文](contribute.md)

| I want to… | Go to |
| --- | --- |
| Report a bug | [Open a bug report](https://github.com/jasoneri/acggr/issues/new?template=bug-report.yml) |
| Request a feature | [Open a feature request](https://github.com/jasoneri/acggr/issues/new?template=feature-request.yml) |
| Contribute code | Send a PR against `pyapp`, see [Contributing code](#contributing-code) |
| Translate | See [Contributing translations](#contributing-translations-i18n) |
| Fix documentation | Markdown under `docs/`, same PR flow |

## Reporting a bug

Search [Issues](https://github.com/jasoneri/acggr/issues) first so the same problem is not reported twice. Include:

1. **Version** — the `ACGGR-py v...` string in the main window title bar.
2. **OS** — Windows 10/11, macOS, or other.
3. **Steps to reproduce** — the shortest sequence from launch to the failure.
4. **Logs** — attach the log file; do not paste the whole log into the description.
5. **Configuration** — the relevant part of `%LOCALAPPDATA%\Acggr\conf.yml`, or screenshots of the settings dialog. Blur private data.
6. **Screenshots / recordings** — for UI problems, a picture saves a round trip.

One issue per problem. If several problems are mixed together, use the most severe one as the title.

## Requesting a feature

Search [Issues](https://github.com/jasoneri/acggr/issues) first. Describe the use case, the expected behaviour, and any alternatives you considered. For a new site integration, include the site URL, what should be scraped, and the login requirements.

## Contributing code

### Environment

Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```bash
uv sync                 # install dependencies
uv run acggr.py         # launch the GUI
```

Use `QT_QPA_PLATFORM=offscreen` on a headless machine. For an import-only smoke test use `uv run acggr.py --check` (equivalent to CI's `ACGG_CHECK_MODE=1`).

### Branches and pull requests

- Branch off `pyapp` and name the branch `pyapp/<site>-<topic>`, e.g. `pyapp/danbooru-pin-tabs` or `pyapp/pawchive-grid`.
- Pull requests target `pyapp`. `main` is a release snapshot and does not accept feature PRs.
- One topic per PR, with a short note on what changed, why, and how it was verified.
- Open an issue for a new feature before writing it, so the direction can be agreed on first.

### Conventions

Layering is a hard rule: `app -> ui -> core -> res`. `ui`/`core`/`res` must not import `app`; `core`/`res` must not import `ui`.

User-visible text uses semantic ids only:

```python
label.setText(qtTrId("browser.toolbar.copy_unfinished"))   # right
label.setText("copy unfinished")                             # wrong
```

Quick checklist:

- Translation ids and wording live apart: code calls `qtTrId("<semantic id>")`, wording lives in `res/i18n/<lang>.yml`.
- Only `res/material_ct.py` and `res/i18n/<lang>.yml` are tracked under `res/`; `.qrc` and raw svg/png/ico are ignored, and `material_ct.py` must not be edited by hand.
- Page scripts (JS injected into the browser) live in `ui/core/browser/js/*.js`; do not embed them in Python strings.
- Docstrings and comments state contracts and constraints only — no history, no measurement stories.
- Do not commit `test/`, local probes, or debugging scripts; a feature PR contains feature code only.

### Before you submit

```bash
uv run acggr.py --check      # must pass
uv run ruff check .          # self-check; existing warnings need not be fixed all at once
```

For UI or theme changes, attach screenshots (one light, one dark) and confirm both themes keep the same layout.

## Contributing translations (i18n)

Acggr's UI text is **id-based**: code holds semantic ids only, and every translation lives in `res/i18n/<lang>.yml`. Two languages are shipped: `en_US` and `zh_CN`.

### File shape

```yaml
# res/i18n/en_US.yml
browser:
  toolbar:
    copy_unfinished: copy unfinished
```

`res/i18n/zh_CN.yml` has the same structure with the Chinese wording. The nesting mirrors the dotted id: `browser.toolbar.copy_unfinished`.

### Adding or changing wording

1. Use `qtTrId("feature.widget.element")` in code; ids are lowercase, dot-separated, at least three segments.
2. Sync the new id into every language table:

   ```bash
   uv run python res/i18n/build.py --sync     # add new keys, drop unused ones
   ```

3. Fill in the wording for each language in `res/i18n/<lang>.yml`; empty values are not allowed.
4. Validate:

   ```bash
   uv run python res/i18n/build.py --check    # missing keys, dead keys, empty values, .qm compile check
   ```

### Rules

- **Ids are a contract.** Do not rename an existing id: that is a new key, and every language table breaks at once.
- **English is not free.** Each language maintains its own yml; there is no "show the source language" fallback.
- Language-dependent site values and protocol values belong in `res/lang_data.py`, not in code.
- Adding a language: create `res/i18n/<lang>.yml`, register the code in `SUPPORTED_LANGUAGES` in `res/lang_data.py`, and add its key to `ARIANG_LANGUAGE_KEYS` in `ui/core/browser/aria2_bridge.py` for the embedded AriaNg page.
- A missing translation shows the id itself in the UI. That is deliberate: gaps stay visible.

### Runtime behaviour

At startup `res.i18n` compiles the yml files into `.qm` and installs them into the `QApplication`; `.qm`, `.ts` and cache stamps are never committed. Switching the UI language restarts the process (Qt evaluates `qtTrId` when a widget is built). The embedded AriaNg page is the exception and repaints in place.

## Contributing documentation

- Documentation lives in `docs/` and is markdown only.
- Follow the [Chinese copywriting guidelines](https://github.com/sparanoid/chinese-copywriting-guidelines) for Chinese text.
- Explain which section changed and why in the PR description; "update docs" is not enough.

## Commit messages

One line naming the area and the change, plus a reason when it is not obvious:

```text
danbooru: fix viewer wheel paging
pawchive: keep selection across tab switches
```

## Security

Do not open a public issue for a security problem. Contact the maintainer privately via the email on the repository profile.