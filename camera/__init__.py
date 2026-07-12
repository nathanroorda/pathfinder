"""Backend-agnostic camera interface.

The rest of the app imports connect/disconnect from here, so swapping the
backend (e.g. a future bluetooth module) means changing only this import.
"""
from .gp2 import connect, disconnect

__all__ = ["connect", "disconnect"]