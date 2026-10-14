import { useCallback, useMemo, useState } from 'react';
import type { FormEvent } from 'react';

import { formatBytes, formatDate, listCoas, orDash } from '../api';
import type { CoaPage, CoaQuery, LoginResult } from '../api';
import {
  ApiErrorNotice,
  Badge,
  EmptyState,
  Field,
  IconRefresh,
  IconSearch,
  TableSkeleton,
} from '../ui';
import { useResource } from '../useAsync';

/**
 * Search screen — the portal's entry point.
 *
 * Filters are sent to the API as-is (`search`, `status`, `date_from`,
 * `date_to`, `plant`, `product`, `page`, `page_size`): the browser filters
 * nothing locally and the result total therefore comes from the server.
 * Selecting a row opens the detail screen for that `coa_id`.
 */

interface Filters {
  search: string;
  status: string;
  dateFrom: string;
  dateTo: string;
  plant: string;
  product: string;
}

const EMPTY_FILTERS: Filters = {
  search: '',
  status: '',
  dateFrom: '',
  dateTo: '',
  plant: '',
  product: '',
};

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: 'All completion statuses' },
  { value: 'completed', label: 'Completed' },
  { value: 'pending', label: 'Pending' },
  { value: 'failed', label: 'Failed' },
];

const PAGE_SIZE = 25;

interface SearchScreenProps {
  session: LoginResult;
  onOpen: (coaId: string) => void;
}

