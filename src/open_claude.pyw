"""Open Claude Desktop in the last account used (instead of always the default profile)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_core  # noqa: E402

claude_core.open_last()
