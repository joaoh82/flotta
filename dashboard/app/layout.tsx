import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

// System font stack rather than `next/font/google`: the scaffold's Geist import
// downloads fonts at build time, and a localhost-only tool should not need the
// network to build.

export const metadata: Metadata = {
  title: "Flotta — fleet",
  description: "Local dashboard for the Flotta fleet runtime.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full bg-white text-neutral-900 dark:bg-neutral-950 dark:text-neutral-100">
        <header className="border-b border-neutral-200 dark:border-neutral-800">
          <div className="mx-auto flex max-w-5xl items-baseline gap-3 px-6 py-4">
            <Link href="/" className="text-lg font-semibold">
              Flotta
            </Link>
            <span className="text-sm text-neutral-500">fleet runtime</span>
            <span className="ml-auto text-xs text-neutral-400">
              local · read-only except kill
            </span>
          </div>
        </header>
        <main className="mx-auto max-w-5xl px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
