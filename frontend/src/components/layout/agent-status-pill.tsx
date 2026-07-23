"use client";

import { useHealthDetail } from "@/hooks/queries";
import { cn } from "@/lib/utils";

/**
 * Agent connectivity pill for the header (UI.md shell).
 *
 * Reads `/health/detail` and surfaces the `agent` component's reachability with a
 * glyph + label (never colour alone): a green dot with the round-trip latency
 * when reachable, a red "offline" when not. While the very first health check is
 * in flight it shows a neutral "checking" state rather than implying either
 * outcome. It intentionally reports only reachability and latency — nothing that
 * resembles tracking or a history of the agent.
 */
export function AgentStatusPill() {
  const { data, isLoading, isError } = useHealthDetail();

  const agent = data?.components.find((component) => component.name === "agent");
  const reachable = agent?.status === "ok";
  const latency = agent?.latency_ms;

  let tone = "neutral";
  let glyph = "\u25CB";
  let label = "Agent: checking";

  if (!isLoading) {
    if (isError || !agent || agent.status === "down") {
      tone = "danger";
      glyph = "\u25CF";
      label = "Agent offline";
    } else if (agent.status === "degraded") {
      tone = "warning";
      glyph = "\u25D0";
      label = "Agent degraded";
    } else if (reachable) {
      tone = "success";
      glyph = "\u25CF";
      label = latency != null ? `Agent online \u00b7 ${Math.round(latency)}ms` : "Agent online";
    }
  }

  const toneClass =
    tone === "success"
      ? "text-success"
      : tone === "danger"
        ? "text-danger"
        : tone === "warning"
          ? "text-warning"
          : "text-fg-muted";

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border border-border-default bg-elevated px-2.5 py-1 text-xs font-medium",
        toneClass,
      )}
      role="status"
      aria-label={label}
    >
      <span aria-hidden="true" className={cn(reachable && "animate-running-pulse")}>
        {glyph}
      </span>
      <span>{label}</span>
    </span>
  );
}
