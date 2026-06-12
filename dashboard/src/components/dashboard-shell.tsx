"use client";

import {
  LayersIcon,
  LineChartIcon,
  SearchIcon,
  CommandIcon,
  MoonIcon,
  SettingsIcon,
  SunIcon,
  UsersRoundIcon,
  XIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import * as React from "react";
import { useTheme } from "next-themes";

import { CommandMenu } from "@/components/command-menu";
import { NavigationProgress } from "@/components/navigation-progress";
import { NotificationBell } from "@/components/notification-bell";
import { QueryPendingOverlay } from "@/components/query-pending-overlay";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { useUiStore } from "@/stores/ui-store";
import { cn } from "@/lib/utils";

/** サイドバー／モバイルメニュー共通のナビ項目。section でグルーピングして表示。 */
const nav = [
  { href: "/", label: "Overview", icon: LineChartIcon, section: "Monitor" },
  { href: "/jobs", label: "Jobs", icon: SearchIcon, section: "Monitor" },
  { href: "/clients", label: "Client analysis", icon: UsersRoundIcon, section: "Monitor" },
  { href: "/sources", label: "Sources", icon: LayersIcon, section: "Operate" },
  { href: "/settings", label: "Settings", icon: SettingsIcon, section: "Operate" },
] as const;

const NAV_SECTIONS = ["Monitor", "Operate"] as const;

function isNavActive(href: string, path: string): boolean {
  return href === "/" ? path === "/" : path === href || path.startsWith(`${href}/`);
}

function SidebarBrand() {
  return (
    <Link
      href="/"
      className="flex items-center gap-2.5 rounded-lg px-2 py-1 transition-colors hover:bg-zinc-100 dark:hover:bg-zinc-900"
    >
      <span className="flex size-7 items-center justify-center rounded-md bg-gradient-to-br from-zinc-900 to-zinc-600 text-[11px] font-bold text-white shadow-sm dark:from-white dark:to-zinc-400 dark:text-black">
        JH
      </span>
      <span className="flex flex-col leading-tight">
        <span className="text-[13px] font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
          Job Hunter
        </span>
        <span className="text-[10px] uppercase tracking-[0.16em] text-zinc-400">Control</span>
      </span>
    </Link>
  );
}

function SidebarNav({ path, onNavigate }: { path: string; onNavigate?: () => void }) {
  return (
    <nav className="mt-6 flex flex-1 flex-col gap-5">
      {NAV_SECTIONS.map((section) => (
        <div key={section} className="flex flex-col gap-1">
          <p className="px-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-400">
            {section}
          </p>
          {nav
            .filter((n) => n.section === section)
            .map(({ href, label, icon: Icon }) => {
              const active = isNavActive(href, path);
              return (
                <Link
                  key={href}
                  href={href}
                  onClick={onNavigate}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "group flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-medium transition-colors",
                    active
                      ? "bg-zinc-900 text-white shadow-sm dark:bg-zinc-50 dark:text-zinc-900"
                      : "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-900 dark:hover:text-zinc-50",
                  )}
                >
                  <Icon
                    className={cn(
                      "size-4 shrink-0 transition-colors",
                      active ? "" : "text-zinc-400 group-hover:text-zinc-600 dark:group-hover:text-zinc-200",
                    )}
                  />
                  {label}
                </Link>
              );
            })}
        </div>
      ))}
    </nav>
  );
}

function ThemeToggle() {
  const { setTheme, resolvedTheme } = useTheme();
  const [hydrated, setHydrated] = React.useState(false);
  React.useEffect(() => setHydrated(true), []);
  const isDark = resolvedTheme === "dark";

  return (
    <Button
      variant="outline"
      size="icon"
      className="size-9 rounded-full"
      onClick={() => setTheme(isDark ? "light" : "dark")}
      aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
      title={hydrated ? (isDark ? "Light mode" : "Dark mode") : "Toggle theme"}
    >
      {hydrated ? (
        isDark ? (
          <SunIcon className="size-4" />
        ) : (
          <MoonIcon className="size-4" />
        )
      ) : (
        <SunIcon className="size-4 opacity-0" />
      )}
    </Button>
  );
}

export function DashboardShell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const setOpenCmd = useUiStore((s) => s.setCommandOpen);
  const [mobileNavOpen, setMobileNavOpen] = React.useState(false);

  // ルート遷移時はモバイルドロワーを閉じる。
  React.useEffect(() => {
    setMobileNavOpen(false);
  }, [path]);

  return (
    <>
      <NavigationProgress />
      <QueryPendingOverlay />
      <CommandMenu />
      <div className="relative flex min-h-screen">
        {/* デスクトップ固定サイドバー */}
        <aside className="fixed inset-y-0 left-0 z-40 hidden w-60 shrink-0 flex-col border-r border-zinc-200 bg-white/95 px-3 pb-6 pt-5 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/95 md:flex">
          <SidebarBrand />
          <SidebarNav path={path} />
          <Separator />
          <p className="mt-4 px-2 text-[11px] leading-relaxed text-zinc-400">
            Lancers & CrowdWorks
            <br />
            freelance intake monitor
          </p>
        </aside>

        {/* モバイルドロワー */}
        {mobileNavOpen ? (
          <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
            <div
              className="absolute inset-0 bg-black/40 backdrop-blur-[1px]"
              onClick={() => setMobileNavOpen(false)}
            />
            <aside className="absolute inset-y-0 left-0 flex w-64 flex-col border-r border-zinc-200 bg-white px-3 pb-6 pt-5 shadow-xl dark:border-zinc-800 dark:bg-zinc-950">
              <div className="flex items-center justify-between">
                <SidebarBrand />
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-8"
                  onClick={() => setMobileNavOpen(false)}
                  aria-label="Close navigation"
                >
                  <XIcon className="size-4" />
                </Button>
              </div>
              <SidebarNav path={path} onNavigate={() => setMobileNavOpen(false)} />
            </aside>
          </div>
        ) : null}

        <div className="flex min-h-screen flex-1 flex-col md:pl-60">
          <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-zinc-200 bg-white/85 px-4 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/85">
            <div className="flex min-w-0 items-center gap-3">
              <Button
                variant="outline"
                size="icon"
                className="size-9 md:hidden"
                onClick={() => setMobileNavOpen(true)}
                aria-label="Open navigation"
              >
                <LayersIcon className="size-4" />
              </Button>
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
                  {nav.find((n) => isNavActive(n.href, path))?.label ?? "Job Hunter"}
                </p>
                <p className="truncate text-xs text-zinc-500 dark:text-zinc-400">
                  Operations console
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2">
              <NotificationBell />
              <Button
                variant="outline"
                size="sm"
                className="hidden gap-1.5 sm:inline-flex"
                onClick={() => setOpenCmd(true)}
                aria-label="Open command palette"
              >
                <CommandIcon className="size-3.5" />
                <span className="text-xs text-zinc-500">⌘K</span>
              </Button>
              <ThemeToggle />
            </div>
          </header>

          <main className="flex-1 px-4 py-6 md:px-8 lg:px-10">{children}</main>
        </div>
      </div>
    </>
  );
}
