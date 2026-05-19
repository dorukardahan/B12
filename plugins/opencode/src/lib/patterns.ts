export const DECISION_RE =
  /(?i)(?:(?:decided|chose|going with|selected|opted for|switched to|went with)\s+.{5,}|(?:will use|using|let.?s use|we.?ll use)\s+\S+\s+(?:instead of|rather than|for|because)\s+|(?:the (?:approach|solution|decision|plan) is to)\s+|(?:switching from|replacing|migrating from)\s+\S+\s+(?:to|with)\s+|(?:karar verdik?|seçtik?|tercih ettik?|bununla gid|bunu kullan)\s*.{5,}|(?:yerine|değil de|bunun yerine)\s+\S+\s+.{3,}|(?:planımız|yaklaşımımız|çözüm(?:ümüz)?)\s+.{5,})/;

export const ERROR_RE =
  /(?i)(?:(?:fixed|resolved|solved|workaround for)\s+.{5,}|(?:the fix|the solution|root cause)\s*(?:is|was|:)\s+|(?:error|bug|issue)\s+.{0,40}(?:was caused by|because|due to|fixed by)|(?:had to|needed to)\s+.{3,40}(?:because|due to|since)\s+.{3,}(?:error|bug|fail|broke|crash)|(?:düzelttik?|çözdük?|giderdik?|fix.?ledik?)\s+.{5,}|(?:hata|bug|sorun)\s+.{0,40}(?:sebebi|nedeni|çözümü|düzeltmesi)|(?:sorun şuydu|hata şuydu|sebebi şuydu)\s*(?::)?\s+)/;

export const LEARNING_RE =
  /(?i)(?:(?:turns out|TIL|important to note|gotcha|pitfall|caveat|note:)\s*(?::|that|,)?\s+|(?:learned|discovered|realized|found out)\s+that\s+|(?:the (?:trick|key|insight|important thing) (?:is|was))\s+|(?:remember|important):\s+|(?:pro.?tip|heads.?up|watch out|be careful|don.?t forget)\s*(?::|,)\s+|(?:meğer|meğerse|anlaşılan)\s+.{5,}|(?:öğrendik?|fark ettik?|keşfettik?)\s+.{3,}|(?:dikkat|önemli|unutma)(?::|\s).{5,})/;

export const PREFERENCE_RE =
  /(?i)(?:(?:user\s+(?:prefers?|wants?|asked for|(?:does ?\x27?n.?t|never)\s+(?:want|like|use)))|(?:always use|never use|convention is|style preference|workflow:)|\[user\]\s+|(?:kullanıcı\s+(?:tercih|istiyor|istemiyor|istemez))|(?:her zaman|hiçbir zaman|asla|daima)\s+(?:kullan|yap|kullanma|yapma))/;

export const TOOL_PREF_RE =
  /(?i)(?:(?:always use|prefer\s+\S+\s+over)\s+.{5,}|(?:works?\s+better\s+than)\s+.{5,}|(?:switched to|switching to)\s+\S+\s+(?:for|because)\s+.{5,}|(?:don.?t use|avoid using|stop using)\s+\S+\s+(?:because|for|since)\s+.{3,}|(?:prefer\s+\S+\s+for)\s+.{5,}|(?:hep\s+\S+\s+kullan)\s*.{5,}|(?:\S+.?[ıi]\s+tercih\s+et)\s*.{5,}|(?:daha\s+iyi\s+çalışıyor)\s*.{3,}|(?:\S+\s+kullanma\s+çünkü)\s+.{5,})/;

