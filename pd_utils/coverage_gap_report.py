"""
Poll PagerDuty and identify on-call coverage gaps.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from pd_utils.model import EscalationRuleCoverage as EscCoverage
from pd_utils.model import ScheduleCoverage as SchCoverage
from pd_utils.util import DateTool
from pd_utils.util import IOUtil
from pd_utils.util import PagerDutyQuery
from pd_utils.util import RuntimeInit

runtime = RuntimeInit("coverage-gap-report")
runtime.init_secrets()
runtime.init_logging()


class CoverageGapReport:
    """Poll all schedules in an instance and identify coverage gaps."""

    log = logging.getLogger()
    base_url = "https://api.pagerduty.com"

    def __init__(
        self,
        token: str,
        *,
        max_query_limit: int = 100,
        look_ahead_days: int = 14,
    ) -> None:
        """
        Required: PagerDuty API v2 token with read access.

        Args:
            max_query_limit: Number of objects to request at once from PD (max: 100)
            look_ahead_days: Number of days to look ahead on schedule (default: 14)
        """
        self._since = DateTool.utcnow_isotime()
        self._until = DateTool.add_offset(self._since, days=look_ahead_days)
        self._max_query_limit = max_query_limit
        self._schedule_map: dict[str, SchCoverage] = {}
        self._escalation_map: dict[str, EscCoverage] = {}

        headers = {
            "Accept": "application/vnd.pagerduty+json;version=2",
            "Authorization": f"Token token={token}",
        }

        self._http = httpx.Client(headers=headers)
        self._query = PagerDutyQuery(token)

    def run_reports(self) -> tuple[str, str]:
        """
        Runs reports. Returns csv strings of Schedule and Escalation reports.

        Raises:
            QueryError
        """
        self._map_schedule_coverages(self._get_all_schedule_ids())
        self._map_escalation_coverages(self._get_all_escalations())
        self._hydrate_escalation_coverage_flags()

        schedule = IOUtil.to_csv_string(list(self._schedule_map.values()))
        escalation = IOUtil.to_csv_string(list(self._escalation_map.values()))
        return schedule, escalation

    def get_schedule_coverage(self, schedule_id: str) -> SchCoverage | None:
        """Get ScheduleCoverage from PagerDuty with specific schedule id."""
        schobj: SchCoverage | None = None
        params = {
            "since": self._since,
            "until": self._until,
            "time_zone": "Etc/UTC",
        }
        resp = self._http.get(f"{self.base_url}/schedules/{schedule_id}", params=params)

        if resp.is_success:
            schobj = SchCoverage.build_from(resp.json())
        else:
            self.log.error("Error fetching schedule %s - %s", schedule_id, resp.text)

        return schobj

    def _get_all_escalations(self) -> list[dict[str, Any]]:
        """Pull all escalation polcies from PagerDuty."""
        self._query.set_query_target("/escalation_policies", "escalation_policies")
        self._query.set_query_params({})

        eps: list[dict[str, Any]] = []
        for result in self._query.run_iter(self._max_query_limit):
            eps.extend(result)
        self.log.info("Discovered %d escalation policies.", len(eps))
        return eps

    def _get_all_schedule_ids(self) -> set[str]:
        """Get all unique schedule IDs."""
        self._query.set_query_target("/schedules", "schedules")
        self._query.set_query_params({})

        sch_ids: list[str] = []
        for schedules in self._query.run_iter(self._max_query_limit):
            sch_ids.extend([sch["id"] for sch in schedules])

        self.log.info("Discovered %d schedules.", len(sch_ids))
        return set(sch_ids)

    def _map_schedule_coverages(self, schedule_ids: set[str]) -> None:
        """Map scheduleId:ScheduleCoverage object, pulling detailed object from PD."""
        self.log.info("Pulling %d schedules for coverage.", len(schedule_ids))

        for idx, sch_id in enumerate(schedule_ids, 1):
            self.log.debug("Pulling %s (%d of %d)", sch_id, idx, len(schedule_ids))

            coverage = self.get_schedule_coverage(sch_id)
            self._schedule_map.update({sch_id: coverage} if coverage else {})

    def _map_escalation_coverages(self, escalations: list[dict[str, Any]]) -> None:
        """Map scheduleId:EscCoverage object, from pulled escalations."""
        self.log.info("Mapping %d escalations for coverage", len(escalations))

        for escalation in escalations:
            ep_coverage = EscCoverage.build_from(escalation)
            rule_map = {f"{ep.policy_id}-{ep.rule_index}": ep for ep in ep_coverage}
            self._escalation_map.update(rule_map)

        self.log.info("%d total objects converted", len(self._escalation_map))

    def _hydrate_escalation_coverage_flags(self) -> None:
        """Test all mapped escalations and set each `has_gap` flag for rules."""
        # NOTE: Schedules should be mapped before this is called

        for ep_rule in self._escalation_map.values():
            times = self._extract_entries(ep_rule.rule_target_ids)
            ep_rule.is_fully_covered = DateTool.is_covered(
                time_slots=times,
                range_start=self._since,
                range_stop=self._until,
            )

    def _extract_entries(self, sch_ids: tuple[str, ...]) -> list[tuple[str, str]]:
        """Extract all timestamp entries from given schedules"""
        entries: list[tuple[str, str]] = []
        schedules = [self._schedule_map[k] for k in sch_ids if k in self._schedule_map]
        for schedule in schedules:
            entries.extend(list(schedule.entries))
        return entries


def main(*, _args: list[str] | None = None) -> int:
    """CLI entry."""
    runtime.add_standard_arguments(email=False)
    runtime.add_argument(
        flag="--look-ahead",
        default="14",
        help_="Number of days to look ahead for gaps, default 14",
    )
    args = runtime.parse_args(_args)
    client = CoverageGapReport(token=args.token, look_ahead_days=int(args.look_ahead))
    schedule_report, escalation_report = client.run_reports()

    now = DateTool.utcnow_isotime().split("T")[0]
    IOUtil.write_to_file(f"schedule_gap_report{now}.csv", schedule_report)
    IOUtil.write_to_file(f"escalation_rule_gap_report{now}.csv", escalation_report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
