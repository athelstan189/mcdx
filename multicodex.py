#!/usr/bin/env python3
import curses

from mcdx.cli import main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, EOFError, curses.error) as err:
        raise SystemExit(str(err))
    except KeyboardInterrupt:
        raise SystemExit(130)
