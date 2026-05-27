"""Agent internals -- extracted modules from run_agent.py.

These modules contain pure utility functions and self-contained classes
that were previously embedded in the 3,600-line run_agent.py. Extracting
them makes run_agent.py focused on the AIAgent orchestrator class.
"""

# Apply runtime patches against upstream SDK quirks before any agent code
# runs.  Importing this module here (rather than editing files under venv/)
# means `uv sync` / `pip install` cannot silently undo the patch.
from agent import openai_sdk_patches as _openai_sdk_patches  # noqa: F401
