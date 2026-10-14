/**
 * Typed HTTP client for the Ventura COA API.
 *
 * The browser only ever talks to one origin: every call is a same-origin
 * request under `/api`, which `vite.config.ts` proxies (prefix stripped) to the
 * FastAPI application mounted at http://api:8000 on the Compose network. There
 * is no absolute API URL in this file and no second base to configure.
 *
 * The contract mirrored here, one-for-one, with the FastAPI routers:
 *
 *   POST /api/auth/login            {"token"}            -> LoginResult
 *   GET  /api/coas?search&status&date_from&date_to&plant&product&page&page_size
 *                                                         -> CoaPage
 *   GET  /api/coas/{coa_id}                              -> COA
 *   POST /api/coas/{coa_id}/download   {"expiry_seconds"?} -> DownloadGrant
 *   POST /api/coas/{coa_id}/print/reauth {"user_id"}        -> PrintReauthGrant
 *   POST /api/coas/{coa_id}/print      {"user_id","reauth_token","printing_plant"?}
 *   POST /api/coas/{coa_id}/validate                      -> ValidateResult
 *   GET  /api/missing-coas                               -> MissingCoaReport
 *   POST /api/admin/users                {username,email,role,organization}
 *
 * The completion gate (FR6) is enforced by the API, never by the browser:
 * download and print answer 409 with
 *   { detail, code: "completion_gate_failed", coa_id, missing_fields }
 * and `ApiError` exposes that refusal as data (`code`, `missingFields`) so the
 * screens render the backend's decision verbatim.
 */

/** Same-origin API prefix; the dev/preview server proxies it to the API. */
export const BASE = '/api';

/* ------------------------------------------------------------- payloads -- */

/** A certificate of analysis as the API reports it. Field names are the API's. */
export interface COA {
  coa_id: string;
  plant_of_origin: string;
  completion_status: string;
  customer_id?: string | null;
  product_code?: string | null;
  lot_number?: string | null;
  issue_date?: string | null;
  file_size_bytes?: number | null;
  status?: string | null;
  generated_at?: string | null;
  completed_at?: string | null;
}

export interface CoaPage {
  items: COA[];
  page: number;
  page_size: number;
  total: number;
}

export interface CoaQuery {
  /** Free text: COA number, customer, vessel, voyage. */
  search?: string;
  /** Completion status filter, e.g. `completed`, `pending`, `failed`. */
  status?: string;
  /** ISO date (YYYY-MM-DD) lower bound on the issue date. */
  dateFrom?: string;
  /** ISO date (YYYY-MM-DD) upper bound on the issue date. */
  dateTo?: string;
  /** FR4: plant of origin. */
  plant?: string;
  /** FR4: product code / product description. */
  product?: string;
  page?: number;
  pageSize?: number;
}

export interface LoginResult {
  user_id: string;
  username: string;
  roles: string[];
  plants: string[];
  expires_at: string;
}

/**
 * FR3 client-side role gating.
 *
 * The API is the authority on who may administer the portal — it answers 403
 * regardless of what the browser renders — but the tab must not be offered to a
 * session that has no business on that screen. One decision, one place: the
 * shell gates the Admin tab on this and the admin screen reuses it for its own
 * entitlement note, so the two can never disagree.
 *
 * External roles (`3pl`, `customer`, `customer_service`, `viewer`, `qa`, …) and
 * an empty role list are therefore not administrators. Matching is
 * case-insensitive and whitespace-tolerant; the values themselves are never
 * normalised or invented by the browser.
 */
const ADMIN_ROLE_NAMES: ReadonlySet<string> = new Set(['internal', 'admin', 'ventura_admin']);

export function hasAdminRole(roles: readonly string[] | null | undefined): boolean {
  if (!Array.isArray(roles)) {
    return false;
  }
  return roles.some(
    (role) => typeof role === 'string' && ADMIN_ROLE_NAMES.has(role.trim().toLowerCase()),
  );
}

