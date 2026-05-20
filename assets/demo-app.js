#!/usr/bin/env node
// B12 demo Ink app — Claude Code v2.1.x TUI simulation.
// Banner styled after the real Claude Code launch screen: rounded-rect
// frame with the title on the top edge, the chunky orange robot avatar
// on the left, welcome line + model meta + cwd + MCP status on the right.
// Below the frame: typed input → Memory tool call → live B12 pill → 3-part
// English response → /mcp slash-command → /exit.

import React, { useState, useEffect, createElement as e } from 'react';
import { render, Box, Text, useApp } from 'ink';
import { execSync } from 'child_process';

const ORANGE = '#da7756';   // Claude Code primary accent
const BLUE   = '#5b9bd5';   // tool-call / brand secondary
const spinnerFrames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
const thinkingVerbs = ['Crunching', 'Pondering', 'Thinking', 'Processing'];

function liveCount() {
  try {
    const home = process.env.B12_DEMO_HOME || '/tmp/b12-demo-home';
    const db = process.platform === 'darwin'
      ? `${home}/Library/Application Support/mcp-memory/sqlite_vec.db`
      : `${home}/.local/share/mcp-memory/sqlite_vec.db`;
    const sql = `SELECT COUNT(*) FROM memories WHERE content LIKE '%MCP server%' OR content LIKE '%mcp_server%' OR content LIKE '%spawned%'`;
    const n = parseInt(execSync(`sqlite3 "${db}" "${sql}"`, { encoding: 'utf8', stdio: ['ignore','pipe','ignore'] }).trim(), 10);
    return Number.isNaN(n) ? null : n;
  } catch { return null; }
}

const SCRIPT = {
  question: 'How does B12 work? Where is the MCP server defined?',
  toolName: 'Memory',
  toolArgs: 'memory_search(query="B12 MCP server", mode="hybrid")',
  toolOutput: 'found 2 matches in /tmp/b12-demo-work',
  response: [
    { type: 'pill' },
    { type: 'text', text: 'B12 has three pieces:' },
    { type: 'br' },
    { type: 'numbered', n: 1, label: 'MCP server', body: '— defined in scripts/b12_mcp_server.py. The host application\n(Claude Code, Codex, Cursor) spawns it as a child process over stdio.' },
    { type: 'numbered', n: 2, label: 'Hook scripts', body: '— under ~/.B12/hooks/. Each hook must exit 0; a non-zero\nexit blocks the host tool call.' },
    { type: 'numbered', n: 3, label: 'SQLite + sqlite-vec', body: '— the local persistent store; hybrid FTS5 + vector\nsearch over 1024-dim BAAI/bge-m3 embeddings.' },
  ],
};

// ─── Banner: rounded-rect frame with title on the top edge, avatar
// on the left, meta column on the right.  Uses Ink's flexbox + fixed
// borderStyle so the right edge stays aligned regardless of inner text.
function Banner() {
  return e(Box, { borderStyle: 'round', borderColor: 'gray', paddingX: 1, width: 80 },
    e(Box, { flexDirection: 'column', width: 12, marginRight: 2 },
      e(Text, { color: ORANGE }, ' ▄▀▀▀▀▀▀▄ '),
      e(Text, { color: ORANGE }, ' █▛▀▀▀▀▜█ '),
      e(Text, { color: ORANGE }, ' █ ◠ ◠ █ '),
      e(Text, { color: ORANGE }, ' █▄▄▄▄▄▄█ '),
      e(Text, { color: ORANGE }, ' ▀▀▀▀▀▀▀▀ '),
    ),
    e(Box, { flexDirection: 'column', flexGrow: 1 },
      e(Box, null,
        e(Text, { bold: true, color: 'white' }, 'Claude Code'),
        e(Text, { color: 'gray' }, ' v2.1.145')),
      e(Text, null, ''),
      e(Box, null,
        e(Text, { color: 'white' }, 'Welcome back '),
        e(Text, { bold: true, color: 'white' }, 'Demo User'),
        e(Text, { color: 'white' }, '!')),
      e(Box, null,
        e(Text, { color: 'white' }, 'Opus 4.7 '),
        e(Text, { color: 'gray' }, '·'),
        e(Text, { color: 'white' }, ' 1M context '),
        e(Text, { color: 'gray' }, '·'),
        e(Text, { color: 'white' }, ' API Usage Billing')),
      e(Text, { dimColor: true }, '/tmp/b12-demo-work'),
      e(Box, null,
        e(Text, { color: 'white' }, 'MCP: '),
        e(Text, { color: 'green' }, '●'),
        e(Text, { color: 'white' }, ' B12 '),
        e(Text, { dimColor: true }, 'connected · 5 tools · 5 memories indexed')),
    ),
  );
}

function Spinner({ verb }) {
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setFrame(f => (f + 1) % spinnerFrames.length), 80);
    return () => clearInterval(t);
  }, []);
  return e(Box, null,
    e(Text, { color: ORANGE }, spinnerFrames[frame]),
    e(Text, { color: 'gray' }, ` ${verb}…`),
    e(Text, { dimColor: true }, '  (esc to interrupt)'));
}

