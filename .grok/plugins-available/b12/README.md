# B12 Memory Plugin for Grok

Native, high-quality persistent memory for Grok CLI using the B12 engine.

This plugin gives Grok first-class, declarative memory capabilities that are **superior** to the legacy hook-based implementations on other platforms.

## Kurulum (Önerilen Yöntem)

```bash
cd ~/Desktop/B12
./install.sh --grok
```

Bu komut otomatik olarak şunları yapar:
- `~/.grok/plugins/b12/` altına plugin'i kopyalar (user plugin olarak)
- `~/.grok/skills/b12-memory/` altına skill'i kopyalar
- `~/.grok/config.toml`'a B12 MCP server'ını ekler
- Proje `AGENTS.md`'ine Grok talimatlarını ekler

---

## Alternatif: Manuel Kurulum

Eğer `install.sh` kullanmak istemiyorsan:

```bash
# Plugin'i user scope'a kopyala (önerilen)
cp -r .grok/plugins-available/b12 ~/.grok/plugins/b12

# Skill'i kopyala
mkdir -p ~/.grok/skills/b12-memory
cp .grok/skills/b12-memory/SKILL.md ~/.grok/skills/b12-memory/SKILL.md

# MCP'yi ~/.grok/config.toml'a ekle (veya install.sh --grok çalıştır)
```

**Not:** Repo içindeki `.grok/plugins-available/b12/` klasörü **paylaşım için** vardır. Doğrudan `.grok/plugins/b12/` içine koymuyoruz ki Grok onu "project plugin" olarak görüp disabled yapmasın. `install.sh --grok` bu klasörü `~/.grok/plugins/b12/` (user scope) altına kopyalar.

## Grok'u Yeniden Başlattıktan Sonra Kontrol

```bash
grok inspect
```

Görmen gerekenler:
- `B12` MCP server'ı bağlı ve tool'lar `B12__memory_*` şeklinde görünüyor
- `b12-memory` skill'i yüklü
- Plugin `b12` listede görünüyor

Ek kontroller:
```bash
/skills
grok mcp list
grok hooks list
```

Eğer her şey görünüyorsa entegrasyon hazır demektir.

## How It Works (Grok Native)

- **Skills** (`b12-memory`): Auto-invokes on memory-related prompts thanks to a high-quality `description` field. Contains the full ritual (SessionStart context, search, store, subagent patterns).
- **MCP**: Full power of the B12 engine (search, store, consolidate, Ebbinghaus decay, quality, dashboard, etc.).
- **Hooks** (optional but powerful): `PreCompact` for intelligent transcript staging before compaction, `SessionEnd` for extraction.
- **Subagents**: For heavy memory work (deep audits, extraction on long sessions), the skill encourages using Grok's native `task` tool + `fork_context` + researcher personas.

## Daily Usage

Just work normally. The skill will guide you to use the right B12 tools at the right time. You will see clean B12 pills like:

`( 💊 B12 🧠 : found 14 memories about the Grok integration, stored 2d ago ✅ )`

For complex memory tasks, the system will suggest or automatically use subagents.

## Architecture Benefits (vs other platforms)

- Declarative (Skills + description) instead of imperative shell scripts.
- LLM-powered extraction and surfacing via subagents (much smarter than pure regex).
- Plugin + marketplace ready.
- Extremely low maintenance (thin wrappers only; 100% reuses the excellent shared Python core).
- Perfectly additive — does not touch any existing Claude Code, OpenCode, or Codex code.

## Files in This Plugin

- `skills/b12-memory/SKILL.md` — The brain (auto-invoking orchestrator)
- `hooks/hooks.json` + `hooks/scripts/` — Thin lifecycle adapters (PreCompact + SessionEnd)
- `.mcp.json` — Declarative MCP registration
- `README.md` — This file

## Updating

After changes to the plugin files, run `grok plugin reload` or restart the session.

---

## Grok'u Kapatıp Yeniden Açtıktan Sonra (Önemli)

1. Grok CLI'yi tamamen kapat.
2. Tekrar `grok` komutuyla aç (B12 klasöründe).
3. Şu komutları çalıştır:

```bash
grok inspect
/skills
grok mcp list
```

4. `b12-memory` skill'i ve `B12__memory_*` tool'ları görünüyorsa her şey hazır demektir.

Artık normal çalışmaya başlayabilirsin. B12 hafızası Grok'un içinde native olarak çalışacak.

## Support

This is part of the official B12 project (https://github.com/dorukardahan/B12).

For the full B12 documentation, see the main `docs/` and `README.md` in the repo root.