/**
 * FR3 client-side role gating for the Report section, mirroring the API's
 * `ROLE_RESOURCES` table (src/core/rbac.py) rather than "everyone":
 *
 *   internal -> every resource, `reports` included
 *   3pl      -> { coas, downloads, prints, reports }
 *   customer -> { coas, downloads }            <- no `reports`
 *
 * `admin` and `ventura_admin` are the portal's spelling of `internal`, so they
 * are admitted on the same terms as `hasAdminRole`; any other role — including
 * a bare `customer` — is not offered the tab. This predicate is the single
 * decision the shell uses, so the tab and the screen it opens can never
 * disagree, and the API remains the authority that ultimately answers 403.
 *
 * Matching is case-insensitive and whitespace-tolerant; role strings are never
 * normalised or invented by the browser.
 */
const REPORTS_ROLE_NAMES: ReadonlySet<string> = new Set([
  'internal',
  'admin',
  'ventura_admin',
  '3pl',
]);

export function hasReportsRole(roles: readonly string[] | null | undefined): boolean {
  if (!Array.isArray(roles)) {
    return false;
  }
  return roles.some(
    (role) => typeof role === 'string' && REPORTS_ROLE_NAMES.has(role.trim().toLowerCase()),
  );
}

export interface ValidateResult {
  coa_id: string;
  completion_status: string;
  releasable: boolean;
  missing_fields: string[];
}

export interface DownloadGrant {
  coa_id: string;
  url: string;
  expires_in: number;
  method: string;
}

export interface PrintRequest {
  userId: string;
  reauthToken: string;
  printingPlant?: string;
}

/**
 * FR8: the re-auth grant the API mints so a print request has a token to carry.
 * Both expiry shapes are accepted — an absolute `expires_at` or a relative
 * `expires_in` — and any further field is passed through untouched.
 */
export interface PrintReauthGrant {
  coa_id: string;
  reauth_token: string;
  expires_at?: string | null;
  expires_in?: number | null;
  [key: string]: unknown;
}

/**
 * The print result is an API-owned payload; the portal renders the fields it
 * knows about and passes the rest through, so a backend addition never has to
 * wait on the frontend.
 */
export interface PrintResult {
  coa_id?: string;
  status?: string;
  printed_at?: string | null;
  printing_plant?: string | null;
  page_count?: number | null;
  detail?: string;
  [key: string]: unknown;
}

export interface MissingRow {
  coa_id: string;
  vessel: string | null;
  voyage: string | null;
  counterparty: string | null;
  priority: string | null;
  required_ship_date: string | null;
  age_days: number | null;
  blocking_reason: string | null;
}

export interface MissingCoaReport {
  items: MissingRow[];
  generated_at: string;
}

export interface AdminUserPayload {
  username: string;
  email: string;
  role: string;
  organization: string;
}

export interface AdminUser {
  username: string;
  email: string;
  role: string;
  organization: string;
  created_at?: string | null;
  [key: string]: unknown;
}

/* ---------------------------------------------------------------- errors -- */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === 'string')
    : [];
}

/**
 * FastAPI reports errors as `{ "detail": ... }`, where `detail` is a string, a
 * validation list or a nested object. Flatten whatever arrived into one
 * readable sentence so no screen has to re-implement the unwrapping.
 */
function readDetail(body: unknown, fallback: string): string {
  if (typeof body === 'string' && body.trim().length > 0) {
    return body;
  }
  if (isRecord(body)) {
    const detail = body.detail;
    if (typeof detail === 'string' && detail.trim().length > 0) {
      return detail;
    }
    if (Array.isArray(detail)) {
      const messages = detail
        .map((entry) => {
          if (isRecord(entry)) {
            return asString(entry.msg) ?? asString(entry.detail);
          }
          return asString(entry);
        })
        .filter((entry): entry is string => Boolean(entry));
      if (messages.length > 0) {
        return messages.join('; ');
      }
    }
    if (isRecord(detail)) {
      return readDetail(detail, fallback);
    }
    const message = asString(body.message);
    if (message) {
      return message;
    }
  }
  return fallback;
}

