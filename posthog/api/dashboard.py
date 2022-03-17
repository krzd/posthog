import json
import secrets
from typing import Any, Dict, Sequence, Type, Union, cast

from django.db.models import Prefetch, QuerySet
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from django.views.decorators.clickjacking import xframe_options_exempt
from rest_framework import exceptions, mixins, response, serializers, viewsets
from rest_framework.authentication import BaseAuthentication, BasicAuthentication, SessionAuthentication
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated, OperandHolder, SingleOperandHolder
from rest_framework.request import Request

from posthog.api.insight import InsightSerializer, InsightViewSet
from posthog.api.routing import StructuredViewSetMixin
from posthog.api.shared import UserBasicSerializer
from posthog.api.tagged_item import TaggedItemSerializerMixin, TaggedItemViewSetMixin
from posthog.auth import PersonalAPIKeyAuthentication
from posthog.constants import INSIGHT_TRENDS
from posthog.event_usage import report_user_action
from posthog.helpers import create_dashboard_from_template
from posthog.models import Dashboard, Insight, Organization, Team
from posthog.models.user import User
from posthog.models.utils import get_deferred_field_set_for_model
from posthog.permissions import ProjectMembershipNecessaryPermissions, TeamMemberAccessPermission
from posthog.utils import render_template


class CanEditDashboard(BasePermission):
    message = "You don't have edit permissions for this dashboard."

    def has_object_permission(self, request: Request, view, dashboard) -> bool:
        if request.method in SAFE_METHODS:
            return True
        return dashboard.can_user_edit(cast(User, request.user).id)


class DashboardSerializer(TaggedItemSerializerMixin, serializers.ModelSerializer):
    items = serializers.SerializerMethodField()
    created_by = UserBasicSerializer(read_only=True)
    use_template = serializers.CharField(write_only=True, allow_blank=True, required=False)
    use_dashboard = serializers.IntegerField(write_only=True, allow_null=True, required=False)
    effective_privilege_level = serializers.SerializerMethodField()

    class Meta:
        model = Dashboard
        fields = [
            "id",
            "name",
            "description",
            "pinned",
            "items",
            "created_at",
            "created_by",
            "is_shared",
            "share_token",
            "deleted",
            "creation_mode",
            "use_template",
            "use_dashboard",
            "filters",
            "tags",
            "restriction_level",
            "effective_restriction_level",
            "effective_privilege_level",
        ]
        read_only_fields = [
            "creation_mode",
            "effective_restriction_level",
        ]

    def create(self, validated_data: Dict, *args: Any, **kwargs: Any) -> Dashboard:
        request = self.context["request"]
        validated_data["created_by"] = request.user
        team = Team.objects.get(id=self.context["team_id"])
        use_template: str = validated_data.pop("use_template", None)
        use_dashboard: int = validated_data.pop("use_dashboard", None)
        validated_data = self._update_creation_mode(validated_data, use_template, use_dashboard)
        tags = validated_data.pop("tags", None)  # tags are created separately below as global tag relationships
        dashboard = Dashboard.objects.create(team=team, **validated_data)

        if use_template:
            try:
                create_dashboard_from_template(use_template, dashboard)
            except AttributeError:
                raise serializers.ValidationError({"use_template": "Invalid value provided."})

        elif use_dashboard:
            try:
                from posthog.api.insight import InsightSerializer

                existing_dashboard = Dashboard.objects.get(id=use_dashboard, team=team)
                existing_dashboard_items = existing_dashboard.items.all()
                for dashboard_item in existing_dashboard_items:
                    override_dashboard_item_data = {
                        "id": None,  # to create a new Insight
                        "dashboard": dashboard.pk,
                        "last_refresh": now(),
                    }
                    new_data = {
                        **InsightSerializer(dashboard_item, context=self.context,).data,
                        **override_dashboard_item_data,
                    }
                    new_tags = new_data.pop("tags", None)
                    insight_serializer = InsightSerializer(data=new_data, context=self.context,)
                    insight_serializer.is_valid()
                    insight_serializer.save()

                    # Create new insight's tags separately. Force create tags on dashboard duplication.
                    self._attempt_set_tags(new_tags, insight_serializer.instance, force_create=True)

            except Dashboard.DoesNotExist:
                raise serializers.ValidationError({"use_dashboard": "Invalid value provided"})

        elif request.data.get("items"):
            for item in request.data["items"]:
                Insight.objects.create(
                    **{key: value for key, value in item.items() if key not in ("id", "deleted", "dashboard", "team")},
                    dashboard=dashboard,
                    team=team,
                )

        # Manual tag creation since this create method doesn't call super()
        self._attempt_set_tags(tags, dashboard)

        report_user_action(
            request.user,
            "dashboard created",
            {
                **dashboard.get_analytics_metadata(),
                "from_template": bool(use_template),
                "template_key": use_template,
                "duplicated": bool(use_dashboard),
                "dashboard_id": use_dashboard,
            },
        )

        return dashboard

    def update(self, instance: Dashboard, validated_data: Dict, *args: Any, **kwargs: Any,) -> Dashboard:
        user = cast(User, self.context["request"].user)
        can_user_restrict = instance.can_user_restrict(user.id)
        if "restriction_level" in validated_data and not can_user_restrict:
            raise exceptions.PermissionDenied(
                "Only the dashboard owner and project admins have the restriction rights required to change the dashboard's restriction level."
            )

        validated_data.pop("use_template", None)  # Remove attribute if present
        if validated_data.get("is_shared") and not instance.share_token:
            instance.share_token = secrets.token_urlsafe(22)

        instance = super().update(instance, validated_data)

        if "request" in self.context:
            report_user_action(user, "dashboard updated", instance.get_analytics_metadata())

        return instance

    def add_dive_source_item(self, items: QuerySet, dive_source_id: int):
        item_as_list = list(i for i in items if i.id == dive_source_id)
        if not item_as_list:
            item_as_list = [Insight.objects.get(pk=dive_source_id)]
        others = list(i for i in items if i.id != dive_source_id)
        return item_as_list + others

    def get_items(self, dashboard: Dashboard):
        if self.context["view"].action == "list":
            return None

        items = dashboard.items.filter(deleted=False).order_by("order").all()
        self.context.update({"dashboard": dashboard})

        dive_source_id = self.context["request"].GET.get("dive_source_id")
        if dive_source_id is not None:
            items = self.add_dive_source_item(items, int(dive_source_id))

        #  Make sure all items have an insight set
        # This should have only happened historically
        for item in items:
            if not item.filters.get("insight"):
                item.filters["insight"] = INSIGHT_TRENDS
                item.save()
        return InsightSerializer(items, many=True, context=self.context).data

    def get_effective_privilege_level(self, dashboard: Dashboard) -> Dashboard.PrivilegeLevel:
        return dashboard.get_effective_privilege_level(self.context["request"].user.id)

    def validate(self, data):
        if data.get("use_dashboard", None) and data.get("use_template", None):
            raise serializers.ValidationError("`use_dashboard` and `use_template` cannot be used together")
        return data

    def _update_creation_mode(self, validated_data, use_template: str, use_dashboard: int):
        if use_template:
            return {**validated_data, "creation_mode": "template"}
        if use_dashboard:
            return {**validated_data, "creation_mode": "duplicate"}

        return {**validated_data, "creation_mode": "default"}


