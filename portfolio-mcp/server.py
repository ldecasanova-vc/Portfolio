#!/usr/bin/env python3
"""
Portfolio MCP Server — Salkantay Ventures
Lee el Master Report (Autom.) en tiempo real: Sheet1 (mensual) y Sheet2 (trimestral).

Modo local (Claude Desktop): stdio, sin autenticación, credentials.json en disco.
Modo nube (Cloud Run + conector remoto en claude.ai): HTTP + OAuth 2.1 propio,
credenciales por variable de entorno. Ver README para ambos modos.

Prerequisitos:
  pip install -r requirements.txt
"""

import base64
import json
import os
import re
import secrets
import time
from typing import Literal, Optional
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import Icon
from pydantic import AnyHttpUrl, BaseModel, Field, ConfigDict

import google.auth
import google.auth.exceptions
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
ICON_FILE = Path(__file__).parent / "icon.png"
FAVICON_FILE = Path(__file__).parent / "favicon.ico"
GOOGLE_CREDENTIALS_JSON_ENV = "GOOGLE_CREDENTIALS_JSON"

MASTER_SPREADSHEET_ID = "12SVOSvGTRaq0f_EyyeLDWWFsSnV1HSKTUv8hZzi5Xms"

SHEET_TAB = {
    "monthly": "Sheet1",
    "quarterly": "Sheet2",
}

LABEL_COLUMNS = ["Startup", "ID", "Category", "Indicator", "Unit"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

MONTH_MAP = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}

STANDARD_INDICATORS = {
    1:  ("Funding",    "Last funding round",            "Text"),
    2:  ("Funding",    "Last funding round size",       "USD"),
    3:  ("Funding",    "Last funding round date",       "YYYY-MM-DD"),
    5:  ("Funding",    "Post-money valuation",          "USD"),
    9:  ("Funding",    "Total equity capital raised",   "USD"),
    10: ("Funding",    "Total debt capital raised",     "USD"),
    11: ("Funding",    "New financing round next 12mo", "Yes/No"),
    15: ("Financials", "Total revenue",                 "USD"),
    18: ("Financials", "ARR",                           "USD"),
    19: ("Financials", "ARR projection EoY",            "USD"),
    20: ("Financials", "Gross profit",                  "USD"),
    21: ("Financials", "EBITDA",                        "USD"),
    22: ("Financials", "Net profit",                    "USD"),
    28: ("Financials", "Cash position",                 "USD"),
    29: ("Financials", "Burn rate",                     "USD"),
    30: ("Financials", "LTV",                           "USD"),
    31: ("Financials", "CAC",                           "USD"),
    32: ("Employment", "Total employees",                "#"),
    34: ("Employment", "Female employees",               "#"),
    36: ("Employment", "Total C-level",                  "#"),
    56: ("Employment", "Total annual wages",             "USD"),
    62: ("Impact",     "Jobs created (cumulative)",      "#"),
    66: ("Impact",     "Lives impacted (cumulative)",    "#"),
    70: ("ESG",        "Scope 1 GHG emissions",          "tCO2eq"),
}

KEY_METRICS = {
    15: "Revenue",
    18: "ARR",
    28: "Cash",
    29: "Burn Rate",
    1:  "Funding Round",
    32: "Employees",
}

# ---------------------------------------------------------------------------
# Cliente Google Sheets — inicializado una sola vez al arrancar el servidor
# ---------------------------------------------------------------------------

