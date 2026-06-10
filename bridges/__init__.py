"""Standalone, loosely-coupled bridges that sit *beside* the Hermes gateway.

Each bridge is its own process and talks to Hermes only through public CLIs
(``hermes``, ``claude``) — never by importing gateway internals — so it stays
robust against churn in the fast-moving agent core.
"""
