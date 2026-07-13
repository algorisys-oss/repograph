#!/usr/bin/env node
/*
 * Dev-only git hook setup, run by the `prepare` lifecycle script.
 *
 * `prepare` also runs when repograph is installed as a git DEPENDENCY (npm clones it
 * into a cache dir and runs prepare to build it). That cache dir is not a git working
 * tree, so the old `git config core.hooksPath .githooks` failed with "fatal: not in a
 * git directory" and broke the CONSUMER's whole `npm install`. This script instead only
 * wires up the hook when we're actually inside repograph's own git checkout with a
 * .githooks dir present, and it NEVER exits non-zero — so consuming installs can't fail.
 */
'use strict';
const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

try {
    const insideWorkTree = (() => {
        try {
            return execSync('git rev-parse --is-inside-work-tree', {
                stdio: ['ignore', 'pipe', 'ignore'],
            }).toString().trim() === 'true';
        } catch {
            return false;
        }
    })();

    if (insideWorkTree && fs.existsSync(path.join(process.cwd(), '.githooks'))) {
        execSync('git config core.hooksPath .githooks', { stdio: 'ignore' });
    }
} catch {
    // Never fail the install (or a consumer's install) over optional dev tooling.
}
