"""Pages router — static HTML page serving and redirects.

Direct port of src/routes/pages.rs from the Rust/Axum project.

All HTML files are read from the ../site/ directory at runtime via
`open(f'../site/{filename}').read()` and returned as HTMLResponse.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.middleware.auth import (
    require_auth,
    require_staff_session,
    require_client_session,
    require_super_session,
)


router = APIRouter(prefix="")


def _serve(filename: str) -> HTMLResponse:
    """Read a static HTML file and return it as HTMLResponse."""
    return HTMLResponse(open(f"../site/{filename}").read())


# ── Core pages ──────────────────────────────────────────────────────────────

@router.get("/")
async def landing():
    """GET / — landing page."""
    return _serve("index.html")


@router.get("/favicon.svg")
async def favicon():
    """GET /favicon.svg — serve favicon with long cache headers."""
    content = open("../site/favicon.svg").read()
    return Response(
        content=content,
        media_type="image/svg+xml",
        headers={"cache-control": "public, max-age=31536000, immutable"},
    )


@router.get("/dashboard")
async def dashboard(request: Request):
    """GET /dashboard — admin dashboard (SPA). Requires admin or super session.

    Super users are redirected to /admin.
    Unauthenticated users are redirected to /auth/login.
    """
    role = request.session.get("role")
    if role == "super":
        return RedirectResponse("/admin", status_code=307)
    try:
        require_auth(request)
    except Exception:
        return RedirectResponse("/auth/login", status_code=307)
    return _serve("dashboard.html")


@router.get("/terms")
async def terms_of_service():
    """GET /terms — Terms of Service page."""
    return _serve("terms-of-service.html")


@router.get("/privacy")
async def privacy_policy():
    """GET /privacy — Privacy Policy page."""
    return _serve("privacy-policy.html")


@router.get("/refunds")
async def refund_policy():
    """GET /refunds — Refund Policy page."""
    return _serve("refund-policy.html")


@router.get("/compare")
async def compare():
    """GET /compare — competitor comparison page."""
    return _serve("compare.html")


@router.get("/intelligent-staff-scheduling")
async def intelligent_staff_scheduling():
    """GET /intelligent-staff-scheduling — pillar page (top-of-funnel SEO)."""
    return _serve("intelligent-staff-scheduling.html")


@router.get("/scheduling-optimization-guide")
async def scheduling_optimization_guide():
    """GET /scheduling-optimization-guide — comprehensive guide page."""
    return _serve("scheduling-optimization-guide.html")


@router.get("/sitemap.xml")
async def sitemap():
    """GET /sitemap.xml — XML sitemap for search engines."""
    content = open("../site/sitemap.xml").read()
    return Response(content=content, media_type="application/xml")


@router.get("/robots.txt")
async def robots_txt():
    """GET /robots.txt — robots.txt for search engines."""
    content = open("../site/robots.txt").read()
    return Response(content=content, media_type="text/plain")


@router.get("/welcome")
async def welcome():
    """GET /welcome — welcome page."""
    return _serve("welcome.html")


# ── Staff portal ────────────────────────────────────────────────────────────

@router.get("/staff")
async def staff_portal(request: Request):
    """GET /staff — staff portal SPA. Requires staff session."""
    try:
        require_staff_session(request)
    except Exception:
        return RedirectResponse("/staff/login", status_code=307)
    return _serve("staff.html")


@router.get("/staff/login")
async def staff_login_redirect(request: Request):
    """GET /staff/login — Staff login page.

    We intentionally do NOT verify or consume the magic link token here.
    The page's client-side JavaScript calls /staff/api/login?token=... to
    verify and consume the token. This prevents messaging-app link-preview
    bots (iMessage, Android Messages, etc.) from consuming the token via
    their pre-fetch HTTP GET before the human taps the link.
    """
    # If already logged in as staff, go straight to the portal
    try:
        require_staff_session(request)
        return RedirectResponse("/staff", status_code=307)
    except Exception:
        pass
    return _serve("staff-login.html")


@router.get("/staff/set-password")
async def get_staff_set_password():
    """GET /staff/set-password — Show staff set-password page."""
    return _serve("staff-set-password.html")


# ── Client portal ────────────────────────────────────────────────────────────

@router.get("/client")
async def client_portal(request: Request):
    """GET /client — client portal SPA. Requires client session."""
    try:
        require_client_session(request)
    except Exception:
        return RedirectResponse("/client/login", status_code=307)
    return _serve("client.html")


@router.get("/client/login")
async def client_login_redirect(request: Request):
    """GET /client/login — Client login page.

    We intentionally do NOT verify or consume the magic link token here.
    The page's client-side JavaScript calls /client/api/login?token=... to
    verify and consume the token. This prevents messaging-app link-preview
    bots (iMessage, Android Messages, etc.) from consuming the token via
    their pre-fetch HTTP GET before the human taps the link.
    """
    # If already logged in as client, go straight to the portal
    try:
        require_client_session(request)
        return RedirectResponse("/client", status_code=307)
    except Exception:
        pass
    return _serve("client-login.html")


@router.get("/client/set-password")
async def get_client_set_password():
    """GET /client/set-password — Show client set-password page."""
    return _serve("client-set-password.html")


# ── Lead magnets ─────────────────────────────────────────────────────────────

@router.get("/cost-calculator")
async def cost_calculator():
    """GET /cost-calculator — Lead magnet: scheduling cost calculator."""
    return _serve("cost-calculator.html")


@router.get("/scheduling-quiz")
async def scheduling_quiz():
    """GET /scheduling-quiz — Lead magnet: scheduling pain point quiz."""
    return _serve("scheduling-quiz.html")


@router.get("/template-generator")
async def template_generator():
    """GET /template-generator — Lead magnet: shift planning template generator."""
    return _serve("template-generator.html")


@router.get("/coverage-analyzer")
async def coverage_analyzer():
    """GET /coverage-analyzer — Lead magnet: shift coverage gap analyzer."""
    return _serve("coverage-analyzer.html")


@router.get("/shift-bidding-optimizer")
async def shift_bidding_optimizer():
    """GET /shift-bidding-optimizer — Lead magnet: optimize shift bid assignments."""
    return _serve("shift-bidding-optimizer.html")


@router.get("/scheduling-roi-estimator")
async def scheduling_roi_estimator():
    """GET /scheduling-roi-estimator — Lead magnet: calculate scheduling ROI with payback period."""
    return _serve("scheduling-roi-estimator.html")


# ── Admin leads dashboard ────────────────────────────────────────────────────

@router.get("/leads-dashboard")
async def leads_dashboard(request: Request):
    """GET /leads-dashboard — Admin view: captured lead data table (super-only)."""
    try:
        require_super_session(request)
    except Exception:
        return RedirectResponse("/auth/login", status_code=307)
    return _serve("leads-dashboard.html")


# ── FAQ / integrations ───────────────────────────────────────────────────────

@router.get("/faq")
async def faq():
    """GET /faq — FAQ page targeting scheduling software FAQ keywords."""
    return _serve("faq.html")


@router.get("/integrations")
async def integrations():
    """GET /integrations — MCP server landing page (Senzii MCP integration for Claude, ChatGPT, etc.)."""
    return _serve("integrations.html")


@router.get("/ai-integration")
async def mcp_docs():
    """GET /ai-integration — MCP server developer docs (tool reference, OAuth endpoints, setup guide).

    Rust route path is /mcp; mapped to /ai-integration per task spec.
    Serves site/mcp.html.
    """
    return _serve("mcp.html")


@router.get("/mcp")
async def mcp_docs_alias():
    """GET /mcp — alias for /ai-integration (matches Rust route)."""
    return _serve("mcp.html")


@router.get("/thank-you")
async def thank_you():
    """GET /thank-you — Thank-you / confirmation page shown after lead capture."""
    return _serve("thank-you.html")


# ── Competitor comparison / alternatives ──────────────────────────────────────

@router.get("/senzii-vs-when-i-work")
async def senzii_vs_when_i_work():
    """GET /senzii-vs-when-i-work — competitor comparison page."""
    return _serve("senzii-vs-when-i-work.html")


@router.get("/senzii-vs-deputy")
async def senzii_vs_deputy():
    """GET /senzii-vs-deputy — competitor comparison page."""
    return _serve("senzii-vs-deputy.html")


@router.get("/senzii-vs-sling")
async def senzii_vs_sling():
    """GET /senzii-vs-sling — competitor comparison page."""
    return _serve("senzii-vs-sling.html")


@router.get("/senzii-vs-homebase")
async def senzii_vs_homebase():
    """GET /senzii-vs-homebase — competitor comparison page."""
    return _serve("senzii-vs-homebase.html")


@router.get("/alternatives")
async def alternatives():
    """GET /alternatives — alternatives hub page linking all comparison pages."""
    return _serve("alternatives.html")


@router.get("/salon-scheduling-software")
async def salon_scheduling_software():
    """GET /salon-scheduling-software — SEO landing page for salon scheduling keyword."""
    return _serve("salon-scheduling-software.html")


# ── Auth set-password (admin) ─────────────────────────────────────────────────
# Rust serves /auth/set-password from this router; the Python auth router is
# mounted at /auth prefix, so we expose it here as /auth/set-password to keep
# all page-serving logic in one place (matches Rust layout).

@router.get("/auth/set-password")
async def get_set_password_form():
    """GET /auth/set-password — Show set-password form (query params carry userId/userEmail from magic link)."""
    return _serve("auth-set-password.html")


# ── SEO landing pages ────────────────────────────────────────────────────────

@router.get("/scheduling-software-for-small-business")
async def scheduling_software_for_small_business():
    """GET /scheduling-software-for-small-business — SEO landing page targeting small business keyword."""
    return _serve("scheduling-software-for-small-business.html")


@router.get("/free-employee-scheduling-software")
async def free_employee_scheduling_software():
    """GET /free-employee-scheduling-software — SEO comparison page for free scheduling tools."""
    return _serve("free-employee-scheduling-software.html")


@router.get("/on-call-scheduling-software")
async def on_call_scheduling_software():
    """GET /on-call-scheduling-software — landing page for on-call scheduling keyword."""
    return _serve("on-call-scheduling-software.html")


@router.get("/shift-swap-software")
async def shift_swap_software():
    """GET /shift-swap-software — landing page for shift swap keyword."""
    return _serve("shift-swap-software.html")


@router.get("/shift-scheduling-software")
async def shift_scheduling_software():
    """GET /shift-scheduling-software — shift scheduling software landing page."""
    return _serve("shift-scheduling-software.html")


@router.get("/scheduling-software")
async def scheduling_software():
    """GET /scheduling-software — SEO landing page for generic head keyword."""
    return _serve("scheduling-software.html")


@router.get("/staff-scheduling-software")
async def staff_scheduling_software():
    """GET /staff-scheduling-software — SEO landing page for staff scheduling keyword."""
    return _serve("staff-scheduling-software.html")


@router.get("/employee-scheduling-software")
async def employee_scheduling_software():
    """GET /employee-scheduling-software — SEO landing page for core keyword."""
    return _serve("employee-scheduling-software.html")


@router.get("/shift-scheduling-app")
async def shift_scheduling_app():
    """GET /shift-scheduling-app — SEO landing page for app keyword."""
    return _serve("shift-scheduling-app.html")


@router.get("/schedule-maker")
async def schedule_maker():
    """GET /schedule-maker — SEO landing page for schedule maker keyword."""
    return _serve("schedule-maker.html")


@router.get("/appointment-scheduling-software")
async def appointment_scheduling_software():
    """GET /appointment-scheduling-software — SEO landing page for "appointment scheduling software" keyword."""
    return _serve("appointment-scheduling-software.html")


@router.get("/scheduling-system")
async def scheduling_system():
    """GET /scheduling-system — SEO landing page for "scheduling system" keyword."""
    return _serve("scheduling-system.html")


@router.get("/scheduling-app")
async def scheduling_app():
    """GET /scheduling-app — SEO landing page for "scheduling app" keyword."""
    return _serve("scheduling-app.html")


@router.get("/employee-scheduling-app")
async def employee_scheduling_app():
    """GET /employee-scheduling-app — SEO landing page for app keyword."""
    return _serve("employee-scheduling-app.html")


@router.get("/employee-scheduling-system")
async def employee_scheduling_system():
    """GET /employee-scheduling-system — SEO landing page for scheduling system keyword."""
    return _serve("employee-scheduling-system.html")


@router.get("/labor-scheduling-software")
async def labor_scheduling_software():
    """GET /labor-scheduling-software — SEO landing page for labor keyword."""
    return _serve("labor-scheduling-software.html")


@router.get("/workforce-scheduling-software")
async def workforce_scheduling_software():
    """GET /workforce-scheduling-software — SEO landing page for workforce keyword."""
    return _serve("workforce-scheduling-software.html")


@router.get("/hotel-scheduling-software")
async def hotel_scheduling_software():
    """GET /hotel-scheduling-software — SEO landing page for hotel/hospitality keyword."""
    return _serve("hotel-scheduling-software.html")


@router.get("/time-clock-software")
async def time_clock_software():
    """GET /time-clock-software — SEO landing page for time clock / time tracking keyword."""
    return _serve("time-clock-software.html")


@router.get("/crew-scheduling-software")
async def crew_scheduling_software():
    """GET /crew-scheduling-software — SEO landing page for crew scheduling keyword."""
    return _serve("crew-scheduling-software.html")


@router.get("/multi-location-scheduling-software")
async def multi_location_scheduling_software():
    """GET /multi-location-scheduling-software — SEO landing page for multi-location scheduling keyword."""
    return _serve("multi-location-scheduling-software.html")


@router.get("/seasonal-scheduling-software")
async def seasonal_scheduling_software():
    """GET /seasonal-scheduling-software — SEO landing page for seasonal scheduling keyword."""
    return _serve("seasonal-scheduling-software.html")


@router.get("/shift-planning-software")
async def shift_planning_software():
    """GET /shift-planning-software — SEO landing page for "shift planning software" keyword."""
    return _serve("shift-planning-software.html")


@router.get("/shift-management-software")
async def shift_management_software():
    """GET /shift-management-software — SEO landing page."""
    return _serve("shift-management-software.html")


@router.get("/workforce-management-software")
async def workforce_management_software():
    """GET /workforce-management-software — SEO landing page."""
    return _serve("workforce-management-software.html")


@router.get("/employee-rostering-software")
async def employee_rostering_software():
    """GET /employee-rostering-software — SEO landing page for employee rostering keyword."""
    return _serve("employee-rostering-software.html")


@router.get("/caregiver-scheduling-software")
async def caregiver_scheduling_software():
    """GET /caregiver-scheduling-software — SEO landing page for caregiver scheduling keyword."""
    return _serve("caregiver-scheduling-software.html")


@router.get("/call-center-scheduling-software")
async def call_center_scheduling_software():
    """GET /call-center-scheduling-software — SEO landing page for call center scheduling keyword."""
    return _serve("call-center-scheduling-software.html")


@router.get("/security-guard-scheduling-software")
async def security_guard_scheduling_software():
    """GET /security-guard-scheduling-software — SEO landing page for security guard scheduling keyword."""
    return _serve("security-guard-scheduling-software.html")


@router.get("/volunteer-scheduling-software")
async def volunteer_scheduling_software():
    """GET /volunteer-scheduling-software — SEO landing page for volunteer scheduling keyword."""
    return _serve("volunteer-scheduling-software.html")


@router.get("/gym-scheduling-software")
async def gym_scheduling_software():
    """GET /gym-scheduling-software — SEO landing page for gym scheduling keyword."""
    return _serve("gym-scheduling-software.html")


@router.get("/dental-office-scheduling-software")
async def dental_office_scheduling_software():
    """GET /dental-office-scheduling-software — SEO landing page for dental office scheduling keyword."""
    return _serve("dental-office-scheduling-software.html")


@router.get("/cleaning-service-scheduling-software")
async def cleaning_service_scheduling_software():
    """GET /cleaning-service-scheduling-software — SEO landing page for cleaning service scheduling keyword."""
    return _serve("cleaning-service-scheduling-software.html")


@router.get("/pharmacy-scheduling-software")
async def pharmacy_scheduling_software():
    """GET /pharmacy-scheduling-software — SEO landing page for pharmacy scheduling keyword."""
    return _serve("pharmacy-scheduling-software.html")


@router.get("/pet-grooming-scheduling-software")
async def pet_grooming_scheduling_software():
    """GET /pet-grooming-scheduling-software — SEO landing page for pet grooming scheduling keyword."""
    return _serve("pet-grooming-scheduling-software.html")


@router.get("/church-scheduling-software")
async def church_scheduling_software():
    """GET /church-scheduling-software — SEO landing page for church scheduling keyword."""
    return _serve("church-scheduling-software.html")


@router.get("/warehouse-scheduling-software")
async def warehouse_scheduling_software():
    """GET /warehouse-scheduling-software — SEO landing page for warehouse scheduling keyword."""
    return _serve("warehouse-scheduling-software.html")


@router.get("/field-service-scheduling-software")
async def field_service_scheduling_software():
    """GET /field-service-scheduling-software — SEO landing page for field service scheduling keyword."""
    return _serve("field-service-scheduling-software.html")


@router.get("/restaurant-scheduling-software")
async def restaurant_scheduling_software():
    """GET /restaurant-scheduling-software — SEO landing page for restaurant scheduling keyword."""
    return _serve("restaurant-scheduling-software.html")


@router.get("/healthcare-scheduling-software")
async def healthcare_scheduling_software():
    """GET /healthcare-scheduling-software — SEO landing page for healthcare scheduling keyword."""
    return _serve("healthcare-scheduling-software.html")


@router.get("/hospitality-scheduling-software")
async def hospitality_scheduling_software():
    """GET /hospitality-scheduling-software — SEO landing page for hospitality scheduling keyword."""
    return _serve("hospitality-scheduling-software.html")


@router.get("/sports-team-scheduling-software")
async def sports_team_scheduling_software():
    """GET /sports-team-scheduling-software — SEO landing page for sports team scheduling keyword."""
    return _serve("sports-team-scheduling-software.html")