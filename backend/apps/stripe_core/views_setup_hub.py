"""Setup Hub API — reset, audit, and webhook registration from the web UI."""

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.access import ProjectOwnedMixin

from apps.stripe_core.hub_keys import HUB_SLUG, sync_vault_to_billing_projects
from .setup_hub import (
    audit_stripe_account,
    register_webhooks_for_user,
    reset_workspace,
    setup_hub_status,
    sync_registry_for_user,
)
from apps.deploy.platform_bootstrap import (
    automate_project_deploy,
    bootstrap_platform_automation,
    platform_automation_status,
    reconcile_local_master_key,
)


class SetupHubView(ProjectOwnedMixin, APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, project_slug: str):
        project = self.get_project(project_slug)
        return Response(setup_hub_status(project, user=request.user))


class SetupHubActionView(ProjectOwnedMixin, APIView):
    project_min_role = "admin"
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, project_slug: str):
        project = self.get_project(project_slug, min_role="admin")
        action = (request.data.get("action") or "").strip().lower()
        dry_run = bool(request.data.get("dryRun") or request.data.get("dry_run"))

        try:
            if action == "reset":
                result = reset_workspace(
                    project,
                    clear_vault=bool(request.data.get("clearVault") or request.data.get("clear_vault")),
                )
                result["status"] = setup_hub_status(project)
                return Response(result)

            if action == "audit":
                data = audit_stripe_account(project)
                return Response(
                    {
                        "ok": True,
                        "audit": data,
                        "status": setup_hub_status(project),
                    }
                )

            if action == "register_webhooks":
                fixes = register_webhooks_for_user(request.user, dry_run=dry_run)
                ok = all(r.get("ok") for r in fixes) if fixes else False
                status_payload = setup_hub_status(project, user=request.user)
                if ok and not dry_run and project.slug == HUB_SLUG:
                    try:
                        audit_stripe_account(project)
                        status_payload = setup_hub_status(project, user=request.user)
                    except ValueError:
                        pass
                return Response(
                    {
                        "ok": ok,
                        "results": fixes,
                        "status": status_payload,
                    },
                    status=status.HTTP_200_OK if ok or dry_run else status.HTTP_400_BAD_REQUEST,
                )

            if action == "sync_registry":
                result = sync_registry_for_user(request.user)
                result["status"] = setup_hub_status(project)
                return Response(result)

            if action == "sync_vault":
                result = sync_vault_to_billing_projects(project, request.user)
                result["status"] = setup_hub_status(project, user=request.user)
                return Response(result)

            if action == "reconcile_master_key":
                result = reconcile_local_master_key()
                result["status"] = setup_hub_status(project, user=request.user)
                return Response(result)

            if action == "bootstrap_platform":
                result = bootstrap_platform_automation(project, user=request.user)
                result["status"] = setup_hub_status(project, user=request.user)
                return Response(result)

            if action == "automate_deploy":
                result = automate_project_deploy(project, user=request.user)
                result["status"] = setup_hub_status(project, user=request.user)
                return Response(result)

            if action == "automation_status":
                return Response(platform_automation_status(project))

            return Response({"error": f"Unknown action: {action}"}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
