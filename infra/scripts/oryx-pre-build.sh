#!/usr/bin/env bash
set -euo pipefail

test -f requirements.lock
test -f frontend/package-lock.json
test -f src/receipt_bench/web/app.py

python -c 'import sys; expected = (3, 12); actual = sys.version_info[:2]; sys.exit(f"Expected Python {expected[0]}.{expected[1]}, got {actual[0]}.{actual[1]}") if actual != expected else None'

# Oryx prioritizes requirements.txt after this pre-build hook. Keep the
# repository lock authoritative without introducing another root manifest.
cp requirements.lock requirements.txt
printf '\n.\n' >> requirements.txt

# The versioned frontend bundle is built during release validation so deployers
# do not depend on the Node/npm version bundled with Oryx's Python image.
test -f src/receipt_bench/demo/static/index.html
