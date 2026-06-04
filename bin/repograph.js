#!/usr/bin/env node
// Thin launcher so repograph (a single-file Python tool) installs as an npm
// CLI. npm links this onto PATH as `repograph`; it forwards all args to the
// bundled repograph.py via the system Python. Override the interpreter with
// the PYTHON env var (e.g. PYTHON=python). Exit code is propagated.
'use strict';

const { spawnSync } = require('child_process');
const path = require('path');

const script = path.join(__dirname, '..', 'repograph.py');
const python = process.env.PYTHON || 'python3';

const res = spawnSync(python, [script, ...process.argv.slice(2)], { stdio: 'inherit' });

if (res.error) {
  if (res.error.code === 'ENOENT') {
    console.error(`repograph: '${python}' not found on PATH. Install Python 3.8+ or set PYTHON=...`);
    process.exit(127);
  }
  console.error(`repograph: failed to launch: ${res.error.message}`);
  process.exit(1);
}

process.exit(res.status === null ? 1 : res.status);
