/**
 * Deprecation-warning helper. Emits a console warning the first time a
 * deprecated API is touched; silent thereafter. Suppressed entirely
 * when `CHECKRD_QUIET_DEPRECATIONS=1` is set in the environment.
 */

const _seen = new Set<string>();

/** Clear the seen-set. Test-only. */
export function _resetDeprecationsForTests(): void {
  _seen.clear();
}

/**
 * Emit a one-shot deprecation warning for `name`. Subsequent calls with
 * the same `name` are no-ops. Warnings route through `console.warn`
 * unless `CHECKRD_QUIET_DEPRECATIONS=1`, which silences everything.
 */
export function deprecationWarning(
  name: string,
  removedInVersion: string,
  detail?: string,
): void {
  if (_seen.has(name)) return;
  _seen.add(name);
  const proc = (globalThis as unknown as { process?: { env?: Record<string, string | undefined> } }).process;
  if (proc?.env?.CHECKRD_QUIET_DEPRECATIONS === "1") return;
  const parts = [
    `checkrd: '${name}' is deprecated and will be removed in ${removedInVersion}.`,
  ];
  if (detail) parts.push(detail);
  parts.push("Set CHECKRD_QUIET_DEPRECATIONS=1 to silence.");
  console.warn(parts.join(" "));
}
