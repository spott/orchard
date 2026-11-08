"""Orchard: per-workspace dev containers on Incus, behind one ssh front door."""

__version__ = "0.4.5"


class OrchardError(Exception):
    """Base class for every error the CLI reports as exit status 1."""
