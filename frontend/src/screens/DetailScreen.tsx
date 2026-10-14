import { useState } from 'react';
import type { FormEvent } from 'react';

import {
  downloadCoa,
  formatBytes,
  formatDate,
  formatDateTime,
  getCoa,
  orDash,
  printCoa,
  requestPrintReauth,
  validateCoa,
} from '../api';
import type { COA, DownloadGrant, LoginResult, PrintReauthGrant, PrintResult } from '../api';
import {
  Alert,
  ApiErrorNotice,
  Badge,
  DefinitionList,
  Field,
  FieldChip,
  IconArrowLeft,
  IconDownload,
  IconPrinter,
  IconRefresh,
  IconShield,
  KeyValue,
  Loading,
  statusTone,
} from '../ui';
import { useAction, useResource } from '../useAsync';

/**
 * Detail screen for one `coa_id`: the full certificate, the completion-gate
 * decision, and the two gated deliveries (download grant, print).
 *
 * The gate belongs to the API. This screen asks `POST /coas/{id}/validate` and
 * renders the answer; when download or print is refused it renders the 409 body
 * — the refusal reason, the machine code and every named `missing_fields`
 * entry — instead of deciding anything in the browser.
 */

interface DetailScreenProps {
  coaId: string;
  session: LoginResult;
  onBack: () => void;
}

/** A re-auth grant this screen minted, held for display until it is superseded. */
interface IssuedReauth {
  token: string;
  coaId: string;
  issuedAt: string;
  /** Rendered deadline, or null when the API reported no expiry at all. */
  expiresAt: string | null;
}

/**
 * The API may report a re-auth deadline as an absolute instant or as a lifetime
 * in seconds; render whichever arrived and invent neither. Returns null when it
 * reported neither.
 */
function readReauthExpiry(grant: PrintReauthGrant): string | null {
  const at = grant.expires_at;
  if (typeof at === 'string' && at.trim().length > 0) {
    return formatDateTime(at);
  }
  const seconds = grant.expires_in;
  if (typeof seconds === 'number' && Number.isFinite(seconds)) {
    return formatDateTime(new Date(Date.now() + seconds * 1000).toISOString());
  }
  return null;
}

