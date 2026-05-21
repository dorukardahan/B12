export type SearchMode = "hybrid" | "semantic" | "exact"

export function effectiveSearchMode(mode: SearchMode | undefined): "hybrid" | "exact" {
  return mode === "exact" ? "exact" : "hybrid"
}
