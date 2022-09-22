import re
from datetime import datetime, timedelta
from functools import cached_property
from typing import Dict, Optional, Tuple

import pytz
from dateutil import parser
from dateutil.relativedelta import relativedelta
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from posthog.models.team import Team
from posthog.queries.util import PERIOD_TO_TRUNC_FUNC, TIME_IN_SECONDS, get_earliest_timestamp


class QueryDateRange:
    def __init__(self, filter, team: Team, should_round: Optional[bool] = None, table="") -> None:
        self._filter = filter
        self._team = team
        self._table = f"{table}." if table else ""
        self._should_round = should_round

    @cached_property
    def date_to_param(self) -> datetime:
        if isinstance(self._filter._date_to, str):
            return self._parse_date(self._filter._date_to)
        elif isinstance(self._filter._date_to, datetime):
            return self._localize_to_team(self._filter._date_to)
        else:
            return self._now

    def get_earliest_timestamp(self):
        try:
            earliest_date = get_earliest_timestamp(self._team.pk)
        except IndexError:
            return timezone.now()  # TODO: fix
        else:
            return earliest_date

    @cached_property
    def date_from_param(self) -> datetime:
        if self._filter._date_from == "all":
            return self.get_earliest_timestamp()
        elif isinstance(self._filter._date_from, str):
            return self._parse_date(self._filter._date_from)
        elif isinstance(self._filter._date_from, datetime):
            return pytz.timezone(self._team.timezone).localize(self._filter._date_from)
        else:
            return self.get_earliest_timestamp()

    @cached_property
    def _now(self):
        return self._localize_to_team(timezone.now())

    def _localize_to_team(self, target: datetime):
        return target.astimezone(pytz.timezone(self._team.timezone))

    def _parse_date(self, input):

        try:
            return self._localize_to_team(datetime.strptime(input, "%Y-%m-%d"))
        except ValueError:
            pass

        # when input also contains the time for intervals "hour" and "minute"
        # the above try fails. Try one more time from isoformat.
        try:
            return self._localize_to_team(parser.isoparse(input))
        except ValueError:
            pass

        regex = r"\-?(?P<number>[0-9]+)?(?P<type>[a-z])(?P<position>Start|End)?"
        match = re.search(regex, input)
        date = self._now
        if not match:
            return date
        if match.group("type") == "h":
            date -= relativedelta(hours=int(match.group("number")))
            return date.replace(minute=0, second=0, microsecond=0)
        elif match.group("type") == "d":
            if match.group("number"):
                date -= relativedelta(days=int(match.group("number")))
        elif match.group("type") == "w":
            if match.group("number"):
                date -= relativedelta(weeks=int(match.group("number")))
        elif match.group("type") == "m":
            if match.group("number"):
                date -= relativedelta(months=int(match.group("number")))
            if match.group("position") == "Start":
                date -= relativedelta(day=1)
            if match.group("position") == "End":
                date -= relativedelta(day=31)
        elif match.group("type") == "q":
            if match.group("number"):
                date -= relativedelta(weeks=13 * int(match.group("number")))
        elif match.group("type") == "y":
            if match.group("number"):
                date -= relativedelta(years=int(match.group("number")))
            if match.group("position") == "Start":
                date -= relativedelta(month=1, day=1)
            if match.group("position") == "End":
                date -= relativedelta(month=12, day=31)
        return date

    @cached_property
    def interval_annotation(self) -> str:
        period = self._filter.interval
        if period is None:
            period = "day"
        ch_function = PERIOD_TO_TRUNC_FUNC.get(period.lower())
        if ch_function is None:
            raise ValidationError(f"Period {period} is unsupported.")
        return ch_function

    @cached_property
    def date_to_clause(self):
        return f"AND {self._table}timestamp <= toDateTime(%(date_to)s, %(timezone)s)"

    @cached_property
    def date_from_clause(self):
        return self._get_timezone_aware_date_condition(">=", "date_from")

    @cached_property
    def date_to(self) -> Tuple[str, Dict]:
        date_to_query = self.date_to_clause
        date_to_param = {"date_to": self.date_to_param.strftime("%Y-%m-%d %H:%M:%S")}

        return date_to_query, date_to_param

    @cached_property
    def date_from(self) -> Tuple[str, Dict]:
        date_from_query = self.date_from_clause
        date_from_param = {"date_from": self.date_from_param.strftime("%Y-%m-%d %H:%M:%S")}

        return date_from_query, date_from_param

    def _get_timezone_aware_date_condition(self, operator: str, date_clause: str) -> str:
        if self.should_round:
            # Truncate function in clickhouse will remove the time granularity and leave only the date
            # Specify that this truncated date is the local timezone target
            # Convert target to UTC so that stored timestamps can be compared accordingly
            # Example: `2022-04-05 07:00:00` -> truncated to `2022-04-05` -> 2022-04-05 00:00:00 PST -> 2022-04-05 07:00:00 UTC

            return f"AND {self._table}timestamp {operator} {self._timezone_date_clause(date_clause)}"
        else:
            return f"AND {self._table}timestamp {operator} toDateTime(%({date_clause})s)"

    def _timezone_date_clause(self, date_clause: str) -> str:
        clause = (
            f"toTimezone(toDateTime({self.interval_annotation}(toDateTime(%({date_clause})s)), %(timezone)s), 'UTC')"
        )

        if self.interval_annotation == "toStartOfWeek":
            return f"toTimezone(toDateTime(toStartOfWeek(toDateTime(%({date_clause})s), 0), %(timezone)s), 'UTC')"

        return clause

    @cached_property
    def _start_time(self) -> datetime:
        return self._filter.date_from or get_earliest_timestamp(self._team.pk)

    @cached_property
    def _end_time(self) -> datetime:
        return self._filter.date_to or timezone.now()

    @cached_property
    def time_difference(self) -> timedelta:
        return self._end_time - self._start_time

    @cached_property
    def num_intervals(self):

        if self._filter.interval == "month":
            rel_delta = relativedelta(self._end_time.replace(day=1), self._start_time.replace(day=1))
            return (rel_delta.years * 12) + rel_delta.months + 1

        return int(self.time_difference.total_seconds() / TIME_IN_SECONDS[self._filter.interval]) + 1

    @cached_property
    def should_round(self):

        if self._should_round is not None:
            return self._should_round

        round_interval = False
        if self._filter.interval in ["week", "month"]:
            round_interval = True
        else:
            round_interval = self.time_difference.total_seconds() >= TIME_IN_SECONDS[self._filter.interval] * 2

        return round_interval
