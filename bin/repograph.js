#!/usr/bin/env node
// Thin launcher so repograph (a single-file Python tool) installs as an npm
// CLI. npm links this onto PATH as `repograph`; it forwards all args to the
// bundled repograph.py via the system Python. Override the interpreter with
// the PYTHON env var (e.g. PYTHON=python). Exit code is propagated.
'use strict';

const { spawnSync } = require('child_process');
const path = require('path');

const script = path.join(__dirname, '..', 'repograph.py');
// PYTHON env wins; otherwise try python3 then python (stock Windows ships the
// latter, not the former).
const candidates = process.env.PYTHON ? [process.env.PYTHON] : ['python3', 'python'];

let res;
for (const python of candidates) {
  res = spawnSync(python, [script, ...process.argv.slice(2)], { stdio: 'inherit' });
  if (!(res.error && res.error.code === 'ENOENT')) break;  // launched (or non-ENOENT failure)
}

if (res.error) {
  if (res.error.code === 'ENOENT') {
    console.error(`repograph: no Python found (tried: ${candidates.join(', ')}). Install Python 3.8+ or set PYTHON=...`);
    process.exit(127);
  }
  console.error(`repograph: failed to launch: ${res.error.message}`);
  process.exit(1);
}

process.exit(res.status === null ? 1 : res.status);
