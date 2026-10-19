"""``python -m app`` -> the account / recovery command line (``app/cli.py``)."""

import sys

from .cli import main

sys.exit(main())
