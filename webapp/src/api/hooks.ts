import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryKey } from "@tanstack/react-query";
import { Accounts, Assets, Jobs, Publications, Scenarios, Styles, System } from "./client";
import {
  TERMINAL_JOB_STATUSES,
  type ClipJob,
  type ClipJobDetail,
  type ScenarioData,
} from "./types";

// A job in flight changes every few seconds; a finished one never does.
// Polling only while something is actually running keeps an idle tab quiet.
const LIVE_POLL_MS = 3000;

function jobIsLive(job?: Pick<ClipJob, "status">) {
  return !!job && !TERMINAL_JOB_STATUSES.includes(job.status);
}

export const keys = {
  health: ["health"] as const,
  accounts: ["accounts"] as const,
  assets: ["assets"] as const,
  jobs: ["jobs"] as const,
  job: (id: number) => ["jobs", id] as const,
  publications: (status?: string) => ["publications", status ?? "all"] as const,
  tasks: ["tasks"] as const,
  styles: ["styles"] as const,
  styleDefaults: (profile?: string) => ["styles", "defaults", profile ?? "talking"] as const,
  scenarios: ["scenarios"] as const,
  scenarioInspect: (fingerprint: string, duration: number, at: number) =>
    ["scenarios", "inspect", duration, at, fingerprint] as const,
};

export function useScenarios() {
  return useQuery({ queryKey: keys.scenarios, queryFn: Scenarios.list });
}

/**
 * The draft, laid out on a clip of that length by the compiler.
 *
 * Keyed by the draft itself, so every edit asks again and the answer for a
 * draft already seen comes back from the cache — dragging a block back to
 * where it was costs nothing. The editor draws this and nothing else: a
 * second implementation of the layout rules in the browser would diverge from
 * the renderer in exactly the small ways nobody notices until a clip is
 * wrong.
 */
export function useScenarioInspect(
  data: ScenarioData | null,
  duration: number,
  at = 0,
) {
  const fingerprint = data ? JSON.stringify(data) : "";
  return useQuery({
    queryKey: keys.scenarioInspect(fingerprint, duration, at),
    queryFn: () => Scenarios.inspect(data as ScenarioData, duration, at),
    enabled: !!data,
    // A compile of the same draft at the same length is the same answer.
    staleTime: Infinity,
    // Keep the last layout on screen while the next one is computed, so the
    // timeline does not blink on every keystroke.
    placeholderData: (previous) => previous,
    retry: false,
  });
}

export function useStyles() {
  return useQuery({ queryKey: keys.styles, queryFn: Styles.list });
}

export function useStyleDefaults(profile?: string) {
  return useQuery({
    queryKey: keys.styleDefaults(profile),
    queryFn: () => Styles.defaults(profile),
    // The defaults only move when the server's configuration does, which does
    // not happen while a tab is open.
    staleTime: 5 * 60 * 1000,
  });
}

export function useHealth() {
  return useQuery({ queryKey: keys.health, queryFn: System.health, refetchInterval: 15000 });
}

export function useAccounts() {
  return useQuery({ queryKey: keys.accounts, queryFn: Accounts.list });
}

export function useAssets() {
  return useQuery({ queryKey: keys.assets, queryFn: Assets.list });
}

export function useJobs() {
  return useQuery({
    queryKey: keys.jobs,
    queryFn: Jobs.list,
    refetchInterval: (query) => {
      const jobs = query.state.data as ClipJob[] | undefined;
      return jobs?.some(jobIsLive) ? LIVE_POLL_MS : false;
    },
  });
}

export function useJob(id: number) {
  return useQuery({
    queryKey: keys.job(id),
    queryFn: () => Jobs.get(id),
    // 0 is "no job chosen yet", which happens on any page that picks one out
    // of a list that has not loaded. Asking for it 404s on every mount.
    enabled: id > 0,
    refetchInterval: (query) => (jobIsLive(query.state.data as ClipJobDetail | undefined) ? LIVE_POLL_MS : false),
  });
}

export function usePublications(status?: string) {
  return useQuery({
    queryKey: keys.publications(status),
    queryFn: () => Publications.list(status),
    refetchInterval: 15000,
  });
}

export function useTasks() {
  return useQuery({ queryKey: keys.tasks, queryFn: () => System.tasks(), refetchInterval: 5000 });
}

/** Wraps a mutation so the lists it affects refresh on success. */
export function useInvalidatingMutation<TArgs, TResult>(
  fn: (args: TArgs) => Promise<TResult>,
  invalidate: readonly QueryKey[],
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      invalidate.forEach((key) => qc.invalidateQueries({ queryKey: key }));
    },
  });
}
