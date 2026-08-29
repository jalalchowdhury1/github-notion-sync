"""Tests for sync.py's pure status logic.

Run: python3 -m unittest test_sync -v   (stdlib only, no deps)

The archived case matters more than it looks. compute_status has always had an
`is_archived -> "Archived"` branch, but until 2026-08-29 it was UNREACHABLE in
production: main() listed repos with include_archived=False, so an archived repo
fell out of `seen_urls` and hit the "vanished from GitHub" sweep instead, landing
in Notion as Status=Deleted. Archived is not deleted -- the repo is still there,
deliberately frozen. These tests pin the intended labelling now that the branch
is actually reachable.
"""
import unittest
from datetime import datetime, timedelta, timezone

import sync


def repo(*, archived=False, pushed_days_ago=1):
    pushed = datetime.now(tz=timezone.utc) - timedelta(days=pushed_days_ago)
    return sync.Repo(
        name="r", full_name="o/r", url="https://github.com/o/r", description="",
        language="Python", is_private=True, is_archived=archived,
        pushed_at=pushed.strftime("%Y-%m-%dT%H:%M:%SZ"), default_branch="main",
    )


def iso(days_ago):
    return (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class ComputeStatus(unittest.TestCase):
    def test_archived_repo_is_archived_not_deleted(self):
        self.assertEqual(sync.compute_status(repo(archived=True)), "Archived")

    def test_archived_wins_over_staleness(self):
        """A repo frozen years ago is Archived, never Stale -- archiving is the fact."""
        self.assertEqual(sync.compute_status(repo(archived=True, pushed_days_ago=900)), "Archived")

    def test_archived_wins_over_a_recent_actions_run(self):
        """Keepalive workflows can outlive the archive flag; the flag still wins."""
        self.assertEqual(
            sync.compute_status(repo(archived=True, pushed_days_ago=900), iso(0)), "Archived")

    def test_recent_push_is_active(self):
        self.assertEqual(sync.compute_status(repo(pushed_days_ago=3)), "Active")

    def test_old_push_is_stale(self):
        self.assertEqual(
            sync.compute_status(repo(pushed_days_ago=sync.STALE_AFTER_DAYS + 30)), "Stale")

    def test_recent_actions_run_rescues_an_old_push(self):
        """A cron-only repo pushes rarely but still runs -- that counts as alive."""
        self.assertEqual(
            sync.compute_status(repo(pushed_days_ago=sync.STALE_AFTER_DAYS + 30), iso(2)),
            "Active")

    def test_unparseable_actions_timestamp_falls_back_to_push_date(self):
        self.assertEqual(
            sync.compute_status(repo(pushed_days_ago=3), "not-a-date"), "Active")


if __name__ == "__main__":
    unittest.main()
