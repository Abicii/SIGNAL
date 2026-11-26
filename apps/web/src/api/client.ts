import type { PlanResponse, ReplanResponse, TodayResponse } from "../types/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  today: () => request<TodayResponse>("/api/v1/today"),
  plan: () => request<PlanResponse>("/api/v1/plan"),
  replan: (input: { kind: string; starts_at: string; ends_at: string; impact: string }) =>
    request<ReplanResponse>("/api/v1/replan", { method: "POST", body: JSON.stringify(input) }),
  feedback: (action: "accept" | "reject", decision_id?: string | null) =>
    request<{ action: string; status: string }>("/api/v1/feedback", { method: "POST", body: JSON.stringify({ action, decision_id }) }),
};
