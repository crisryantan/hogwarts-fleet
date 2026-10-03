"""The Hogwarts fleet scripts and hooks.

Standard library only, on the Mac's /usr/bin/python3. Same rules as the store:
no environment variables, absolute binary paths, every location a constant in
fleet.config, and store access only through the hogwarts package API.

Every entry point runs through the wrapper line used by bin/castle:
/usr/bin/env -i /usr/bin/python3 -I -B -X pycache_prefix=/var/empty -c 'import sys; sys.path.insert(0, "/Users/crisryantan/.hogwarts"); from fleet.<module> import main; sys.exit(main())'
"""

__version__ = "0.1.0"
