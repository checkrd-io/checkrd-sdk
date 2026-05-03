/**
 * Demonstrate the per-status error class hierarchy.
 *
 * Run with: npx tsx examples/error-handling.ts
 */
import Checkrd, {
  AuthenticationError,
  APIConnectionError,
  APIStatusError,
  NotFoundError,
  RateLimitError,
} from "@checkrd/api";

async function main() {
  const client = new Checkrd({
    apiKey: process.env.CHECKRD_API_KEY ?? "ck_live_invalid",
  });

  try {
    await client.agents.retrieve("00000000-0000-0000-0000-000000000000");
  } catch (err) {
    if (err instanceof AuthenticationError) {
      console.log(`401 — bad credentials. requestId=${err.requestId ?? "<none>"}`);
    } else if (err instanceof NotFoundError) {
      console.log(`404 — agent does not exist in this workspace. message=${err.message}`);
    } else if (err instanceof RateLimitError) {
      const retryAfter = err.response.headers.get("retry-after");
      console.log(`429 — slow down. retry after: ${retryAfter ?? "?"}s`);
    } else if (err instanceof APIStatusError) {
      console.log(`unexpected status ${err.status}: ${err.message}`);
    } else if (err instanceof APIConnectionError) {
      console.log("could not reach the control plane (DNS, TCP, TLS)");
    } else {
      throw err;
    }
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
