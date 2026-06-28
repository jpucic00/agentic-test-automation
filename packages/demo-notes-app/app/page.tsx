import { redirect } from "next/navigation";

// The app has no dedicated landing page; send visitors to the login screen.
export default function Home() {
  redirect("/login");
}
