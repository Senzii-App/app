"""MCP tool registry.

TOOL_HANDLERS maps each tool name to its handler function. Handlers are
registered here rather than at their definition sites so the tool modules
stay free of routing concerns.
"""

from . import (
    admin,
    assignments,
    certifications,
    clients,
    metrics,
    shifts,
    staff,
    staffing_requests,
    work_sites,
)
from .common import ToolHandler

TOOL_HANDLERS: dict[str, ToolHandler] = {}

TOOL_HANDLERS["list_staff"] = staff.list_staff
TOOL_HANDLERS["get_staff"] = staff.get_staff
TOOL_HANDLERS["create_staff"] = staff.create_staff
TOOL_HANDLERS["add_staff_certification"] = staff.add_staff_certification
TOOL_HANDLERS["set_staff_availability"] = staff.set_staff_availability
TOOL_HANDLERS["delete_staff"] = staff.delete_staff
TOOL_HANDLERS["send_staff_magic_link"] = staff.send_staff_magic_link

TOOL_HANDLERS["list_clients"] = clients.list_clients
TOOL_HANDLERS["create_client"] = clients.create_client
TOOL_HANDLERS["send_client_magic_link"] = clients.send_client_magic_link

TOOL_HANDLERS["list_shifts"] = shifts.list_shifts
TOOL_HANDLERS["delete_shift"] = shifts.delete_shift
TOOL_HANDLERS["find_candidates"] = shifts.find_candidates

TOOL_HANDLERS["list_assignments"] = assignments.list_assignments
TOOL_HANDLERS["create_assignment"] = assignments.create_assignment
TOOL_HANDLERS["unassign_assignment"] = assignments.unassign_assignment

TOOL_HANDLERS["list_staffing_requests"] = staffing_requests.list_staffing_requests
TOOL_HANDLERS["create_staffing_request"] = staffing_requests.create_staffing_request
TOOL_HANDLERS["convert_staffing_request"] = staffing_requests.convert_staffing_request
TOOL_HANDLERS["cancel_staffing_request"] = staffing_requests.cancel_staffing_request

TOOL_HANDLERS["list_work_sites"] = work_sites.list_work_sites
TOOL_HANDLERS["create_work_site"] = work_sites.create_work_site

TOOL_HANDLERS["list_certifications"] = certifications.list_certifications
TOOL_HANDLERS["create_certification"] = certifications.create_certification
TOOL_HANDLERS["update_certification"] = certifications.update_certification
TOOL_HANDLERS["delete_certification"] = certifications.delete_certification

TOOL_HANDLERS["get_org_metrics"] = metrics.get_org_metrics

TOOL_HANDLERS["run_sql"] = admin.run_sql