<!-- You can erase any parts of this template not applicable to your Pull Request. -->

## Description
<!-- 简要描述本次 PR 的改动内容 / Write a brief description of the changes introduced by this PR -->

## Related Issues
<!--
  Link to the issue that is fixed by this PR (if there is one)
  e.g. Fixes #1234, Addresses #1234, Related to #1234, etc.
-->

### Checklist:

* [ ] PR 目标分支为 `pyapp`，分支名符合 `pyapp/<site>-<topic>` 规范
* [ ] Have you checked to ensure there aren't other open [Pull Requests](../../pulls) for the same update/change?
* [ ] Have you linted your code with [ruff](https://docs.astral.sh/ruff/) locally prior to submission?
* [ ] Have you successfully ran `uv run acggr.py --check` with your changes locally? (UI 相关改动另附截图或说明 / attach screenshots or notes for UI changes)
* [ ] Have you respected the layer constraints (`app -> ui -> core -> res`, no reverse imports)?
