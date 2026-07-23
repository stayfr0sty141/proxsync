/**
 * In-memory holder for the access token and CSRF token.
 *
 * Per docs/ARCHITECTURE.md the access token lives in browser memory only — never
 * a cookie — so a CSRF cannot replay it. It is deliberately module-level rather
 * than React state: the fetch client (a plain function, not a hook) reads it on
 * every request, and a page reload intentionally drops it, forcing a refresh
 * from the HttpOnly cookie. The CSRF token is the double-submit value the SPA
 * echoes back in the `X-CSRF-Token` header on cookie-authenticated mutations.
 */

let accessToken: string | null = null;
let csrfToken: string | null = null;

/** Subscribers notified when the token transitions in/out of "present", so the
 * auth provider can re-render (e.g. flip from the login screen to the app). */
type Listener = (hasToken: boolean) => void;
const listeners = new Set<Listener>();

function notify(): void {
  const has = accessToken !== null;
  for (const listener of listeners) {
    listener(has);
  }
}

export const tokenStore = {
  getAccessToken(): string | null {
    return accessToken;
  },

  getCsrfToken(): string | null {
    return csrfToken;
  },

  set(access: string, csrf: string): void {
    accessToken = access;
    csrfToken = csrf;
    notify();
  },

  /** Update only the access token after a silent refresh (CSRF is unchanged). */
  setAccessToken(access: string): void {
    accessToken = access;
    notify();
  },

  clear(): void {
    accessToken = null;
    csrfToken = null;
    notify();
  },

  hasToken(): boolean {
    return accessToken !== null;
  },

  subscribe(listener: Listener): () => void {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },
};