export const ARCH_RE =
  /(?i)(?:(?:the\s+architecture\s+is)\s+.{5,}|(?:we\s+structured\s+it\s+as)\s+.{5,}|(?:the\s+pattern\s+we\s+use\s+is)\s+.{5,}|(?:built\s+on\s+top\s+of)\s+.{5,}|(?:the\s+design\s+is)\s+.{5,}|(?:using\s+\S+\s+(?:pattern|approach|architecture))\s+.{3,}|(?:the\s+(?:system|service|module|component)\s+(?:is structured|is designed|follows))\s+.{5,}|(?:mimari(?:si|miz)?\s+(?:şöyle|böyle|olarak))\s*.{5,}|(?:yapı(?:sı|mız)?\s+(?:şöyle|böyle|olarak))\s*.{5,}|(?:tasarım(?:ı|ımız)?\s+(?:şöyle|böyle|olarak))\s*.{5,}|(?:bunun\s+üzerine\s+kurduk)\s*.{5,}|(?:yaklaşım\s+olarak)\s+.{5,})/;

export const WORKFLOW_RE =
  /(?i)(?:(?:the\s+workflow\s+is)\s*(?::)?\s+.{5,}|(?:the\s+process\s+is)\s*(?::)?\s+.{5,}|(?:first\s+\S+\s+then)\s+.{5,}|(?:deploy\s+with)\s+.{5,}|(?:run\s+\S+\s+before)\s+.{5,}|(?:the\s+pipeline\s+is)\s*(?::)?\s+.{5,}|(?:the\s+(?:build|release|test|ci)\s+(?:process|pipeline|flow)\s+(?:is|goes|works))\s+.{5,}|(?:step\s+\d+\s*(?::|is|,))\s+.{5,}|(?:iş\s*akışı(?:mız)?\s*(?::|şöyle|böyle))\s*.{5,}|(?:süreç\s*(?::|şöyle|böyle))\s*.{5,}|(?:önce\s+\S+\s+sonra)\s+.{5,}|(?:deploy\s+için)\s+.{5,}|(?:sırasıyla)\s+.{5,})/;

export const FILE_CONV_RE =
  /(?i)(?:(?:files?\s+go\s+in)\s+.{5,}|(?:naming\s+convention\s+(?:is|for))\s+.{5,}|(?:put\s+\S+\s+in\s+(?:the\s+)?\S+\s+directory)\s*.{3,}|(?:file\s+structure\s+(?:is|looks))\s+.{5,}|(?:organized\s+as)\s+.{5,}|(?:(?:directory|folder)\s+(?:structure|layout|convention)\s+(?:is|for))\s+.{5,}|(?:dosyalar\s+\S+.?[ea]\s+konur)\s*.{3,}|(?:isimlendirme\s+kuralı)\s*.{5,}|(?:dosya\s+yapısı)\s*.{5,}|(?:düzen\s+olarak)\s+.{5,})/;

export const CORRECTION_RE =
  /(?i)(?:(?:not\s+.{3,30}(?:,\s*|\s+but\s+)(?:it.?s|actually)\s+.{3,30})|(?:(?:wrong|incorrect)\s+.{0,20}(?:should be|is actually)\s+.{3,30})|(?:changed?\s+(?:from|my)\s+.{3,30}\s+to\s+.{3,30})|(?:(?:yanlış|hatalı)\s+.{3,30}(?:aslında|artık|olarak)\s+.{3,30})|(?:(?:değil)\s+.{3,30}(?:artık|şimdi)\s+.{3,30}))/;

export const INFRA_RE =
  /(?i)(?:(?:(?:server|host|ip|vps|ssh)\s+.{0,30}(?:\d{1,3}\.){3}\d{1,3})|(?:ssh\s+[-\w]+@[\w.-]+)|(?:(?:version|sürüm)\s+.{0,10}v?\d+\.\d+)|(?:port\s+\d{2,5}))/;

export const CONTENT_RE =
  /(?i)(?:(?:(?:blog|article)\s+.{0,30}(?:published|approved|rejected|hazır|onaylandı))|(?:(?:editorial|content)\s+decision\s*:\s+.{5,})|(?:(?:do not|never|asla)\s+(?:write|post|publish|mention)\s+.{5,})|(?:(?:review|feedback)\s*:\s+.{5,}))/;