/**
 * Every failed call raises this. The gate refusal, the re-auth rejection and the
 * session expiry are distinguishable without string-matching on prose:
 *
 *   err.isGateRefusal  -> 409, code === 'completion_gate_failed'
 *   err.missingFields  -> the named required fields the gate is still waiting on
 *   err.isUnauthorized -> 401, the Cognito/API session must be re-established
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | undefined;
  readonly coaId: string | undefined;
  readonly missingFields: string[];
  readonly detail: string;
  readonly body: unknown;

  constructor(status: number, body: unknown, fallbackMessage?: string) {
    const detail = readDetail(
      body,
      fallbackMessage ?? `The API answered ${status} without a description.`,
    );
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.body = body;
    this.code = isRecord(body) ? asString(body.code) : undefined;
    this.coaId = isRecord(body) ? asString(body.coa_id) : undefined;
    this.missingFields = isRecord(body)
      ? asStringArray(body.missing_fields)
      : [];
  }

  /** The FR6 completion gate refused the request: 409 completion_gate_failed. */
  get isGateRefusal(): boolean {
    return this.status === 409 && this.code === 'completion_gate_failed';
  }

  /** The session is gone or the re-auth token was rejected: sign in again. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  /** Nothing reached the API (DNS, proxy, container down, aborted request). */
  get isNetwork(): boolean {
    return this.status === 0;
  }
}

/** Narrow an unknown catch binding to ApiError without losing the message. */
export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error;
  }
  if (error instanceof Error) {
    return new ApiError(0, { detail: error.message }, error.message);
  }
  return new ApiError(0, { detail: String(error) }, String(error));
}

/**
 * Session-expiry signal. `request` is the single place a 401 can originate, so
 * it publishes one cross-cutting event here instead of every screen owning a
 * subscription: the app shell turns it into the sign-in-again notice, while the
 * screen that made the call still renders the failure inline. Returns an
 * unsubscribe function for effect cleanup.
 */
type UnauthorizedListener = () => void;

const unauthorizedListeners = new Set<UnauthorizedListener>();

export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
}

function notifyUnauthorized(): void {
  for (const listener of [...unauthorizedListeners]) {
    listener();
  }
}

/* -------------------------------------------------------------- request -- */

type QueryValue = string | number | undefined | null;

interface RequestOptions {
  method?: 'GET' | 'POST';
  body?: unknown;
  query?: Record<string, QueryValue>;
}

/**
 * One JSON request against the same origin. Empty / nullish query values are
 * dropped rather than sent as `?search=`, so the API's own defaults apply.
 * Non-2xx responses raise ApiError carrying the parsed body.
 */
async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query } = options;

  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === '') {
      continue;
    }
    search.set(key, String(value));
  }
  const queryString = search.toString();
  const url = `${BASE}${path}${queryString ? `?${queryString}` : ''}`;

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    throw new ApiError(
      0,
      {
        detail:
          cause instanceof Error && cause.message
            ? cause.message
            : `The portal could not reach ${url}.`,
      },
    );
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text.length > 0) {
    try {
      payload = JSON.parse(text) as unknown;
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    if (response.status === 401) {
      notifyUnauthorized();
    }
    throw new ApiError(response.status, payload);
  }
  return payload as T;
}

function segment(value: string): string {
  return encodeURIComponent(value);
}

/* ------------------------------------------------------------ operations -- */

/**
 * Exchange a Cognito id token for the portal session. Called exactly once per
 * authenticated mount; the returned roles/plants drive the shell identity and
 * the screens' affordances.
 */
export function login(token: string): Promise<LoginResult> {
  return request<LoginResult>('/auth/login', { method: 'POST', body: { token } });
}