function UserPrompt({ value, typing }) {
  return e(Box, null,
    e(Text, { color: 'gray', bold: true }, '> '),
    e(Text, { color: 'white' }, value),
    typing && e(Text, { color: BLUE }, '▋'));
}

function ToolCall({ name, args, output }) {
  return e(Box, { flexDirection: 'column', marginTop: 1 },
    e(Box, null,
      e(Text, { color: BLUE }, '● '),
      e(Text, { color: 'white' }, `${name}(`),
      e(Text, { color: 'gray' }, args),
      e(Text, { color: 'white' }, ')')),
    e(Box, null,
      e(Text, { color: 'gray' }, '  ⎿ '),
      e(Text, { color: 'green' }, output)));
}

function Pill({ count }) {
  const display = count == null ? '?' : String(count);
  return e(Box, { marginTop: 1 },
    e(Text, { dimColor: true }, `( 💊 B12 🧠 : found ${display} memories about MCP server ✅ )`));
}

function ResponseLine({ block, count }) {
  if (block.type === 'pill') return e(Pill, { count });
  if (block.type === 'br') return e(Text, null, '');
  if (block.type === 'text') return e(Text, { color: 'white' }, block.text);
  if (block.type === 'numbered') {
    return e(Box, { flexDirection: 'column' },
      e(Box, null,
        e(Text, { color: ORANGE }, `${block.n}. `),
        e(Text, { bold: true, color: 'white' }, block.label),
        e(Text, { color: 'white' }, ' '),
        e(Text, { color: 'gray' }, block.body.split('\n')[0])),
      ...block.body.split('\n').slice(1).map((l, i) =>
        e(Text, { key: i, color: 'gray' }, `   ${l}`)));
  }
  return e(Text, { color: 'white' }, block.text || '');
}

function McpStatus() {
  return e(Box, { flexDirection: 'column', marginTop: 1 },
    e(Box, null,
      e(Text, { color: 'gray', bold: true }, '> '),
      e(Text, { color: 'white' }, '/mcp')),
    e(Box, { marginTop: 1 },
      e(Text, { bold: true, color: 'white' }, 'Manage MCP servers'),
      e(Text, { dimColor: true }, '   (1 connected)')),
    e(Box, null,
      e(Text, { color: 'gray' }, '  '),
      e(Text, { color: 'green' }, '●'),
      e(Text, { bold: true, color: 'white' }, ' B12'),
      e(Text, { dimColor: true }, '   5 tools: memory_store, memory_search, memory_update, memory_quality, memory_dashboard')));
}

function App() {
  const [step, setStep] = useState(0);
  const [typed, setTyped] = useState('');
  const [charIdx, setCharIdx] = useState(0);
  const [showSpinner, setShowSpinner] = useState(false);
  const [verb, setVerb] = useState('Crunching');
  const [showTool, setShowTool] = useState(false);
  const [shownLines, setShownLines] = useState(0);
  const [showMcp, setShowMcp] = useState(false);
  const [showExit, setShowExit] = useState(false);
  const [count] = useState(() => liveCount());
  const { exit } = useApp();

  useEffect(() => {
    if (step !== 0) return;
    if (charIdx < SCRIPT.question.length) {
      const t = setTimeout(() => {
        setTyped(SCRIPT.question.slice(0, charIdx + 1));
        setCharIdx(c => c + 1);
      }, 35);
      return () => clearTimeout(t);
    }
    const t = setTimeout(() => {
      setVerb(thinkingVerbs[Math.floor(Math.random() * thinkingVerbs.length)]);
      setShowSpinner(true);
      setStep(1);
    }, 500);
    return () => clearTimeout(t);
  }, [step, charIdx]);

  useEffect(() => {
    if (step !== 1) return;
    const timers = [];
    timers.push(setTimeout(() => { setShowSpinner(false); setShowTool(true); }, 1800));
    timers.push(setTimeout(() => setShownLines(1), 2400));
    timers.push(setTimeout(() => setShownLines(2), 3100));
    timers.push(setTimeout(() => setShownLines(3), 3500));
    timers.push(setTimeout(() => setShownLines(4), 3900));
    timers.push(setTimeout(() => setShownLines(5), 4700));
    timers.push(setTimeout(() => setShownLines(6), 5500));
    timers.push(setTimeout(() => setShowMcp(true), 7500));
    timers.push(setTimeout(() => setShowExit(true), 10500));
    timers.push(setTimeout(() => exit(), 12000));
    return () => timers.forEach(clearTimeout);
  }, [step, exit]);

  return e(Box, { flexDirection: 'column', padding: 1 },
    e(Banner),
    e(Box, { marginTop: 1 }, e(UserPrompt, { value: typed, typing: charIdx < SCRIPT.question.length })),
    showSpinner && e(Box, { marginTop: 1 }, e(Spinner, { verb })),
    showTool && e(ToolCall, { name: SCRIPT.toolName, args: SCRIPT.toolArgs, output: SCRIPT.toolOutput }),
    ...SCRIPT.response.slice(0, shownLines).map((b, i) => e(ResponseLine, { key: i, block: b, count })),
    showMcp && e(McpStatus),
    showExit && e(Box, { marginTop: 1 },
      e(Text, { color: 'gray', bold: true }, '> '),
      e(Text, { color: 'white' }, '/exit')),
  );
}

render(e(App));