export const IMPLICIT_DECISION_RE =
  /(?i)(?:(?:let.?s\s+(?:go\s+with|use|try|pick|choose|stick with)\s+.{3,80})|(?:going\s+to\s+use\s+.{3,80})|(?:plan\s+is\s+to\s+.{3,80})|(?:(?:I|we).?(?:ll|will)\s+(?:go with|use|try|pick)\s+.{3,80})|(?:(?:better|best)\s+to\s+(?:use|go with|try)\s+.{3,80})|(?:(?:yapacağız|kullanalım|geçelim|deneyelim|seçelim)\s+.{3,80})|(?:(?:bununla|bunu|şunu)\s+(?:gidelim|deneyelim|kullanalım)\s*.{0,80})|(?:(?:en iyisi|daha iyi)\s+.{3,80}))/;

export const REASON_RE =
  /(?i)(?:(?:because\s+.{10,200})|(?:since\s+.{10,200})|(?:the\s+reason\s+(?:is|was|being)\s+.{10,200})|(?:due\s+to\s+.{10,200})|(?:this\s+is\s+(?:because|since|due to)\s+.{10,200})|(?:çünkü\s+.{10,200})|(?:(?:nedeni|sebebi|sebebiyle)\s+.{10,200})|(?:(?:bunun\s+nedeni|bunun\s+sebebi)\s+.{10,200}))/;

export const BLOCKER_RE =
  /(?i)(?:(?:blocked\s+by\s+.{5,150})|(?:waiting\s+for\s+.{5,150})|(?:can.?t\s+proceed\s+.{5,150})|(?:stuck\s+on\s+.{5,150})|(?:(?:depends|dependent)\s+on\s+.{5,150})|(?:need\s+to\s+(?:wait|resolve|fix)\s+.{5,150})|(?:(?:bekliyor|takıldık|tıkandık)\s+.{5,150})|(?:(?:buna\s+bağlı|bundan\s+önce)\s+.{5,150})|(?:(?:çözmemiz|düzeltmemiz)\s+(?:lazım|gerek)\s*.{0,150}))/;

const SUMMARY_MARKERS: readonly string[] = [
  '# Session Summary',
  '## Decisions Made',
  '## Errors & Fixes',
  '## Key Learnings',
  '## User Preferences',
  '## What Was Done',
  '## Sprint Handoff',
  '## User Requests',
  '## Files Modified',
];

export function summaryFilter(text: string): boolean {
  if (!text) return false;
  let count = 0;
  for (const marker of SUMMARY_MARKERS) {
    if (text.includes(marker)) {
      count++;
      if (count >= 2) return true;
    }
  }
  return false;
}

const PREFIX_RE = /^\[([^\]]{2,30})\]/;

const PREFIX_MAP: Record<string, string> = {
  decision: 'decision',
  'error fix': 'error_fix',
  error: 'error_fix',
  gotcha: 'learning',
  learning: 'learning',
  preference: 'preference',
  progress: 'observation',
  observation: 'observation',
  architecture: 'knowledge',
  pattern: 'knowledge',
  reference: 'knowledge',
  review: 'knowledge',
  note: 'knowledge',
  handoff: 'session_summary',
  audit: 'knowledge',
  test: 'knowledge',
};

export function classifyByPrefix(
  content: string
): { type: string; confidence: number } | null {
  if (!content) return null;
  const m = content.trim().match(PREFIX_RE);
  if (!m) return null;
  const tag = m[1].trim().toLowerCase();
  for (const [key, typ] of Object.entries(PREFIX_MAP)) {
    if (tag.includes(key)) return { type: typ, confidence: 1.0 };
  }
  return null;
}

