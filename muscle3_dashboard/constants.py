CARD_MARGIN = 5
"""Margin for cards in dashboard"""
MAX_LINES = 1000
"""Max number of lines in terminal"""
TERMINAL_HEIGHT = 500
"""Standard height of terminal"""
UNUSED_SESSION_LIFETIME_MILLISECONDS = 5000
"""Milliseconds before inactive session should quit"""
CHECK_UNUSED_SESSIONS_MILLISECONDS = 3000
"""Milliseconds between checks if session is inactive"""
IDLE_SHUTDOWN_SECONDS = 300
"""Seconds an m3dash server waits with no open viewers before it shuts
itself down, so a forgotten server doesn't block the next launch from
rebinding its (fixed, per-user) port."""
SHOW_PORT_ICONS = False
"""Draw per-port icons in the simulation graph. Off gives a more compact
graph with conduits attached directly to component edges."""
