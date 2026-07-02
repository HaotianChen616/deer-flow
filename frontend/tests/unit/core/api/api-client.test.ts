import { afterEach, expect, test, rs } from "@rstest/core";

import {
  clearReconnectRun,
  getAPIClient,
  isInactiveRunStreamError,
  isRunNotCancellableError,
} from "@/core/api/api-client";

function makeSessionStorage() {
  const values = new Map<string, string>();
  return {
    getItem: rs.fn((key: string) => values.get(key) ?? null),
    removeItem: rs.fn((key: string) => {
      values.delete(key);
    }),
    setItem: rs.fn((key: string, value: string) => {
      values.set(key, value);
    }),
  };
}

afterEach(() => {
  rs.unstubAllGlobals();
});

test("identifies inactive run stream errors", () => {
  const error = Object.assign(
    new Error(
      'HTTP 409: {"detail":"Run run-1 is not active on this worker and cannot be streamed"}',
    ),
    { status: 409 },
  );

  expect(isInactiveRunStreamError(error)).toBe(true);
});

test("does not classify unrelated conflict errors as inactive streams", () => {
  const error = Object.assign(new Error("HTTP 409: run is still active"), {
    status: 409,
  });

  expect(isInactiveRunStreamError(error)).toBe(false);
});

test("clears matching reconnect metadata", () => {
  const sessionStorage = makeSessionStorage();
  sessionStorage.setItem("lg:stream:thread-1", "run-1");
  rs.stubGlobal("window", { sessionStorage });

  clearReconnectRun("thread-1", "run-1");

  expect(sessionStorage.removeItem).toHaveBeenCalledWith("lg:stream:thread-1");
});

test("keeps newer reconnect metadata", () => {
  const sessionStorage = makeSessionStorage();
  sessionStorage.setItem("lg:stream:thread-1", "newer-run");
  rs.stubGlobal("window", { sessionStorage });

  clearReconnectRun("thread-1", "stale-run");

  expect(sessionStorage.removeItem).not.toHaveBeenCalled();
});

test("ignores reconnect metadata storage access failures", () => {
  rs.stubGlobal("window", {
    get sessionStorage() {
      throw new DOMException("Blocked", "SecurityError");
    },
  });

  expect(() => clearReconnectRun("thread-1", "run-1")).not.toThrow();
});

test("clears stale reconnect metadata when join stream cannot be resumed", async () => {
  const sessionStorage = makeSessionStorage();
  sessionStorage.setItem("lg:stream:thread-1", "run-1");
  rs.stubGlobal("window", {
    location: { origin: "http://localhost:2026" },
    sessionStorage,
  });
  rs.stubGlobal(
    "fetch",
    rs.fn(async () => {
      return new Response(
        JSON.stringify({
          detail:
            "Run run-1 is not active on this worker and cannot be streamed",
        }),
        { status: 409 },
      );
    }),
  );

  await expect(
    getAPIClient(true).runs.joinStream("thread-1", "run-1").next(),
  ).resolves.toMatchObject({ done: true });

  expect(sessionStorage.removeItem).toHaveBeenCalledWith("lg:stream:thread-1");
});

test("rethrows unrelated streaming errors", async () => {
  const sessionStorage = makeSessionStorage();
  sessionStorage.setItem("lg:stream:thread-1", "run-1");
  rs.stubGlobal("window", {
    location: { origin: "http://localhost:2026" },
    sessionStorage,
  });
  rs.stubGlobal(
    "fetch",
    rs.fn(async () => {
      return new Response(JSON.stringify({ detail: "run is still active" }), {
        status: 409,
      });
    }),
  );

  await expect(
    getAPIClient(true).runs.joinStream("thread-1", "run-1").next(),
  ).rejects.toThrow("HTTP 409");

  expect(sessionStorage.removeItem).not.toHaveBeenCalled();
});

test("identifies terminal-state cancel conflicts", () => {
  const error = Object.assign(
    new Error(
      'HTTP 409: {"detail":"Run run-1 is not cancellable (status: success)"}',
    ),
    { status: 409 },
  );

  expect(isRunNotCancellableError(error)).toBe(true);
});

