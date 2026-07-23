"use client";

import { useState } from "react";
import { toast } from "sonner";
import { useAuth } from "@/components/providers/auth-provider";
import { api } from "@/lib/api/client";
import { ApiError, NetworkError } from "@/lib/api/problem";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ChangePasswordRequest } from "@/types/api";

export function ChangePasswordDialog({
  onClose,
  forced = false,
}: Readonly<{
  onClose?: () => void;
  forced?: boolean;
}>) {
  const { logout } = useAuth();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: React.SubmitEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);

    if (newPassword.length < 12) {
      setError("New password must be at least 12 characters long.");
      return;
    }

    if (newPassword !== confirmPassword) {
      setError("New passwords do not match.");
      return;
    }

    if (newPassword === currentPassword) {
      setError("New password must differ from your current password.");
      return;
    }

    setSubmitting(true);
    try {
      const payload: ChangePasswordRequest = {
        current_password: currentPassword,
        new_password: newPassword,
      };
      await api.post("/auth/change-password", payload);
      toast.success("Password changed successfully. Please sign in again.");
      if (onClose) onClose();
      await logout();
    } catch (err) {
      if (err instanceof ApiError) {
        const fieldErrors = err.problem.errors;
        if (fieldErrors && Object.keys(fieldErrors).length > 0) {
          const firstKey = Object.keys(fieldErrors)[0]!;
          const msg = fieldErrors[firstKey]?.[0] || "Invalid value";
          setError(`${msg}`);
        } else {
          setError(err.problem.detail || err.problem.title || "Failed to change password.");
        }
      } else if (err instanceof NetworkError) {
        setError("Can't reach the server. Check your connection and try again.");
      } else {
        setError("An unexpected error occurred.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm">
      <Card className="w-full max-w-md shadow-2xl border-border-default">
        <CardHeader>
          <CardTitle className="text-base">
            {forced ? "Password Change Required" : "Change Your Password"}
          </CardTitle>
          <p className="text-xs text-fg-muted">
            {forced
              ? "You must change your password before using the rest of the application."
              : "Enter your current password and choose a new password."}
          </p>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="current-password">Current Password</Label>
              <Input
                id="current-password"
                type="password"
                autoComplete="current-password"
                required
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                disabled={submitting}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="new-password">New Password (min. 12 characters)</Label>
              <Input
                id="new-password"
                type="password"
                autoComplete="new-password"
                required
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                disabled={submitting}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="confirm-password">Confirm New Password</Label>
              <Input
                id="confirm-password"
                type="password"
                autoComplete="new-password"
                required
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                disabled={submitting}
              />
            </div>

            {error && (
              <p
                role="alert"
                className="rounded-md border border-danger bg-danger-muted px-3 py-2 text-xs text-danger"
              >
                {error}
              </p>
            )}

            <div className="flex items-center justify-end gap-2 pt-2">
              {!forced && onClose && (
                <Button type="button" variant="secondary" onClick={onClose} disabled={submitting}>
                  Cancel
                </Button>
              )}
              <Button
                type="submit"
                disabled={submitting || !currentPassword || !newPassword || !confirmPassword}
              >
                {submitting ? "Updating\u2026" : "Update Password"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