export function scoreExtraction(text: string, category: string): number {
  let score = 0;
  const tl = text.toLowerCase();

  const hasAny = (words: string[]): boolean => words.some((w) => tl.includes(w));

  switch (category) {
    case 'decision':
      if (hasAny(['instead of', 'over', 'rather than', 'because', 'tradeoff', 'yerine', 'çünkü', 'sebebiyle', 'nedeniyle'])) score += 2;
      if (hasAny(['chose', 'decided', 'selected', 'opted', 'karar', 'seçtik', 'tercih', 'gidelim'])) score += 1;
      break;

    case 'error': {
      const hasProblem = hasAny(['error', 'bug', 'crash', 'fail', 'broke', 'hata', 'sorun', 'çöktü', 'bozuldu']);
      const hasResolution = hasAny(['fixed', 'resolved', 'solved', 'workaround', 'caused by', 'root cause', 'düzelttik', 'çözdük', 'giderdik', 'sebebi', 'nedeni']);
      if (hasProblem && hasResolution) score += 3;
      break;
    }

    case 'learning':
      if (hasAny(['turns out', 'gotcha', 'pitfall', 'caveat', 'important to note', 'meğer', 'meğerse', 'anlaşılan', 'dikkat', 'önemli'])) score += 2;
      if (hasAny(['because', 'so that', 'çünkü', 'dolayı'])) score += 1;
      break;

    case 'preference':
      if (hasAny(['always', 'never', 'prefer', 'convention', 'her zaman', 'asla', 'hiçbir zaman', 'tercih'])) score += 1;
      if (hasAny(['user', '[user]', 'kullanıcı'])) score += 2;
      break;

    case 'tool_pref':
      if (hasAny(['always', 'never', 'prefer', 'better', 'works better', 'hep', 'asla', 'tercih', 'daha iyi'])) score += 2;
      if (hasAny(['because', 'instead of', 'over', 'çünkü', 'yerine'])) score += 1;
      break;

    case 'arch':
      if (hasAny(['architecture', 'pattern', 'design', 'structure', 'layer', 'mimari', 'tasarım', 'yapı', 'katman'])) score += 1;
      if (hasAny(['because', 'so that', 'enables', 'çünkü', 'sağlar'])) score += 1;
      break;

    case 'workflow':
      if (hasAny(['first', 'then', 'before', 'after', 'step', 'pipeline', 'önce', 'sonra', 'adım', 'sırasıyla'])) score += 2;
      break;

    case 'file_conv':
      if (hasAny(['directory', 'folder', 'path', 'naming', 'convention', 'dizin', 'klasör', 'dosya', 'isimlendirme'])) score += 2;
      break;

    case 'correction':
      if (hasAny(['not', 'actually', 'wrong', 'incorrect', 'should be', 'değil', 'yanlış', 'hatalı', 'aslında'])) score += 2;
      if (hasAny(['changed', 'updated', 'renamed', 'değiştirdik'])) score += 1;
      break;

    case 'infra':
      if (/(?:\d{1,3}\.){3}\d{1,3}/.test(text)) score += 2;
      if (/port\s+\d{2,5}/.test(tl)) score += 1;
      if (/v?\d+\.\d+/.test(text)) score += 1;
      if (hasAny(['trying', 'test', 'debug', 'attempt'])) score -= 2;
      break;

    case 'content':
      if (hasAny(['approved', 'published', 'onaylandı', 'yayınlandı'])) score += 2;
      if (hasAny(['never', 'always', 'asla', 'her zaman'])) score += 1;
      break;
  }

  if (text.length < 40) score -= 1;

  if (/[/\\][\w.-]+\.\w+/.test(text)) score += 1;
  if (/v?\d+\.\d+/.test(text)) score += 1;
  if (/(?:npm|pip|brew|cargo|go|docker|git|kubectl|yarn|bun)\s/.test(tl)) score += 1;

  return score;
}

interface ExtractedItem {
  content: string
  category: string
  score: number
}

const PATTERN_TABLE: Array<[RegExp, string, number]> = [
  [DECISION_RE, "decision", 8],
  [IMPLICIT_DECISION_RE, "implicit_decision", 7],
  [ERROR_RE, "error", 8],
  [LEARNING_RE, "learning", 7],
  [PREFERENCE_RE, "preference", 9],
  [TOOL_PREF_RE, "tool_pref", 7],
  [ARCH_RE, "architecture", 7],
  [WORKFLOW_RE, "workflow", 6],
  [CORRECTION_RE, "correction", 8],
  [REASON_RE, "reasoning", 6],
  [BLOCKER_RE, "blocker", 8],
  [FILE_CONV_RE, "file_convention", 6],
  [INFRA_RE, "infrastructure", 5],
  [CONTENT_RE, "content", 6],
]

