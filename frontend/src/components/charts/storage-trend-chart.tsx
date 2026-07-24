"use client";

import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { StorageSnapshotResponse } from "@/types/api";
import { formatBytes } from "@/lib/format";

/**
 * 30-day storage trend (UI.md page 7, Recharts). Renders committed storage
 * samples as a filled area of used bytes over time. Axis and tooltip formatting
 * reuse the shared byte formatter so the chart reads in the same IEC units as
 * every table cell. Colours come from the accent token via a gradient so the
 * chart follows the active theme.
 */
export function StorageTrendChart({
  samples,
}: Readonly<{
  samples: StorageSnapshotResponse[];
}>) {
  const data = samples.map((s) => ({
    ts: new Date(s.captured_at).getTime(),
    used: s.used_bytes,
    percent: s.used_percent,
  }));

  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
          <defs>
            <linearGradient id="usedFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.4} />
              <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis
            dataKey="ts"
            type="number"
            domain={["dataMin", "dataMax"]}
            scale="time"
            tickFormatter={(ts: number) => new Date(ts).toISOString().slice(5, 10)}
            stroke="var(--fg-subtle)"
            fontSize={11}
          />
          <YAxis
            tickFormatter={(v: number) => formatBytes(v, 0)}
            stroke="var(--fg-subtle)"
            fontSize={11}
            width={64}
          />
          <Tooltip
            contentStyle={{
              background: "var(--bg-elevated)",
              border: "1px solid var(--border)",
              borderRadius: "6px",
              fontSize: "12px",
              color: "var(--fg-default)",
            }}
            labelFormatter={(ts) =>
              new Date(ts as number).toISOString().slice(0, 16).replace("T", " ")
            }
            formatter={(value) => [formatBytes(typeof value === "number" ? value : 0), "Used"]}
          />
          <Area
            type="monotone"
            dataKey="used"
            stroke="var(--accent)"
            strokeWidth={2}
            fill="url(#usedFill)"
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
