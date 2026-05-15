import { join } from "path"
import { existsSync, readFileSync } from "fs"
import { homedir } from "os"
import { B12Database, getDbPath } from "../lib/db.js"
import * as daemon from "../lib/daemon.js"

const B12_BASE = process.env.B12_DATA_DIR || join(homedir(), ".B12")

const STOPWORDS_EN = new Set([
  "the","a","an","is","are","was","were","be","been","being","have","has","had",
  "do","does","did","will","would","could","should","may","might","shall","can",
  "to","of","in","for","on","with","at","by","from","as","into","through","during",
  "before","after","above","below","between","out","off","over","under","again",
  "further","then","once","here","there","when","where","why","how","all","both",
  "each","few","more","most","other","some","such","no","nor","not","only","own",
  "same","so","than","too","very","just","because","but","and","or","if","while",
  "this","that","these","those","it","its","what","which","who","whom","i","me",
  "my","we","our","you","your","he","him","his","she","her","they","them","their",
  "about","up","also","like","get","make","go","know","take","see","come","think",
  "look","want","give","use","find","tell","ask","work","seem","feel","try","leave",
  "call","keep","let","begin","show","hear","play","run","move","live","believe",
  "bring","happen","write","provide","sit","stand","lose","pay","meet","include",
  "continue","set","learn","change","lead","understand","watch","follow","stop",
  "create","speak","read","allow","add","spend","grow","open","walk","win","offer",
  "remember","love","consider","appear","buy","wait","serve","die","send","expect",
  "build","stay","fall","cut","reach","kill","remain","suggest","raise","pass",
  "sell","require","report","decide","pull","really","much","thing","any","even",
])

const STOPWORDS_TR = new Set([
  "bir","bu","şu","da","de","ve","ile","için","ama","fakat","yada","veya","gibi",
  "kadar","daha","en","çok","az","her","hiç","bazı","tüm","hepsi","bütün","başka",
  "sonra","önce","ilk","son","yeni","eski","büyük","küçük","iyi","kötü","uzun",
  "kısa","genel","özel","aynı","farklı","nasıl","neden","niçin","nerede","ne zaman",
  "kim","ne","hangi","kaç","nasıl","kez","olarak","üzere","gibi","sanki",
  "ben","sen","o","biz","siz","onlar","benim","senin","onun","bizim","sizin","onların",
  "bana","sana","ona","bize","size","onlara","beni","seni","onu","bizi","sizi","onları",
  "benle","senle","onla","bizle","sizle","onlarla","evet","hayır","belki","tabii",
  "tamam","olur","olmaz","iyi","güzel","peki","demek","var","yok","mi","mı","mu","mü",
])

const ALL_STOPWORDS = new Set([...STOPWORDS_EN, ...STOPWORDS_TR])

interface QueryAlias {
  [key: string]: string[]
}

let _aliases: QueryAlias | null = null

function loadAliases(): QueryAlias {
  if (_aliases) return _aliases
  const aliasPath = join(B12_BASE, "hooks", "scripts", "query_aliases.json")
  if (existsSync(aliasPath)) {
    try {
      _aliases = JSON.parse(readFileSync(aliasPath, "utf-8"))
      return _aliases!
    } catch {}
  }
  _aliases = {}
  return _aliases
}

export function extractKeywords(text: string): string[] {
  const words = text
    .toLowerCase()
    .replace(/[^\w\u00C0-\u024F\u1E00-\u1EFF]/g, " ")
    .split(/\s+/)
    .filter((w) => w.length >= 3 && !ALL_STOPWORDS.has(w))
  return [...new Set(words)]
}

export function buildFtsQuery(keywords: string[]): string {
  if (keywords.length === 0) return ""

  const aliases = loadAliases()
  const expanded: string[] = []

  for (const kw of keywords.slice(0, 8)) {
    expanded.push(`"${kw.replace(/"/g, '""')}"`)
    if (aliases[kw]) {
      for (const alias of aliases[kw].slice(0, 2)) {
        expanded.push(`"${alias.replace(/"/g, '""')}"`)
      }
    }
  }

  return expanded.join(" OR ")
}

function isGreeting(text: string): boolean {
  const t = text.toLowerCase().trim()
  const greetings = [
    "hi","hello","hey","merhaba","selam","naber","nasılsın","good morning",
    "good afternoon","good evening","günaydın","iyi günler","iyi akşamlar",
  ]
  return greetings.some((g) => t === g || (t.length < 20 && t.startsWith(g)))
}

function isSlashCommand(text: string): boolean {
  return text.trim().startsWith("/")
}

function isShortCommand(text: string): boolean {
  return text.trim().split(/\s+/).length <= 2 && text.length < 15
}

export async function messageRetrieval(
  userMessage: string,
  project: string,
  db: B12Database
): Promise<string> {
  const startTime = Date.now()

  if (isGreeting(userMessage) || isSlashCommand(userMessage) || isShortCommand(userMessage)) {
    return ""
  }

  const keywords = extractKeywords(userMessage)
  if (keywords.length === 0) return ""

  const ftsQuery = buildFtsQuery(keywords)

  let ftsResults = db.search({
    query: ftsQuery,
    mode: "hybrid",
    tags: [`proj:${project}`],
    limit: 10,
  })

  let semanticResults: Array<{ id: number; display: string; score: number }> = []
  try {
    const daemonAlive = await daemon.health()
    if (daemonAlive.alive) {
      semanticResults = await daemon.semanticSearch(
        keywords.join(" "),
        getDbPath(),
        5
      )
    }
  } catch {}

  const mergedMap = new Map<number, { display: string; score: number }>()
  for (const r of ftsResults) {
    mergedMap.set(r.id, { display: r.display, score: r.score })
  }
  for (const r of semanticResults) {
    const existing = mergedMap.get(r.id)
    if (existing) {
      mergedMap.set(r.id, {
        display: existing.display,
        score: Math.max(existing.score, r.score) * 1.1,
      })
    } else {
      mergedMap.set(r.id, { display: r.display, score: r.score * 0.8 })
    }
  }

  const merged = [...mergedMap.entries()]
    .map(([id, v]) => ({ id, ...v }))
    .sort((a, b) => b.score - a.score)
    .slice(0, 5)

  if (merged.length === 0) return ""

  let reranked = false
  if (merged.length > 1) {
    try {
      const rankedIds = await daemon.rerank(
        keywords.join(" "),
        getDbPath(),
        merged.map((m) => m.id)
      )
      if (rankedIds.length > 0) {
        reranked = true
        const idOrder = new Map(rankedIds.map((id, i) => [id, i]))
        merged.sort((a, b) => {
          const oa = idOrder.get(a.id) ?? 999
          const ob = idOrder.get(b.id) ?? 999
          return oa - ob
        })
      }
    } catch {}
  }

  db.boostStrength(merged.map((m) => m.id))

  const latencyMs = Date.now() - startTime
  const stagingDir = join(B12_BASE, "memory-staging")
  try {
    db.logFeedback(stagingDir, {
      query: userMessage.slice(0, 200),
      keywords: keywords.join(","),
      resultCount: merged.length,
      reranked,
      queryMode: "hybrid",
      skipReason: "",
      searchSource: "plugin",
      latencyMs,
      project,
    })
  } catch {}

  const lines = merged.map(
    (m) => `${m.display} (score: ${m.score.toFixed(2)})`
  )
  return `## Relevant Memories\n${lines.join("\n")}`
}
