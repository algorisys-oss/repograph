#!/usr/bin/env node
// Thin launcher for the repograph MCP server (a single-file, stdlib-only Python
// program). npm links this onto PATH as `repograph-mcp`; it forwards stdio to
// the bundled repograph_mcp.py via the system Python so MCP clients (e.g. Claude
// Code) can spawn it portably from any repo that depends on repograph. Override
// the interpreter with the PYTHON env var (e.g. PYTHON=python). Exit code is
// propagated.
'use strict';

const { spawnSync } = require('child_process');
const path = require('path');

const script = path.join(__dirname, '..', 'repograph_mcp.py');
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
    console.error(`repograph-mcp: no Python found (tried: ${candidates.join(', ')}). Install Python 3.8+ or set PYTHON=...`);
    process.exit(127);
  }
  console.error(`repograph-mcp: failed to launch: ${res.error.message}`);
  process.exit(1);
}

process.exit(res.status === null ? 1 : res.status);
