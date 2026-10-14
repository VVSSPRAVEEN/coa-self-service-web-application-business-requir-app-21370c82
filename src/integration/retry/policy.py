"""Retry policy configuration with exponential backoff and jitter."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """Configuration for exponential backoff retry behavior.

    Attributes:
        base_delay: Initial delay in seconds before the first retry.
        multiplier: Factor by which the delay increases after each attempt.
        max_delay: Maximum delay cap in seconds between retries.
        jitter: Maximum random jitter in seconds added to each delay.
        max_attempts: Total number of attempts (including the initial call).
    """

    base_delay: float = 1.0
    multiplier: float = 2.0
    max_delay: float = 60.0
    jitter: float = 1.0
    max_attempts: int = 5

    def get_delay(self, attempt: int) -> float:
        """Calculate the delay for a given attempt number (0-indexed).

        Args:
            attempt: The current attempt number (0 for first retry).

        Returns:
            The computed delay in seconds, capped at max_delay with jitter applied.
        """
        if attempt < 0:
            raise ValueError("Attempt must be non-negative")

        raw_delay = self.base_delay * (self.multiplier ** attempt)
        capped_delay = min(raw_delay, self.max_delay)
        jitter_value = random.uniform(0, self.jitter)
        return capped_delay + jitter_value

    def should_retry(self, attempt: int) -> bool:
        """Determine if another retry attempt is allowed.

        Args:
            attempt: The number of attempts already made (1-indexed).

        Returns:
            True if more attempts remain, False otherwise.
        """
        return attempt < self.max_attempts


DEFAULT_RETRY_POLICY: RetryPolicy = RetryPolicy()