export function extractPatterns(text: string, maxLen: number = 500): ExtractedItem[] {
  if (!text || text.length < 20) return []
  if (summaryFilter(text.slice(0, 2000))) return []

  const prefixResult = classifyByPrefix(text)
  if (prefixResult) {
    return [{ content: text.slice(0, 300), category: prefixResult.type, score: 9 }]
  }

  const matches: ExtractedItem[] = []
  const seen = new Set<string>()

  for (const [regex, category, baseScore] of PATTERN_TABLE) {
    const flags = regex.flags.includes("g") ? regex.flags : regex.flags + "g"
    const globalRegex = new RegExp(regex.source, flags)
    let m: RegExpExecArray | null
    while ((m = globalRegex.exec(text)) !== null) {
      const matchText = m[0].trim()
      if (matchText.length < 15 || matchText.length > maxLen) continue
      const key = matchText.slice(0, 80)
      if (seen.has(key)) continue
      seen.add(key)
      matches.push({ content: matchText, category, score: baseScore })
    }
  }

  return matches
}

// ── OpenCode `[M#]` macro verbs ────────────────────────────
// Mirror of transcript_adapter.extract_macro_verbs (Python). Format:
// `[M#<type>] <content>` or `[M#<type>:<importance>] <content>`.
// Codex PR #50 round 3 P2: anchor to line start (optional leading
// whitespace + optional `>` quote marker) via /m flag so quoted
// examples / template docs aren't ingested as real macros.
const MACRO_VERB_RE =
  /^[ \t>]*\[M#([a-zA-Z_][a-zA-Z0-9_-]{0,31})(?::([123]))?\]\s+([^\n]{4,400})/gm

const MACRO_TYPE_ALIASES: Record<string, string> = {
  dec: "decision", err: "gotcha", err_fix: "gotcha", fix: "gotcha",
  learn: "learning", pref: "preference", arch: "architecture",
}
// Codex PR #50 round 2 P2: drop unknown types so memory_type stays
// canonical. Mirror of Python's _MACRO_TYPE_ALLOWLIST.
const MACRO_TYPE_ALLOWLIST = new Set([
  "decision", "learning", "gotcha", "preference", "architecture", "pattern",
])
const MACRO_IMPORTANCE: Record<string, number> = { "1": 1.0, "2": 1.5, "3": 2.0 }

export interface MacroVerb {
  type: string
  importance: number
  content: string
  source: "user" | "assistant" | "system"
}

export function extractMacroVerbs(
  messages: { role: string; content: string }[],
  maxCount: number = 20,
): MacroVerb[] {
  const seen = new Set<string>()
  const out: MacroVerb[] = []
  for (const msg of messages) {
    const text = msg.content || ""
    if (!text || !text.includes("[M#")) continue
    MACRO_VERB_RE.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = MACRO_VERB_RE.exec(text)) !== null) {
      let t = (m[1] || "").toLowerCase()
      t = MACRO_TYPE_ALIASES[t] || t
      if (!MACRO_TYPE_ALLOWLIST.has(t)) continue
      const content = m[3].trim()
      const key = `${t}::${content.slice(0, 120)}`
      if (seen.has(key)) continue
      seen.add(key)
      const role = msg.role === "user" || msg.role === "assistant"
        ? (msg.role as "user" | "assistant") : "system"
      out.push({
        type: t,
        importance: MACRO_IMPORTANCE[m[2] || "1"] || 1.0,
        content,
        source: role,
      })
      if (out.length >= maxCount) return out
    }
  }
  return out
}

export function dedup(items: string[], maxCount: number = 5): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const item of items) {
    const short = item.slice(0, 80);
    if (!seen.has(short) && result.length < maxCount) {
      result.push(item);
      seen.add(short);
    }
  }
  return result;
}
