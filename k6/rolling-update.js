import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

//Runs for the full duration of a rolling update. This is the test that either
//substantiates or kills the zero-downtime claim, so its thresholds are
//absolute: a single failed request fails the run.
//
//  k6 run -e BASE_URL=http://192.168.71.250 -e DURATION=10m k6/rolling-update.js
//
//Start this BEFORE step 1 of RUNBOOK.md and let it run past the final step.

const nodeHits = new Counter('node_hits');
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
      maxVUs: 200,
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
    if (node) nodeHits.add(1, { node });
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
  for (const [name, metric] of Object.entries(data.metrics)) {
    const m = name.match(/^node_hits\{node:(.+)\}$/);
    if (m) lines.push(`    ${m[1].padEnd(8)} ${metric.values.count}`);
  }
  lines.push('');
  lines.push('  Both nodes should appear, and each should show a stretch of zero');
  lines.push('  traffic in the Nginx access log while it was out of the pool.');

  return {
    stdout: '\n' + lines.join('\n') + '\n',
    'evidence/rolling-update-summary.json': JSON.stringify(data, null, 2),
  };
}
