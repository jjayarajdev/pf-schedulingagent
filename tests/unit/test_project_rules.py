"""Tests for ProjectStatusRules — scheduling constraint validation."""

import pytest

from tools.project_rules import ProjectStatusRules


class TestCanSchedule:
    def test_new_project_can_schedule(self):
        allowed, reason = ProjectStatusRules.can_schedule("new")
        assert allowed is True
        assert reason == "OK"

    def test_pending_reschedule_can_schedule(self):
        allowed, _ = ProjectStatusRules.can_schedule("Pending Reschedule")
        assert allowed is True

    def test_not_scheduled_can_schedule(self):
        allowed, _ = ProjectStatusRules.can_schedule("not scheduled")
        assert allowed is True

    def test_already_scheduled_cannot_schedule(self):
        allowed, reason = ProjectStatusRules.can_schedule("scheduled")
        assert allowed is False
        assert "already" in reason.lower() or "reschedule" in reason.lower()

    def test_completed_cannot_schedule(self):
        allowed, reason = ProjectStatusRules.can_schedule("completed")
        assert allowed is False

    def test_cancelled_cannot_schedule(self):
        allowed, reason = ProjectStatusRules.can_schedule("cancelled")
        assert allowed is False

    def test_on_hold_cannot_schedule(self):
        allowed, reason = ProjectStatusRules.can_schedule("on hold")
        assert allowed is False

    def test_in_progress_blocked(self):
        allowed, reason = ProjectStatusRules.can_schedule("in progress")
        assert allowed is False
        assert "contact" in reason.lower()

    def test_pending_confirmation_blocked(self):
        allowed, reason = ProjectStatusRules.can_schedule("pending confirmation")
        assert allowed is False

    def test_unknown_status_allowed(self):
        allowed, _ = ProjectStatusRules.can_schedule("some-unknown-status")
        assert allowed is True

    def test_case_insensitive(self):
        allowed, _ = ProjectStatusRules.can_schedule("NEW")
        assert allowed is True

    def test_whitespace_handled(self):
        allowed, _ = ProjectStatusRules.can_schedule("  new  ")
        assert allowed is True


class TestCanReschedule:
    def test_scheduled_can_reschedule(self):
        allowed, _ = ProjectStatusRules.can_reschedule("scheduled")
        assert allowed is True

    def test_new_without_date_cannot_reschedule(self):
        allowed, reason = ProjectStatusRules.can_reschedule("new", has_scheduled_date=False)
        assert allowed is False
        assert "doesn't have" in reason.lower()

    def test_new_with_date_can_reschedule(self):
        """Data inconsistency: schedulable status but has a date — allow reschedule."""
        allowed, _ = ProjectStatusRules.can_reschedule("new", has_scheduled_date=True)
        assert allowed is True

    def test_completed_cannot_reschedule(self):
        allowed, _ = ProjectStatusRules.can_reschedule("completed")
        assert allowed is False

    def test_in_progress_cannot_reschedule(self):
        allowed, _ = ProjectStatusRules.can_reschedule("in progress")
        assert allowed is False

    def test_unknown_status_cannot_reschedule(self):
        allowed, _ = ProjectStatusRules.can_reschedule("mystery-status")
        assert allowed is False


class TestCanCancel:
    def test_scheduled_can_cancel(self):
        allowed, _ = ProjectStatusRules.can_cancel("scheduled")
        assert allowed is True

    def test_new_without_date_cannot_cancel(self):
        allowed, reason = ProjectStatusRules.can_cancel("new", has_scheduled_date=False)
        assert allowed is False
        assert "doesn't have" in reason.lower()

    def test_new_with_date_can_cancel(self):
        allowed, _ = ProjectStatusRules.can_cancel("new", has_scheduled_date=True)
        assert allowed is True

    def test_completed_cannot_cancel(self):
        allowed, _ = ProjectStatusRules.can_cancel("completed")
        assert allowed is False


class TestNeedsCancelBeforeReschedule:
    def test_scheduled_needs_cancel(self):
        assert ProjectStatusRules.needs_cancel_before_reschedule("scheduled") is True

    def test_new_with_date_no_cancel_needed(self):
        assert ProjectStatusRules.needs_cancel_before_reschedule("new", has_scheduled_date=True) is False

    def test_new_without_date_no_cancel_needed(self):
        assert ProjectStatusRules.needs_cancel_before_reschedule("new") is False
