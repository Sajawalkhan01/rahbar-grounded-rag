"""Errors a provider raises. The answer service retries once on any of these, then falls
back, so a down or confused model never becomes a 500 for the user."""

from __future__ import annotations


class ProviderError(RuntimeError):
    """The model could not be reached, timed out, or returned an unusable reply."""