export function SearchScreen({ session, onOpen }: SearchScreenProps) {
  const [draft, setDraft] = useState<Filters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<CoaQuery>({});
  const [page, setPage] = useState(1);

  const query = useMemo<CoaQuery>(
    () => ({
      ...applied,
      page,
      pageSize: PAGE_SIZE,
    }),
    [applied, page],
  );

  const loadPage = useCallback(() => listCoas(query), [query]);
  const { data, error, loading, reload } = useResource<CoaPage>(loadPage, [
    query.search,
    query.status,
    query.dateFrom,
    query.dateTo,
    query.plant,
    query.product,
    query.page,
  ]);

  const pageResult = data;
  const items = pageResult?.items ?? [];
  const total = pageResult?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPage(1);
    setApplied({
      search: draft.search.trim(),
      status: draft.status,
      dateFrom: draft.dateFrom,
      dateTo: draft.dateTo,
      plant: draft.plant.trim(),
      product: draft.product.trim(),
    });
  }

  function resetFilters() {
    setDraft(EMPTY_FILTERS);
    setPage(1);
    setApplied({});
  }

  const hasFilters =
    Boolean(applied.search) ||
    Boolean(applied.status) ||
    Boolean(applied.dateFrom) ||
    Boolean(applied.dateTo) ||
    Boolean(applied.plant) ||
    Boolean(applied.product);

  return (
    <div className="stack">
      <header className="page-head">
        <div className="page-head__text">
          <p className="page-head__eyebrow">COA self-service</p>
          <h1 className="page-head__title">Search certificates of analysis</h1>
          <p className="page-head__lede">
            Filter the plant register by certificate, customer or issue date, then open a
            certificate to validate it, download the signed PDF or print it for the plant
            file. Plants in scope for {session.username}:{' '}
            {session.plants.length > 0 ? session.plants.join(', ') : 'all assigned plants'}.
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
            Refresh
          </button>
        </div>
      </header>

      <section className="card" aria-labelledby="search-filters-title">
        <div className="card__head">
          <h2 className="card__title" id="search-filters-title">
            Filters
          </h2>
          <p className="card__hint">
            Sent to the API as <span className="mono">search</span>,{' '}
            <span className="mono">status</span>, <span className="mono">date_from</span>,{' '}
            <span className="mono">date_to</span>, <span className="mono">plant</span>,{' '}
            <span className="mono">product</span>
          </p>
        </div>
        <div className="card__body">
          <form className="filter-bar" onSubmit={applyFilters} noValidate>
            <div className="filter-bar__search">
              <Field
                label="Search COAs"
                htmlFor="filter-search"
                hint="COA number, customer or vessel"
              >
                <input
                  id="filter-search"
                  className="input"
                  type="search"
                  name="search"
                  autoComplete="off"
                  placeholder="COA-2026-004182"
                  value={draft.search}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, search: event.target.value }))
                  }
                />
              </Field>
            </div>

            <div className="filter-bar__status">
              <Field label="Completion status" htmlFor="filter-status">
                <select
                  id="filter-status"
                  className="select"
                  name="status"
                  value={draft.status}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, status: event.target.value }))
                  }
                >
                  {STATUS_OPTIONS.map((option) => (
                    <option key={option.value || 'all'} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </Field>
            </div>

            <div className="filter-bar__dates">
              <Field label="Issue date from" htmlFor="filter-date-from">
                <input
                  id="filter-date-from"
                  className="input"
                  type="date"
                  name="date_from"
                  value={draft.dateFrom}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, dateFrom: event.target.value }))
                  }
                />
              </Field>
              <Field label="Issue date to" htmlFor="filter-date-to">
                <input
                  id="filter-date-to"
                  className="input"
                  type="date"
                  name="date_to"
                  value={draft.dateTo}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, dateTo: event.target.value }))
                  }
                />
              </Field>
            </div>

            <div className="filter-bar__scope">
              <Field label="Plant" htmlFor="filter-plant" hint="Plant of origin">
                <input
                  id="filter-plant"
                  className="input"
                  type="text"
                  name="plant"
                  autoComplete="off"
                  list="filter-plants"
                  placeholder="VEC"
                  value={draft.plant}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, plant: event.target.value }))
                  }
                />
                <datalist id="filter-plants">
                  {session.plants.map((plant) => (
                    <option key={plant} value={plant} />
                  ))}
                </datalist>
              </Field>
              <Field label="Product" htmlFor="filter-product" hint="Product code">
                <input
                  id="filter-product"
                  className="input"
                  type="text"
                  name="product"
                  autoComplete="off"
                  placeholder="ALM-RAW-24"
                  value={draft.product}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, product: event.target.value }))
                  }
                />
              </Field>
            </div>

            <div className="filter-bar__actions">
              <button type="submit" className="btn btn--primary">
                <IconSearch size={16} />
                Search certificates
              </button>
              <button type="button" className="btn btn--ghost" onClick={resetFilters}>
                Reset filters
              </button>
              {hasFilters ? (
                <span className="muted small">
                  {loading
                    ? 'Querying the API…'
                    : `${total} certificate${total === 1 ? '' : 's'} matched`}
                </span>
              ) : null}
            </div>
          </form>
        </div>
      </section>

      <section className="card" aria-labelledby="search-results-title">
        <div className="card__head">
          <h2 className="card__title" id="search-results-title">
            Results
          </h2>
          <p className="card__hint">
            {loading
              ? 'Loading certificates…'
              : `Page ${page} of ${totalPages} · ${total} certificate${total === 1 ? '' : 's'}`}
          </p>
        </div>

        <div className="card__body card__body--flush">
          {error ? (
            <div className="card__body">
              <ApiErrorNotice error={error} onRetry={reload} />
            </div>
          ) : loading ? (
            <div className="card__body card__body--flush">
              <TableSkeleton rows={6} columns={8} />
            </div>
          ) : items.length === 0 ? (
            <EmptyState
              title="No certificates matched"
              hint={
                hasFilters
                  ? 'Widen the filters — or clear them to browse the whole register.'
                  : 'The register returned no certificates for this session.'
              }
              action={
                hasFilters ? (
                  <button type="button" className="btn btn--secondary" onClick={resetFilters}>
                    Reset filters
                  </button>
                ) : null
              }
            />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <caption className="sr-only">
                  Certificates of analysis matching the current filters
                </caption>
                <thead>
                  <tr>
                    <th scope="col">COA</th>
                    <th scope="col">Plant of origin</th>
                    <th scope="col">Customer</th>
                    <th scope="col">Product</th>
                    <th scope="col">Lot</th>
                    <th scope="col">Issue date</th>
                    <th scope="col">File size</th>
                    <th scope="col">Completion status</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((coa) => (
                    <tr
                      key={coa.coa_id}
                      className="is-interactive"
                      tabIndex={0}
                      onClick={() => onOpen(coa.coa_id)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          onOpen(coa.coa_id);
                        }
                      }}
                    >
                      <td>
                        <button
                          type="button"
                          className="row-action"
                          onClick={(event) => {
                            event.stopPropagation();
                            onOpen(coa.coa_id);
                          }}
                        >
                          {coa.coa_id}
                        </button>
                      </td>
                      <td>{orDash(coa.plant_of_origin)}</td>
                      <td className="mono">{orDash(coa.customer_id)}</td>
                      <td className="mono">{orDash(coa.product_code)}</td>
                      <td className="mono">{orDash(coa.lot_number)}</td>
                      <td>{formatDate(coa.issue_date)}</td>
                      <td className="numeric">{formatBytes(coa.file_size_bytes)}</td>
                      <td>
                        <Badge value={coa.completion_status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {!error && !loading && items.length > 0 ? (
          <div className="card__foot">
            <p className="pagination__status">
              Showing {(page - 1) * PAGE_SIZE + 1}–
              {Math.min(page * PAGE_SIZE, total)} of {total}
            </p>
            <div className="pagination">
              <button
                type="button"
                className="btn btn--secondary btn--sm"
                onClick={() => setPage((current) => Math.max(1, current - 1))}
                disabled={page <= 1}
              >
                Previous
              </button>
              <button
                type="button"
                className="btn btn--secondary btn--sm"
                onClick={() => setPage((current) => current + 1)}
                disabled={page >= totalPages}
              >
                Next
              </button>
              <span className="pagination__status">
                <span className="mono">page={pageResult?.page ?? page}</span>
                {' / '}
                <span className="mono">page_size={pageResult?.page_size ?? PAGE_SIZE}</span>
              </span>
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}
