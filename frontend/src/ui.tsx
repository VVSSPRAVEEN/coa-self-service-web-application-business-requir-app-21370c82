import type { ReactElement, ReactNode } from 'react';

import type { ApiError } from './api';

/**
 * Portal design-system primitives.
 *
 * Every visual value these components use comes from a token in styles.css —
 * no component in this file hard-codes a colour, a pixel measurement or a
 * duration. Icons are inline SVG (no emoji, no icon-font request); they inherit
 * `currentColor` so a badge, a button and a table row all read as one system.
 */

/* ------------------------------------------------------------------ tone -- */

export type Tone = 'complete' | 'pending' | 'blocked' | 'neutral' | 'info';

const COMPLETE_WORDS = new Set([
  'completed',
  'complete',
  'releasable',
  'released',
  'generated',
  'ready',
  'active',
  'success',
  'succeeded',
  'ok',
]);

const PENDING_WORDS = new Set([
  'pending',
  'in_progress',
  'processing',
  'incomplete',
  'partial',
  'awaiting',
  'draft',
  'queued',
]);

const BLOCKED_WORDS = new Set([
  'failed',
  'failure',
  'error',
  'blocked',
  'missing',
  'expired',
  'rejected',
  'void',
  'overdue',
]);

/**
 * Map an API status value onto the design system's state tones. Unrecognised
 * values fall through to `neutral` and are rendered verbatim — the portal never
 * invents a status the API did not report.
 */
export function statusTone(status: string | null | undefined): Tone {
  const value = (status ?? '').trim().toLowerCase();
  if (value.length === 0) {
    return 'neutral';
  }
  if (COMPLETE_WORDS.has(value)) {
    return 'complete';
  }
  if (PENDING_WORDS.has(value)) {
    return 'pending';
  }
  if (BLOCKED_WORDS.has(value)) {
    return 'blocked';
  }
  return 'neutral';
}

/** Shipping urgency reads on the same tone scale as completion status. */
export function priorityTone(priority: string | null | undefined): Tone {
  const value = (priority ?? '').trim().toLowerCase();
  if (value === 'critical' || value === 'high') {
    return 'blocked';
  }
  if (value === 'medium') {
    return 'pending';
  }
  if (value === 'low') {
    return 'info';
  }
  return 'neutral';
}

export function label(value: string | null | undefined, fallback = 'Unknown'): string {
  const trimmed = (value ?? '').trim();
  return trimmed.length > 0 ? trimmed : fallback;
}

/* ----------------------------------------------------------------- icons -- */

interface IconProps {
  size?: number;
  className?: string;
}

function svgProps(size: number, className?: string) {
  return {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.75,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
    focusable: 'false' as const,
    className,
  };
}

export function IconSearch({ size = 16, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </svg>
  );
}

export function IconDocument({ size = 16, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5" />
      <path d="M9 13h6M9 17h4" />
    </svg>
  );
}

export function IconChart({ size = 16, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
    </svg>
  );
}

export function IconUsers({ size = 16, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <circle cx="9" cy="8" r="3.5" />
      <path d="M2.5 20a6.5 6.5 0 0 1 13 0" />
      <path d="M16 5.2a3.5 3.5 0 0 1 0 5.6M18 20a6.4 6.4 0 0 0-2.2-4.8" />
    </svg>
  );
}

export function IconCheck({ size = 14, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <path d="m4 12.5 5 5L20 6.5" />
    </svg>
  );
}

export function IconClock({ size = 14, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </svg>
  );
}

export function IconAlert({ size = 14, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <path d="M12 4.5 21 19.5H3z" />
      <path d="M12 10v4M12 16.8v.2" />
    </svg>
  );
}

export function IconInfo({ size = 14, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5M12 8.2v.2" />
    </svg>
  );
}

