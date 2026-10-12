"""Serving: deliver stored knowledge before the agent's next decision.

Host hooks are thin. They turn a host event into a ``Moment``, hand it to
``Serving.handle`` and print the ``Reply`` in the host's shape. ``Serving``
owns all state: the per-turn budget, deny-once records, context epochs and the
per-moment log.
"""
