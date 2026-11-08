from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from swiftpy.admin.auth import (
    AdminUser,
    clear_admin_session,
    create_admin_session,
    get_admin_authenticator,
    get_admin_session_store,
    get_current_admin,
)
from swiftpy.admin.registry import (
    AdminSite,
    ModelAdmin,
    set_admin_route_compiler,
)
from swiftpy.ai.observability.observer import get_observability
from swiftpy.database.orm.model import Model
from swiftpy.http.request import Request
from swiftpy.http.response import (
    Response,
    ResponseHeaders,
    json_response,
    not_found,
)
from swiftpy.http.router import Router

if TYPE_CHECKING:
    from swiftpy.core.application import Application

logger = logging.getLogger("swiftpy.admin.views")


def _redirect(url: str, status_code: int = 302) -> Response:
    """Construct an HTTP redirect response."""
    headers = ResponseHeaders({"location": url})
    return Response(content="", status_code=status_code, headers=headers)


def _wants_json(request: Request) -> bool:
    """Determine if the client requested JSON response."""
    if request.query_params.get("format") == "json":
        return True
    accept = request.headers.get("accept", "").lower()
    return "application/json" in accept and "text/html" not in accept


async def _extract_payload(request: Request) -> dict[str, Any]:
    """Extract submitted form parameters or JSON payload from Request."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            data = await request.json()
            return dict(data) if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    raw_body = await request.body()
    if not raw_body:
        return {}
    decoded = raw_body.decode("utf-8", errors="replace")
    parsed = parse_qs(decoded, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


def _model_repr(instance: Any) -> str:
    """Construct a clean, human-readable string representation of a model instance."""
    if not isinstance(instance, Model):
        return str(instance)
    pk_col = getattr(instance, "__primary_key__", "id")
    pk_val = getattr(instance, pk_col, None)

    for attr in ("name", "title", "username", "email", "label"):
        val = getattr(instance, attr, None)
        if val:
            return f"{val} (#{pk_val})" if pk_val is not None else str(val)

    return f"{instance.__class__.__name__} #{pk_val}" if pk_val is not None else instance.__class__.__name__


def _coerce_pk(m_admin: ModelAdmin, raw_id: Any) -> Any:
    """Coerce raw primary key from URL parameter to its declared Python type."""
    pk_col = getattr(m_admin.model, "__primary_key__", "id")
    for f in m_admin.get_fields():
        if f.name == pk_col:
            try:
                return f.coerce_value(raw_id)
            except (ValueError, TypeError):
                pass
    if isinstance(raw_id, str) and raw_id.isdigit():
        return int(raw_id)
    return raw_id


def _render_base_layout(
    site: AdminSite,
    title: str,
    body_html: str,
    admin_user: AdminUser | None = None,
    prefix: str = "/admin",
    active_slug: str | None = None,
    flash_error: str | None = None,
    flash_success: str | None = None,
) -> str:
    """Render the master HTML shell with navbar, sidebar, and responsive styling."""
    models = site.all_registered()
    sidebar_items_html = []
    dash_active = 'class="active"' if active_slug is None else ""
    sidebar_items_html.append(f'<li><a href="{prefix}/" {dash_active}>📊 Dashboard</a></li>')

    for m_admin in models:
        is_active = 'class="active"' if active_slug == m_admin.slug else ""
        sidebar_items_html.append(
            f'<li><a href="{prefix}/{m_admin.slug}" {is_active}>📁 {html.escape(m_admin.plural_title)}</a></li>'
        )

    ai_active = 'class="active"' if active_slug == "ai-usage" else ""
    sidebar_items_html.append(f'<li><a href="{prefix}/ai-usage" {ai_active}>🤖 AI Usage</a></li>')

    sidebar_html = "\n".join(sidebar_items_html)

    alerts_html = ""
    if flash_error:
        alerts_html += f'<div class="alert alert-error">{html.escape(flash_error)}</div>'
    if flash_success:
        alerts_html += f'<div class="alert alert-success">{html.escape(flash_success)}</div>'

    user_info_html = ""
    if admin_user:
        csrf_tok = get_admin_session_store().generate_csrf_token(admin_user)
        user_info_html = f"""
        <div class="user-profile">
            <span>👤 <strong>{html.escape(admin_user.username)}</strong></span>
            <form action="{prefix}/logout" method="POST" style="display:inline; margin-left:12px;">
                <input type="hidden" name="_csrf_token" value="{html.escape(csrf_tok, quote=True)}">
                <button type="submit" class="btn btn-sm btn-outline">Sign Out</button>
            </form>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)} | {html.escape(site.title)}</title>
    <style>
        :root {{
            --bg-color: #0f172a;
            --surface-color: #1e293b;
            --surface-hover: #334155;
            --border-color: #334155;
            --primary: #38bdf8;
            --primary-hover: #0284c7;
            --danger: #ef4444;
            --danger-hover: #dc2626;
            --success: #10b981;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: var(--font-sans);
            background-color: var(--bg-color);
            color: var(--text-main);
            display: flex;
            min-height: 100vh;
        }}
        a {{ color: var(--primary); text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        /* Sidebar */
        aside.sidebar {{
            width: 260px;
            background-color: var(--surface-color);
            border-right: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
        }}
        .brand {{
            padding: 20px;
            font-size: 1.25rem;
            font-weight: 700;
            border-bottom: 1px solid var(--border-color);
            color: var(--primary);
        }}
        .nav-list {{ list-style: none; padding: 15px 10px; flex: 1; }}
        .nav-list li {{ margin-bottom: 6px; }}
        .nav-list a {{
            display: block;
            padding: 10px 14px;
            border-radius: 6px;
            color: var(--text-main);
            font-size: 0.95rem;
            transition: background 0.15s ease;
        }}
        .nav-list a:hover, .nav-list a.active {{
            background-color: var(--surface-hover);
            color: var(--primary);
            text-decoration: none;
        }}
        /* Main Container */
        .main-wrapper {{
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow-y: auto;
        }}
        header.topbar {{
            height: 64px;
            background-color: var(--surface-color);
            border-bottom: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 30px;
        }}
        main.content {{
            padding: 30px;
            flex: 1;
        }}
        .page-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 24px;
        }}
        .page-header h1 {{ font-size: 1.75rem; font-weight: 600; }}
        /* Buttons */
        .btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 9px 18px;
            font-size: 0.9rem;
            font-weight: 500;
            border-radius: 6px;
            cursor: pointer;
            border: 1px solid transparent;
            text-decoration: none !important;
            transition: all 0.15s ease;
        }}
        .btn-primary {{ background-color: var(--primary); color: #0f172a; font-weight: 600; }}
        .btn-primary:hover {{ background-color: var(--primary-hover); }}
        .btn-danger {{ background-color: var(--danger); color: #fff; }}
        .btn-danger:hover {{ background-color: var(--danger-hover); }}
        .btn-outline {{ background-color: transparent; border-color: var(--border-color); color: var(--text-main); }}
        .btn-outline:hover {{ background-color: var(--surface-hover); }}
        .btn-sm {{ padding: 5px 10px; font-size: 0.8rem; }}
        /* Tables */
        .card {{
            background-color: var(--surface-color);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);
        }}
        table.data-table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.95rem;
        }}
        table.data-table th {{
            background-color: rgba(255,255,255,0.03);
            border-bottom: 1px solid var(--border-color);
            padding: 14px 18px;
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
            font-size: 0.75rem;
            letter-spacing: 0.05em;
        }}
        table.data-table td {{
            padding: 14px 18px;
            border-bottom: 1px solid var(--border-color);
            vertical-align: middle;
        }}
        table.data-table tbody tr:hover {{
            background-color: rgba(255,255,255,0.02);
        }}
        .badge {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
            background: rgba(56, 189, 248, 0.15);
            color: var(--primary);
        }}
        /* Forms */
        .form-group {{ margin-bottom: 20px; }}
        .form-group label {{
            display: block;
            margin-bottom: 8px;
            font-size: 0.9rem;
            font-weight: 500;
            color: var(--text-main);
        }}
        .form-control {{
            width: 100%;
            padding: 10px 14px;
            font-size: 0.95rem;
            border-radius: 6px;
            border: 1px solid var(--border-color);
            background-color: #0f172a;
            color: var(--text-main);
            outline: none;
            transition: border-color 0.15s ease;
        }}
        .form-control:focus {{ border-color: var(--primary); }}
        textarea.form-control {{ min-height: 120px; resize: vertical; }}
        /* Alerts */
        .alert {{
            padding: 14px 18px;
            border-radius: 6px;
            margin-bottom: 20px;
            font-size: 0.95rem;
        }}
        .alert-error {{ background-color: rgba(239, 68, 68, 0.15); border: 1px solid var(--danger); color: #fca5a5; }}
        .alert-success {{ background-color: rgba(16, 185, 129, 0.15); border: 1px solid var(--success); color: #6ee7b7; }}
    </style>
</head>
<body>
    <aside class="sidebar">
        <div class="brand">⚡ {html.escape(site.title)}</div>
        <ul class="nav-list">
            {sidebar_html}
        </ul>
    </aside>
    <div class="main-wrapper">
        <header class="topbar">
            <div><strong>Admin Console</strong></div>
            {user_info_html}
        </header>
        <main class="content">
            {alerts_html}
            {body_html}
        </main>
    </div>
</body>
</html>
"""


def _render_login_page(
    site: AdminSite,
    prefix: str = "/admin",
    error_message: str | None = None,
) -> str:
    """Render dedicated admin login screen."""
    error_html = (
        f'<div class="alert alert-error">{html.escape(error_message)}</div>'
        if error_message
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Admin Sign In | {html.escape(site.title)}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #0f172a;
            color: #f8fafc;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
        }}
        .login-card {{
            width: 100%;
            max-width: 400px;
            background-color: #1e293b;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 36px;
            box-shadow: 0 10px 25px -5px rgba(0,0,0,0.3);
        }}
        .brand {{
            font-size: 1.5rem;
            font-weight: 700;
            color: #38bdf8;
            margin-bottom: 8px;
            text-align: center;
        }}
        .subtitle {{
            font-size: 0.9rem;
            color: #94a3b8;
            margin-bottom: 24px;
            text-align: center;
        }}
        .form-group {{ margin-bottom: 18px; }}
        label {{ display: block; margin-bottom: 6px; font-size: 0.9rem; font-weight: 500; }}
        input[type="text"], input[type="password"] {{
            width: 100%;
            padding: 10px 14px;
            border-radius: 6px;
            border: 1px solid #334155;
            background-color: #0f172a;
            color: #f8fafc;
            box-sizing: border-box;
            outline: none;
        }}
        input:focus {{ border-color: #38bdf8; }}
        .btn-submit {{
            width: 100%;
            padding: 12px;
            background-color: #38bdf8;
            color: #0f172a;
            font-weight: 600;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            margin-top: 10px;
        }}
        .btn-submit:hover {{ background-color: #0284c7; }}
        .alert-error {{
            background-color: rgba(239, 68, 68, 0.15);
            border: 1px solid #ef4444;
            color: #fca5a5;
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 18px;
            font-size: 0.9rem;
        }}
    </style>
</head>
<body>
    <div class="login-card">
        <div class="brand">⚡ {html.escape(site.title)}</div>
        <div class="subtitle">Secure session-based administrative access</div>
        {error_html}
        <form action="{prefix}/login" method="POST">
            <div class="form-group">
                <label for="username">Username</label>
                <input type="text" id="username" name="username" required autofocus placeholder="admin">
            </div>
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required placeholder="••••••••">
            </div>
            <button type="submit" class="btn-submit">Sign In to Admin</button>
        </form>
    </div>
</body>
</html>
"""


def _html_response(content: str, status_code: int = 200) -> Response:
    return Response(
        content=content,
        status_code=status_code,
        headers={"content-type": "text/html; charset=utf-8"},
    )


def create_admin_router(site: AdminSite, prefix: str = "/admin") -> Router:
    """Construct a complete Router containing all CRUD and authentication endpoints for AdminSite."""
    clean_prefix = "/" + prefix.strip("/")
    router = Router()

    # --------------------------------------------------------------------------
    # Authentication & Dashboard
    # --------------------------------------------------------------------------

    async def login_get(request: Request) -> Response:
        admin_user = get_current_admin(request)
        if admin_user:
            return _redirect(f"{clean_prefix}/")
        return _html_response(_render_login_page(site, prefix=clean_prefix))

    async def login_post(request: Request) -> Response:
        payload = await _extract_payload(request)
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))

        authenticator = get_admin_authenticator()
        user = authenticator.authenticate(username, password)
        if not user:
            if _wants_json(request):
                return json_response({"detail": "Invalid admin credentials."}, status_code=401)
            return _html_response(
                _render_login_page(site, prefix=clean_prefix, error_message="Invalid username or password."),
                status_code=401,
            )

        if _wants_json(request):
            resp = json_response({"user": user.to_dict(), "message": "Authenticated successfully."})
        else:
            resp = _redirect(f"{clean_prefix}/")

        create_admin_session(resp, user)
        return resp

    async def logout_handler(request: Request) -> Response:
        if _wants_json(request):
            resp = json_response({"message": "Logged out successfully."})
        else:
            resp = _redirect(f"{clean_prefix}/login")
        clear_admin_session(resp)
        return resp

    async def dashboard_get(request: Request) -> Response:
        admin_user = get_current_admin(request)
        if not admin_user:
            if _wants_json(request):
                return json_response({"detail": "Admin authentication required."}, status_code=401)
            return _redirect(f"{clean_prefix}/login")

        registered = site.all_registered()
        cards_html = []
        stats: dict[str, Any] = {}

        ai_observer = get_observability()
        try:
            ai_stats = await ai_observer.get_stats()
        except (RuntimeError, OSError, ValueError):
            ai_stats = {
                "total_calls": 0,
                "total_tokens": 0,
                "total_cost": 0.0,
                "avg_latency_ms": 0.0,
            }

        ai_card_html = f"""
        <div class="card" style="padding: 24px; margin-bottom: 24px; border: 1px solid rgba(56,189,248,0.3); background: linear-gradient(135deg, rgba(30,41,59,0.8), rgba(15,23,42,0.9));">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <div>
                    <h2 style="font-size:1.25rem; font-weight:600; margin-bottom:6px; color:#38bdf8;">
                        🤖 AI Observability & Usage
                    </h2>
                    <p style="color:var(--text-muted); font-size:0.9rem; margin:0;">
                        {ai_stats.get('total_calls', 0):,} calls &bull; {ai_stats.get('total_tokens', 0):,} tokens &bull; ${ai_stats.get('total_cost', 0.0):.4f} spend
                    </p>
                </div>
                <div>
                    <a href="{clean_prefix}/ai-usage" class="btn btn-primary btn-sm">Open AI Dashboard</a>
                </div>
            </div>
        </div>
        """

        for m_admin in registered:
            try:
                count = await m_admin.model.query().count()
            except (RuntimeError, OSError, ValueError):
                count = 0
            stats[m_admin.slug] = {"title": m_admin.plural_title, "count": count}

            cards_html.append(f"""
            <div class="card" style="padding: 24px; margin-bottom: 20px;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <div>
                        <h2 style="font-size:1.25rem; font-weight:600; margin-bottom:4px;">
                            📁 {html.escape(m_admin.plural_title)}
                        </h2>
                        <span class="badge">{count} records</span>
                    </div>
                    <div>
                        <a href="{clean_prefix}/{m_admin.slug}" class="btn btn-primary btn-sm">Browse Records</a>
                        <a href="{clean_prefix}/{m_admin.slug}/create" class="btn btn-outline btn-sm">+ Add New</a>
                    </div>
                </div>
            </div>
            """)

        if _wants_json(request):
            return json_response({"models": stats, "ai_usage": ai_stats, "user": admin_user.to_dict()})

        body = f"""
        <div class="page-header">
            <h1>Admin Dashboard</h1>
        </div>
        <p style="color:var(--text-muted); margin-bottom:24px;">Welcome back, <strong>{html.escape(admin_user.username)}</strong>. Select a model to manage its data:</p>
        <div>
            {ai_card_html}
            {''.join(cards_html) if cards_html else '<p>No models currently registered.</p>'}
        </div>
        """
        html_str = _render_base_layout(site, "Dashboard", body, admin_user=admin_user, prefix=clean_prefix)
        return _html_response(html_str)

    async def ai_usage_view(request: Request) -> Response:
        admin_user = get_current_admin(request)
        if not admin_user:
            if _wants_json(request):
                return json_response({"detail": "Admin authentication required."}, status_code=401)
            return _redirect(f"{clean_prefix}/login")

        page = 1
        per_page = 50
        try:
            page = max(1, int(request.query_params.get("page", 1)))
            per_page = max(1, min(200, int(request.query_params.get("per_page", 50))))
        except (ValueError, TypeError):
            pass

        provider = request.query_params.get("provider") or None
        model = request.query_params.get("model") or None
        status = request.query_params.get("status") or None
        start_date = request.query_params.get("start_date") or None
        end_date = request.query_params.get("end_date") or None

        offset = (page - 1) * per_page

        observer = get_observability()
        stats = await observer.get_stats()
        recent = await observer.get_observations(
            limit=per_page,
            offset=offset,
            provider=provider,
            model=model,
            status=status,
            start_date=start_date,
            end_date=end_date,
        )

        if _wants_json(request):
            return json_response({
                "page": page,
                "per_page": per_page,
                "stats": stats,
                "recent_observations": [r.to_dict() for r in recent],
            })

        total_calls = stats.get("total_calls", 0)
        successful_calls = stats.get("successful_calls", 0)
        failed_calls = stats.get("failed_calls", 0)
        total_tokens = stats.get("total_tokens", 0)
        input_tokens = stats.get("total_input_tokens", 0)
        output_tokens = stats.get("total_output_tokens", 0)
        total_cost = stats.get("total_cost", 0.0)
        avg_latency = stats.get("avg_latency_ms", 0.0)

        by_provider = stats.get("by_provider", {})
        provider_rows_html = []
        for p_name, p_data in sorted(by_provider.items()):
            provider_rows_html.append(f"""
            <tr>
                <td><strong>{html.escape(p_name)}</strong></td>
                <td>{p_data.get('calls', 0)}</td>
                <td>{p_data.get('tokens', 0):,}</td>
                <td>${p_data.get('cost', 0.0):.6f}</td>
            </tr>
            """)

        by_model = stats.get("by_model", {})
        model_rows_html = []
        for m_name, m_data in sorted(by_model.items()):
            model_rows_html.append(f"""
            <tr>
                <td><strong>{html.escape(m_name)}</strong></td>
                <td>{m_data.get('calls', 0)}</td>
                <td>{m_data.get('tokens', 0):,}</td>
                <td>${m_data.get('cost', 0.0):.6f}</td>
            </tr>
            """)

        obs_rows_html = []
        for r in recent:
            if r.status == "success":
                status_badge = '<span class="badge" style="background:rgba(16,185,129,0.2);color:#34d399;">success</span>'
            elif r.status == "cancelled":
                status_badge = '<span class="badge" style="background:rgba(245,158,11,0.2);color:#fbbf24;">cancelled</span>'
            else:
                status_badge = '<span class="badge" style="background:rgba(239,68,68,0.2);color:#f87171;">error</span>'

            time_str = r.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            ttft_display = f"{r.ttft_ms:.1f}ms" if r.ttft_ms is not None else "-"
            obs_rows_html.append(f"""
            <tr>
                <td style="white-space:nowrap;font-size:0.85rem;color:var(--text-muted);">{time_str}</td>
                <td><span class="badge">{html.escape(r.provider)}</span></td>
                <td><code>{html.escape(r.model)}</code></td>
                <td>{r.input_tokens:,}</td>
                <td>{r.output_tokens:,}</td>
                <td>{ttft_display}</td>
                <td>{r.latency_ms:.1f}ms</td>
                <td>${r.cost:.6f}</td>
                <td>{status_badge}</td>
                <td><code style="font-size:0.75rem;" title="{html.escape(r.prompt_hash)}">{r.prompt_hash[:10]}...</code></td>
            </tr>
            """)

        prev_link = (
            f'<a href="{clean_prefix}/ai-usage?page={page - 1}&per_page={per_page}" class="btn btn-outline btn-sm">&laquo; Prev</a>'
            if page > 1
            else ""
        )
        next_link = (
            f'<a href="{clean_prefix}/ai-usage?page={page + 1}&per_page={per_page}" class="btn btn-outline btn-sm" style="margin-left:8px;">Next &raquo;</a>'
            if len(recent) == per_page
            else ""
        )

        body = f"""
        <div class="page-header" style="margin-bottom:24px;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <div>
                    <h1>🤖 AI Observability & Usage</h1>
                    <p style="color:var(--text-muted); margin-top:4px;">
                        Live telemetry across all LLM providers, latency metrics, and estimated API spend.
                    </p>
                </div>
                <div>
                    <a href="{clean_prefix}/ai-usage?format=json" class="btn btn-outline btn-sm" target="_blank">Export JSON</a>
                </div>
            </div>
        </div>

        <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:16px; margin-bottom:28px;">
            <div class="card" style="padding:20px;">
                <div style="color:var(--text-muted); font-size:0.85rem; font-weight:600; text-transform:uppercase;">Total Invocations</div>
                <div style="font-size:2rem; font-weight:700; margin:8px 0;">{total_calls:,}</div>
                <div style="font-size:0.85rem; color:var(--text-muted);">{successful_calls} success &bull; {failed_calls} failed</div>
            </div>
            <div class="card" style="padding:20px;">
                <div style="color:var(--text-muted); font-size:0.85rem; font-weight:600; text-transform:uppercase;">Total Tokens</div>
                <div style="font-size:2rem; font-weight:700; margin:8px 0;">{total_tokens:,}</div>
                <div style="font-size:0.85rem; color:var(--text-muted);">{input_tokens:,} in / {output_tokens:,} out</div>
            </div>
            <div class="card" style="padding:20px;">
                <div style="color:var(--text-muted); font-size:0.85rem; font-weight:600; text-transform:uppercase;">Estimated Spend</div>
                <div style="font-size:2rem; font-weight:700; margin:8px 0; color:#38bdf8;">${total_cost:.4f}</div>
                <div style="font-size:0.85rem; color:var(--text-muted);">Standard pricing model</div>
            </div>
            <div class="card" style="padding:20px;">
                <div style="color:var(--text-muted); font-size:0.85rem; font-weight:600; text-transform:uppercase;">Average Latency</div>
                <div style="font-size:2rem; font-weight:700; margin:8px 0;">{avg_latency:.1f}ms</div>
                <div style="font-size:0.85rem; color:var(--text-muted);">Round-trip execution</div>
            </div>
        </div>

        <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:28px;">
            <div class="card" style="padding:20px;">
                <h3 style="font-size:1.1rem; margin-bottom:14px;">Provider Breakdown</h3>
                <table class="data-table" style="width:100%;">
                    <thead><tr><th>Provider</th><th>Calls</th><th>Tokens</th><th>Cost</th></tr></thead>
                    <tbody>{''.join(provider_rows_html) if provider_rows_html else '<tr><td colspan="4" style="color:var(--text-muted);">No provider data yet</td></tr>'}</tbody>
                </table>
            </div>
            <div class="card" style="padding:20px;">
                <h3 style="font-size:1.1rem; margin-bottom:14px;">Model Breakdown</h3>
                <table class="data-table" style="width:100%;">
                    <thead><tr><th>Model</th><th>Calls</th><th>Tokens</th><th>Cost</th></tr></thead>
                    <tbody>{''.join(model_rows_html) if model_rows_html else '<tr><td colspan="4" style="color:var(--text-muted);">No model data yet</td></tr>'}</tbody>
                </table>
            </div>
        </div>

        <div class="card" style="padding:20px;">
            <h3 style="font-size:1.1rem; margin-bottom:14px;">Recent Observations</h3>
            <div style="overflow-x:auto;">
                <table class="data-table" style="width:100%;">
                    <thead>
                        <tr>
                            <th>Time</th><th>Provider</th><th>Model</th><th>Prompt Tokens</th><th>Output Tokens</th><th>TTFT</th><th>Latency</th><th>Cost</th><th>Status</th><th>Prompt Hash</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(obs_rows_html) if obs_rows_html else '<tr><td colspan="10" style="text-align:center; color:var(--text-muted);">No LLM calls recorded yet.</td></tr>'}
                    </tbody>
                </table>
            </div>
            <div style="display:flex; justify-content:space-between; align-items:center; margin-top:16px;">
                <div style="color:var(--text-muted); font-size:0.9rem;">
                    Page {page} &bull; Showing {len(recent)} entries
                </div>
                <div>
                    {prev_link}
                    {next_link}
                </div>
            </div>
        </div>
        """

        html_str = _render_base_layout(site, "AI Usage", body, admin_user=admin_user, prefix=clean_prefix, active_slug="ai-usage")
        return _html_response(html_str)

    router.add_route("GET", f"{clean_prefix}/login", login_get)
    router.add_route("POST", f"{clean_prefix}/login", login_post)
    router.add_route("GET", f"{clean_prefix}/logout", logout_handler)
    router.add_route("POST", f"{clean_prefix}/logout", logout_handler)
    router.add_route("GET", f"{clean_prefix}/", dashboard_get)
    router.add_route("GET", f"{clean_prefix}", dashboard_get)
    router.add_route("GET", f"{clean_prefix}/ai-usage", ai_usage_view)

    # --------------------------------------------------------------------------
    # CRUD Routes per Registered Model
    # --------------------------------------------------------------------------

    async def list_view(request: Request, slug: str) -> Response:
        admin_user = get_current_admin(request)
        if not admin_user:
            if _wants_json(request):
                return json_response({"detail": "Admin authentication required."}, status_code=401)
            return _redirect(f"{clean_prefix}/login")

        item = site.get_by_slug(slug)
        if not item:
            return not_found(f"Model '{slug}' not found.")
        model_cls, m_admin = item

        # Query records with eager loaded BelongsTo relations
        query = m_admin.get_queryset()
        search_query = request.query_params.get("q")
        if search_query and m_admin.search_fields:
            for field in m_admin.search_fields:
                query.where_ilike(field, f"%{search_query}%")

        try:
            records = await query.get()
        except Exception:
            logger.exception("Error querying records for admin list")
            records = []

        if _wants_json(request):
            data = [
                r.to_dict() if hasattr(r, "to_dict") else getattr(r, "_attributes", {})
                for r in records
            ]
            return json_response({"model": m_admin.title, "count": len(records), "data": data})

        # Build table headers
        list_display = m_admin.get_list_display()
        th_cells = [f"<th>{html.escape(_humanize_header(col))}</th>" for col in list_display]
        th_cells.append('<th style="text-align:right;">Actions</th>')
        thead_html = f"<tr>{''.join(th_cells)}</tr>"

        # Build table rows
        pk_col = getattr(model_cls, "__primary_key__", "id")
        relationships = m_admin.get_relationships()
        belongs_to_map = {
            meta.foreign_key: meta.name
            for meta in relationships.values()
            if meta.relation_type == "BelongsTo" and meta.foreign_key
        }

        tbody_rows = []
        for rec in records:
            pk_val = getattr(rec, pk_col, None)
            td_cells = []
            for col in list_display:
                if col in belongs_to_map:
                    rel_name = belongs_to_map[col]
                    related_obj = getattr(rec, rel_name, None)
                    if related_obj:
                        display_val = f'<span class="badge">{html.escape(_model_repr(related_obj))}</span>'
                    else:
                        raw_fk = getattr(rec, col, None)
                        display_val = html.escape(str(raw_fk) if raw_fk is not None else "-")
                else:
                    raw_val = getattr(rec, col, None)
                    display_val = html.escape(str(raw_val) if raw_val is not None else "-")

                td_cells.append(f"<td>{display_val}</td>")

            csrf_tok = get_admin_session_store().generate_csrf_token(admin_user)
            actions_html = f"""
            <td style="text-align:right;">
                <a href="{clean_prefix}/{slug}/{pk_val}/edit" class="btn btn-sm btn-outline">Edit</a>
                <form action="{clean_prefix}/{slug}/{pk_val}/delete" method="POST" style="display:inline; margin-left:6px;" onsubmit="return confirm('Delete this record?');">
                    <input type="hidden" name="_csrf_token" value="{html.escape(csrf_tok, quote=True)}">
                    <button type="submit" class="btn btn-sm btn-danger">Delete</button>
                </form>
            </td>
            """
            td_cells.append(actions_html)
            tbody_rows.append(f"<tr>{''.join(td_cells)}</tr>")

        tbody_html = (
            "\n".join(tbody_rows)
            if tbody_rows
            else f'<tr><td colspan="{len(list_display) + 1}" style="text-align:center; color:var(--text-muted); padding:30px;">No records found.</td></tr>'
        )

        body = f"""
        <div class="page-header">
            <div>
                <h1>{html.escape(m_admin.plural_title)}</h1>
                <p style="color:var(--text-muted); font-size:0.9rem;">{len(records)} total records</p>
            </div>
            <div>
                <a href="{clean_prefix}/{slug}/create" class="btn btn-primary">+ Add {html.escape(m_admin.title)}</a>
            </div>
        </div>
        <div class="card">
            <table class="data-table">
                <thead>{thead_html}</thead>
                <tbody>{tbody_html}</tbody>
            </table>
        </div>
        """
        html_str = _render_base_layout(
            site,
            m_admin.plural_title,
            body,
            admin_user=admin_user,
            prefix=clean_prefix,
            active_slug=slug,
        )
        return _html_response(html_str)

    async def create_get(request: Request, slug: str) -> Response:
        admin_user = get_current_admin(request)
        if not admin_user:
            return _redirect(f"{clean_prefix}/login")

        item = site.get_by_slug(slug)
        if not item:
            return not_found(f"Model '{slug}' not found.")
        _, m_admin = item

        csrf_tok = get_admin_session_store().generate_csrf_token(admin_user)
        form_fields_html = await _build_form_inputs(m_admin, csrf_token=csrf_tok)
        body = f"""
        <div class="page-header">
            <h1>Create {html.escape(m_admin.title)}</h1>
            <a href="{clean_prefix}/{slug}" class="btn btn-outline btn-sm">← Back to List</a>
        </div>
        <div class="card" style="padding: 28px; max-width: 650px;">
            <form action="{clean_prefix}/{slug}/create" method="POST">
                {form_fields_html}
                <div style="margin-top:24px; display:flex; gap:10px;">
                    <button type="submit" class="btn btn-primary">Save {html.escape(m_admin.title)}</button>
                    <a href="{clean_prefix}/{slug}" class="btn btn-outline">Cancel</a>
                </div>
            </form>
        </div>
        """
        html_str = _render_base_layout(
            site,
            f"Create {m_admin.title}",
            body,
            admin_user=admin_user,
            prefix=clean_prefix,
            active_slug=slug,
        )
        return _html_response(html_str)

    async def create_post(request: Request, slug: str) -> Response:
        admin_user = get_current_admin(request)
        if not admin_user:
            if _wants_json(request):
                return json_response({"detail": "Admin authentication required."}, status_code=401)
            return _redirect(f"{clean_prefix}/login")

        item = site.get_by_slug(slug)
        if not item:
            return not_found(f"Model '{slug}' not found.")
        model_cls, m_admin = item

        payload = await _extract_payload(request)
        session_store = get_admin_session_store()
        csrf_tok = payload.get("_csrf_token") or request.headers.get("x-csrf-token")
        if not session_store.verify_csrf_token(csrf_tok, admin_user):
            if _wants_json(request):
                return json_response({"detail": "CSRF verification failed: missing or invalid CSRF token."}, status_code=403)
            return _html_response(
                _render_base_layout(
                    site,
                    "Forbidden",
                    '<div class="alert alert-error">CSRF verification failed: missing or invalid CSRF token.</div>',
                    admin_user=admin_user,
                    prefix=clean_prefix,
                ),
                status_code=403,
            )

        fields = m_admin.get_form_fields()
        data: dict[str, Any] = {}
        protected_fields = {m_admin.primary_key, "created_at", "updated_at"}

        for f in fields:
            if f.name not in protected_fields and f.name in payload:
                try:
                    data[f.name] = f.coerce_value(payload[f.name])
                except ValueError as err:
                    if _wants_json(request):
                        return json_response({"error": str(err)}, status_code=422)
                    csrf_t = session_store.generate_csrf_token(admin_user)
                    form_fields_html = await _build_form_inputs(m_admin, values=payload, csrf_token=csrf_t)
                    body = f"""
                    <div class="page-header">
                        <h1>Create {html.escape(m_admin.title)}</h1>
                    </div>
                    <div class="card" style="padding: 28px; max-width: 650px;">
                        <form action="{clean_prefix}/{slug}/create" method="POST">
                            {form_fields_html}
                            <div style="margin-top:24px; display:flex; gap:10px;">
                                <button type="submit" class="btn btn-primary">Save {html.escape(m_admin.title)}</button>
                            </div>
                        </form>
                    </div>
                    """
                    html_str = _render_base_layout(
                        site,
                        f"Create {m_admin.title}",
                        body,
                        admin_user=admin_user,
                        prefix=clean_prefix,
                        active_slug=slug,
                        flash_error=f"Validation error: {err}",
                    )
                    return _html_response(html_str, status_code=422)

        try:
            instance = model_cls(**data)
            await instance.save()
        except Exception as exc:
            logger.exception("Failed to create record for %s", slug)
            if _wants_json(request):
                return json_response({"error": str(exc)}, status_code=422)
            csrf_t = session_store.generate_csrf_token(admin_user)
            form_fields_html = await _build_form_inputs(m_admin, values=payload, csrf_token=csrf_t)
            body = f"""
            <div class="page-header">
                <h1>Create {html.escape(m_admin.title)}</h1>
            </div>
            <div class="card" style="padding: 28px; max-width: 650px;">
                <form action="{clean_prefix}/{slug}/create" method="POST">
                    {form_fields_html}
                    <div style="margin-top:24px; display:flex; gap:10px;">
                        <button type="submit" class="btn btn-primary">Save {html.escape(m_admin.title)}</button>
                    </div>
                </form>
            </div>
            """
            html_str = _render_base_layout(
                site,
                f"Create {m_admin.title}",
                body,
                admin_user=admin_user,
                prefix=clean_prefix,
                active_slug=slug,
                flash_error=f"Error saving record: {exc}",
            )
            return _html_response(html_str, status_code=422)

        if _wants_json(request):
            return json_response(
                instance.to_dict() if hasattr(instance, "to_dict") else getattr(instance, "_attributes", {}),
                status_code=201,
            )
        return _redirect(f"{clean_prefix}/{slug}")

    async def edit_get(request: Request, slug: str, id: str) -> Response:
        admin_user = get_current_admin(request)
        if not admin_user:
            return _redirect(f"{clean_prefix}/login")

        item = site.get_by_slug(slug)
        if not item:
            return not_found(f"Model '{slug}' not found.")
        model_cls, m_admin = item

        pk_col = getattr(model_cls, "__primary_key__", "id")
        pk_val = _coerce_pk(m_admin, id)
        record = await model_cls.query().where(pk_col, pk_val).first()
        if not record:
            return not_found(f"{m_admin.title} with ID {id} not found.")

        current_values: dict[str, Any] = {}
        for f in m_admin.get_fields():
            val = getattr(record, f.name, None)
            if val is not None:
                current_values[f.name] = val
        csrf_tok = get_admin_session_store().generate_csrf_token(admin_user)
        form_fields_html = await _build_form_inputs(m_admin, values=current_values, csrf_token=csrf_tok)

        body = f"""
        <div class="page-header">
            <h1>Edit {html.escape(m_admin.title)} #{html.escape(id)}</h1>
            <a href="{clean_prefix}/{slug}" class="btn btn-outline btn-sm">← Back to List</a>
        </div>
        <div class="card" style="padding: 28px; max-width: 650px;">
            <form action="{clean_prefix}/{slug}/{id}/edit" method="POST">
                {form_fields_html}
                <div style="margin-top:24px; display:flex; gap:10px;">
                    <button type="submit" class="btn btn-primary">Update {html.escape(m_admin.title)}</button>
                    <a href="{clean_prefix}/{slug}" class="btn btn-outline">Cancel</a>
                </div>
            </form>
        </div>
        """
        html_str = _render_base_layout(
            site,
            f"Edit {m_admin.title} #{id}",
            body,
            admin_user=admin_user,
            prefix=clean_prefix,
            active_slug=slug,
        )
        return _html_response(html_str)

    async def edit_post(request: Request, slug: str, id: str) -> Response:
        admin_user = get_current_admin(request)
        if not admin_user:
            if _wants_json(request):
                return json_response({"detail": "Admin authentication required."}, status_code=401)
            return _redirect(f"{clean_prefix}/login")

        item = site.get_by_slug(slug)
        if not item:
            return not_found(f"Model '{slug}' not found.")
        model_cls, m_admin = item

        pk_col = getattr(model_cls, "__primary_key__", "id")
        pk_val = _coerce_pk(m_admin, id)
        record = await model_cls.query().where(pk_col, pk_val).first()
        if not record:
            return not_found(f"{m_admin.title} with ID {id} not found.")

        payload = await _extract_payload(request)
        session_store = get_admin_session_store()
        csrf_tok = payload.get("_csrf_token") or request.headers.get("x-csrf-token")
        if not session_store.verify_csrf_token(csrf_tok, admin_user):
            if _wants_json(request):
                return json_response({"detail": "CSRF verification failed: missing or invalid CSRF token."}, status_code=403)
            return _html_response(
                _render_base_layout(
                    site,
                    "Forbidden",
                    '<div class="alert alert-error">CSRF verification failed: missing or invalid CSRF token.</div>',
                    admin_user=admin_user,
                    prefix=clean_prefix,
                ),
                status_code=403,
            )

        fields = m_admin.get_form_fields()
        protected_fields = {m_admin.primary_key, "created_at", "updated_at"}

        for f in fields:
            if f.name not in protected_fields and f.name in payload:
                try:
                    setattr(record, f.name, f.coerce_value(payload[f.name]))
                except ValueError as err:
                    if _wants_json(request):
                        return json_response({"error": str(err)}, status_code=422)
                    csrf_t = session_store.generate_csrf_token(admin_user)
                    form_fields_html = await _build_form_inputs(m_admin, values=payload, csrf_token=csrf_t)
                    body = f"""
                    <div class="card" style="padding: 28px; max-width: 650px;">
                        <form action="{clean_prefix}/{slug}/{id}/edit" method="POST">
                            {form_fields_html}
                            <button type="submit" class="btn btn-primary">Update {html.escape(m_admin.title)}</button>
                        </form>
                    </div>
                    """
                    html_str = _render_base_layout(
                        site,
                        f"Edit {m_admin.title} #{id}",
                        body,
                        admin_user=admin_user,
                        prefix=clean_prefix,
                        active_slug=slug,
                        flash_error=f"Validation error: {err}",
                    )
                    return _html_response(html_str, status_code=422)

        try:
            await record.save()
        except Exception as exc:
            logger.exception("Failed to update record #%s for %s", id, slug)
            if _wants_json(request):
                return json_response({"error": str(exc)}, status_code=422)
            csrf_t = session_store.generate_csrf_token(admin_user)
            form_fields_html = await _build_form_inputs(m_admin, values=payload, csrf_token=csrf_t)
            body = f"""
            <div class="card" style="padding: 28px; max-width: 650px;">
                <form action="{clean_prefix}/{slug}/{id}/edit" method="POST">
                    {form_fields_html}
                    <button type="submit" class="btn btn-primary">Update {html.escape(m_admin.title)}</button>
                </form>
            </div>
            """
            html_str = _render_base_layout(
                site,
                f"Edit {m_admin.title} #{id}",
                body,
                admin_user=admin_user,
                prefix=clean_prefix,
                active_slug=slug,
                flash_error=f"Error updating record: {exc}",
            )
            return _html_response(html_str, status_code=422)

        if _wants_json(request):
            return json_response(
                record.to_dict() if hasattr(record, "to_dict") else getattr(record, "_attributes", {})
            )
        return _redirect(f"{clean_prefix}/{slug}")

    async def delete_handler(request: Request, slug: str, id: str) -> Response:
        admin_user = get_current_admin(request)
        if not admin_user:
            if _wants_json(request):
                return json_response({"detail": "Admin authentication required."}, status_code=401)
            return _redirect(f"{clean_prefix}/login")

        item = site.get_by_slug(slug)
        if not item:
            return not_found(f"Model '{slug}' not found.")
        model_cls, m_admin = item

        payload = await _extract_payload(request)
        session_store = get_admin_session_store()
        csrf_tok = payload.get("_csrf_token") or request.headers.get("x-csrf-token")
        if not session_store.verify_csrf_token(csrf_tok, admin_user):
            if _wants_json(request):
                return json_response({"detail": "CSRF verification failed: missing or invalid CSRF token."}, status_code=403)
            return _html_response(
                _render_base_layout(
                    site,
                    "Forbidden",
                    '<div class="alert alert-error">CSRF verification failed: missing or invalid CSRF token.</div>',
                    admin_user=admin_user,
                    prefix=clean_prefix,
                ),
                status_code=403,
            )

        pk_col = getattr(model_cls, "__primary_key__", "id")
        pk_val = _coerce_pk(m_admin, id)
        record = await model_cls.query().where(pk_col, pk_val).first()
        if not record:
            return not_found(f"Record with ID {id} not found.")

        try:
            await record.delete()
        except Exception as exc:
            logger.exception("Failed to delete record #%s for %s", id, slug)
            if _wants_json(request):
                return json_response({"error": str(exc)}, status_code=500)

        if _wants_json(request):
            return json_response({"message": f"Record #{id} deleted successfully."})
        return _redirect(f"{clean_prefix}/{slug}")

    # Register CRUD routes
    router.add_route("GET", f"{clean_prefix}/{{slug}}", list_view)
    router.add_route("GET", f"{clean_prefix}/{{slug}}/create", create_get)
    router.add_route("POST", f"{clean_prefix}/{{slug}}/create", create_post)
    router.add_route("GET", f"{clean_prefix}/{{slug}}/{{id}}/edit", edit_get)
    router.add_route("POST", f"{clean_prefix}/{{slug}}/{{id}}/edit", edit_post)
    router.add_route("POST", f"{clean_prefix}/{{slug}}/{{id}}/delete", delete_handler)
    router.add_route("DELETE", f"{clean_prefix}/{{slug}}/{{id}}", delete_handler)

    return router


async def _build_form_inputs(
    m_admin: ModelAdmin,
    values: dict[str, Any] | None = None,
    csrf_token: str | None = None,
) -> str:
    """Dynamically generate form inputs for model declared fields and relationships."""
    current_values = values or {}
    fields = m_admin.get_form_fields()
    relationships = m_admin.get_relationships()
    html_parts: list[str] = []

    if csrf_token:
        html_parts.append(
            f'<input type="hidden" name="_csrf_token" value="{html.escape(csrf_token, quote=True)}">'
        )

    for f in fields:
        val = current_values.get(f.name, f.default if f.default is not None else "")
        field_id = f"field_{f.name}"
        label_text = html.escape(f.label)

        # Relationship select input for BelongsTo foreign keys
        if f.is_foreign_key and f.foreign_relation_name:
            rel_meta = relationships.get(f.foreign_relation_name)
            options_html = ['<option value="">-- Select --</option>']
            if rel_meta:
                try:
                    target_model = rel_meta.get_target_model()
                    if target_model is not None:
                        target_records = await target_model.query().get()
                        target_pk = getattr(target_model, "__primary_key__", "id")

                        for t_rec in target_records:
                            t_pk_val = getattr(t_rec, target_pk, None)
                            selected = ' selected="selected"' if str(t_pk_val) == str(val) else ""
                            opt_text = html.escape(_model_repr(t_rec), quote=True)
                            escaped_pk = html.escape(str(t_pk_val), quote=True)
                            options_html.append(f'<option value="{escaped_pk}"{selected}>{opt_text}</option>')
                except (RuntimeError, OSError, ValueError) as exc:
                    logger.debug("Failed to query target relation records: %s", exc)

            input_html = f"""
            <select id="{html.escape(field_id, quote=True)}" name="{html.escape(f.name, quote=True)}" class="form-control">
                {''.join(options_html)}
            </select>
            """
        elif f.input_type == "textarea":
            input_html = f'<textarea id="{html.escape(field_id, quote=True)}" name="{html.escape(f.name, quote=True)}" class="form-control">{html.escape(str(val), quote=True)}</textarea>'
        elif f.input_type == "checkbox":
            checked = "checked" if val in (True, "true", "1", 1, "on") else ""
            input_html = f'<input type="checkbox" id="{html.escape(field_id, quote=True)}" name="{html.escape(f.name, quote=True)}" value="true" {checked} style="width:18px; height:18px;">'
        else:
            val_str = html.escape(str(val), quote=True) if val != "" else ""
            input_html = f'<input type="{html.escape(f.input_type, quote=True)}" id="{html.escape(field_id, quote=True)}" name="{html.escape(f.name, quote=True)}" value="{val_str}" class="form-control">'

        html_parts.append(f"""
        <div class="form-group">
            <label for="{html.escape(field_id, quote=True)}">{label_text}</label>
            {input_html}
        </div>
        """)

    return "\n".join(html_parts)


def _humanize_header(col: str) -> str:
    if col.lower() == "id":
        return "ID"
    clean = col.removesuffix("_id") if col.endswith("_id") and len(col) > 3 else col
    return clean.replace("_", " ").title()


def compile_admin_routes(site: AdminSite, app: Application, prefix: str = "/admin") -> None:
    """Mount the compiled admin router directly onto a SwiftPY Application."""
    router = create_admin_router(site, prefix=prefix)
    # Merge admin routes into application's router
    for route in router.routes:
        app.router.add_compiled_route(route)


# Wire route compiler into AdminSite
set_admin_route_compiler(compile_admin_routes, create_admin_router)

__all__ = [
    "compile_admin_routes",
    "create_admin_router",
]
