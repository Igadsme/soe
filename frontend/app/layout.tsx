import React from "react";
import "./styles.css";

export const metadata = { title: "Soe — content operations", description: "Rights-first clip production and publishing" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body>{children}</body></html>;
}