class DashboardsViewSet(TaggedItemViewSetMixin, StructuredViewSetMixin, viewsets.ModelViewSet):
    queryset = Dashboard.objects.order_by("name")
    serializer_class = DashboardSerializer
    authentication_classes = [
        PersonalAPIKeyAuthentication,
        SessionAuthentication,
        BasicAuthentication,
    ]
    permission_classes = [
        IsAuthenticated,
        ProjectMembershipNecessaryPermissions,
        TeamMemberAccessPermission,
        CanEditDashboard,
    ]

    def get_queryset(self) -> QuerySet:
        queryset = super().get_queryset()
        if self.action != "update":
            # Soft-deleted dashboards can be brought back with a PATCH request
            queryset = queryset.filter(deleted=False)

        deferred_fields = set.union(
            get_deferred_field_set_for_model(
                Organization, fields_not_deferred={"available_features"}, field_prefix="team__organization__"
            ),
            get_deferred_field_set_for_model(Team, fields_not_deferred={"organization", "name"}, field_prefix="team__"),
        )

        queryset = (
            queryset.select_related("team__organization", "created_by")
            .defer(*deferred_fields)
            .prefetch_related(Prefetch("items", queryset=Insight.objects.filter(deleted=False).order_by("order")))
        )
        return queryset

    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> response.Response:
        pk = kwargs["pk"]
        queryset = self.get_queryset()
        dashboard = get_object_or_404(queryset, pk=pk)
        dashboard.last_accessed_at = now()
        dashboard.save()
        serializer = DashboardSerializer(dashboard, context={"view": self, "request": request})
        return response.Response(serializer.data)


class LegacyDashboardsViewSet(DashboardsViewSet):
    legacy_team_compatibility = True

    def get_parents_query_dict(self) -> Dict[str, Any]:
        if not self.request.user.is_authenticated or "share_token" in self.request.GET:
            return {}
        return {"team_id": self.team_id}


class SharedDashboardsViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = Dashboard.objects.filter(is_shared=True)
    serializer_class = DashboardSerializer
    authentication_classes: Sequence[Type[BaseAuthentication]] = []
    permission_classes: Sequence[Union[Type[BasePermission], OperandHolder, SingleOperandHolder]] = []
    lookup_field = "share_token"


@xframe_options_exempt
def shared_dashboard(request: HttpRequest, share_token: str):
    dashboard = get_object_or_404(Dashboard, is_shared=True, share_token=share_token)
    shared_dashboard_serialized = {
        "id": dashboard.id,
        "share_token": dashboard.share_token,
        "name": dashboard.name,
        "description": dashboard.description,
        "team_name": dashboard.team.name,
    }

    return render_template(
        "shared_dashboard.html",
        request=request,
        context={"shared_dashboard_serialized": json.dumps(shared_dashboard_serialized)},
    )


class LegacyInsightViewSet(InsightViewSet):
    legacy_team_compatibility = True
