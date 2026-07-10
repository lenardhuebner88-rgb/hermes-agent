import { Component, type ErrorInfo, type ReactNode } from "react";
import { RotateCw, TriangleAlert } from "lucide-react";
import { Eyebrow } from "./primitives";

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
      <section role="alert" className="rounded-card border border-status-alert/30 bg-status-alert/10 p-4 text-status-alert">
        <div className="flex items-start gap-3">
          <TriangleAlert className="mt-0.5 size-5 shrink-0" />
          <div className="min-w-0 flex-1">
            <Eyebrow className="text-status-alert">Ansicht abgestürzt</Eyebrow>
            <h2 className="mt-1 text-emph font-semibold text-ink">Diese Control-Ansicht konnte nicht gerendert werden.</h2>
            <p className="mt-2 text-sec leading-6 text-ink-2">Bitte neu laden. Der Rest des Dashboards bleibt unverändert.</p>
            <button type="button" onClick={() => window.location.reload()} className="mt-4 inline-flex min-h-12 items-center gap-2 rounded-card border border-line bg-surface-2 px-3 text-sec font-medium text-ink hover:border-live hover:bg-live/10 hover:text-live">
              <RotateCw className="h-4 w-4" />
              Neu laden
            </button>
          </div>
        </div>
      </section>
    );
  }
}
