"""MUSCLE3 recorder actor -- temporarily disabled.

The previous implementation depended on libmuscle internals (the private
``Instance._communicator`` and its end-of-stream signalling), which broke
with muscle3 0.11 and would break again on any future internal change. It
is switched off until a design based only on muscle3's public API is in
place; the old implementation is in the git history.
"""

raise ImportError("Recorder actor is temporarily disabled")
