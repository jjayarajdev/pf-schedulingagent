"""Project status rules for scheduling actions.

Centralizes validation logic for what actions are allowed based on project status.
Ported from v1.2.9's workflow_state_machine.py ProjectStatusRules.
"""

import logging

logger = logging.getLogger(__name__)


class ProjectStatusRules:
    """Rules for what scheduling actions are allowed based on project status.

    Each check method returns a ``(bool, str)`` tuple: ``(allowed, reason)``.
    Status strings are normalized to lowercase for comparison.
    """

    # Status categories (lowercase for comparison)
    SCHEDULABLE: set[str] = {"new", "pending reschedule", "not scheduled", "ready to schedule"}
    SCHEDULED: set[str] = {
        "scheduled", "tentatively scheduled", "customer scheduled",
        "store scheduled", "install scheduled", "hdms scheduled",
    }
    TERMINAL: set[str] = {"completed", "cancelled", "closed", "on hold"}
    BLOCKED: set[str] = {"in progress", "pending confirmation"}

    @classmethod
    def _normalize(cls, status: str) -> str:
        """Normalize a status string for comparison."""
        return status.lower().strip() if status else ""

    # ------------------------------------------------------------------
    # Schedule
    # ------------------------------------------------------------------

    @classmethod
    def can_schedule(cls, status: str, has_scheduled_date: bool = False) -> tuple[bool, str]:
        """Check if a project can be scheduled.

        Returns:
            ``(allowed, reason)`` -- *reason* is ``"OK"`` when allowed.
        """
        s = cls._normalize(status)

        if s in cls.SCHEDULED:
            return False, f"Project is already {status}. Would you like to reschedule?"

        if s in cls.TERMINAL:
            return False, f"Project is {status} and cannot be scheduled."

        if s in cls.BLOCKED:
            return False, f"Project is {status} and cannot be scheduled right now. The office can help — offer to transfer the customer."

        if s in cls.SCHEDULABLE:
            return True, "OK"

        # Unknown status -- allow but log
        logger.warning("Unknown project status for schedule check: %s", status)
        return True, "OK"

    # ------------------------------------------------------------------
    # Reschedule
    # ------------------------------------------------------------------

    @classmethod
    def can_reschedule(cls, status: str, has_scheduled_date: bool = False) -> tuple[bool, str]:
        """Check if a project can be rescheduled.

        Returns:
            ``(allowed, reason)``
        """
        s = cls._normalize(status)

        if s in cls.SCHEDULED:
            return True, "OK"

        # Data inconsistency: schedulable status but has a date
        if s in cls.SCHEDULABLE and has_scheduled_date:
            logger.info(
                "Data inconsistency: status=%s but has_scheduled_date=True", status
            )
            return True, "OK"

        if s in cls.SCHEDULABLE:
            return False, "This project doesn't have a scheduled appointment to reschedule."

        if s in cls.TERMINAL or s in cls.BLOCKED:
            return False, f"Project is {status} and cannot be rescheduled. The office can help — offer to transfer the customer."

        logger.warning("Unknown project status for reschedule check: %s", status)
        return False, f"Cannot reschedule project with status: {status}"

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    @classmethod
    def can_cancel(cls, status: str, has_scheduled_date: bool = False) -> tuple[bool, str]:
        """Check if a project appointment can be cancelled.

        Returns:
            ``(allowed, reason)``
        """
        s = cls._normalize(status)

        if s in cls.SCHEDULED:
            return True, "OK"

        if s in cls.SCHEDULABLE and has_scheduled_date:
            return True, "OK"

        if s in cls.SCHEDULABLE:
            return False, "This project doesn't have a scheduled appointment to cancel."

        return False, f"Project is {status} and cannot be cancelled."

    # ------------------------------------------------------------------
    # Cancel-before-reschedule check
    # ------------------------------------------------------------------

    @classmethod
    def needs_cancel_before_reschedule(cls, status: str, has_scheduled_date: bool = False) -> bool:
        """Check if the existing appointment must be cancelled before rescheduling.

        Returns:
            ``True`` if a cancel step is needed first.
        """
        s = cls._normalize(status)

        if s in cls.SCHEDULED:
            return True

        # Data inconsistency -- might already be unscheduled on PF side
        if s in cls.SCHEDULABLE and has_scheduled_date:
            return False

        return False
