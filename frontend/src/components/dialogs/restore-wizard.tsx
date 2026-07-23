"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/problem";
import { queryDomains } from "@/lib/query-keys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { formatDuration } from "@/lib/format";
import type {
  PreflightReport,
  RestoreCreatedResponse,
  RestoreMode,
  RestoreRequest,
} from "@/types/api";

/**
 * Restore wizard (UI.md page 5) implementing the backend's two-phase flow
 * (docs/HANDOFF §M6): step 1 collects the target, step 2 runs preflight and — if
 * nothing blocks — creates a pending restore that returns a one-time token with a
 * short TTL, step 3 requires the operator to re-type the target VMID and confirm
 * before the token expires. The confirmation re-runs every preflight check on the
 * live host, so a 409 here means the world changed and the report is shown afresh
 * rather than executing against stale facts. A restore destroys its target, so
 * this flow is deliberately heavy: typed confirmation, a visible countdown, and
 * no automatic retry anywhere.
 */

type Step = "target" | "preflight" | "confirm";

const EMPTY_FORM: RestoreRequest = {
  backup_id: 0,
  restore_mode: "qemu",
  target_vmid: 0,
  target_storage: "",
  target_node: "",
  overwrite_existing: false,
  force_stop: false,
  start_after_restore: false,
};

