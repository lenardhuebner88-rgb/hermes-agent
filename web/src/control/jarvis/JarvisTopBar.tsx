import { Link } from "react-router-dom";

export function JarvisTopBar() {
  return (
    <nav className="jv-topbar" aria-label="Jarvis-Navigation">
      <Link className="jv-topbar-back" to="/control">
        ← Dashboard
      </Link>
      <Link className="jv-topbar-klassik" to="/control/projekte-klassisch">
        Klassik
      </Link>
    </nav>
  );
}