export function DetailScreen({ coaId, session, onBack }: DetailScreenProps) {
  const loadCoa = () => getCoa(coaId);
  const { data, error, loading, reload } = useResource<COA>(loadCoa, [coaId]);

  const validate = useAction((id: string) => validateCoa(id));
  const download = useAction((id: string, expiry?: number) => downloadCoa(id, expiry));
  const reauth = useAction((id: string, operator: string) => requestPrintReauth(id, operator));
  const print = useAction(
    (id: string, userId: string, token: string, plant?: string) =>
      printCoa(id, { userId, reauthToken: token, printingPlant: plant }),
  );

  const [expirySeconds, setExpirySeconds] = useState('');
  const [userId, setUserId] = useState(session.user_id);
  const [reauthToken, setReauthToken] = useState('');
  const [printingPlant, setPrintingPlant] = useState(session.plants[0] ?? '');
  const [issued, setIssued] = useState<IssuedReauth | null>(null);
  const [reauthNotice, setReauthNotice] = useState<string | null>(null);

  const hasToken = reauthToken.trim().length > 0;

  function requestDownload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = expirySeconds.trim().length > 0 ? Number(expirySeconds) : undefined;
    void download.run(coaId, Number.isFinite(parsed) ? parsed : undefined);
  }

  async function mintReauthToken() {
    const operator = userId.trim();
    const grant = await reauth.run(coaId, operator);
    if (!grant) {
      return;
    }
    const token = typeof grant.reauth_token === 'string' ? grant.reauth_token : '';
    if (token.length === 0) {
      setReauthNotice('The API issued no re-auth token. Try again or contact the platform team.');
      return;
    }
    setIssued({
      token,
      coaId:
        typeof grant.coa_id === 'string' && grant.coa_id.trim().length > 0 ? grant.coa_id : coaId,
      issuedAt: new Date().toISOString(),
      expiresAt: readReauthExpiry(grant),
    });
    setReauthToken(token);
    setReauthNotice(null);
    print.reset();
  }

  async function requestPrint(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = reauthToken.trim();
    if (token.length === 0) {
      return;
    }
    const printed = await print.run(
      coaId,
      userId.trim(),
      token,
      printingPlant.trim().length > 0 ? printingPlant.trim() : undefined,
    );
    if (printed === null && print.errorRef.current?.isUnauthorized) {
      setIssued(null);
      setReauthToken('');
      setReauthNotice(
        'The API rejected this print token (HTTP 401). Mint a fresh one in step 1 — the previous token is now cleared.',
      );
    }
  }

  return (
    <div className="stack">
      <header className="page-head">
        <div className="page-head__text">
          <p className="page-head__eyebrow">Certificate of analysis</p>
          <h1 className="page-head__title mono">{coaId}</h1>
          <p className="page-head__lede">
            Validation, download and printing are decided by the API. Nothing on this screen
            releases a document on its own.
          </p>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn btn--secondary" onClick={onBack}>
            <IconArrowLeft size={16} />
            Back to search
          </button>
          <button
            type="button"
            className="btn btn--secondary"
            onClick={reload}
            disabled={loading}
          >
            <IconRefresh size={16} />
            Reload
          </button>
        </div>
      </header>

      {error ? (
        <ApiErrorNotice
          error={error}
          title={
            error.status === 404
              ? 'Certificate not found'
              : 'The certificate could not be loaded'
          }
          onRetry={reload}
        />
      ) : null}

      {loading ? <Loading message={`Loading ${coaId}…`} /> : null}

      {data ? (
        <>
          <section className="card" aria-labelledby="detail-certificate-title">
            <div className="card__head">
              <h2 className="card__title" id="detail-certificate-title">
                Certificate record
              </h2>
              <Badge value={data.completion_status} />
            </div>
            <div className="card__body">
              <DefinitionList
                items={[
                  ['coa_id', <span className="mono">{orDash(data.coa_id)}</span>],
                  ['plant_of_origin', orDash(data.plant_of_origin)],
                  ['customer_id', <span className="mono">{orDash(data.customer_id)}</span>],
                  ['product_code', <span className="mono">{orDash(data.product_code)}</span>],
                  ['lot_number', <span className="mono">{orDash(data.lot_number)}</span>],
                  ['issue_date', formatDate(data.issue_date)],
                  ['status', <Badge value={data.status} />],
                  ['file_size_bytes', formatBytes(data.file_size_bytes)],
                  ['generated_at', formatDateTime(data.generated_at)],
                  ['completed_at', formatDateTime(data.completed_at)],
                ]}
              />
            </div>
          </section>

          <section className="card" aria-labelledby="detail-gate-title">
            <div className="card__head">
              <h2 className="card__title" id="detail-gate-title">
                <span className="row">
                  <IconShield size={18} />
                  Completion gate
                </span>
              </h2>
              <p className="card__hint">POST /api/coas/{coaId}/validate</p>
            </div>
            <div className="card__body stack">
              <div className="row">
                <button
                  type="button"
                  className="btn btn--primary"
                  onClick={() => void validate.run(coaId)}
                  disabled={validate.pending}
                >
                  {validate.pending ? 'Validating…' : 'Validate completion'}
                </button>
                <span className="muted small">
                  Ask the API whether this certificate may be released.
                </span>
              </div>

              {validate.error ? (
                <ApiErrorNotice error={validate.error} title="Validation failed" />
              ) : null}

              {validate.result ? (
                <Alert
                  tone={
                    validate.result.releasable ? 'success' : 'warn'
                  }
                  title={
                    validate.result.releasable
                      ? 'Releasable — every required test field is populated'
                      : 'Not releasable yet — the completion gate is unsatisfied'
                  }
                >
                  <p className="small">
                    completion_status:{' '}
                    <span className="mono">{orDash(validate.result.completion_status)}</span> ·
                    releasable: <span className="mono">{String(validate.result.releasable)}</span>
                  </p>
                  {validate.result.missing_fields.length > 0 ? (
                    <div className="stack stack--tight">
                      <p className="small">
                        missing_fields ({validate.result.missing_fields.length}):
                      </p>
                      <ul className="chip-list">
                        {validate.result.missing_fields.map((name) => (
                          <li key={name}>
                            <FieldChip name={name} />
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : (
                    <p className="small">missing_fields: none</p>
                  )}
                </Alert>
              ) : null}
            </div>
          </section>

          <section className="card" aria-labelledby="detail-download-title">
            <div className="card__head">
              <h2 className="card__title" id="detail-download-title">
                <span className="row">
                  <IconDownload size={18} />
                  Download
                </span>
              </h2>
              <p className="card__hint">POST /api/coas/{coaId}/download</p>
            </div>
            <div className="card__body stack">
              <form className="row" onSubmit={requestDownload}>
                <div className="field">
                  <label className="field__label" htmlFor="download-expiry">
                    Link expiry (seconds)
                  </label>
                  <input
                    id="download-expiry"
                    className="input"
                    type="number"
                    min={1}
                    max={604800}
                    step={1}
                    placeholder="900"
                    value={expirySeconds}
                    onChange={(event) => setExpirySeconds(event.target.value)}
                  />
                  <span className="field__hint">Optional — the API default applies if empty</span>
                </div>
                <button
                  type="submit"
                  className="btn btn--primary"
                  disabled={download.pending}
                >
                  {download.pending ? 'Requesting…' : 'Request download link'}
                </button>
              </form>

              {download.error ? (
                <ApiErrorNotice
                  error={download.error}
                  title={
                    download.error.isGateRefusal
                      ? 'Download refused by the completion gate'
                      : 'Download could not be issued'
                  }
                />
              ) : null}

              {download.result ? <DownloadGrantCard grant={download.result} /> : null}
            </div>
          </section>

          <section className="card" aria-labelledby="detail-print-title">
            <div className="card__head">
              <h2 className="card__title" id="detail-print-title">
                <span className="row">
                  <IconPrinter size={18} />
                  Print
                </span>
              </h2>
              <p className="card__hint">
                Two steps: re-authenticate, then print — the API refuses a print without a
                fresh token
              </p>
            </div>
            <div className="card__body stack">
              <div className="steps">
                <section
                  className="step"
                  data-state={issued ? 'done' : 'current'}
                  aria-labelledby="print-reauth-title"
                >
                  <span className="step__index" aria-hidden="true">
                    1
                  </span>
                  <div className="step__body">
                    <div className="step__head">
                      <h3 className="step__title" id="print-reauth-title">
                        Re-authenticate
                      </h3>
                      <p className="step__hint">
                        <span className="mono">POST /api/coas/{coaId}/print/reauth</span> — issues
                        the short-lived <span className="mono">reauth_token</span> that step 2
                        sends.
                      </p>
                    </div>

                    <div className="row">
                      <button
                        type="button"
                        className="btn btn--primary"
                        onClick={() => void mintReauthToken()}
                        disabled={reauth.pending || userId.trim().length === 0}
                      >
                        <IconShield size={16} />
                        {reauth.pending
                          ? 'Requesting…'
                          : issued
                            ? 'Re-authenticate again'
                            : 'Re-authenticate'}
                      </button>
                      <span className="muted small">
                        Requests a token for{' '}
                        <span className="mono">{userId.trim() || 'the user id below'}</span>.
                      </span>
                    </div>

                    {reauth.error ? (
                      <ApiErrorNotice
                        error={reauth.error}
                        title={
                          reauth.error.isUnauthorized
                            ? 'Re-authentication rejected (401)'
                            : 'The print token could not be issued'
                        }
                      />
                    ) : null}

                    {issued ? (
                      <Alert tone="success" title="Print token issued">
                        <KeyValue
                          rows={[
                            ['coa_id', <span className="mono">{orDash(issued.coaId)}</span>],
                            ['reauth_token', <span className="mono">{issued.token}</span>],
                            ['issued_at', formatDateTime(issued.issuedAt)],
                            ['expires_at', issued.expiresAt ?? 'not reported'],
                          ]}
                        />
                      </Alert>
                    ) : null}

                    {reauthNotice ? (
                      <Alert tone="error" title="Sign in again to print">
                        <p className="small">{reauthNotice}</p>
                      </Alert>
                    ) : null}
                  </div>
                </section>

                <section className="step" aria-labelledby="print-submit-title">
                  <span className="step__index" aria-hidden="true">
                    2
                  </span>
                  <div className="step__body">
                    <div className="step__head">
                      <h3 className="step__title" id="print-submit-title">
                        Print
                      </h3>
                      <p className="step__hint">
                        <span className="mono">POST /api/coas/{coaId}/print</span> — the token
                        from step 1 is filled in below and stays editable.
                      </p>
                    </div>

                    <form className="stack" onSubmit={requestPrint} noValidate>
                      <div className="grid-3">
                        <Field label="User id" htmlFor="print-user-id">
                          <input
                            id="print-user-id"
                            className="input"
                            type="text"
                            autoComplete="username"
                            value={userId}
                            onChange={(event) => setUserId(event.target.value)}
                          />
                        </Field>
                        <Field
                          label="Re-auth token"
                          htmlFor="print-reauth-token"
                          hint="Pre-filled by step 1 — editing it is allowed"
                        >
                          <input
                            id="print-reauth-token"
                            className="input mono"
                            type="text"
                            name="reauth_token"
                            autoComplete="off"
                            spellCheck={false}
                            value={reauthToken}
                            onChange={(event) => {
                              setReauthToken(event.target.value);
                              if (event.target.value.trim().length > 0) {
                                setReauthNotice(null);
                              }
                            }}
                          />
                        </Field>
                        <Field
                          label="Printing plant"
                          htmlFor="print-plant"
                          hint="Optional — defaults to your first assigned plant"
                        >
                          <input
                            id="print-plant"
                            className="input"
                            type="text"
                            list="printing-plants"
                            value={printingPlant}
                            onChange={(event) => setPrintingPlant(event.target.value)}
                          />
                          <datalist id="printing-plants">
                            {session.plants.map((plant) => (
                              <option key={plant} value={plant} />
                            ))}
                          </datalist>
                        </Field>
                      </div>
                      <div className="row">
                        <button
                          type="submit"
                          className="btn btn--primary"
                          disabled={print.pending || !hasToken}
                        >
                          {print.pending ? 'Printing…' : 'Print certificate'}
                        </button>
                        {hasToken ? null : (
                          <span className="field__hint">
                            Re-authenticate to mint a print token
                          </span>
                        )}
                        {print.pending ? (
                          <Loading message="Waiting for the print service" />
                        ) : null}
                      </div>
                    </form>

                    {print.error ? (
                      <ApiErrorNotice
                        error={print.error}
                        title={
                          print.error.isUnauthorized
                            ? 'Re-authentication failed (401)'
                            : print.error.isGateRefusal
                              ? 'Print refused by the completion gate'
                              : 'Print request failed'
                        }
                      />
                    ) : null}

                    {print.result ? <PrintResultCard result={print.result} /> : null}
                  </div>
                </section>
              </div>
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
}

function DownloadGrantCard({ grant }: { grant: DownloadGrant }) {
  return (
    <Alert tone="success" title="Download link issued">
      <ul className="chip-list">
        <li>
          <FieldChip name={`method: ${orDash(grant.method)}`} />
        </li>
        <li>
          <FieldChip name={`expires_in: ${grant.expires_in}s`} />
        </li>
        <li>
          <FieldChip name={`coa_id: ${orDash(grant.coa_id)}`} />
        </li>
      </ul>
      <p className="small">
        <a href={grant.url} target="_blank" rel="noreferrer noopener">
          Open the signed certificate ({orDash(grant.method)})
        </a>
      </p>
    </Alert>
  );
}

function PrintResultCard({ result }: { result: PrintResult }) {
  return (
    <Alert
      tone={result.status && statusTone(result.status) === 'blocked' ? 'error' : 'success'}
      title="Print job accepted"
    >
      <KeyValue
        rows={[
          ['coa_id', <span className="mono">{orDash(result.coa_id)}</span>],
          ['status', <Badge value={result.status} />],
          ['printed_at', formatDateTime(result.printed_at ?? null)],
          ['printing_plant', orDash(result.printing_plant)],
          ['page_count', orDash(result.page_count)],
        ]}
      />
    </Alert>
  );
}