test("does not classify not-active-on-worker cancel as terminal", () => {
  // A run still pending/running on another worker is a real cancel failure —
  // it must stay visible and must NOT be swallowed.
  const error = Object.assign(
    new Error(
      'HTTP 409: {"detail":"Run run-1 is not active on this worker and cannot be cancelled"}',
    ),
    { status: 409 },
  );

  expect(isRunNotCancellableError(error)).toBe(false);
});

test("swallows terminal-state cancel 409 and clears stale key", async () => {
  const sessionStorage = makeSessionStorage();
  sessionStorage.setItem("lg:stream:thread-1", "run-1");
  rs.stubGlobal("window", {
    location: { origin: "http://localhost:2026" },
    sessionStorage,
  });
  rs.stubGlobal(
    "fetch",
    rs.fn(async () => {
      return new Response(
        JSON.stringify({
          detail: "Run run-1 is not cancellable (status: success)",
        }),
        { status: 409 },
      );
    }),
  );

  // Resolves (no throw) — cancelling an already-finished run is a no-op.
  await expect(getAPIClient(true).runs.cancel("thread-1", "run-1")).resolves.toBeUndefined();

  expect(sessionStorage.removeItem).toHaveBeenCalledWith("lg:stream:thread-1");
});

test("rethrows not-active-on-worker cancel 409", async () => {
  const sessionStorage = makeSessionStorage();
  sessionStorage.setItem("lg:stream:thread-1", "run-1");
  rs.stubGlobal("window", {
    location: { origin: "http://localhost:2026" },
    sessionStorage,
  });
  rs.stubGlobal(
    "fetch",
    rs.fn(async () => {
      return new Response(
        JSON.stringify({
          detail:
            "Run run-1 is not active on this worker and cannot be cancelled",
        }),
        { status: 409 },
      );
    }),
  );

  await expect(
    getAPIClient(true).runs.cancel("thread-1", "run-1"),
  ).rejects.toThrow("HTTP 409");

  expect(sessionStorage.removeItem).not.toHaveBeenCalled();
});

test("short-circuits reconnect to a terminal run", async () => {
  const sessionStorage = makeSessionStorage();
  sessionStorage.setItem("lg:stream:thread-1", "run-1");
  const fetchFn = rs.fn(async (url: string | URL) => {
    const path = url.toString();
    // Preflight GET /threads/{tid}/runs/{runId} reports a finished run.
    if (path.endsWith("/runs/run-1")) {
      return new Response(JSON.stringify({ status: "success" }), {
        status: 200,
      });
    }
    // If join were attempted it must never run; fail loudly if it does.
    return new Response(JSON.stringify({ detail: "unexpected join" }), {
      status: 500,
    });
  });
  rs.stubGlobal("window", {
    location: { origin: "http://localhost:2026" },
    sessionStorage,
  });
  rs.stubGlobal("fetch", fetchFn);

  const gen = getAPIClient(true).runs.joinStream("thread-1", "run-1");
  await expect(gen.next()).resolves.toMatchObject({ done: true });

  // Preflight only — no stream/join request beyond the GET.
  expect(fetchFn).toHaveBeenCalledTimes(1);
  expect(sessionStorage.removeItem).toHaveBeenCalledWith("lg:stream:thread-1");
});

test("falls back to join when preflight cannot resolve the run", async () => {
  const sessionStorage = makeSessionStorage();
  sessionStorage.setItem("lg:stream:thread-1", "run-1");
  const fetchFn = rs.fn(async (url: string | URL) => {
    const path = url.toString();
    // Preflight GET 404s (record evicted) — must fall back to join.
    if (path.endsWith("/runs/run-1")) {
      return new Response(JSON.stringify({ detail: "Run run-1 not found" }), {
        status: 404,
      });
    }
    // Join then surfaces the inactive-stream 409 and clears the key.
    return new Response(
      JSON.stringify({
        detail: "Run run-1 is not active on this worker and cannot be streamed",
      }),
      { status: 409 },
    );
  });
  rs.stubGlobal("window", {
    location: { origin: "http://localhost:2026" },
    sessionStorage,
  });
  rs.stubGlobal("fetch", fetchFn);

  await expect(
    getAPIClient(true).runs.joinStream("thread-1", "run-1").next(),
  ).resolves.toMatchObject({ done: true });

  expect(sessionStorage.removeItem).toHaveBeenCalledWith("lg:stream:thread-1");
});
