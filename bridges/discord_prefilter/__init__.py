"""Discord pre-filter bridge.

A standalone Discord bot (its own token, scoped to one channel) that triages
each inbound message with a cheap model on the Max subscription (``claude -p``,
no API call) into trivial / escalate / noise — answering trivial messages
itself for free and escalating real work to the full Hermes agent.

See ``README.md`` for setup and the project plan for rationale.
"""
