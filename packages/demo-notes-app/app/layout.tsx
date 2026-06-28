import type { Metadata } from "next";

import "./globals.css";
import Bootstrap from "@/components/Bootstrap";
import NavBar from "@/components/NavBar";

export const metadata: Metadata = {
  title: "Demo Notes",
  description:
    "A tiny demo app (register, login, notes) used as the app under test for the AI test-generation pipeline.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Bootstrap />
        <NavBar />
        <main className="container">{children}</main>
      </body>
    </html>
  );
}
