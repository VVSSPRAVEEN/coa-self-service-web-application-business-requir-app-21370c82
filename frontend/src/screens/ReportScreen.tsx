import {
  formatDate,
  formatDateTime,
  listMissingCoas,
  orDash,
} from '../api';
import type { LoginResult, MissingCoaReport, MissingRow } from '../api';
import {
  ApiErrorNotice,
  Badge,
  DefinitionList,
  EmptyState,
  IconChart,
  IconRefresh,
  TableSkeleton,
  priorityTone,
} from '../ui';
import { useResource } from '../useAsync';

/**
 * Missing-COAs report.
 *
 * Everything on this screen is the API's own view of what has not arrived yet:
 * `GET /api/missing-coas` returns the rows and the `generated_at` stamp of the
 * run, so the table and the summary tiles both describe the same snapshot. The
 * portal adds no ageing rule of its own — `age_days` and `priority` are read,
 * not recomputed.
 */

interface ReportScreenProps {
  session: LoginResult;
  onOpen: (coaId: string) => void;
}

export function ReportScreen({ session, onOpen }: ReportScreenProps) {
  const loadReport = () => listMissingCoas();
  const { data, error, loading, reload } = useResource<MissingCoaReport>(loadReport, []);

  const rows: MissingRow[] = data?.items ?? [];
  const overdue = rows.filter((row) => (row.age_days ?? 0) >= 7).length;
  const urgent = rows.filter((row) => {
    const priority = (row.priority ?? '').toLowerCase();
    return priority === 'critical' || priority === 'high';
  }).length;
  const oldest = rows.reduce<number>(
    (current, row) => Math.max(current, row.age_days ?? 0),
    0,
  );

  return (
    <div className="stack">
      <header className="page-head">
        <div className="page-head__text">
          <p className="page-head__eyebrow">Report</p>
          <h1 className="page-head__title">Missing certificates of analysis</h1>
          <p className="page-head__lede">
            Certificates the downstream process is waiting on, ordered by urgency. Every
            counterparty and ship date below is the value the discovery report supplied.
          </p>
        </div>
        <div className="page-head__actions">
          <button
            type="button"
            className="btn btn--secondary"
            onClick={reload}
            disabled={loading}
          >
            <IconRefresh size={16} />
            Refresh report
          </button>
        </div>
      </header>

      {data ? (
        <section className="card" aria-labelledby="report-summary-title">
          <div className="card__head">
            <h2 className="card__title" id="report-summary-title">
              <span className="row">
                <IconChart size={18} />
                Snapshot
              </span>
            </h2>
            <p className="card__hint">
              generated_at{' '}
              <span className="mono">{formatDateTime(data.generated_at)}</span>
            </p>
          </div>
          <div className="card__body">
            <DefinitionList
              items={[
                ['Missing certificates', String(rows.length)],
                ['High / critical priority', String(urgent)],
                ['Waiting 7 days or more', String(overdue)],
                ['Oldest outstanding (days)', String(oldest)],
                [
                  'Requested by',
                  <span className="mono">{session.username}</span>,
                ],
              ]}
            />
          </div>
        </section>
      ) : null}

      <section className="card" aria-labelledby="report-rows-title">
        <div className="card__head">
          <h2 className="card__title" id="report-rows-title">
            Outstanding certificates
          </h2>
          <p className="card__hint">
            {loading
              ? 'Loading report…'
              : `${rows.length} row${rows.length === 1 ? '' : 's'} in this snapshot`}
          </p>
        </div>

        <div className="card__body card__body--flush">
          {error ? (
            <div className="card__body">
              <ApiErrorNotice error={error} onRetry={reload} />
            </div>
          ) : loading ? (
            <TableSkeleton rows={6} columns={8} />
          ) : rows.length === 0 ? (
            <EmptyState
              title="Nothing is missing"
              hint="Every certificate the downstream process is waiting for has arrived. Refresh to re-run the report."
              action={
                <button type="button" className="btn btn--secondary" onClick={reload}>
                  <IconRefresh size={16} />
                  Refresh report
                </button>
              }
            />
          ) : (
            <div className="table-wrap">
              <table className="table table--dense">
                <caption className="sr-only">
                  Certificates of analysis that have not arrived, by urgency
                </caption>
                <thead>
                  <tr>
                    <th scope="col">COA</th>
                    <th scope="col">Priority</th>
                    <th scope="col">Counterparty</th>
                    <th scope="col">Vessel</th>
                    <th scope="col">Voyage</th>
                    <th scope="col">Required ship date</th>
                    <th scope="col">Age (days)</th>
                    <th scope="col">Blocking reason</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.coa_id}>
                      <td>
                        <button
                          type="button"
                          className="row-action"
                          onClick={() => onOpen(row.coa_id)}
                        >
                          {row.coa_id}
                        </button>
                      </td>
                      <td>
                        <Badge value={row.priority} tone={priorityTone(row.priority)} />
                      </td>
                      <td>{orDash(row.counterparty)}</td>
                      <td className="mono">{orDash(row.vessel)}</td>
                      <td className="mono">{orDash(row.voyage)}</td>
                      <td>{formatDate(row.required_ship_date)}</td>
                      <td className="numeric">{orDash(row.age_days)}</td>
                      <td className="small cell-wrap">{orDash(row.blocking_reason)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
