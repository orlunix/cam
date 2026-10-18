import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const source = fileURLToPath(new URL('../../../web/js/mobile/terminal-key-composer.js', import.meta.url));
const temp = path.join(os.tmpdir(), `terminal-key-composer-${process.pid}.mjs`);
fs.copyFileSync(source, temp);
const { createTerminalKeyComposer, encodeTerminalKey } = await import(`file://${temp}`);

assert.equal(encodeTerminalKey('/', {}).bytes, '/');
assert.equal(encodeTerminalKey('Tab', {}, 'key').bytes, '\t');
assert.equal(encodeTerminalKey('a', { ctrl: true }).bytes, '\x01');
assert.equal(encodeTerminalKey('/', { alt: true }).bytes, '\x1b/');
assert.equal(encodeTerminalKey('/', { shift: true }).bytes, '?');
assert.equal(encodeTerminalKey('Tab', { shift: true }, 'key').bytes, '\x1b[Z');
assert.equal(encodeTerminalKey('Up', { ctrl: true, alt: true, shift: true }, 'key').bytes, '\x1b[1;8A');
assert.equal(encodeTerminalKey('PPage', { ctrl: true }, 'key').bytes, '\x1b[5;5~');

const composer = createTerminalKeyComposer();
assert.equal(composer.hasModifiers(), false);
assert.equal(composer.toggleModifier('ctrl').ctrl, true);
assert.equal(composer.hasModifiers(), true);
assert.equal(composer.toggleModifier('ctrl').ctrl, false);
assert.equal(composer.hasModifiers(), false);

composer.toggleModifier('ctrl');
composer.toggleModifier('shift');
assert.equal(composer.queueKey('Tab', 'key').preview, 'Ctrl+Shift+Tab');
assert.equal(composer.queueKey('a').bytes, '\x01');
assert.equal(composer.preview(), 'Ctrl+Shift+Tab Ctrl+Shift+a');
assert.equal(composer.draft('echo ', true), 'echo \x1b[1;6I\x01\r');
assert.equal(composer.hasPending(), true);
assert.equal(composer.hasModifiers(), true);
assert.equal(composer.flush('echo ', false), 'echo \x1b[1;6I\x01');
assert.equal(composer.hasModifiers(), false);
assert.equal(composer.hasPending(), false);
assert.equal(composer.preview(), '');

composer.toggleModifier('alt');
composer.queueKey('/');
assert.equal(composer.flush('', true), '\x1b/\r');
assert.equal(composer.hasModifiers(), false);

composer.toggleModifier('ctrl');
composer.queueKey('a');
assert.equal(composer.draft('x'), 'x\x01');
assert.equal(composer.hasPending(), true);
composer.clear();
assert.equal(composer.hasModifiers(), false);
assert.equal(composer.hasPending(), false);

fs.unlinkSync(temp);
console.log('terminal-key-composer: all tests passed');
