"""Shared project access control for personal and organization-owned projects."""

from __future__ import annotations

from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied

from apps.projects.models import Project

ROLE_RANK = {
    "viewer": 0,
    "member": 1,
    "admin": 2,
    "owner": 3,
}


def _role_rank(role: str) -> int:
    return ROLE_RANK.get(role, -1)


def org_membership(user, organization):
    if organization is None:
        return None
    from apps.organizations.models import Membership

    return Membership.objects.filter(organization=organization, user=user).first()


def user_org_role(user, organization) -> str | None:
    membership = org_membership(user, organization)
    return membership.role if membership else None


def projects_for_user(user):
    from apps.organizations.models import Membership

    org_ids = Membership.objects.filter(user=user).values_list("organization_id", flat=True)
    return Project.objects.filter(owner=user) | Project.objects.filter(organization_id__in=org_ids)


def get_project_for_user(user, slug: str, *, min_role: str = "viewer") -> Project:
    project = get_object_or_404(Project, slug=slug)

    from apps.stripe_core.hub_keys import HUB_SLUG
    from apps.stripe_core.portfolio_workspace import reconcile_hub_workspace

    if project.slug != HUB_SLUG:
        reconcile_hub_workspace(project)

    if project.organization_id:
        membership = org_membership(user, project.organization)
        if not membership:
            raise Http404
        if _role_rank(membership.role) < _role_rank(min_role):
            raise PermissionDenied("Insufficient organization role for this action")
        return project

    if project.owner_id != user.id:
        raise Http404
    return project


class ProjectOwnedMixin:
    """Resolve a project the current user may access."""

    project_min_role = "member"

    def get_project(self, project_slug: str, *, min_role: str | None = None) -> Project:
        role = min_role if min_role is not None else self.project_min_role
        return get_project_for_user(self.request.user, project_slug, min_role=role)
