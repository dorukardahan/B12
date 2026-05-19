# B12 Memory System — Continue.dev Behavioral Rules

Paste this block into `~/.continue/config.yaml` under `rules:` (Continue
honors per-file Markdown rules in `~/.continue/rules/*.md` as of late
2025). Or copy as `.continue/rules/b12-memory.md` in any project root.

---

You have access to B12's persistent memory system via the MCP server `B12`.

**CALL `memory_search` BEFORE answering when ANY of these holds:**
- the user uses a recall verb (remember, recall, last time, before, previously, prior, earlier, said, told, mentioned, stored / TR: hatırla, hatırlıyor, geçen sefer, daha önce, demiştik, söylemiştim, kaydetmiştik)
- the user references work that is not visible in the current conversation
- starting a non-trivial task in this project

**CALL `memory_store` WHEN:** a decision, fact, preference, or workflow pattern that should outlive this conversation. Always include tags `[proj:<name>, user:<setup>]` and metadata `{project:"<name>", scope:"<type>"}`.

**TOOLS:** `memory_search` (mode=hybrid, ISO-date `after`/`before`), `memory_store`, `memory_update`, `memory_quality`. Surface a pill `( 💊 B12 🧠 : found N memories about [topic] ✅ )` on retrieval, `( 💊 B12 🧠 : saved to memory ✅ )` on store, `❌` only when the user explicitly asked and nothing was found.

Default tag scope is `proj:<project-name>` (extracted from cwd) to keep retrieval focused; widen to no tag filter when results are sparse.

For importance scoring, time-window searches, scope types, and dual memory layers (B12 SQLite vs Continue's own `~/.continue/sessions/`), see the B12 docs in the project repo.
