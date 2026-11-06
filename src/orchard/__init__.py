"""Orchard: per-workspace dev containers on Incus, behind one ssh front door."""

__version__ = "0.3.0"


class OrchardError(Exception):
    """Base class for every error the CLI reports as exit status 1."""
