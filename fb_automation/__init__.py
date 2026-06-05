"""Compatibility package for running from the renamed project directory.

The project files live directly in this directory, whose name is not a valid
Python package identifier. This shim lets imports such as
``fb_automation.live_emulation`` resolve modules from the parent directory.
"""

from pathlib import Path

__path__ = [str(Path(__file__).resolve().parent.parent)]
