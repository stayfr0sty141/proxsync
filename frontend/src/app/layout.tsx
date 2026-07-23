import type { Metadata } from "next";
import "@/styles/globals.css";
import { ThemeProvider } from "@/components/providers/theme-provider";
import { QueryProvider } from "@/components/providers/query-provider";
import { AuthProvider } from "@/components/providers/auth-provider";
import { Toaster } from "@/components/ui/toaster";

export const metadata: Metadata = {
  title: "ProxSync",
  description: "Proxmox backup, sync and restore dashboard",
};

/**
 * Root layout. Provider order matters: theme is outermost (it only writes an
 * attribute and must be available before paint to avoid a flash), then the query
 * client, then auth (which uses the query/fetch layer during its bootstrap
 * refresh). The Toaster is mounted once here so any component can raise a toast.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <ThemeProvider>
          <QueryProvider>
            <AuthProvider>{children}</AuthProvider>
          </QueryProvider>
          <Toaster />
        </ThemeProvider>
      </body>
    </html>
  );
}
