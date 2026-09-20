// One source-order scanner, shared by the static guard tests.
//
// NOT a `.test.js` — the runner's glob is `tests/**/*.test.js`, so this is a
// helper and never runs as a suite of its own.
//
// WHY THIS EXISTS
// ---------------
// callable.test.js and i18n-keys.test.js both need "the code, with comments
// and string literals removed". Both did it with a sequence of regex passes,
// and a sequence of regexes cannot parse a language whose lexical states nest.
// Three holes shipped, each found the hard way:
//
//   1. A nested template literal. clients.js builds markup with
//      `... ${cond ? `${x} check(s) need attention` : '...'}`; a
//      /`(?:\\.|[^`\\])*`/ pattern pairs the OUTER opening backtick with the
//      INNER one, leaving the text between the inner pair exposed — so the
//      words "check(s) need attention" were reported as a call to a missing
//      check().
//
//   2. Block comments stripped BEFORE strings. `accept: 'image/*'` in main.js
//      contains `/*`, which opens a pseudo-comment that blanks everything
//      until the next real `*/` — 23+ consecutive lines of live code in one
//      run, taking real call sites and t() keys out of both tests' view. The
//      guards were not reporting those files as clean; they were not reading
//      them.
//
//   3. Order dependence in general: `const q = /"/g;` and `const sep = '//';`
//      each put the next pass into the wrong state.
//
// The fix is to stop ordering passes and walk the source once, tracking which
// lexical state each character is actually in. Newlines are preserved so line
// numbers survive, which both callers rely on for their messages.

/** Is a `/` at `i` the start of a regex literal rather than a division?
 *
 *  The standard heuristic: look back past whitespace at the last significant
 *  character. After an identifier, a number, `)`, `]` or `}` a slash is
 *  division; after anything else (an operator, a comma, `(`, `return`, the
 *  start of the file) it opens a regex. `}` is genuinely ambiguous in JS —
 *  block-vs-object-literal — and is treated as division, which is the safe
 *  direction here: mistaking a regex for division leaves its contents visible
 *  (a possible false POSITIVE, which a human then reads), while the reverse
 *  blanks live code (a false negative, which is how holes 1-3 above hid).
 */
function regexCanStart(src, i) {
  let k = i - 1;
  while (k >= 0 && /\s/.test(src[k])) k -= 1;
  if (k < 0) return true;
  const c = src[k];
  if (/[\w$)\]}]/.test(c)) {
    // `return /re/`, `typeof /re/`, `case /re/` — a keyword, not a value.
    const word = /[\w$]+$/.exec(src.slice(Math.max(0, k - 12), k + 1));
    if (word && ['return', 'typeof', 'case', 'in', 'of', 'delete', 'void',
                 'instanceof', 'do', 'else', 'yield', 'await'].includes(word[0])) {
      return true;
    }
    return false;
  }
  return true;
}

/**
 * Blank every comment and string literal in `src`, preserving length and
 * newlines so offsets and line numbers are unchanged.
 *
 * @param {string} src
 * @param {{keepStrings?: boolean}} [opts] - keepStrings leaves quoted string
 *   CONTENT in place (i18n-keys needs to read `t('some.key')`) while still
 *   removing comments, templates and regexes.
 */
export function blankNonCode(src, opts = {}) {
  const keepStrings = !!opts.keepStrings;
  const out = src.split('');
  const blank = (from, to) => {
    for (let k = from; k < to && k < out.length; k++) {
      if (out[k] !== '\n') out[k] = ' ';
    }
  };

  let i = 0;
  while (i < src.length) {
    const c = src[i];

    // --- line comment ---
    if (c === '/' && src[i + 1] === '/') {
      const end = src.indexOf('\n', i);
      const stop = end === -1 ? src.length : end;
      blank(i, stop);
      i = stop;
      continue;
    }

    // --- block comment ---
    if (c === '/' && src[i + 1] === '*') {
      const end = src.indexOf('*/', i + 2);
      const stop = end === -1 ? src.length : end + 2;
      blank(i, stop);
      i = stop;
      continue;
    }

    // --- quoted string ---
    if (c === '"' || c === "'") {
      const start = i;
      i += 1;
      while (i < src.length) {
        if (src[i] === '\\') { i += 2; continue; }
        if (src[i] === c || src[i] === '\n') { i += 1; break; }
        i += 1;
      }
      // Keep the quotes either way so a caller reading `t('key')` still sees
      // the call shape; only the CONTENT is optionally preserved.
      if (!keepStrings) blank(start + 1, i - 1);
      continue;
    }

    // --- template literal, including nested ${ … } and inner templates ---
    if (c === '`') {
      const start = i;
      i += 1;
      const depths = [0];              // substitution depth of the current template
      while (i < src.length) {
        const d = src[i];
        if (d === '\\') { i += 2; continue; }
        const depth = depths[depths.length - 1];
        if (depth === 0 && d === '`') {
          i += 1;
          depths.pop();
          if (!depths.length) break;   // closed the outermost template
          continue;
        }
        if (d === '$' && src[i + 1] === '{') { depths[depths.length - 1] += 1; i += 2; continue; }
        if (depth > 0 && d === '}') { depths[depths.length - 1] -= 1; i += 1; continue; }
        if (depth > 0 && d === '`') { depths.push(0); i += 1; continue; }
        i += 1;
      }
      blank(start, i);
      continue;
    }

    // --- regex literal ---
    if (c === '/' && regexCanStart(src, i)) {
      const start = i;
      let j = i + 1;
      let inClass = false;
      let closed = false;
      while (j < src.length) {
        const d = src[j];
        if (d === '\\') { j += 2; continue; }
        if (d === '\n') break;                 // unterminated: not a regex
        if (d === '[') { inClass = true; j += 1; continue; }
        if (d === ']') { inClass = false; j += 1; continue; }
        if (d === '/' && !inClass) { j += 1; closed = true; break; }
        j += 1;
      }
      if (closed) {
        while (j < src.length && /[gimsuyvd]/.test(src[j])) j += 1;
        blank(start, j);
        i = j;
        continue;
      }
      // Not a regex after all — fall through and treat it as one character.
    }

    i += 1;
  }
  return out.join('');
}
