export const TYPE_PRIORITY: Record<string, number> = {
  decision: 10,
  error_fix: 9,
  learning: 8,
  preference: 8,
  file_modified: 7,
  user_request: 6,
  progress: 5,
  general_work: 2,
};

export const DEFAULT_TYPE_PRIORITY = 1;

export const IMPORTANCE_LEVELS = {
  critical: 2.0,
  important: 1.5,
  normal: 1.0,
  temporary: 0.7,
} as const;

export const MAX_STRENGTH = 5.0;
export const DEFAULT_STRENGTH_BOOST = 0.2;
export const FSRS_DECAY = 0.9;
export const FSRS_FACTOR = 9.0;

export interface FSRSParams {
  w: number[];
  requestRetention: number;
}

export const DEFAULT_FSRS_PARAMS: FSRSParams = {
  w: [0.4, 0.6, 2.4, 5.8, 4.93, 0.94, 0.86, 0.01, 1.49, 0.14, 0.94, 2.18, 0.05, 0.34, 1.26, 0.29, 2.61],
  requestRetention: 0.9,
};

export function retrievability(stability: number, elapsedDays: number): number {
  if (stability <= 0) return 0;
  if (elapsedDays <= 0) return 1;
  return 1 / (1 + elapsedDays / (FSRS_FACTOR * stability));
}

export function forgettingCurve(stability: number, elapsedDays: number): number {
  return Math.pow(retrievability(stability, elapsedDays), FSRS_DECAY);
}

export function stabilityAfterRecall(
  stability: number,
  difficulty: number,
  retrievabilityValue: number,
  params: FSRSParams = DEFAULT_FSRS_PARAMS,
  grade: "easy" | "good" | "hard" = "good",
): number {
  const hardPenalty = grade === "hard" ? params.w[15] : 1;
  const easyBonus = grade === "easy" ? params.w[16] : 1;
  const w = params.w;
  const newStability =
    stability *
    (1 +
      Math.exp(w[8]) *
        (11 - difficulty) *
        Math.pow(stability, -w[9]) *
        (Math.exp(w[10] * (1 - retrievabilityValue)) - 1) *
        hardPenalty *
        easyBonus);
  return Math.max(stability * 0.1, Math.min(newStability, MAX_STRENGTH * 10));
}

export function stabilityAfterForget(
  difficulty: number,
  retrievabilityValue: number,
  params: FSRSParams = DEFAULT_FSRS_PARAMS,
): number {
  const w = params.w;
  const newStability =
    w[11] *
    Math.pow(difficulty, -w[12]) *
    (Math.pow(stabilityAfterRecall(1, difficulty, retrievabilityValue, params) + 1, w[13]) - 1) *
    Math.exp(w[14] * (1 - retrievabilityValue));
  return Math.max(0.1, newStability);
}

export interface MemoryEntry {
  strength: number;
  importance_score?: number;
  memory_type?: string;
  last_reviewed?: string;
  created_at?: string;
  review_count?: number;
}

function elapsedDaysSince(timestamp?: string): number | null {
  if (!timestamp) return null;
  const parsed = new Date(timestamp).getTime();
  if (!Number.isFinite(parsed)) return null;
  return Math.max(0, (Date.now() - parsed) / 86400000);
}

export function computePriorityScore(
  entry: MemoryEntry,
  elapsedDays: number = 0,
): number {
  const typePriority =
    TYPE_PRIORITY[entry.memory_type ?? ""] ?? DEFAULT_TYPE_PRIORITY;
  const importance = entry.importance_score ?? IMPORTANCE_LEVELS.normal;
  const retrievabilityValue = retrievability(entry.strength, elapsedDays);
  return typePriority * importance * retrievabilityValue;
}

export function simpleReview(
  entry: MemoryEntry,
  grade: "easy" | "good" | "hard" | "again" = "good",
  useFSRS: boolean = false,
  params: FSRSParams = DEFAULT_FSRS_PARAMS,
): Pick<MemoryEntry, "strength"> {
  if (!useFSRS) {
    let boost = DEFAULT_STRENGTH_BOOST;
    switch (grade) {
      case "easy":
        boost = 0.4;
        break;
      case "good":
        boost = 0.2;
        break;
      case "hard":
        boost = 0.1;
        break;
      case "again":
        boost = -0.3;
        break;
    }
    return {
      strength: Math.min(Math.max(entry.strength + boost, 0.1), MAX_STRENGTH),
    };
  }

  const difficulty = Math.max(
    1,
    Math.min(10, 10 - (entry.importance_score ?? IMPORTANCE_LEVELS.normal) * 3),
  );
  const elapsedDays = entry.last_reviewed
    ? elapsedDaysSince(entry.last_reviewed) ?? 0
    : 0;
  const ret = retrievability(entry.strength, elapsedDays);

  let newStability: number;
  if (grade === "again") {
    newStability = stabilityAfterForget(difficulty, ret, params);
  } else {
    newStability = stabilityAfterRecall(entry.strength, difficulty, ret, params, grade);
  }

  const strengthScale = Math.min(newStability / 2, MAX_STRENGTH);

  return { strength: Math.max(0.1, strengthScale) };
}

export function shouldReview(
  entry: MemoryEntry,
  targetRetention: number = 0.9,
): boolean {
  if (!entry.last_reviewed) return true;
  const elapsedDays = elapsedDaysSince(entry.last_reviewed);
  if (elapsedDays === null) return true;
  const ret = retrievability(entry.strength, elapsedDays);
  return ret < targetRetention;
}

export function nextReviewInterval(
  stability: number,
  targetRetention: number = 0.9,
): number {
  return FSRS_FACTOR * stability * (1 / targetRetention - 1);
}
