"""Seatbelt and experimental Linux Landlock sandbox environments for Bub."""

import logging

from bub_overalls.config import OverallsConfig
from bub_overalls.environment import OverallsEnvironment

__all__ = ["OverallsConfig", "OverallsEnvironment"]

logging.getLogger(__name__).addHandler(logging.NullHandler())
