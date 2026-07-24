import type { Metadata } from "next";
import { Inter, DM_Mono } from "next/font/google";
import "./globals.css";
import LeftNav from "@/components/LeftNav";
import { RecentlyVisited } from "@/components/RecentlyVisited";

// Body font — matches doctorshero-frontend's brand typography (see
// doctorshero-frontend/app/layout.tsx). Headings use "Product Sans" instead
// (self-hosted, @font-face in globals.css) — same pairing as the main portal.
const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  weight: ["400", "500", "700"],
});

const dmMono = DM_Mono({
  variable: "--font-dm-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: "DoctorsHero Knowledge Hub",
  description: "Clinical knowledge base — browse and search evidence-based topics.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${inter.variable} ${dmMono.variable}`}>
      <body>
        <div className="app-shell">
          <LeftNav />
          <div className="app-shell-middle">{children}</div>
          <RecentlyVisited />
        </div>
      </body>
    </html>
  );
}
