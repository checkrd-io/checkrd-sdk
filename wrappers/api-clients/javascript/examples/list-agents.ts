/**
 * List every agent in the caller's workspace, paginating transparently.
 *
 * Run with: npx tsx examples/list-agents.ts
 */
import Checkrd from "@checkrd/api";

async function main() {
  const client = new Checkrd({ apiKey: process.env.CHECKRD_API_KEY });

  let count = 0;
  for await (const agent of client.agents.list()) {
    const status = agent.kill_switch_active ? "killed" : "live";
    console.log(`${agent.id}\t${agent.name}\t${status}`);
    count++;
  }

  console.log(`\n${count} agents in workspace.`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
