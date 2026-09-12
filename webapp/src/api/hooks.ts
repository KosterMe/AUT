import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryKey } from "@tanstack/react-query";
import { Accounts, Assets, Jobs, Publications, Styles, System } from "./client";
import { TERMINAL_JOB_STATUSES, type ClipJob, type ClipJobDetail } from "./types";

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
};

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
