"use client";

import { useMemo } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { CRON_PRESETS, describeCron, validateCron } from "@/lib/cron";
import { cn } from "@/lib/utils";

/**
 * Cron builder (UI.md CronBuilder). A raw crontab field with one-click presets
 * and a live, human-readable summary plus inline validation. It deliberately
 * does not compute fire times — the authoritative preview comes from
 * `/backup-jobs/{id}/preview` (the crontab-vs-APScheduler weekday quirk fixed in
 * M3 lives on the backend). This gives instant feedback while the operator types
 * without duplicating the scheduler's logic.
 */
export function CronBuilder({
  value,
  onChange,
}: Readonly<{
  value: string;
  onChange: (expression: string) => void;
}>) {
  const validation = useMemo(() => validateCron(value), [value]);
  const summary = useMemo(() => describeCron(value), [value]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="cron">Cron expression</Label>
        <Input
          id="cron"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          aria-invalid={!validation.valid}
          aria-describedby="cron-summary"
          className="font-mono"
          placeholder="0 1 * * *"
        />
      </div>

      <p
        id="cron-summary"
        className={cn("text-xs", validation.valid ? "text-fg-muted" : "text-danger")}
      >
        {validation.valid ? summary : validation.error}
      </p>

      <div className="flex flex-wrap gap-1.5">
        {CRON_PRESETS.map((preset) => (
          <Button
            key={preset.expression}
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => onChange(preset.expression)}
          >
            {preset.label}
          </Button>
        ))}
      </div>
    </div>
  );
}
