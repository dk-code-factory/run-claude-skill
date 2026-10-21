# Repository guidance

Keep this plugin self-contained. Runtime dependencies are Python's standard
library and Claude Code; do not import another workflow package.

Run `python3 -m unittest discover -s tests -v` for behavioral checks. Tests must
not call a live model or require credentials. Use an explicit opt-in command
for live smoke tests. Never save credentials or private review output here.
