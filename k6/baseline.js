import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Counter, Trend } from 'k6/metrics';

//Baseline load against the Nginx-fronted endpoint under normal conditions.
//Two jobs: establish normal-condition numbers, and measure how traffic actually
//splits between the two nodes so the upstream weights can be set from data
//instead of guessed from clock speeds.
//
//  k6 run -e BASE_URL=http://192.168.71.245 k6/baseline.js

const errorRate   = new Rate('errors');
const nodeHits    = new Counter('node_hits');
const nodeLatency = new Trend('node_latency', true);

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
        nodeHits.add(1, { node });
        nodeLatency.add(res.timings.duration, { node });
      }
    } catch (_) {
      //body was not JSON — already counted as a failure by the check above
    }
  }

  sleep(1);
}

export function handleSummary(data) {
  //k6's default summary reports node_hits as one total. The per-node split is
  //the number we actually came for, so pull it out of the sub-metrics.
  const lines = ['', '=== traffic distribution by node ==='];
  const counts = {};
  let total = 0;

  for (const [name, metric] of Object.entries(data.metrics)) {
    const m = name.match(/^node_hits\{node:(.+)\}$/);
    if (m) {
      const n = metric.values.count;
      counts[m[1]] = n;
      total += n;
    }
  }

  if (total === 0) {
    lines.push('  no /health responses carried a node field');
  } else {
    for (const [node, n] of Object.entries(counts).sort()) {
      const pct = ((n / total) * 100).toFixed(1);
      lines.push(`  ${node.padEnd(8)} ${String(n).padStart(6)} req  ${pct.padStart(5)}%`);
    }
    lines.push('');
    lines.push('  Compare against the weights in deploy/nginx/brp-upstream.conf.');
    lines.push('  If the observed split matches the configured weights but p95');
    lines.push('  differs sharply between nodes, the weights are wrong for the');
    lines.push('  hardware — retune toward equal per-node latency, not equal load.');
  }

  return {
    stdout: '\n' + lines.join('\n') + '\n',
    'evidence/baseline-summary.json': JSON.stringify(data, null, 2),
  };
}
