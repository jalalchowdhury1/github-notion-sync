"""Tests for fleet_health digest logic.

Run: python3 -m unittest test_fleet_health -v   (stdlib only, no deps)
"""
import unittest

import fleet_health as fh


def stale_gh(name, repo, days=1.2):
    """A gh_run probe that found NO run at all — the job never started."""
    return {"name": name, "repo": repo, "probe": "gh_run", "ok": False,
            "detail": f"no run in {days}d (limit 24h)\nlast run: https://x/1",
            "cfg": f"probe=gh_run · repo={repo} · max_age_h=24"}


def broken_gh(name, repo):
    """A gh_run probe whose run DID start and then failed — a real repo bug."""
    return {"name": name, "repo": repo, "probe": "gh_run", "ok": False,
            "detail": "last run failure (2h ago)\nrun: https://x/2",
            "cfg": f"probe=gh_run · repo={repo} · max_age_h=24"}


def healthy(name):
    return {"name": name, "repo": None, "probe": "web_200", "ok": True,
            "detail": "HTTP 200", "cfg": "probe=web_200"}


class CorrelatedStaleness(unittest.TestCase):

    def test_stale_probes_in_different_repos_are_reported_as_one_event(self):
        results = [stale_gh("a (daily)", "repo-one"),
                   stale_gh("b (nightly)", "repo-two"),
                   healthy("c")]
        digest = fh.format_digest(results)
        self.assertIn("ONE event", digest)
        self.assertIn("2 repos", digest)

    def test_one_event_banner_names_the_dispatcher_as_the_suspect(self):
        results = [stale_gh("a", "repo-one"), stale_gh("b", "repo-two")]
        self.assertIn("dispatcher", fh.format_digest(results))

    def test_stale_probes_inside_one_repo_do_not_blame_github(self):
        results = [stale_gh("a (daily)", "solo"), stale_gh("a (weekly)", "solo")]
        digest = fh.format_digest(results)
        self.assertIn("ONE event", digest)
        self.assertIn("same repo", digest)
        self.assertNotIn("dispatcher", digest)

    def test_a_single_stale_probe_gets_no_banner(self):
        results = [stale_gh("a", "repo-one"), healthy("c")]
        self.assertNotIn("ONE event", fh.format_digest(results))

    def test_runs_that_started_and_failed_are_not_correlated_staleness(self):
        results = [broken_gh("a", "repo-one"), broken_gh("b", "repo-two")]
        self.assertNotIn("ONE event", fh.format_digest(results))

    def test_banner_survives_when_the_digest_has_to_be_trimmed(self):
        many = []
        for i in range(30):
            r = stale_gh(f"job-{i}", f"repo-{i}")
            r["detail"] += "\n" + ("x" * 900)
            many.append(r)
        digest = fh.format_digest(many)
        self.assertLessEqual(len(digest), fh.TELEGRAM_LIMIT)
        self.assertIn("ONE event", digest)


if __name__ == "__main__":
    unittest.main()