def _build_sheets_client():
    """Credenciales, en este orden:
    1. GOOGLE_CREDENTIALS_JSON: el JSON completo de una Service Account.
    2. credentials.json en disco (uso local con clave).
    3. Credenciales por defecto de Google (ADC): en Cloud Run, la Service
       Account con la que corre el servicio, sin clave descargada; en local,
       las de `gcloud auth application-default login`."""
    env_json = os.environ.get(GOOGLE_CREDENTIALS_JSON_ENV)
    if env_json:
        info = json.loads(env_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    elif CREDENTIALS_FILE.exists():
        creds = service_account.Credentials.from_service_account_file(
            str(CREDENTIALS_FILE), scopes=SCOPES
        )
    else:
        try:
            creds, _ = google.auth.default(scopes=SCOPES)
        except google.auth.exceptions.DefaultCredentialsError as e:
            raise RuntimeError(
                f"Sin credenciales: no hay {GOOGLE_CREDENTIALS_JSON_ENV}, ni credentials.json en "
                f"{CREDENTIALS_FILE}, ni credenciales por defecto de Google.\nSigue el README."
            ) from e
    return build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

SHEETS_CLIENT = _build_sheets_client()

# ---------------------------------------------------------------------------
# OAuth 2.1 propio (solo para el modo HTTP/nube) — habilitado únicamente si
# MCP_AUTH_USERNAME y MCP_AUTH_PASSWORD están definidas. En modo local
# (Claude Desktop, stdio) esto no se usa y no hace falta configurarlo.
#
# Basado en el ejemplo de referencia oficial del SDK de MCP
# (examples/servers/simple-auth de modelcontextprotocol/python-sdk), adaptado
# a un solo servidor combinado (Authorization Server + Resource Server) y a
# las credenciales del Master en vez de un usuario demo.
# ---------------------------------------------------------------------------

AUTH_USERNAME = os.environ.get("MCP_AUTH_USERNAME")
AUTH_PASSWORD = os.environ.get("MCP_AUTH_PASSWORD")
PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/")
HTTP_MODE = os.environ.get("MCP_TRANSPORT", "stdio").lower() == "http"

# Logo que el cliente (claude.ai, Desktop) puede mostrar junto al conector.
ICONS = (
    [Icon(
        src="data:image/png;base64," + base64.b64encode(ICON_FILE.read_bytes()).decode(),
        mimeType="image/png",
        sizes=["256x256"],
    )]
    if ICON_FILE.exists() else None
)

if HTTP_MODE:
    from starlette.exceptions import HTTPException
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, RedirectResponse, Response

    from mcp.server.auth.provider import (
        AccessToken,
        AuthorizationCode,
        AuthorizationParams,
        OAuthAuthorizationServerProvider,
        RefreshToken,
        construct_redirect_uri,
    )
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    if not AUTH_USERNAME or not AUTH_PASSWORD:
        raise RuntimeError(
            "MCP_TRANSPORT=http requiere MCP_AUTH_USERNAME y MCP_AUTH_PASSWORD "
            "(las credenciales que vas a usar para aprobar el acceso desde claude.ai)."
        )
    if not PUBLIC_URL:
        # Bootstrap: en el primer deploy a Cloud Run todavía no sabes la URL
        # pública (Cloud Run recién te la asigna). Arranca igual con un
        # placeholder para que el deploy no falle, y una vez que tengas la
        # URL real, vuelve a desplegar con MCP_PUBLIC_URL puesta — el login
        # OAuth no funciona correctamente hasta ese segundo deploy.
        import sys as _sys
        print(
            "AVISO: MCP_PUBLIC_URL no está definida, arrancando con un placeholder. "
            "El login OAuth NO va a funcionar hasta que redespliegues con la URL pública real.",
            file=_sys.stderr,
        )
        PUBLIC_URL = "http://localhost:8080"

    MCP_SCOPE = "portfolio"

    class SimpleOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
        """Servidor de autorización mínimo: un solo usuario (tú), login por
        formulario con usuario/contraseña fijos (MCP_AUTH_USERNAME/PASSWORD),
        códigos y tokens en memoria. Si el servicio se reinicia hay que volver
        a aprobar el acceso desde claude.ai — aceptable para uso personal."""

        def __init__(self):
            self.clients: dict[str, OAuthClientInformationFull] = {}
            self.auth_codes: dict[str, AuthorizationCode] = {}
            self.tokens: dict[str, AccessToken] = {}
            self.state_mapping: dict[str, dict[str, str | None]] = {}

        async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
            return self.clients.get(client_id)

        async def register_client(self, client_info: OAuthClientInformationFull) -> None:
            if not client_info.client_id:
                raise ValueError("No client_id provided")
            self.clients[client_info.client_id] = client_info

        async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
            state = params.state or secrets.token_hex(16)
            self.state_mapping[state] = {
                "redirect_uri": str(params.redirect_uri),
                "code_challenge": params.code_challenge,
                "redirect_uri_provided_explicitly": str(params.redirect_uri_provided_explicitly),
                "client_id": client.client_id,
                "resource": params.resource,
            }
            return f"{PUBLIC_URL}/login?state={state}&client_id={client.client_id}"

        async def get_login_page(self, state: str) -> HTMLResponse:
            if not state:
                raise HTTPException(400, "Missing state parameter")
            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Portfolio MCP — Salkantay Ventures</title>
                <style>
                    body {{ font-family: -apple-system, Arial, sans-serif; max-width: 420px; margin: 60px auto; padding: 0 20px; }}
                    .form-group {{ margin-bottom: 14px; }}
                    label {{ font-size: 13px; font-weight: 600; }}
                    input {{ width: 100%; padding: 9px; margin-top: 4px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 6px; }}
                    button {{ background-color: #0092B7; color: white; padding: 10px 16px; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; width: 100%; }}
                </style>
            </head>
            <body>
                <h2>Portfolio MCP</h2>
                <p>Ingresa tus credenciales para autorizar el acceso desde claude.ai.</p>
                <form action="{PUBLIC_URL}/login/callback" method="post">
                    <input type="hidden" name="state" value="{state}">
                    <div class="form-group">
                        <label>Usuario</label>
                        <input type="text" name="username" required>
                    </div>
                    <div class="form-group">
                        <label>Contraseña</label>
                        <input type="password" name="password" required>
                    </div>
                    <button type="submit">Autorizar acceso</button>
                </form>
            </body>
            </html>
            """
            return HTMLResponse(content=html)

        async def handle_login_callback(self, request: Request) -> Response:
            form = await request.form()
            username = form.get("username")
            password = form.get("password")
            state = form.get("state")
            if not isinstance(username, str) or not isinstance(password, str) or not isinstance(state, str):
                raise HTTPException(400, "Missing or invalid username, password, or state parameter")

            state_data = self.state_mapping.get(state)
            if not state_data:
                raise HTTPException(400, "Invalid state parameter")

            if not secrets.compare_digest(username, AUTH_USERNAME) or not secrets.compare_digest(password, AUTH_PASSWORD):
                raise HTTPException(401, "Credenciales inválidas")

            redirect_uri = state_data["redirect_uri"]
            code_challenge = state_data["code_challenge"]
            redirect_uri_provided_explicitly = state_data["redirect_uri_provided_explicitly"] == "True"
            client_id = state_data["client_id"]
            resource = state_data.get("resource")
            assert redirect_uri is not None and code_challenge is not None and client_id is not None

            new_code = f"mcp_{secrets.token_hex(16)}"
            self.auth_codes[new_code] = AuthorizationCode(
                code=new_code,
                client_id=client_id,
                redirect_uri=AnyHttpUrl(redirect_uri),
                redirect_uri_provided_explicitly=redirect_uri_provided_explicitly,
                expires_at=time.time() + 300,
                scopes=[MCP_SCOPE],
                code_challenge=code_challenge,
                resource=resource,
            )
            del self.state_mapping[state]
            return RedirectResponse(url=construct_redirect_uri(redirect_uri, code=new_code, state=state), status_code=302)

        async def load_authorization_code(
            self, client: OAuthClientInformationFull, authorization_code: str
        ) -> AuthorizationCode | None:
            return self.auth_codes.get(authorization_code)

        async def exchange_authorization_code(
            self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
        ) -> OAuthToken:
            if authorization_code.code not in self.auth_codes:
                raise ValueError("Invalid authorization code")
            if not client.client_id:
                raise ValueError("No client_id provided")

            token = f"mcp_{secrets.token_hex(32)}"
            self.tokens[token] = AccessToken(
                token=token,
                client_id=client.client_id,
                scopes=authorization_code.scopes,
                expires_at=int(time.time()) + 3600 * 24 * 7,  # 7 días
                resource=authorization_code.resource,
            )
            del self.auth_codes[authorization_code.code]
            return OAuthToken(
                access_token=token, token_type="Bearer", expires_in=3600 * 24 * 7,
                scope=" ".join(authorization_code.scopes),
            )

        async def load_access_token(self, token: str) -> AccessToken | None:
            access_token = self.tokens.get(token)
            if not access_token:
                return None
            if access_token.expires_at and access_token.expires_at < time.time():
                del self.tokens[token]
                return None
            return access_token

        async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
            return None

        async def exchange_refresh_token(
            self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str],
        ) -> OAuthToken:
            raise NotImplementedError("Refresh tokens not supported")

        async def revoke_token(self, token, token_type_hint: str | None = None) -> None:
            tok = token.token if hasattr(token, "token") else token
            self.tokens.pop(tok, None)

    oauth_provider = SimpleOAuthProvider()
    mcp = FastMCP(
        "portfolio_mcp",
        icons=ICONS,
        auth_server_provider=oauth_provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(PUBLIC_URL),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[MCP_SCOPE], default_scopes=[MCP_SCOPE],
            ),
            required_scopes=[MCP_SCOPE],
            resource_server_url=None,
        ),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
    )

    @mcp.custom_route("/login", methods=["GET"])
    async def login_page_handler(request):
        state = request.query_params.get("state")
        if not state:
            raise HTTPException(400, "Missing state parameter")
        return await oauth_provider.get_login_page(state)

    @mcp.custom_route("/login/callback", methods=["POST"])
    async def login_callback_handler(request):
        return await oauth_provider.handle_login_callback(request)

    @mcp.custom_route("/favicon.ico", methods=["GET"])
    async def favicon_handler(request):
        if not FAVICON_FILE.exists():
            raise HTTPException(404)
        return Response(FAVICON_FILE.read_bytes(), media_type="image/x-icon")

    @mcp.custom_route("/icon.png", methods=["GET"])
    async def icon_handler(request):
        if not ICON_FILE.exists():
            raise HTTPException(404)
        return Response(ICON_FILE.read_bytes(), media_type="image/png")

else:
    mcp = FastMCP("portfolio_mcp", icons=ICONS)

# ---------------------------------------------------------------------------
# Helpers de parsing
# ---------------------------------------------------------------------------

def parse_month_header(label: str) -> Optional[str]:
    """'Jan - 22' -> '2022-01'"""
    m = re.match(r'^([A-Za-z]{3})\s*-\s*(\d{2})$', label.strip())
    if not m:
        return None
    month = MONTH_MAP.get(m.group(1).title())
    return f"20{m.group(2)}-{month}" if month else None


def parse_quarter_header(label: str) -> Optional[str]:
    """'1Q-22' -> '2022-Q1'"""
    m = re.match(r'^([1-4])Q\s*-\s*(\d{2})$', label.strip())
    return f"20{m.group(2)}-Q{m.group(1)}" if m else None


def fetch_master(granularity: Literal["monthly", "quarterly"] = "quarterly") -> list[dict]:
    """Lee y parsea el Master Report completo (todas las startups) en una sola llamada.

    La fila de encabezado se ubica dinámicamente buscando
    Startup/ID/Category/Indicator/Unit, en vez de asumir un número de fila
    fijo — el Master puede tener filas de instrucciones o espaciadoras arriba.
    """
    tab = SHEET_TAB[granularity]
    result = SHEETS_CLIENT.values().get(
        spreadsheetId=MASTER_SPREADSHEET_ID,
        range=tab,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    raw = result.get("values", [])

    header_idx = next(
        (i for i, row in enumerate(raw)
         if [str(c).strip() for c in row[:5]] == LABEL_COLUMNS),
        None,
    )
    if header_idx is None:
        raise RuntimeError(
            f"No se encontró la fila de encabezado (Startup/ID/Category/Indicator/Unit) en '{tab}'."
        )

    header = raw[header_idx]
    parse_header = parse_quarter_header if granularity == "quarterly" else parse_month_header
    col_periods: dict[int, str] = {}
    for i in range(5, len(header)):
        label = str(header[i]).strip()
        period = parse_header(label) if label else None
        if period:
            col_periods[i] = period

    records = []
    for row in raw[header_idx + 1:]:
        if not row or not str(row[0]).strip():
            continue
        startup = str(row[0]).strip()
        id_raw = row[1] if len(row) > 1 else None
        try:
            indicator_id = int(float(str(id_raw))) if id_raw not in (None, "") else None
        except (ValueError, TypeError):
            indicator_id = None
        category = str(row[2]).strip() if len(row) > 2 else ""
        indicator_name = str(row[3]).strip() if len(row) > 3 else ""
        unit = str(row[4]).strip() if len(row) > 4 else ""

        for col_idx, period in col_periods.items():
            if col_idx >= len(row):
                continue
            raw_val = row[col_idx]
            if raw_val in (None, ""):
                continue
            try:
                num = float(raw_val)
                value = str(int(num)) if num == int(num) else str(round(num, 4))
                value_num = num
            except (ValueError, TypeError):
                value = str(raw_val).strip()
                value_num = None

            records.append({
                "startup": startup,
                "indicator_id": indicator_id,
                "indicator_name": indicator_name,
                "category": category,
                "unit": unit,
                "period": period,
                "value": value,
                "value_num": value_num,
            })
    return records


def latest_value(records: list[dict], indicator_id: int, period: Optional[str] = None) -> Optional[dict]:
    matches = [r for r in records if r["indicator_id"] == indicator_id]
    if not matches:
        return None
    if period:
        exact = [r for r in matches if r["period"] == period]
        return exact[0] if exact else None
    return max(matches, key=lambda r: r["period"])


def resolve_startup(name: str, known: list[str]) -> Optional[str]:
    lname = name.strip().lower()
    for s in known:
        if s.lower() == lname:
            return s
    return None


# ---------------------------------------------------------------------------
# Tool 1: Listar startups
# ---------------------------------------------------------------------------

@mcp.tool(name="portfolio_list_startups")
def portfolio_list_startups() -> str:
    """Lista todas las startups del portafolio de Salkantay Ventures, leídas del Master Report.

    Returns:
        str: JSON con lista de nombres de startups.
    """
    records = fetch_master("quarterly")
    return json.dumps(sorted({r["startup"] for r in records}), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 2: Métricas de una startup
# ---------------------------------------------------------------------------

class GetMetricsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    startup:      str                              = Field(..., description="Nombre de la startup (ej. 'Welli', 'Galgo')")
    category:     Optional[str]                     = Field(default=None, description="'Funding', 'Financials', 'Employment', 'Impact', 'ESG', 'Business-Specific'")
    indicator_id: Optional[int]                     = Field(default=None, description="ID del indicador. Ej: 15=Revenue, 18=ARR, 28=Cash, 29=Burn, 32=Employees")
    period_from:  Optional[str]                     = Field(default=None, description="Período inicial (ej. '2024-01' mensual, '2024-Q3' trimestral)")
    period_to:    Optional[str]                     = Field(default=None, description="Período final (mismo formato)")
    latest_only:  bool                               = Field(default=False, description="Si True, solo devuelve el valor más reciente de cada indicador")
    granularity:  Literal["monthly", "quarterly"]    = Field(default="quarterly", description="Sheet1 (mensual) o Sheet2 (trimestral) del Master")


@mcp.tool(name="portfolio_get_startup_metrics")
def portfolio_get_startup_metrics(params: GetMetricsInput) -> str:
    """Obtiene métricas de una startup leyendo el Master Report en tiempo real.

    Returns:
        str: JSON con lista de {indicator_id, indicator, category, unit, period, value}.
    """
    records = fetch_master(params.granularity)
    known = sorted({r["startup"] for r in records})
    startup = resolve_startup(params.startup, known)
    if startup is None:
        close = [s for s in known if params.startup.lower() in s.lower()]
        return json.dumps({"error": f"'{params.startup}' no encontrada.", "sugerencias": close})

    records = [r for r in records if r["startup"] == startup]

    if params.indicator_id is not None:
        records = [r for r in records if r["indicator_id"] == params.indicator_id]
    elif params.category:
        records = [r for r in records if r["category"].lower() == params.category.lower()]

    if params.period_from:
        records = [r for r in records if r["period"] >= params.period_from]
    if params.period_to:
        records = [r for r in records if r["period"] <= params.period_to]

    if params.latest_only:
        seen: dict[str, dict] = {}
        for r in records:
            k = r["indicator_name"]
            if k not in seen or r["period"] > seen[k]["period"]:
                seen[k] = r
        records = list(seen.values())

    return json.dumps(
        [{"indicator_id": r["indicator_id"], "indicator": r["indicator_name"],
          "category": r["category"], "unit": r["unit"],
          "period": r["period"], "value": r["value"]}
         for r in records],
        ensure_ascii=False, indent=2
    )


# ---------------------------------------------------------------------------
# Tool 3: Comparar startups
# ---------------------------------------------------------------------------

class CompareInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    indicator_id:   Optional[int]                    = Field(default=None, description="ID del indicador. Ej: 15=Revenue, 18=ARR, 28=Cash, 29=Burn, 32=Employees")
    indicator_name: Optional[str]                    = Field(default=None, description="Nombre parcial (ej. 'ARR', 'burn rate', 'employees')")
    period:         Optional[str]                    = Field(default=None, description="Período a comparar. Si None, usa el más reciente de cada startup.")
    startups:       Optional[list[str]]              = Field(default=None, description="Lista de startups. Si None, compara todas.")
    granularity:    Literal["monthly", "quarterly"]  = Field(default="quarterly", description="Sheet1 (mensual) o Sheet2 (trimestral) del Master")


@mcp.tool(name="portfolio_compare_startups")
def portfolio_compare_startups(params: CompareInput) -> str:
    """Compara un indicador entre múltiples startups. Devuelve ranking de mayor a menor.

    Lee el Master Report en una sola llamada (todas las startups a la vez).

    Returns:
        str: JSON con {indicator, ranking: [{rank, startup, period, value, unit}]}.
    """
    if params.indicator_id is None and params.indicator_name is None:
        return json.dumps({"error": "Debes proveer indicator_id o indicator_name."})

    records = fetch_master(params.granularity)
    known = sorted({r["startup"] for r in records})
    target = set(params.startups) if params.startups else set(known)

    by_startup: dict[str, list[dict]] = {}
    for r in records:
        if r["startup"] in target or r["startup"].lower() in {t.lower() for t in target}:
            by_startup.setdefault(r["startup"], []).append(r)

    ranking = []
    for name, recs in by_startup.items():
        if params.indicator_id is not None:
            matches = [r for r in recs if r["indicator_id"] == params.indicator_id]
        else:
            matches = [r for r in recs if params.indicator_name.lower() in r["indicator_name"].lower()]
        if not matches:
            continue

        best = (
            next((r for r in matches if r["period"] == params.period), None)
            if params.period
            else max(matches, key=lambda r: r["period"])
        )
        if best:
            ranking.append({
                "startup":   name,
                "period":    best["period"],
                "value":     best["value"],
                "value_num": best["value_num"],
                "unit":      best["unit"],
                "indicator": best["indicator_name"],
            })

    ranking.sort(key=lambda r: r.get("value_num") or 0, reverse=True)
    for i, r in enumerate(ranking):
        r["rank"] = i + 1
        r.pop("value_num", None)

    label = next((r.get("indicator") for r in ranking if "indicator" in r), None) or \
            (f"ID:{params.indicator_id}" if params.indicator_id else params.indicator_name)

    return json.dumps({
        "indicator":   label,
        "period_note": params.period or "último período disponible por startup",
        "ranking":     ranking,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool 4: Resumen del portafolio
# ---------------------------------------------------------------------------

class SummaryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    startups:     Optional[list[str]]                = Field(default=None, description="Startups a incluir. Si None, incluye todas.")
    period:       Optional[str]                      = Field(default=None, description="Período (ej. '2025-Q1'). Si None, usa el más reciente.")
    granularity:  Literal["monthly", "quarterly"]     = Field(default="quarterly", description="Sheet1 (mensual) o Sheet2 (trimestral) del Master")


@mcp.tool(name="portfolio_get_summary")
def portfolio_get_summary(params: SummaryInput) -> str:
    """Resumen del portafolio con métricas clave: Revenue, ARR, Cash, Burn Rate, Funding, Empleados.

    Lee el Master Report en una sola llamada — siempre datos actualizados desde el último
    'Consolidar Dashboards' automático.

    Returns:
        str: JSON con {startup: {metric: {value, unit, period}}}.
    """
    records = fetch_master(params.granularity)
    known = sorted({r["startup"] for r in records})
    target = sorted(params.startups) if params.startups else known

    by_startup: dict[str, list[dict]] = {}
    for r in records:
        by_startup.setdefault(r["startup"], []).append(r)

    summary = {}
    for name in target:
        resolved = resolve_startup(name, known)
        if resolved is None:
            summary[name] = {"error": f"'{name}' no encontrada en el Master."}
            continue
        recs = by_startup.get(resolved, [])
        data = {}
        for ind_id, label in KEY_METRICS.items():
            best = latest_value(recs, ind_id, params.period)
            if best:
                data[label] = {"value": best["value"], "unit": best["unit"], "period": best["period"]}
        summary[resolved] = data

    return json.dumps(summary, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool 5: Buscar indicadores
# ---------------------------------------------------------------------------

@mcp.tool(name="portfolio_search_indicators")
def portfolio_search_indicators(
    query:    Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    """Lista indicadores estándar del portafolio con sus IDs.

    Útil para descubrir qué métricas existen antes de consultar datos.

    Args:
        query:    Texto a buscar en el nombre (ej. 'revenue', 'employees', 'ARR')
        category: Filtrar por categoría ('Funding', 'Financials', 'Employment', 'Impact', 'ESG')

    Returns:
        str: JSON con [{id, category, name, unit}].
    """
    results = [
        {"id": ind_id, "category": cat, "name": name, "unit": unit}
        for ind_id, (cat, name, unit) in STANDARD_INDICATORS.items()
        if (not query    or query.lower()    in name.lower())
        and (not category or category.lower() == cat.lower())
    ]
    return json.dumps(results, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if HTTP_MODE:
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