/** Search / list COAs. Server-side filtering and pagination, API-decided. */
export function listCoas(params: CoaQuery = {}): Promise<CoaPage> {
  return request<CoaPage>('/coas', {
    query: {
      search: params.search,
      status: params.status,
      date_from: params.dateFrom,
      date_to: params.dateTo,
      plant: params.plant,
      product: params.product,
      page: params.page,
      page_size: params.pageSize,
    },
  });
}

/** Full COA detail view, including completion status. */
export function getCoa(coaId: string): Promise<COA> {
  return request<COA>(`/coas/${segment(coaId)}`);
}

/**
 * Mint a short-lived download grant. Rejects with ApiError.isGateRefusal
 * (409 + missing_fields) while the completion gate is unsatisfied.
 */
export function downloadCoa(
  coaId: string,
  expirySeconds?: number,
): Promise<DownloadGrant> {
  const body: { expiry_seconds?: number } = {};
  if (typeof expirySeconds === 'number' && Number.isFinite(expirySeconds)) {
    body.expiry_seconds = Math.trunc(expirySeconds);
  }
  return request<DownloadGrant>(`/coas/${segment(coaId)}/download`, {
    method: 'POST',
    body,
  });
}

/**
 * FR8 step one: mint the short-lived `reauth_token` that `printCoa` requires.
 * There is no browser-side substitute for this token, so the detail screen has
 * to call it and hand the operator a visible path to a valid token.
 */
export function requestPrintReauth(coaId: string, userId: string): Promise<PrintReauthGrant> {
  return request<PrintReauthGrant>(`/coas/${segment(coaId)}/print/reauth`, {
    method: 'POST',
    body: { user_id: userId },
  });
}

/**
 * Print a COA. Requires the operator's user id plus a fresh re-auth token;
 * rejects 401 when re-auth fails and 409 with missing_fields when the
 * completion gate refuses.
 */
export function printCoa(coaId: string, printRequest: PrintRequest): Promise<PrintResult> {
  const body: {
    user_id: string;
    reauth_token: string;
    printing_plant?: string;
  } = {
    user_id: printRequest.userId,
    reauth_token: printRequest.reauthToken,
  };
  if (printRequest.printingPlant && printRequest.printingPlant.trim().length > 0) {
    body.printing_plant = printRequest.printingPlant.trim();
  }
  return request<PrintResult>(`/coas/${segment(coaId)}/print`, {
    method: 'POST',
    body,
  });
}

/** Ask the API whether this COA may be released, and what is still missing. */
export function validateCoa(coaId: string): Promise<ValidateResult> {
  return request<ValidateResult>(`/coas/${segment(coaId)}/validate`, {
    method: 'POST',
  });
}

/** The missing-COAs report: expected-but-absent certificates, by urgency. */
export function listMissingCoas(): Promise<MissingCoaReport> {
  return request<MissingCoaReport>('/missing-coas');
}

/** Create an external portal user. 201 on success, 409 duplicate, 422 bad role. */
export function createAdminUser(payload: AdminUserPayload): Promise<AdminUser> {
  return request<AdminUser>('/admin/users', {
    method: 'POST',
    body: {
      username: payload.username.trim(),
      email: payload.email.trim(),
      role: payload.role,
      organization: payload.organization.trim(),
    },
  });
}

/* --------------------------------------------------------- presentation -- */

/**
 * Presentation helpers shared by the screens. They format what the API sent and
 * never invent a value the API did not report.
 */

export function formatDate(value: string | null | undefined): string {
  if (!value) {
    return '—';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
  });
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return '—';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatBytes(bytes: number | null | undefined): string {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) {
    return '—';
  }
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const digits = unitIndex === 0 || value >= 10 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[unitIndex]}`;
}

export function orDash(value: string | number | null | undefined): string {
  if (value === null || value === undefined) {
    return '—';
  }
  if (typeof value === 'number') {
    return Number.isFinite(value) ? String(value) : '—';
  }
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : '—';
}
