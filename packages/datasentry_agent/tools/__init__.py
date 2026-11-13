"""Read-only tools. There is deliberately no write tool in this package.

Note what is NOT re-exported here: the ``catalog()`` helper. Binding a function
of that name onto the package would shadow ``datasentry_agent.tools.catalog``,
the module -- so ``import datasentry_agent.tools.catalog`` would silently hand
back a function instead of a module. Import the helper from the module that
defines it.
"""

from datasentry_agent.tools.catalog import ToolContext, ToolResult, run_tool, to_evidence

__all__ = ["ToolContext", "ToolResult", "run_tool", "to_evidence"]
