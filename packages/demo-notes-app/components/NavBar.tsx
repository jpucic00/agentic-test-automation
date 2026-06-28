"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { currentUser, logout } from "@/lib/auth";

export default function NavBar() {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<string | null>(null);

  // Re-read the session on every navigation: the root layout (and this NavBar) persist
  // across client-side route changes, so a one-time mount effect would miss login/logout.
  useEffect(() => {
    setUser(currentUser());
  }, [pathname]);

  function handleLogout() {
    logout();
    setUser(null);
    router.push("/login");
  }

  return (
    <nav className="navbar">
      <Link id="nav-home" href="/" className="brand">
        Demo Notes
      </Link>
      <div className="nav-links">
        {user ? (
          <>
            <span className="user-menu">{user}</span>
            {/* Intentionally a non-semantic div (no button/role/id/aria) so locators must
                fall back to CSS/XPath/text — the classic "non-button clickable" case. */}
            <div className="btn" onClick={handleLogout}>
              Log out
            </div>
          </>
        ) : (
          <>
            <Link id="nav-login" href="/login">
              Login
            </Link>
            <Link id="nav-register" href="/register">
              Register
            </Link>
          </>
        )}
      </div>
    </nav>
  );
}
