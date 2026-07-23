"use client";

import { useState } from "react";
import { toast } from "sonner";
import { Copy, Check, Terminal, ShieldCheck, Server } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";

function copyToClipboard(text: string, setCopied: (val: boolean) => void) {
  void navigator.clipboard.writeText(text);
  setCopied(true);
  toast.success("Command copied to clipboard");
  setTimeout(() => setCopied(false), 2000);
}

export function AgentSetupWizard() {
  const [proxmoxIp, setProxmoxIp] = useState("");
  const [dashboardIp, setDashboardIp] = useState("");
  const [dumpRoot, setDumpRoot] = useState("/mnt/backup-hdd/dump");
  const [storageName, setStorageName] = useState("backup-hdd");
  const [copiedInstallCmd, setCopiedInstallCmd] = useState(false);
  const [copiedTokenCmd, setCopiedTokenCmd] = useState(false);

  const pveIpValue = proxmoxIp.trim() || "<AGENT_IP>";
  const dashIpValue = dashboardIp.trim() || "<DASHBOARD_IP>";

  const installCommand = `cd /tmp && git clone https://github.com/stayfr0sty141/proxsync.git && cd proxsync/deploy/host && ./install-agent.sh --agent-ip ${pveIpValue} --dashboard-ip ${dashIpValue} --dump-root ${dumpRoot || "/mnt/backup-hdd/dump"} --backup-storage ${storageName || "backup-hdd"}`;

  const tokenCommand = `pveum user add proxsync@pve && pveum acl modify / --user proxsync@pve --role PVEAuditor && pveum user token add proxsync@pve dashboard --privsep 0`;

  return (
    <Card className="mb-6 border-accent-muted bg-surface/60 shadow-md">
      <CardHeader>
        <div className="flex items-center gap-2">
          <Server className="size-5 text-accent" />
          <CardTitle className="text-base">Agent Installation Wizard</CardTitle>
        </div>
        <p className="text-xs text-fg-muted">
          Generate step-by-step installation commands for your Proxmox VE host without giving root
          SSH access to the dashboard.
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        {/* Input Parameters */}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div className="flex flex-col gap-1">
            <Label htmlFor="wiz-pve-ip" className="text-xs">
              Proxmox Host IP
            </Label>
            <Input
              id="wiz-pve-ip"
              placeholder="e.g. 10.0.0.10"
              value={proxmoxIp}
              onChange={(e) => setProxmoxIp(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="wiz-dash-ip" className="text-xs">
              Dashboard LXC IP
            </Label>
            <Input
              id="wiz-dash-ip"
              placeholder="e.g. 10.0.0.20"
              value={dashboardIp}
              onChange={(e) => setDashboardIp(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="wiz-dump-root" className="text-xs">
              Dump Root Directory
            </Label>
            <Input
              id="wiz-dump-root"
              placeholder="/mnt/backup-hdd/dump"
              value={dumpRoot}
              onChange={(e) => setDumpRoot(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="wiz-storage" className="text-xs">
              Backup Storage Name
            </Label>
            <Input
              id="wiz-storage"
              placeholder="backup-hdd"
              value={storageName}
              onChange={(e) => setStorageName(e.target.value)}
            />
          </div>
        </div>

        {/* Step-by-step Commands */}
        <div className="flex flex-col gap-4 border-t border-border-muted pt-4">
          {/* Step 1 */}
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center gap-2 text-xs font-semibold text-fg">
              <span className="flex size-5 items-center justify-center rounded-full bg-accent text-[10px] text-white">
                1
              </span>
              <span>Install Backup Agent on Proxmox VE Host (as root via SSH / PVE Shell)</span>
            </div>
            <div className="relative flex items-center overflow-x-auto rounded-md border border-border-default bg-black/40 p-3 font-mono text-xs font-normal text-fg">
              <Terminal className="mr-2 size-4 shrink-0 text-fg-muted" />
              <code className="flex-1 whitespace-pre">{installCommand}</code>
              <Button
                variant="secondary"
                size="sm"
                className="ml-2 shrink-0 gap-1"
                onClick={() => copyToClipboard(installCommand, setCopiedInstallCmd)}
              >
                {copiedInstallCmd ? (
                  <>
                    <Check className="size-3.5 text-success" />
                    <span>Copied</span>
                  </>
                ) : (
                  <>
                    <Copy className="size-3.5" />
                    <span>Copy</span>
                  </>
                )}
              </Button>
            </div>
          </div>

          {/* Step 2 */}
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center gap-2 text-xs font-semibold text-fg">
              <span className="flex size-5 items-center justify-center rounded-full bg-accent text-[10px] text-white">
                2
              </span>
              <span>Create Read-Only PVEAuditor Token on Proxmox Host</span>
            </div>
            <div className="relative flex items-center overflow-x-auto rounded-md border border-border-default bg-black/40 p-3 font-mono text-xs font-normal text-fg">
              <ShieldCheck className="mr-2 size-4 shrink-0 text-fg-muted" />
              <code className="flex-1 whitespace-pre">{tokenCommand}</code>
              <Button
                variant="secondary"
                size="sm"
                className="ml-2 shrink-0 gap-1"
                onClick={() => copyToClipboard(tokenCommand, setCopiedTokenCmd)}
              >
                {copiedTokenCmd ? (
                  <>
                    <Check className="size-3.5 text-success" />
                    <span>Copied</span>
                  </>
                ) : (
                  <>
                    <Copy className="size-3.5" />
                    <span>Copy</span>
                  </>
                )}
              </Button>
            </div>
          </div>

          {/* Step 3 */}
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center gap-2 text-xs font-semibold text-fg">
              <span className="flex size-5 items-center justify-center rounded-full bg-accent text-[10px] text-white">
                3
              </span>
              <span>Fill in Credentials below</span>
            </div>
            <p className="pl-7 text-xs text-fg-muted">
              {"Copy the "}
              <code className="rounded bg-elevated px-1 text-fg">Agent Base URL</code>
              {` (e.g. https://${pveIpValue}:8765) and `}
              <code className="rounded bg-elevated px-1 text-fg">HMAC Secret</code>
              {" printed by Step 1 into the Agent Settings form below, then click "}
              <strong>Save</strong>.
            </p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
