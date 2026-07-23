import { describe, it, expect, beforeEach, vi } from "vitest";
import { tokenStore } from "./token-store";

/**
 * Tests for the in-memory token holder. Per ARCHITECTURE.md the access token
 * lives only in memory; these tests pin that set/clear behaviour and the
 * subscriber notification the auth provider relies on to flip between the login
 * screen and the app.
 */

describe("tokenStore", () => {
  beforeEach(() => {
    tokenStore.clear();
  });

  it("starts empty", () => {
    expect(tokenStore.getAccessToken()).toBeNull();
    expect(tokenStore.getCsrfToken()).toBeNull();
    expect(tokenStore.hasToken()).toBe(false);
  });

  it("stores the access and csrf tokens together", () => {
    tokenStore.set("access-1", "csrf-1");
    expect(tokenStore.getAccessToken()).toBe("access-1");
    expect(tokenStore.getCsrfToken()).toBe("csrf-1");
    expect(tokenStore.hasToken()).toBe(true);
  });

  it("updates only the access token on a silent refresh", () => {
    tokenStore.set("access-1", "csrf-1");
    tokenStore.setAccessToken("access-2");
    expect(tokenStore.getAccessToken()).toBe("access-2");
    // The CSRF token is unchanged by a plain access refresh.
    expect(tokenStore.getCsrfToken()).toBe("csrf-1");
  });

  it("clears both tokens", () => {
    tokenStore.set("access-1", "csrf-1");
    tokenStore.clear();
    expect(tokenStore.getAccessToken()).toBeNull();
    expect(tokenStore.getCsrfToken()).toBeNull();
  });

  it("notifies subscribers when the token presence changes", () => {
    const listener = vi.fn();
    const unsubscribe = tokenStore.subscribe(listener);
    tokenStore.set("access-1", "csrf-1");
    expect(listener).toHaveBeenLastCalledWith(true);
    tokenStore.clear();
    expect(listener).toHaveBeenLastCalledWith(false);
    unsubscribe();
    tokenStore.set("access-2", "csrf-2");
    // No further calls after unsubscribe.
    expect(listener).toHaveBeenCalledTimes(2);
  });
});
