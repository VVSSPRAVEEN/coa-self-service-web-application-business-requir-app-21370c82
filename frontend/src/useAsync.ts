import { useCallback, useEffect, useRef, useState } from 'react';
import type { DependencyList, MutableRefObject } from 'react';

import { toApiError } from './api';
import type { ApiError } from './api';

/**
 * Data-loading and action hooks shared by the portal screens.
 *
 * Both hooks keep the same three concerns out of the screens: an in-flight
 * flag, the API error as `ApiError` (so a screen can show the status, the code
 * and `missing_fields`), and a settled `data` value that is never left stale
 * against a newer request.
 */

export interface AsyncState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
}

/**
 * Load a resource whenever `deps` change, with a `reload()` for the manual
 * Refresh buttons. A request whose result arrives after unmount, or after a
 * newer request was started, is discarded.
 */
export function useResource<T>(
  loader: () => Promise<T>,
  deps: DependencyList,
): AsyncState<T> & { reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);

    loaderRef.current().then(
      (value) => {
        if (!active) {
          return;
        }
        setData(value);
        setError(null);
        setLoading(false);
      },
      (cause: unknown) => {
        if (!active) {
          return;
        }
        setData(null);
        setError(toApiError(cause));
        setLoading(false);
      },
    );

    return () => {
      active = false;
    };
    // The caller owns the dependency list; the loader is read through a ref so
    // an inline arrow function cannot retrigger the effect on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce, ...deps]);

  const reload = useCallback(() => {
    setNonce((current) => current + 1);
  }, []);

  return { data, error, loading, reload };
}

export interface ActionState<A extends unknown[], R> {
  result: R | null;
  error: ApiError | null;
  pending: boolean;
  /** Resolves with the result, or `null` when the call was refused. */
  run: (...args: A) => Promise<R | null>;
  reset: () => void;
  /**
   * The most recent failure as a ref. A caller that awaits `run` and then needs
   * to act on *why* it failed reads this: the `error` state copy has not
   * necessarily rendered yet in that same tick.
   */
  errorRef: MutableRefObject<ApiError | null>;
}

/**
 * Wrap an imperative API call (validate, download, print, create user) so a
 * screen only has to render a pending flag and an error. `reset()` clears the
 * previous outcome before a fresh attempt.
 */
export function useAction<A extends unknown[], R>(
  operation: (...args: A) => Promise<R>,
): ActionState<A, R> {
  const [result, setResult] = useState<R | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [pending, setPending] = useState(false);
  const errorRef = useRef<ApiError | null>(null);

  const operationRef = useRef(operation);
  operationRef.current = operation;

  const reset = useCallback(() => {
    setResult(null);
    setError(null);
    errorRef.current = null;
  }, []);

  const run = useCallback(async (...args: A): Promise<R | null> => {
    setPending(true);
    setError(null);
    setResult(null);
    errorRef.current = null;
    try {
      const value = await operationRef.current(...args);
      setResult(value);
      return value;
    } catch (cause) {
      const apiError = toApiError(cause);
      errorRef.current = apiError;
      setError(apiError);
      return null;
    } finally {
      setPending(false);
    }
  }, []);

  return { result, error, pending, run, reset, errorRef };
}
