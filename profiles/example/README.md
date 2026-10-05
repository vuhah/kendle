# A workspace profile

A profile is what makes kendle fit one team: the `kendle.toml` at the workspace root, plus any scripts
its gate steps call. It belongs to the workspace, not to kendle - keep yours with the workspace (or
in a private repo of its own), never in kendle's.

- [`kendle.toml`](kendle.toml) - every setting, with its default and a line on what it does.
- [`gate/check-message.py`](gate/check-message.py) - a commit-message check for `[gate] message`,
  showing the one protocol a gate script needs: exit 0 to pass, and `::warn:: <name>: <detail>` /
  `::fail:: <name>: <detail>` lines for extra rows in the gate's report.

`kendle init` writes a minimal `kendle.toml`; copy what you need from here.
