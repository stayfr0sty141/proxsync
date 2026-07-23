import { redirect } from "next/navigation";

/**
 * Bare `/settings` has no content of its own; it redirects to the first section
 * so the sub-nav always has an active item. The section pages enforce the admin
 * role via AuthGuard.
 */
export default function SettingsIndexPage() {
  redirect("/settings/general");
}