export function IconDot({ size = 14, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <circle cx="12" cy="12" r="4" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function IconDownload({ size = 16, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <path d="M12 3v11" />
      <path d="m7.5 10 4.5 4.5L16.5 10" />
      <path d="M4 20h16" />
    </svg>
  );
}

export function IconPrinter({ size = 16, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <path d="M7 9V3h10v6" />
      <path d="M5 9h14a2 2 0 0 1 2 2v5h-4" />
      <path d="M3 16V11a2 2 0 0 1 2-2" />
      <path d="M7 14h10v7H7z" />
    </svg>
  );
}

export function IconShield({ size = 16, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <path d="M12 3 5 6v6c0 4 3 7 7 9 4-2 7-5 7-9V6z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}

export function IconArrowLeft({ size = 16, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <path d="M19 12H5" />
      <path d="m11 6-6 6 6 6" />
    </svg>
  );
}

export function IconRefresh({ size = 16, className }: IconProps) {
  return (
    <svg {...svgProps(size, className)}>
      <path d="M20 11a8 8 0 1 0-1.6 5.6" />
      <path d="M20 4v6h-6" />
    </svg>
  );
}

const TONE_GLYPHS: Record<Tone, (props: IconProps) => ReactElement> = {
  complete: IconCheck,
  pending: IconClock,
  blocked: IconAlert,
  neutral: IconDot,
  info: IconInfo,
};

/* ---------------------------------------------------------------- badges -- */

interface BadgeProps {
  value: string | null | undefined;
  tone?: Tone;
  className?: string;
}

/**
 * A status or priority pill: tinted wash + ink text + glyph, with the API's
 * value rendered verbatim (uppercased by the token-driven stylesheet) so the
 * portal and the API never disagree about what a record is.
 */
export function Badge({ value, tone, className }: BadgeProps) {
  const text = label(value);
  const resolved = tone ?? statusTone(value);
  const Glyph = TONE_GLYPHS[resolved];
  return (
    <span className={`badge badge--${resolved}${className ? ` ${className}` : ''}`} title={text}>
      <span className="badge__glyph">
        <Glyph size={12} />
      </span>
      {text}
    </span>
  );
}

/** A named required field the completion gate is still waiting on. */
export function FieldChip({ name }: { name: string }) {
  return <span className="chip">{name}</span>;
}

/* ---------------------------------------------------------------- alerts -- */

export type AlertTone = 'error' | 'warn' | 'info' | 'success' | 'neutral';

interface AlertProps {
  tone: AlertTone;
  title: string;
  children?: ReactNode;
  actions?: ReactNode;
}

const ALERT_GLYPHS: Record<AlertTone, (props: IconProps) => ReactElement> = {
  error: IconAlert,
  warn: IconAlert,
  info: IconInfo,
  success: IconCheck,
  neutral: IconInfo,
};

export function Alert({ tone, title, children, actions }: AlertProps) {
  const Glyph = ALERT_GLYPHS[tone];
  return (
    <div className={`alert alert--${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      <span className="alert__glyph">
        <Glyph size={16} />
      </span>
      <div className="alert__body">
        <p className="alert__title">{title}</p>
        {children ? <div className="alert__text">{children}</div> : null}
        {actions ? <div className="alert__actions">{actions}</div> : null}
      </div>
    </div>
  );
}

/**
 * Render a failed API call. The HTTP status, the machine-readable code and the
 * gate's `missing_fields` are all shown: an operator has to be able to see why
 * the API refused, not just that something went wrong.
 */
export function ApiErrorNotice({
  error,
  title,
  onRetry,
  retryLabel = 'Try again',
}: {
  error: ApiError;
  title?: string;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  const heading =
    title ??
    (error.isGateRefusal
      ? 'Blocked by the completion gate'
      : error.isUnauthorized
        ? 'Session rejected (401)'
        : error.isNetwork
          ? 'The API could not be reached'
          : `Request failed (HTTP ${error.status})`);

  return (
    <Alert
      tone={error.isGateRefusal ? 'warn' : 'error'}
      title={heading}
      actions={
        onRetry ? (
          <button type="button" className="btn btn--secondary btn--sm" onClick={onRetry}>
            <IconRefresh size={14} />
            {retryLabel}
          </button>
        ) : undefined
      }
    >
      <p>{error.detail}</p>
      <p className="small">
        <span className="mono">HTTP {error.status}</span>
        {error.code ? (
          <>
            {' · code '}
            <span className="mono">{error.code}</span>
          </>
        ) : null}
        {error.coaId ? (
          <>
            {' · coa_id '}
            <span className="mono">{error.coaId}</span>
          </>
        ) : null}
      </p>
      {error.missingFields.length > 0 ? (
        <div className="stack stack--tight">
          <p className="small">
            Required fields still missing ({error.missingFields.length}):
          </p>
          <ul className="chip-list">
            {error.missingFields.map((name) => (
              <li key={name}>
                <FieldChip name={name} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Alert>
  );
}

/* ------------------------------------------------------- loading / empty -- */

export function Loading({ message = 'Loading…' }: { message?: string }) {
  return (
    <p className="loading" role="status">
      <span className="spinner" aria-hidden="true" />
      {message}
    </p>
  );
}

/** Placeholder rows drawn while a table's first page is in flight. */
export function TableSkeleton({ rows = 5, columns = 6 }: { rows?: number; columns?: number }) {
  return (
    <table className="table" aria-hidden="true">
      <tbody>
        {Array.from({ length: rows }, (_, rowIndex) => (
          <tr key={rowIndex}>
            {Array.from({ length: columns }, (_, columnIndex) => (
              <td key={columnIndex}>
                <div className="skeleton skeleton-row" />
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty__glyph">
        <IconDocument size={24} />
      </span>
      <p className="empty__title">{title}</p>
      {hint ? <p className="small">{hint}</p> : null}
      {action}
    </div>
  );
}

/* ----------------------------------------------------------------- forms -- */

interface FieldProps {
  label: string;
  htmlFor: string;
  hint?: string;
  children: ReactNode;
}

/** Label + control + hint, wired for assistive technology. */
export function Field({ label: text, htmlFor, hint, children }: FieldProps) {
  return (
    <div className="field">
      <label className="field__label" htmlFor={htmlFor}>
        {text}
      </label>
      {children}
      {hint ? (
        <span className="field__hint" id={`${htmlFor}-hint`}>
          {hint}
        </span>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------- key/value -- */

export function DefinitionList({ items }: { items: Array<[string, ReactNode]> }) {
  return (
    <dl className="definition-list">
      {items.map(([term, value]) => (
        <div key={term}>
          <dt>{term}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

interface KeyValueProps {
  rows: Array<[string, ReactNode]>;
}

/** Stacked label/value rows for long, ordered field sets. */
export function KeyValue({ rows }: KeyValueProps) {
  return (
    <dl className="kv">
      {rows.map(([term, value]) => (
        <div className="kv__row" key={term}>
          <dt className="kv__key">{term}</dt>
          <dd className="kv__value">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
