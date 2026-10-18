// Cross-platform equivalent of `CI_REQUIRE_DOM=1 node --test ...`.
//
// `VAR=1 cmd` is POSIX shell syntax; cmd.exe and PowerShell reject it, so the
// `test:ci` script could not run on a Windows checkout. Setting the variable
// from node and spawning node's own test runner works the same everywhere
// without adding a dependency (cross-env) to a project whose only dev
// dependency is jsdom.
import { spawnSync } from 'node:child_process';

const env = { ...process.env, CI_REQUIRE_DOM: '1' };
// Quoted-glob semantics: node expands these itself (node >= 21), no shell involved.
const args = ['--test', ...process.argv.slice(2),
  ...(process.argv.length > 2 ? [] : ['tests/**/*.test.js', '../apps/*/tests/*.test.js'])];
const r = spawnSync(process.execPath, args, { stdio: 'inherit', env });
process.exit(r.status ?? 1);
