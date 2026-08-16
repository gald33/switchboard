"""`python -m switchboard_viewer`, for anyone who would rather not rely on a
console script being on PATH."""

from .viewer import main

raise SystemExit(main())
