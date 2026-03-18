"""Tenacity retry decorators for transient AWS errors."""

import logging

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)


def _before_log(retry_state):
    logger.warning(
        "Retry attempt %d for %s",
        retry_state.attempt_number,
        retry_state.fn.__name__ if retry_state.fn else "unknown",
    )


retry_bedrock = retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=5, jitter=1),
    before_sleep=_before_log,
    reraise=True,
)

retry_s3 = retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.3, max=3, jitter=0.5),
    before_sleep=_before_log,
    reraise=True,
)

retry_secrets = retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(2),
    wait=wait_exponential_jitter(initial=0.5, max=3, jitter=0.5),
    before_sleep=_before_log,
    reraise=True,
)
