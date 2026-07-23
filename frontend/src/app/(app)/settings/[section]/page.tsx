"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { toast } from "sonner";
import { AuthGuard } from "@/components/layout/auth-guard";
import { useSettingsSection, useInvalidatingMutation } from "@/hooks/queries";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/problem";
import { queryDomains } from "@/lib/query-keys";
import { PageHeader } from "@/components/ui/page-header";
import { DataState } from "@/components/ui/data-state";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import type { SectionResponse } from "@/types/api";

/**
 * Settings (UI.md page 9, admin-only). A left sub-nav of the six sections and a
 * right form pane bound to `/settings/{section}`. The form is generic: it renders
 * a field per value key and a masked "configured [date]" control per secret with
 * a Replace affordance, matching the write-only secret handling on the backend.
 * A dirty-state bar offers Discard/Save; nothing is sent until Save, and a secret
 * left untouched is never re-submitted so its stored value is preserved.
 */

const SECTIONS: { key: string; label: string }[] = [
  { key: "general", label: "General" },
  { key: "gdrive", label: "Google Drive" },
  { key: "telegram", label: "Telegram" },
  { key: "retention", label: "Retention" },
  { key: "agent", label: "Agent" },
  { key: "proxmox", label: "Proxmox" },
];

export default function SettingsPage() {
  const params = useParams<{ section: string }>();
  const section = params.section;
  const query = useSettingsSection(section);

  return (
    <AuthGuard requiredRole="admin">
      <div>
        <PageHeader title="Settings" description="Connection and behaviour configuration" />
        <div className="grid grid-cols-1 gap-4 md:grid-cols-[200px_1fr]">
          <nav aria-label="Settings sections" className="flex flex-col gap-1">
            {SECTIONS.map((s) => (
              <Link
                key={s.key}
                href={`/settings/${s.key}`}
                aria-current={s.key === section ? "page" : undefined}
                className={cn(
                  "rounded-md px-3 py-2 text-sm",
                  s.key === section
                    ? "bg-accent-muted text-accent"
                    : "text-fg-muted hover:bg-elevated hover:text-fg-default",
                )}
              >
                {s.label}
              </Link>
            ))}
          </nav>

          <DataState
            isLoading={query.isLoading}
            isError={query.isError}
            error={query.error}
            data={query.data}
            onRetry={() => {
              query.refetch();
            }}
            loadingRows={5}
          >
            {(data) => <SectionForm key={section} section={section} data={data} />}
          </DataState>
        </div>
      </div>
    </AuthGuard>
  );
}

function SectionForm({
  section,
  data,
}: Readonly<{
  section: string;
  data: SectionResponse;
}>) {
  const [values, setValues] = useState<Record<string, unknown>>(data.values);
  const [secretEdits, setSecretEdits] = useState<Record<string, string>>({});

  // Reset local state when the section (and therefore its data) changes.
  useEffect(() => {
    setValues(data.values);
    setSecretEdits({});
  }, [data]);

  const save = useInvalidatingMutation(
    (payload: Record<string, unknown>) => api.put(`/settings/${section}`, payload),
    [queryDomains.storage],
  );

  const dirty =
    JSON.stringify(values) !== JSON.stringify(data.values) || Object.keys(secretEdits).length > 0;

  function handleSave() {
    // Only send secrets the operator actually typed; untouched secrets are
    // omitted so their stored value is preserved (write-only handling).
    const payload = { ...values, ...secretEdits };
    save.mutate(payload, {
      onSuccess: () => {
        toast.success("Settings saved");
        setSecretEdits({});
      },
      onError: (err) =>
        toast.error(
          err instanceof ApiError ? err.problem.detail || err.problem.title : "Save failed",
        ),
    });
  }

  function handleDiscard() {
    setValues(data.values);
    setSecretEdits({});
  }

  return (
    <Card>
      <CardContent className="flex flex-col gap-4 pt-4">
        {Object.entries(values).map(([key, value]) => (
          <div key={key} className="flex flex-col gap-1.5">
            <Label htmlFor={key}>{humanise(key)}</Label>
            <Input
              id={key}
              value={stringifyValue(value)}
              onChange={(e) =>
                setValues((prev) => ({ ...prev, [key]: coerce(value, e.target.value) }))
              }
            />
          </div>
        ))}

        {Object.entries(data.secrets).map(([key, hint]) => (
          <div key={key} className="flex flex-col gap-1.5">
            <Label htmlFor={`secret-${key}`}>{humanise(key)}</Label>
            {key in secretEdits ? (
              <Input
                id={`secret-${key}`}
                type="password"
                placeholder="Enter new value"
                value={secretEdits[key]}
                onChange={(e) => setSecretEdits((prev) => ({ ...prev, [key]: e.target.value }))}
              />
            ) : (
              <div className="flex items-center gap-2">
                <span className="text-xs text-fg-muted">{secretHintText(hint)}</span>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setSecretEdits((prev) => ({ ...prev, [key]: "" }))}
                >
                  Replace
                </Button>
              </div>
            )}
          </div>
        ))}

        {dirty && (
          <div className="flex items-center justify-end gap-2 border-t border-border-muted pt-4">
            <Button variant="secondary" onClick={handleDiscard} disabled={save.isPending}>
              Discard
            </Button>
            <Button onClick={handleSave} disabled={save.isPending}>
              {save.isPending ? "Saving\u2026" : "Save"}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function humanise(key: string): string {
  return key
    .split("_")
    .map((word) => (word.length > 0 ? word[0]!.toUpperCase() + word.slice(1) : word))
    .join(" ");
}

function secretHintText(hint: { configured: boolean; hint: string | null }): string {
  if (!hint.configured) return "Not set";
  return hint.hint ? `Configured (${hint.hint})` : "Configured";
}

function stringifyValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return typeof value === "string" ? value : JSON.stringify(value);
}

function coerce(original: unknown, next: string): unknown {
  if (typeof original === "number") return Number(next);
  if (typeof original === "boolean") return next === "true";
  return next;
}
