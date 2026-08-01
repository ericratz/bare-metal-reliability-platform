import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Counter, Trend } from 'k6/metrics';

//Baseline load against the Nginx-fronted endpoint under normal conditions.
//Two jobs: establish normal-condition numbers, and measure how traffic actually
//splits between the two nodes so the upstream weights can be set from data
//instead of guessed from clock speeds.
//
//  k6 run -e BASE_URL=http://192.168.71.245 k6/baseline.js

const errorRate = new Rate('errors');

//One metric per node rather than one metric tagged by node — see the comment in
//rolling-update.js. Tag-split sub-metrics do not reliably reach handleSummary,
//and the failure here was worse than an empty section: this script would have
//reported "no /health responses carried a node field", which is a diagnosis of
//the wrong component. The responses were fine; the metric keys were not where
//the summary looked. The per-node split is the only thing this script exists to
//produce, so it must not be able to fail quietly.
const nodeHits = {
  node1: new Counter('node_hits_node1'),
  node2: new Counter('node_hits_node2'),
};
const nodeLatency = {
  node1: new Trend('node_latency_node1', true),
  node2: new Trend('node_latency_node2', true),
};
const unknownNode = new Counter('node_hits_unknown');

const BASE_URL = __ENV.BASE_URL || 'http://localhost';

export const options = {
  vus: 10,
  duration: __ENV.DURATION || '30s',
  thresholds: {
    http_req_failed:   ['rate<0.05'],   //fail if error rate >= 5%
    http_req_duration: ['p(95)<500'],   //fail if p95 latency >= 500ms
    errors:            ['rate<0.05'],
  },
};

export default function () {
  //Weighted toward /health because it is the only endpoint that names the node
  //that served it — that attribution is what the weight tuning depends on.
  //Hitting it through the LB (not per-node) is the point: we want to observe
  //the balancer's actual distribution, not confirm each node is up.
  const roll = Math.random();
  const path = roll < 0.6 ? '/health' : roll < 0.8 ? '/' : '/slo';

  const res = http.get(`${BASE_URL}${path}`, { tags: { endpoint: path } });

  const ok = check(res, {
    'status 2xx': (r) => r.status >= 200 && r.status < 300,
  });
  errorRate.add(!ok);

  if (path === '/health' && res.status === 200) {
    try {
      const node = res.json('node');
      if (node) {
        if (nodeHits[node]) {
          nodeHits[node].add(1);
          nodeLatency[node].add(res.timings.duration);
        } else {
          unknownNode.add(1);
        }
      }
    } catch (_) {
      //body was not JSON — already counted as a failure by the check above
    }
  }

  sleep(1);
}

export function handleSummary(data) {
  //The per-node split is the number we actually came for; k6's own summary only
  //reports totals.
  const lines = ['', '=== traffic distribution by node ==='];
  const nodes = ['node1', 'node2'];
  const counts = {};
  let total = 0;

  for (const node of nodes) {
    const metric = data.metrics[`node_hits_${node}`];
    const n = metric ? metric.values.count : 0;
    counts[node] = n;
    total += n;
  }

  const unknown = data.metrics.node_hits_unknown;
  const unknownCount = unknown ? unknown.values.count : 0;

  if (total === 0) {
    lines.push('  no /health response was attributed to a known node');
    lines.push('  (either none carried a node field, or every value was unrecognised)');
  } else {
    for (const node of nodes) {
      const n = counts[node];
      const pct = ((n / total) * 100).toFixed(1);
      const latency = data.metrics[`node_latency_${node}`];
      const p95Value = latency ? latency.values['p(95)'] : undefined;
      const p95 = typeof p95Value === 'number' ? `  p95 ${p95Value.toFixed(1)}ms` : '';
      lines.push(`  ${node.padEnd(8)} ${String(n).padStart(6)} req  ${pct.padStart(5)}%${p95}`);
    }
    lines.push('');
    lines.push('  Compare against the weights in deploy/nginx/brp-upstream.conf.');
    lines.push('  If the observed split matches the configured weights but p95');
    lines.push('  differs sharply between nodes, the weights are wrong for the');
    lines.push('  hardware — retune toward equal per-node latency, not equal load.');
  }

  if (unknownCount > 0) {
    lines.push('');
    lines.push(`  UNKNOWN  ${unknownCount} req served by a node naming itself neither`);
    lines.push('  node1 nor node2. NODE_NAME is wrong somewhere, and every split');
    lines.push('  above is measured against an incomplete denominator.');
  }

  return {
    stdout: '\n' + lines.join('\n') + '\n',
    'evidence/baseline-summary.json': JSON.stringify(data, null, 2),
  };
}
