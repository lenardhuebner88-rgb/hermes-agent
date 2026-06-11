import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RotateCw } from "lucide-react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Control route crashed", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <section role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-red-100">
        <div className="flex items-start gap-3">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" />
          <div className="min-w-0 flex-1">
            <p className="hc-eyebrow text-red-200">Ansicht abgestürzt</p>
            <h2 className="mt-1 text-lg font-semibold text-white">Diese Control-Ansicht konnte nicht gerendert werden.</h2>
            <p className="mt-2 text-sm leading-6 hc-soft">Bitte neu laden. Der Rest des Dashboards bleibt unverändert.</p>
            <button type="button" onClick={() => window.location.reload()} className="hc-hit mt-4 inline-flex min-h-10 items-center gap-2 rounded-lg border border-red-400/40 bg-red-500/10 px-3 text-sm font-medium text-red-100 hover:bg-red-500/20">
              <RotateCw className="h-4 w-4" />
              Neu laden
            </button>
          </div>
        </div>
      </section>
    );
  }
}
