import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

//Runs for the full duration of a rolling update. This is the test that either
//substantiates or kills the zero-downtime claim, so its thresholds are
//absolute: a single failed request fails the run.
//
//  k6 run -e BASE_URL=http://192.168.71.245 -e DURATION=10m k6/rolling-update.js
//
//Start this BEFORE step 1 of RUNBOOK.md and let it run past the final step.

//One counter per node, not one counter tagged by node. A tagged Counter records
//the tag on every sample, but the tag-split sub-metrics only reach
//handleSummary if k6 materialises them, and the key they arrive under
//(`node_hits{node:node1}`) is an output detail rather than an API. Scraping
//that key printed an empty `served by:` section under k6 v2 while the request
//count was correct — the run looked fully verified with half its evidence
//silently missing, which is the failure mode this whole repo is about.
const nodeHits = {
  node1: new Counter('node_hits_node1'),
  node2: new Counter('node_hits_node2'),
};
//A `node` value that is neither gets its own counter instead of being dropped:
//it means NODE_NAME is wrong somewhere, which misattributes every per-node
//number in the run and would otherwise look identical to a node serving nothing.
const unknownNode = new Counter('node_hits_unknown');
const failures = new Counter('failed_requests');

const BASE_URL = __ENV.BASE_URL || 'http://localhost';

export const options = {
  scenarios: {
    steady: {
      //constant-arrival-rate, NOT a fixed VU count. With VUs (a closed model)
      //requests queue behind slow responses, offered load silently drops when a
      //node goes away, and the test quietly stops exercising the exact window
      //it was written to measure. An open model keeps sending 20 req/s whether
      //or not the system is keeping up, which is what a real client does.
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.RATE || 20),
      timeUnit: '1s',
      duration: __ENV.DURATION || '10m',
      preAllocatedVUs: 20,
      //Capped, because k6 runs on node2 — which is also one of the two backends
      //under test. An open model holds the request rate steady by allocating
      //more VUs when responses slow down, so an unbounded ceiling turns a
      //latency blip into a load spike on a box that is already serving half the
      //fleet's traffic, and the test then reports that spike as the platform's
      //fault. See RUNBOOK.md §2 step 0 for why there is nowhere better to run it.
      maxVUs: Number(__ENV.MAX_VUS || 200),
    },
  },
  thresholds: {
    //Zero tolerance. Not "under 1%" — the claim is zero dropped requests, so
    //the test asserts exactly that and nothing softer.
    http_req_failed: ['rate==0'],
    checks:          ['rate==1.00'],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/health`);

  const ok = check(res, {
    'status 200': (r) => r.status === 200,
  });

  if (!ok) {
    failures.add(1);
    //Printed inline so the moment of failure is visible in the console next to
    //the runbook step being performed, rather than only in the end summary.
    console.error(
      `FAILED ${new Date().toISOString()} status=${res.status} ` +
      `error=${res.error || 'none'} duration=${res.timings.duration}ms`
    );
    return;
  }

  try {
    const node = res.json('node');
    if (node) (nodeHits[node] || unknownNode).add(1);
  } catch (_) {
    //non-JSON 200 — unexpected, but the check above already passed it
  }
}

export function handleSummary(data) {
  const lines = ['', '=== rolling update result ==='];

  const failed = data.metrics.http_req_failed;
  const total  = data.metrics.http_reqs ? data.metrics.http_reqs.values.count : 0;
  const failRate = failed ? failed.values.rate : 0;
  const failCount = Math.round(failRate * total);

  lines.push(`  total requests : ${total}`);
  lines.push(`  failed         : ${failCount}`);
  lines.push('');
  lines.push(
    failCount === 0
      ? '  ZERO DROPPED REQUESTS across the rolling update.'
      : `  ${failCount} request(s) dropped — the zero-downtime claim does NOT hold.`
  );

  lines.push('');
  lines.push('  served by:');

  const count = (name) => {
    const m = data.metrics[name];
    return m ? m.values.count : 0;
  };
  const attributed = count('node_hits_node1') + count('node_hits_node2');

  for (const node of ['node1', 'node2']) {
    lines.push(`    ${node.padEnd(8)} ${count(`node_hits_${node}`)}`);
  }

  const unknown = count('node_hits_unknown');
  if (unknown > 0) {
    lines.push(`    UNKNOWN  ${unknown}`);
    lines.push('    ^ /health reported a node name that is neither node1 nor node2.');
    lines.push('      NODE_NAME is wrong on some node; per-node numbers are unsafe.');
  }

  //These totals CANNOT tell you whether a rollout happened, and a previous
  //version of this file claimed they could — it warned when the two nodes came
  //within 10% of each other, on the reasoning that draining a node starves it
  //of ~1200 requests per minute. True for one node. But §2 drains BOTH, for
  //roughly equal periods, so the two shortfalls cancel and a correct run lands
  //near-even too: a no-drain run measured 9010/8990, and a correct one 11950/
  //12050. No threshold separates those. The check fired on correct runs and
  //stayed silent on the failure it was written for.
  //
  //Whether traffic actually left a node is a question about time, and this
  //summary only has totals. The access log has both — see below.
  if (attributed === 0 && total > 0) {
    //Explicit, because a silently empty section is what this replaced: it read
    //as "nothing to report" when it meant "the reporting broke".
    lines.push('');
    lines.push('  NO PER-NODE ATTRIBUTION — requests succeeded but none carried a');
    lines.push('  usable node field. The zero-dropped result above still stands;');
    lines.push('  the by-node half of RUNBOOK.md §2 step 8 does not.');
  }

  lines.push('');
  lines.push('  Both nodes must appear above — but these totals do NOT prove the');
  lines.push('  rollout happened. A run where nothing was drained looks the same.');
  lines.push('  Confirm each node has a stretch of zero traffic, on node1:');
  lines.push('');
  lines.push("    sudo awk -F'[][]' '/upstream=192.168.71.252/ {print substr($2,1,16)}' \\");
  lines.push('      /var/log/nginx/brp_access.log | uniq -c | tail -30');
  lines.push('');
  lines.push('  Per-minute counts of node2-served requests. The drain window is the');
  lines.push('  gap. Swap the address for node1. That gap is the evidence; this');
  lines.push('  summary only proves nothing failed while it was happening.');

  return {
    stdout: '\n' + lines.join('\n') + '\n',
    'evidence/rolling-update-summary.json': JSON.stringify(data, null, 2),
  };
}
