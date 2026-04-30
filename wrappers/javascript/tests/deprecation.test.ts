import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  deprecationWarning,
  _resetDeprecationsForTests,
} from "../src/_deprecation.js";

let warnSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  _resetDeprecationsForTests();
  warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
});

afterEach(() => {
  warnSpy.mockRestore();
  delete process.env["CHECKRD_QUIET_DEPRECATIONS"];
});

describe("deprecationWarning", () => {
  it("warns on first use, silent thereafter", () => {
    deprecationWarning("old-api", "v2.0");
    deprecationWarning("old-api", "v2.0");
    deprecationWarning("old-api", "v2.0");
    expect(warnSpy).toHaveBeenCalledOnce();
  });

  it("includes the version and optional detail", () => {
    deprecationWarning("legacy.call", "v2.0", "use newCall() instead");
    const msg = String(warnSpy.mock.calls[0]?.[0]);
    expect(msg).toContain("legacy.call");
    expect(msg).toContain("v2.0");
    expect(msg).toContain("newCall");
  });

  it("suppresses every warning under CHECKRD_QUIET_DEPRECATIONS=1", () => {
    process.env["CHECKRD_QUIET_DEPRECATIONS"] = "1";
    deprecationWarning("quiet-api", "v2.0");
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it("keeps separate memory per name", () => {
    deprecationWarning("a", "v2.0");
    deprecationWarning("b", "v2.0");
    deprecationWarning("a", "v2.0");
    expect(warnSpy).toHaveBeenCalledTimes(2);
  });
});