export function RestoreWizard() {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<Step>("target");
  const [form, setForm] = useState<RestoreRequest>(EMPTY_FORM);
  const [preflight, setPreflight] = useState<PreflightReport | null>(null);
  const [created, setCreated] = useState<RestoreCreatedResponse | null>(null);
  const [typedVmid, setTypedVmid] = useState("");
  const [busy, setBusy] = useState(false);

  function update<K extends keyof RestoreRequest>(key: K, value: RestoreRequest[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function reset() {
    setStep("target");
    setForm(EMPTY_FORM);
    setPreflight(null);
    setCreated(null);
    setTypedVmid("");
  }

  async function runPreflight() {
    setBusy(true);
    try {
      const report = await api.post<PreflightReport>("/restores/preflight", form);
      setPreflight(report);
      setStep("preflight");
    } catch (err) {
      toast.error(errorMessage(err, "Preflight failed"));
    } finally {
      setBusy(false);
    }
  }

  async function createRestore() {
    setBusy(true);
    try {
      const res = await api.post<RestoreCreatedResponse>("/restores", form);
      setCreated(res);
      setStep("confirm");
    } catch (err) {
      toast.error(errorMessage(err, "Could not create restore"));
    } finally {
      setBusy(false);
    }
  }

  async function confirmRestore() {
    if (!created) return;
    setBusy(true);
    try {
      await api.post(`/restores/${created.id}/confirm`, {
        confirmation_token: created.confirmation_token,
        target_vmid: Number(typedVmid),
      });
      toast.success("Restore confirmed and queued");
      queryClient.invalidateQueries({ queryKey: queryDomains.restores });
      reset();
    } catch (err) {
      // A 409 means the live re-check now blocks: surface the fresh report and
      // send the operator back to review it rather than pretending it succeeded.
      if (err instanceof ApiError && err.isConflict) {
        toast.error("The host changed since preflight — please review and start again.");
        reset();
      } else {
        toast.error(errorMessage(err, "Confirmation failed"));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>New restore</CardTitle>
      </CardHeader>
      <CardContent>
        {step === "target" && (
          <TargetStep form={form} update={update} busy={busy} onNext={runPreflight} />
        )}
        {step === "preflight" && preflight && (
          <PreflightStep
            report={preflight}
            busy={busy}
            onBack={() => setStep("target")}
            onProceed={createRestore}
          />
        )}
        {step === "confirm" && created && (
          <ConfirmStep
            created={created}
            typedVmid={typedVmid}
            setTypedVmid={setTypedVmid}
            busy={busy}
            onCancel={reset}
            onConfirm={confirmRestore}
          />
        )}
      </CardContent>
    </Card>
  );
}

function TargetStep({
  form,
  update,
  busy,
  onNext,
}: Readonly<{
  form: RestoreRequest;
  update: <K extends keyof RestoreRequest>(key: K, value: RestoreRequest[K]) => void;
  busy: boolean;
  onNext: () => void;
}>) {
  const valid =
    form.backup_id > 0 && form.target_vmid > 0 && form.target_storage && form.target_node;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field label="Backup ID">
          <Input
            type="number"
            value={form.backup_id || ""}
            onChange={(e) => update("backup_id", Number(e.target.value))}
          />
        </Field>
        <Field label="Restore mode">
          <Select
            value={form.restore_mode}
            onChange={(e) => update("restore_mode", e.target.value as RestoreMode)}
          >
            <option value="qemu">QEMU (VM)</option>
            <option value="lxc">LXC (container)</option>
          </Select>
        </Field>
        <Field label="Target VMID">
          <Input
            type="number"
            value={form.target_vmid || ""}
            onChange={(e) => update("target_vmid", Number(e.target.value))}
          />
        </Field>
        <Field label="Target node">
          <Input value={form.target_node} onChange={(e) => update("target_node", e.target.value)} />
        </Field>
        <Field label="Target storage">
          <Input
            value={form.target_storage}
            onChange={(e) => update("target_storage", e.target.value)}
          />
        </Field>
      </div>

      <div className="flex flex-col gap-2">
        <Checkbox
          label="Overwrite existing guest"
          checked={form.overwrite_existing}
          onChange={(v) => update("overwrite_existing", v)}
        />
        <Checkbox
          label="Force stop the target if running"
          checked={form.force_stop}
          onChange={(v) => update("force_stop", v)}
        />
        <Checkbox
          label="Start guest after restore"
          checked={form.start_after_restore}
          onChange={(v) => update("start_after_restore", v)}
        />
      </div>

      <div className="flex justify-end">
        <Button onClick={onNext} disabled={!valid || busy}>
          {busy ? "Checking…" : "Run preflight"}
        </Button>
      </div>
    </div>
  );
}

function PreflightStep({
  report,
  busy,
  onBack,
  onProceed,
}: Readonly<{
  report: PreflightReport;
  busy: boolean;
  onBack: () => void;
  onProceed: () => void;
}>) {
  return (
    <div className="flex flex-col gap-4">
      <ul className="flex flex-col gap-1">
        {report.checks.map((check) => (
          <li
            key={check.name}
            className="flex items-start gap-2 rounded-md border border-border-muted px-3 py-2"
          >
            <span aria-hidden="true" className={cn("font-mono", checkToneClass(check.status))}>
              {checkGlyph(check.status)}
            </span>
            <div className="flex flex-col">
              <span className="text-sm text-fg">{check.name}</span>
              <span className="text-xs text-fg-muted">{check.detail}</span>
            </div>
          </li>
        ))}
      </ul>

      {report.blocking && (
        <p
          role="alert"
          className="rounded-md border border-danger bg-danger-muted px-3 py-2 text-xs text-danger"
        >
          One or more checks block this restore. Resolve them and run preflight again.
        </p>
      )}

      <div className="flex justify-between">
        <Button variant="secondary" onClick={onBack} disabled={busy}>
          Back
        </Button>
        <Button onClick={onProceed} disabled={report.blocking || busy}>
          {busy ? "Preparing…" : "Continue"}
        </Button>
      </div>
    </div>
  );
}

function ConfirmStep({
  created,
  typedVmid,
  setTypedVmid,
  busy,
  onCancel,
  onConfirm,
}: Readonly<{
  created: RestoreCreatedResponse;
  typedVmid: string;
  setTypedVmid: (value: string) => void;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}>) {
  const remaining = useCountdown(created.expires_at);
  const expired = remaining <= 0;
  const matches = Number(typedVmid) === created.preflight.target_vmid;

  return (
    <div className="flex flex-col gap-4">
      <div
        className={cn(
          "rounded-md border px-3 py-2 text-xs",
          expired
            ? "border-danger bg-danger-muted text-danger"
            : "border-warning bg-warning-muted text-warning",
        )}
        role="alert"
      >
        {expired
          ? "This confirmation window has expired. Start the restore again."
          : `Confirm within ${formatDuration(remaining)} — this window closes automatically.`}
      </div>

      <p className="text-sm text-fg-muted">
        This will restore over VMID{" "}
        <span className="font-semibold text-fg">{created.preflight.target_vmid}</span>. Re-type the
        target VMID to confirm you intend to write to it.
      </p>

      <Field label="Type the target VMID to confirm">
        <Input
          type="number"
          value={typedVmid}
          onChange={(e) => setTypedVmid(e.target.value)}
          disabled={expired}
          aria-invalid={typedVmid.length > 0 && !matches}
        />
      </Field>

      <div className="flex justify-between">
        <Button variant="secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button variant="danger" onClick={onConfirm} disabled={!matches || expired || busy}>
          {busy ? "Confirming…" : "Confirm restore"}
        </Button>
      </div>
    </div>
  );
}

function Field({ label, children }: Readonly<{ label: string; children: React.ReactNode }>) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label>{label}</Label>
      {children}
    </div>
  );
}

function Checkbox({
  label,
  checked,
  onChange,
}: Readonly<{ label: string; checked: boolean; onChange: (value: boolean) => void }>) {
  return (
    <label className="flex items-center gap-2 text-sm text-fg-default">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="size-4 accent-accent"
      />
      {label}
    </label>
  );
}

/**
 * Countdown to an ISO deadline, in whole seconds remaining. Ticks once a second
 * and never goes negative, so the confirm button can gate on `<= 0`.
 */
function useCountdown(deadlineIso: string): number {
  const [remaining, setRemaining] = useState(() =>
    Math.max(0, Math.floor((new Date(deadlineIso).getTime() - Date.now()) / 1000)),
  );
  useEffect(() => {
    const tick = () => {
      setRemaining(Math.max(0, Math.floor((new Date(deadlineIso).getTime() - Date.now()) / 1000)));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [deadlineIso]);
  return remaining;
}

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.problem.detail || err.problem.title || fallback;
  return fallback;
}

/** Tone class for a preflight check status (extracted to avoid a nested ternary). */
function checkToneClass(status: string): string {
  if (status === "pass") return "text-success";
  if (status === "warn") return "text-warning";
  return "text-danger";
}

/** Glyph for a preflight check status (pass/warn/fail), never colour-only. */
function checkGlyph(status: string): string {
  if (status === "pass") return "\u2714";
  if (status === "warn") return "\u26a0";
  return "\u2717";
}
